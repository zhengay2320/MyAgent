from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn

from eo_agent.api import create_app
from eo_agent.imagery.config import load_imagery_config
from eo_agent.imagery.providers import EarthEngineProvider, MockImageryProvider
from eo_agent.schemas import TaskRequest, TaskStatus, WorkflowMode
from eo_agent.service import TaskService

DEMO_QUERY = "对武汉演示区2025年10月与2024年9月进行变化检测"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eo_agent", description="EO-Agent V0.1")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="运行离线模拟演示")
    demo.add_argument("--scenario", default="cloudy")
    demo.add_argument("--output-dir", default="outputs/demo")
    demo.add_argument("--model", default="mock")
    demo.add_argument("--workflow", choices=["legacy", "scientific"], default="legacy")
    demo.add_argument("--episode")
    run = sub.add_parser("run", help="运行自定义两时期任务")
    run.add_argument("--query", required=True)
    run.add_argument("--aoi-id")
    run.add_argument("--scenario", default="cloudy")
    run.add_argument("--model", default="mock")
    run.add_argument("--output-dir", default="outputs/run")
    run.add_argument("--workflow", choices=["legacy", "scientific"], default="legacy")
    run.add_argument("--episode")
    serve = sub.add_parser("serve", help="启动本地 FastAPI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--output-dir", default="outputs/api")
    imagery = sub.add_parser("imagery", help="影像准备工具")
    imagery_sub = imagery.add_subparsers(dest="imagery_command", required=True)
    doctor = imagery_sub.add_parser("doctor", help="检查影像后端依赖与显式配置")
    doctor.add_argument("--provider", choices=["mock", "gee"], default="mock")
    doctor.add_argument("--check-remote", action="store_true")
    return parser


def _print_result(result) -> None:
    print(f"task_id: {result.task_id}")
    print(f"status: {result.status.value}")
    print(f"contains_mock: {result.contains_mock}")
    print("steps:")
    for step in result.steps:
        print(f"  - {step}")
    print(f"report_html: {result.report_html or '无'}")
    print(f"report_json: {result.report_json or '无'}")
    if result.error:
        print(f"message: {result.error}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "imagery":
        config = load_imagery_config()
        provider = (
            MockImageryProvider()
            if args.provider == "mock"
            else EarthEngineProvider(
                project_id=os.getenv("EE_PROJECT_ID"),
                max_catalog_limit=config.catalog_limit_per_window,
                max_request_uncompressed_mib=config.max_request_uncompressed_mib,
            )
        )
        result = dict(provider.doctor(check_remote=args.check_remote))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        ready = bool(result.get("ready")) or result.get("status") in {
            "local_ready",
            "remote_ready",
        }
        return 0 if ready else 2
    if args.command == "serve":
        uvicorn.run(create_app(Path(args.output_dir)), host=args.host, port=args.port)
        return 0
    if args.command == "demo":
        request = TaskRequest(
            query=DEMO_QUERY,
            aoi_id="wuhan_demo_100km2",
            scenario=args.scenario,
            model_profile=args.model,
            workflow_mode=WorkflowMode(args.workflow),
            episode_id=args.episode,
        )
    else:
        request = TaskRequest(
            query=args.query,
            aoi_id=args.aoi_id,
            scenario=args.scenario,
            model_profile=args.model,
            workflow_mode=WorkflowMode(args.workflow),
            episode_id=args.episode,
        )
    try:
        result = TaskService(args.output_dir).run(request)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    _print_result(result)
    if result.status in {TaskStatus.COMPLETED, TaskStatus.PARTIAL}:
        return 0
    if result.status == TaskStatus.WAITING_INPUT:
        return 3
    return 1
