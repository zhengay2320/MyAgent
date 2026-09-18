from __future__ import annotations

import hmac
import os
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from urllib.parse import urlparse

import yaml
from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from eo_agent.config import PROJECT_ROOT
from eo_agent.imagery.repository import ApprovalStateError, StalePlanError
from eo_agent.imagery.schemas import (
    DownloadApprovalRequest,
    DownloadPlanUpdateRequest,
    ImageryTaskRequest,
)
from eo_agent.imagery.service import (
    ImageryConflict,
    ImageryTaskNotFound,
    ImageryTaskService,
    task_request_from_patch,
)


def install_imagery_routes(
    app: FastAPI, output_dir: str | Path
) -> ImageryTaskService:
    package_root = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(package_root / "templates"))
    app.mount(
        "/imagery/static",
        StaticFiles(directory=str(package_root / "static")),
        name="imagery-static",
    )
    service = ImageryTaskService(output_dir)
    csrf_token = token_urlsafe(32)
    router = APIRouter()

    def require_csrf(request: Request, x_csrf_token: str | None = Header(default=None)) -> None:
        if not x_csrf_token or not hmac.compare_digest(x_csrf_token, csrf_token):
            raise HTTPException(status_code=403, detail="CSRF 校验失败，请从本应用页面重试")
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or parsed.netloc != request.headers.get(
                "host"
            ):
                raise HTTPException(status_code=403, detail="只允许同源页面修改本地任务")

    @router.get("/imagery", include_in_schema=False)
    def imagery_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="imagery.html",
            context={"csrf_token": csrf_token},
        )

    @router.get("/api/imagery/config")
    def imagery_config() -> dict[str, Any]:
        model_document = yaml.safe_load(
            (PROJECT_ROOT / "configs" / "models.yaml").read_text(encoding="utf-8")
        )
        profiles = []
        for name, raw in model_document["profiles"].items():
            required_env = [
                raw.get("api_key_env"),
                raw.get("base_url_env"),
                raw.get("model_env"),
            ]
            required_env = [item for item in required_env if item]
            configured = not required_env or all(os.getenv(item) for item in required_env)
            profiles.append(
                {
                    "value": name,
                    "label": (
                        "MockLLM（离线）"
                        if name == "mock"
                        else f"{name}（{'已配置' if configured else '未配置'}）"
                    ),
                    "enabled": name == "mock" or configured,
                    "note": "真实模型失败不会回退 Mock" if name != "mock" else "不访问网络",
                }
            )
        aois = []
        fixture = yaml.safe_load(
            (PROJECT_ROOT / "fixtures" / "aois.json").read_text(encoding="utf-8")
        )
        for identifier, value in fixture.items():
            aois.append(
                {
                    "aoi_id": identifier,
                    "name": value.get("name", identifier),
                    "has_geometry": isinstance(value.get("geometry"), dict),
                    "is_synthetic": bool(value.get("is_synthetic")),
                    "description": value.get("description"),
                    "geometry_summary": value.get("geometry", {}).get("type"),
                }
            )
        root = service.config.resolved_download_root(PROJECT_ROOT)
        return {
            "default_model_profile": service.config.model_profile,
            "default_provider": service.config.provider,
            "model_profiles": profiles,
            "data_providers": [
                {
                    "value": "mock",
                    "label": "Mock（离线合成数据）",
                    "enabled": True,
                    "note": "完整走相同检索、审批、下载与校验路径",
                },
                {
                    "value": "gee",
                    "label": "Google Earth Engine（需显式配置）",
                    "enabled": True,
                    "note": "不会自动认证；缺少依赖、项目或凭据时明确失败",
                },
            ],
            "registered_aois": aois,
            "default_download_root": str(root),
            "default_download_root_resolved": str(root),
            "directory_notice": "保存目录位于运行 Python 后端的机器。",
            "csrf_token": csrf_token,
            "real_connections_verified": False,
        }

    @router.post(
        "/api/imagery/tasks",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[],
    )
    def create_imagery_task(request: Request, body: ImageryTaskRequest):
        require_csrf(request, request.headers.get("x-csrf-token"))
        try:
            return service.create_task(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/imagery/tasks/{task_id}")
    def get_imagery_task(task_id: str):
        return _not_found(lambda: service.get_task(task_id))

    @router.get("/api/imagery/tasks/{task_id}/events")
    def stream_imagery_events(
        task_id: str,
        request: Request,
        after_seq: int = Query(default=0, ge=0),
    ):
        header = request.headers.get("last-event-id")
        if header and header.isdigit():
            after_seq = max(after_seq, int(header))
        try:
            stream = service.stream_events(task_id, after_seq)
        except ImageryTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @router.patch("/api/imagery/tasks/{task_id}/request")
    async def update_imagery_request(task_id: str, request: Request):
        require_csrf(request, request.headers.get("x-csrf-token"))
        try:
            raw = await request.json()
            body, version = task_request_from_patch(raw)
            return service.update_request(task_id, body, version)
        except ImageryTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ImageryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/imagery/tasks/{task_id}/candidates")
    def get_imagery_candidates(task_id: str):
        return _not_found(lambda: service.get_candidates(task_id))

    @router.get("/api/imagery/tasks/{task_id}/plan")
    def get_imagery_plan(task_id: str):
        return _not_found(lambda: service.get_plan(task_id))

    @router.patch("/api/imagery/tasks/{task_id}/plan")
    def update_imagery_plan(task_id: str, request: Request, body: DownloadPlanUpdateRequest):
        require_csrf(request, request.headers.get("x-csrf-token"))
        try:
            return service.update_plan(task_id, body).model_dump(mode="json")
        except ImageryTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ImageryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/imagery/tasks/{task_id}/approve")
    def approve_imagery_plan(task_id: str, request: Request, body: DownloadApprovalRequest):
        require_csrf(request, request.headers.get("x-csrf-token"))
        try:
            return service.approve(task_id, body)
        except ImageryTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except (ImageryConflict, StalePlanError, ApprovalStateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/imagery/tasks/{task_id}/cancel")
    def cancel_imagery_task(task_id: str, request: Request):
        require_csrf(request, request.headers.get("x-csrf-token"))
        try:
            return service.cancel(task_id)
        except ImageryTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    @router.post("/api/imagery/tasks/{task_id}/resume")
    def resume_imagery_task(task_id: str, request: Request):
        require_csrf(request, request.headers.get("x-csrf-token"))
        try:
            return service.resume(task_id)
        except ImageryTaskNotFound as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ImageryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/imagery/tasks/{task_id}/artifacts/{artifact_id}")
    def get_imagery_artifact(task_id: str, artifact_id: str):
        try:
            path, media_type = service.artifact_path(task_id, artifact_id)
        except (ImageryTaskNotFound, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="产物不存在") from exc
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    app.include_router(router)
    app.state.imagery_service = service

    app.router.add_event_handler("shutdown", service.close)

    return service


def _not_found(function):
    try:
        return function()
    except ImageryTaskNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
