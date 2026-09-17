from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from eo_agent.schemas import ArtifactRef


class ArtifactStore:
    def __init__(self, output_root: Path, task_id: str) -> None:
        self.output_root = output_root.resolve()
        self.task_id = task_id
        self.task_dir = (self.output_root / task_id).resolve()
        self.artifact_dir = self.task_dir / "artifacts"
        self.artifact_dir.mkdir(parents=True, exist_ok=False)

    def _ensure_owned(self, path: Path) -> None:
        if self.task_dir not in path.resolve().parents and path.resolve() != self.task_dir:
            raise ValueError("产物路径越出任务目录")

    def write_bytes(
        self, relative_path: str, content: bytes, media_type: str, contains_mock: bool
    ) -> ArtifactRef:
        path = (self.task_dir / relative_path).resolve()
        self._ensure_owned(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
        return ArtifactRef(
            artifact_id=uuid4().hex,
            relative_path=path.relative_to(self.task_dir).as_posix(),
            media_type=media_type,
            checksum_sha256=hashlib.sha256(content).hexdigest(),
            contains_mock=contains_mock,
        )

    def write_json(self, relative_path: str, value: object, contains_mock: bool) -> ArtifactRef:
        content = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        return self.write_bytes(relative_path, content, "application/json", contains_mock)

    def resolve(self, relative_path: str) -> Path:
        path = (self.task_dir / relative_path).resolve()
        self._ensure_owned(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path
