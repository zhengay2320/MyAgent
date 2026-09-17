from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from eo_agent.schemas import ArtifactRef, TaskStatus


class TaskRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    query TEXT NOT NULL,
                    scenario TEXT NOT NULL,
                    workflow_mode TEXT NOT NULL DEFAULT 'legacy',
                    model_profile TEXT NOT NULL,
                    contains_mock INTEGER NOT NULL,
                    task_dir TEXT NOT NULL,
                    report_html TEXT,
                    report_json TEXT,
                    error TEXT,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    checksum_sha256 TEXT NOT NULL,
                    contains_mock INTEGER NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "workflow_mode" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN workflow_mode TEXT NOT NULL DEFAULT 'legacy'"
                )

    def create_task(
        self,
        task_id: str,
        query: str,
        scenario: str,
        model_profile: str,
        task_dir: Path,
        workflow_mode: str = "legacy",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO tasks
                (task_id,status,query,scenario,workflow_mode,model_profile,contains_mock,
                 task_dir,result_json)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    TaskStatus.CREATED.value,
                    query,
                    scenario,
                    workflow_mode,
                    model_profile,
                    1,
                    str(task_dir.resolve()),
                    "{}",
                ),
            )

    def finish_task(
        self,
        task_id: str,
        status: TaskStatus,
        contains_mock: bool,
        result: dict[str, Any],
        report_html: str | None,
        report_json: str | None,
        error: str | None,
        artifacts: list[ArtifactRef],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE tasks SET status=?,contains_mock=?,result_json=?,report_html=?,
                report_json=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE task_id=?""",
                (
                    status.value,
                    int(contains_mock),
                    json.dumps(result, ensure_ascii=False, default=str),
                    report_html,
                    report_json,
                    error,
                    task_id,
                ),
            )
            conn.executemany(
                """INSERT OR REPLACE INTO artifacts
                (artifact_id,task_id,relative_path,media_type,checksum_sha256,contains_mock)
                VALUES (?,?,?,?,?,?)""",
                [
                    (
                        item.artifact_id,
                        task_id,
                        item.relative_path,
                        item.media_type,
                        item.checksum_sha256,
                        int(item.contains_mock),
                    )
                    for item in artifacts
                ],
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["contains_mock"] = bool(value["contains_mock"])
        value["result"] = json.loads(value.pop("result_json"))
        return value

    def get_artifact(self, task_id: str, artifact_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE task_id=? AND artifact_id=?",
                (task_id, artifact_id),
            ).fetchone()
        return dict(row) if row else None
