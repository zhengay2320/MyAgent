from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from eo_agent.imagery.repository import ImageryRepository


class ImageryArtifactStore:
    def __init__(self, root: Path, task_id: str, repository: ImageryRepository) -> None:
        self.root = root.resolve()
        self.task_id = task_id
        self.task_dir = (self.root / task_id).resolve()
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.repository = repository

    def _resolve(self, relative_path: str) -> Path:
        if Path(relative_path).is_absolute():
            raise ValueError("控制产物只接受任务内相对路径")
        path = (self.task_dir / relative_path).resolve()
        if self.task_dir != path and self.task_dir not in path.parents:
            raise ValueError("控制产物路径越出任务目录")
        return path

    def write_bytes(
        self,
        relative_path: str,
        content: bytes,
        media_type: str,
        contains_mock: bool,
        *,
        artifact_id: str | None = None,
    ) -> str:
        path = self._resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)
        identifier = artifact_id or f"A-{hashlib.sha256(content).hexdigest()[:20]}"
        self.repository.add_artifact(
            identifier,
            self.task_id,
            path.relative_to(self.task_dir).as_posix(),
            media_type,
            contains_mock,
        )
        return identifier

    def write_json(
        self,
        relative_path: str,
        value: object,
        contains_mock: bool,
        *,
        artifact_id: str | None = None,
    ) -> str:
        return self.write_bytes(
            relative_path,
            json.dumps(value, ensure_ascii=False, indent=2, default=str).encode(),
            "application/json",
            contains_mock,
            artifact_id=artifact_id,
        )

    def resolve_artifact(self, artifact_id: str) -> tuple[Path, str]:
        row = self.repository.get_artifact(self.task_id, artifact_id)
        if row is None:
            raise FileNotFoundError(artifact_id)
        path = self._resolve(row["relative_path"])
        if not path.is_file():
            raise FileNotFoundError(path)
        return path, row["media_type"]
