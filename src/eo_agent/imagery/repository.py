from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class ImageryRepository:
    """SQLite source of truth; every operation owns its connection."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS imagery_tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    task_version INTEGER NOT NULL,
                    plan_version INTEGER NOT NULL DEFAULT 0,
                    plan_hash TEXT,
                    query TEXT NOT NULL,
                    model_profile TEXT NOT NULL,
                    data_backend TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    parsed_json TEXT,
                    aoi_json TEXT,
                    candidates_json TEXT,
                    plan_json TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    download_started INTEGER NOT NULL DEFAULT 0,
                    control_dir TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS imagery_events (
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    call_id TEXT,
                    action_id TEXT,
                    file_id TEXT,
                    plan_version INTEGER,
                    details_json TEXT NOT NULL,
                    details_ref TEXT,
                    PRIMARY KEY(task_id, sequence),
                    FOREIGN KEY(task_id) REFERENCES imagery_tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS imagery_llm_calls (
                    call_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    validation_json TEXT,
                    accepted INTEGER,
                    action_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES imagery_tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS imagery_approvals (
                    approval_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    plan_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    approval_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, idempotency_key),
                    FOREIGN KEY(task_id) REFERENCES imagery_tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS imagery_files (
                    task_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    bytes_received INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER,
                    rate_bps REAL,
                    sha256 TEXT,
                    validation_json TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, file_id),
                    FOREIGN KEY(task_id) REFERENCES imagery_tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS imagery_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    contains_mock INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES imagery_tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_imagery_events_task_seq
                    ON imagery_events(task_id, sequence);
                """
            )

    def create_task(
        self,
        *,
        task_id: str,
        status: str,
        query: str,
        model_profile: str,
        data_backend: str,
        request: dict[str, Any],
        control_dir: Path,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO imagery_tasks
                (task_id,status,task_version,query,model_profile,data_backend,request_json,
                 control_dir,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    status,
                    1,
                    query,
                    model_profile,
                    data_backend,
                    _json(request),
                    str(control_dir.resolve()),
                    now,
                    now,
                ),
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM imagery_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        for key in (
            "request_json",
            "parsed_json",
            "aoi_json",
            "candidates_json",
            "plan_json",
        ):
            value[key.removesuffix("_json")] = _loads(value.pop(key), None)
        value["cancel_requested"] = bool(value["cancel_requested"])
        value["download_started"] = bool(value["download_started"])
        return value

    def update_task(self, task_id: str, **values: Any) -> None:
        allowed = {
            "status",
            "task_version",
            "plan_version",
            "plan_hash",
            "request_json",
            "parsed_json",
            "aoi_json",
            "candidates_json",
            "plan_json",
            "error",
            "cancel_requested",
            "download_started",
            "query",
            "model_profile",
            "data_backend",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"不允许更新字段: {', '.join(sorted(unknown))}")
        if not values:
            return
        encoded: dict[str, Any] = {}
        for key, value in values.items():
            encoded[key] = _json(value) if key.endswith("_json") else value
        encoded["updated_at"] = datetime.now(UTC).isoformat()
        assignments = ",".join(f"{key}=?" for key in encoded)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE imagery_tasks SET {assignments} WHERE task_id=?",  # noqa: S608
                (*encoded.values(), task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)

    def append_event(
        self,
        task_id: str,
        *,
        event_type: str,
        stage: str,
        summary: str,
        details: dict[str, Any] | None = None,
        call_id: str | None = None,
        action_id: str | None = None,
        file_id: str | None = None,
        plan_version: int | None = None,
        details_ref: str | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next FROM imagery_events WHERE task_id=?",
                (task_id,),
            ).fetchone()
            sequence = int(row["next"])
            connection.execute(
                """INSERT INTO imagery_events
                (task_id,sequence,timestamp,event_type,stage,summary,call_id,action_id,
                 file_id,plan_version,details_json,details_ref)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    sequence,
                    now,
                    event_type,
                    stage,
                    summary,
                    call_id,
                    action_id,
                    file_id,
                    plan_version,
                    _json(details or {}),
                    details_ref,
                ),
            )
        return {
            "task_id": task_id,
            "sequence": sequence,
            "timestamp": now,
            "event_type": event_type,
            "stage": stage,
            "summary": summary,
            "call_id": call_id,
            "action_id": action_id,
            "file_id": file_id,
            "plan_version": plan_version,
            "details": details or {},
            "details_ref": details_ref,
        }

    def events_after(self, task_id: str, sequence: int = 0) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM imagery_events WHERE task_id=? AND sequence>? ORDER BY sequence",
                (task_id, sequence),
            ).fetchall()
        return [self._event(row) for row in rows]

    def _event(self, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["details"] = _loads(value.pop("details_json"), {})
        return value

    def start_llm_call(
        self, call_id: str, task_id: str, purpose: str, request: dict[str, Any]
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO imagery_llm_calls
                (call_id,task_id,purpose,status,request_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""",
                (call_id, task_id, purpose, "waiting", _json(request), now, now),
            )

    def finish_llm_call(
        self,
        call_id: str,
        *,
        status: str,
        response: dict[str, Any] | None,
        validation: dict[str, Any] | None,
        accepted: bool | None,
        action: dict[str, Any] | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE imagery_llm_calls SET status=?,response_json=?,validation_json=?,
                accepted=?,action_json=?,updated_at=? WHERE call_id=?""",
                (
                    status,
                    _json(response) if response is not None else None,
                    _json(validation) if validation is not None else None,
                    None if accepted is None else int(accepted),
                    _json(action) if action is not None else None,
                    datetime.now(UTC).isoformat(),
                    call_id,
                ),
            )

    def list_llm_calls(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM imagery_llm_calls WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            for key in ("request_json", "response_json", "validation_json", "action_json"):
                value[key.removesuffix("_json")] = _loads(value.pop(key), None)
            value["accepted"] = None if value["accepted"] is None else bool(value["accepted"])
            values.append(value)
        return values

    def create_approval_once(
        self,
        *,
        approval_id: str,
        task_id: str,
        plan_version: int,
        plan_hash: str,
        idempotency_key: str,
        approval: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM imagery_approvals WHERE task_id=? AND idempotency_key=?",
                (task_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return self._approval(existing), False
            task = connection.execute(
                "SELECT plan_version,plan_hash,status,download_started FROM imagery_tasks "
                "WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if int(task["plan_version"]) != plan_version or task["plan_hash"] != plan_hash:
                raise StalePlanError("计划版本或哈希已过期")
            if task["status"] != "WAITING_DOWNLOAD_APPROVAL":
                raise ApprovalStateError(f"当前状态不允许审批: {task['status']}")
            connection.execute(
                """INSERT INTO imagery_approvals
                (approval_id,task_id,plan_version,plan_hash,idempotency_key,approval_json,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    approval_id,
                    task_id,
                    plan_version,
                    plan_hash,
                    idempotency_key,
                    _json(approval),
                    now,
                ),
            )
            connection.execute(
                "UPDATE imagery_tasks SET status='DOWNLOADING',download_started=1,updated_at=? "
                "WHERE task_id=?",
                (now, task_id),
            )
        return {
            "approval_id": approval_id,
            "task_id": task_id,
            "plan_version": plan_version,
            "plan_hash": plan_hash,
            "idempotency_key": idempotency_key,
            "approval": approval,
            "created_at": now,
        }, True

    def latest_approval(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM imagery_approvals WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return self._approval(row) if row else None

    def _approval(self, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["approval"] = _loads(value.pop("approval_json"), {})
        return value

    def upsert_file(self, task_id: str, file_id: str, **values: Any) -> None:
        current = self.get_file(task_id, file_id)
        merged = {
            "status": "queued",
            "relative_path": "",
            "bytes_received": 0,
            "total_bytes": None,
            "rate_bps": None,
            "sha256": None,
            "validation_json": None,
            "error": None,
        }
        if current:
            merged.update(current)
        merged.update(values)
        validation = merged.get("validation")
        if validation is not None:
            merged["validation_json"] = _json(validation)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO imagery_files
                (task_id,file_id,status,relative_path,bytes_received,total_bytes,rate_bps,
                 sha256,validation_json,error,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id,file_id) DO UPDATE SET
                 status=excluded.status,relative_path=excluded.relative_path,
                 bytes_received=excluded.bytes_received,total_bytes=excluded.total_bytes,
                 rate_bps=excluded.rate_bps,sha256=excluded.sha256,
                 validation_json=excluded.validation_json,error=excluded.error,
                 updated_at=excluded.updated_at""",
                (
                    task_id,
                    file_id,
                    merged["status"],
                    merged["relative_path"],
                    merged["bytes_received"],
                    merged["total_bytes"],
                    merged["rate_bps"],
                    merged["sha256"],
                    merged["validation_json"],
                    merged["error"],
                    now,
                ),
            )

    def get_file(self, task_id: str, file_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM imagery_files WHERE task_id=? AND file_id=?",
                (task_id, file_id),
            ).fetchone()
        return self._file(row) if row else None

    def list_files(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM imagery_files WHERE task_id=? ORDER BY file_id", (task_id,)
            ).fetchall()
        return [self._file(row) for row in rows]

    def _file(self, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["validation"] = _loads(value.pop("validation_json"), None)
        return value

    def add_artifact(
        self,
        artifact_id: str,
        task_id: str,
        relative_path: str,
        media_type: str,
        contains_mock: bool,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO imagery_artifacts
                (artifact_id,task_id,relative_path,media_type,contains_mock,created_at)
                VALUES (?,?,?,?,?,?)""",
                (
                    artifact_id,
                    task_id,
                    relative_path,
                    media_type,
                    int(contains_mock),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_artifact(self, task_id: str, artifact_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM imagery_artifacts WHERE task_id=? AND artifact_id=?",
                (task_id, artifact_id),
            ).fetchone()
        return dict(row) if row else None

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT artifact_id,relative_path,media_type,contains_mock,created_at "
                "FROM imagery_artifacts WHERE task_id=? ORDER BY created_at,artifact_id",
                (task_id,),
            ).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["contains_mock"] = bool(value["contains_mock"])
        return values

    def mark_running_interrupted(self) -> int:
        active = (
            "PARSING",
            "SEARCHING_OPTICAL",
            "PREVIEWING",
            "ASSESSING_QUALITY",
            "PLANNING",
            "SEARCHING_SAR",
            "DOWNLOADING",
            "VERIFYING",
        )
        placeholders = ",".join("?" for _ in active)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE imagery_tasks SET status='INTERRUPTED',updated_at=? "  # noqa: S608
                f"WHERE status IN ({placeholders})",
                (datetime.now(UTC).isoformat(), *active),
            )
        return cursor.rowcount


class StalePlanError(RuntimeError):
    pass


class ApprovalStateError(RuntimeError):
    pass
