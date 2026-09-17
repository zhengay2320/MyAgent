from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from eo_agent.api import create_app
from eo_agent.schemas import TaskRequest, TaskStatus
from eo_agent.service import TaskService

DEMO_QUERY = "对武汉演示区2025年10月与2024年9月进行变化检测"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eo_agent", description="EO-Agent V0.1")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="运行离线模拟演示")
    demo.add_argument("--scenario", default="cloudy")
    demo.add_argument("--output-dir", default="outputs/demo")
    demo.add_argument("--model", default="mock")
    run = sub.add_parser("run", help="运行自定义两时期任务")
    run.add_argument("--query", required=True)
    run.add_argument("--aoi-id")
    run.add_argument("--scenario", default="cloudy")
    run.add_argument("--model", default="mock")
    run.add_argument("--output-dir", default="outputs/run")
    serve = sub.add_parser("serve", help="启动本地 FastAPI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--output-dir", default="outputs/api")
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
    if args.command == "serve":
        uvicorn.run(create_app(Path(args.output_dir)), host=args.host, port=args.port)
        return 0
    if args.command == "demo":
        request = TaskRequest(
            query=DEMO_QUERY,
            aoi_id="wuhan_demo_100km2",
            scenario=args.scenario,
            model_profile=args.model,
        )
    else:
        request = TaskRequest(
            query=args.query,
            aoi_id=args.aoi_id,
            scenario=args.scenario,
            model_profile=args.model,
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
