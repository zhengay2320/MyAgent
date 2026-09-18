from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from eo_agent.imagery.router import install_imagery_routes
from eo_agent.schemas import TaskRequest
from eo_agent.service import TaskService


def create_app(output_dir: str | Path = "outputs/api") -> FastAPI:
    app = FastAPI(title="EO-Agent V0.1", version="0.1.0")
    service = TaskService(output_dir)
    app.state.service = service
    install_imagery_routes(app, output_dir)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "offline-first"}

    @app.post("/api/tasks/run")
    def run_task(request: TaskRequest) -> dict:
        try:
            return service.run(request).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict:
        task = service.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task

    @app.get("/api/tasks/{task_id}/trace")
    def get_trace(task_id: str) -> list[dict]:
        try:
            return service.get_trace(task_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="轨迹不存在") from exc

    @app.get("/api/tasks/{task_id}/report")
    def get_report(task_id: str, format: str = Query("html", pattern="^(html|json)$")):
        try:
            path = service.report_path(task_id, format)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="报告不存在") from exc
        media = "text/html" if format == "html" else "application/json"
        return FileResponse(path, media_type=media)

    @app.get("/api/tasks/{task_id}/artifacts/{artifact_id}")
    def get_artifact(task_id: str, artifact_id: str):
        try:
            path, media = service.artifact_path(task_id, artifact_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="产物不存在") from exc
        return FileResponse(path, media_type=media)

    return app
