from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from time import monotonic

from eo_agent.imagery.repository import ImageryRepository

TERMINAL_STATES = {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}


class EventBroker:
    """Persistent event log with in-process wakeups; SQLite remains authoritative."""

    def __init__(self, repository: ImageryRepository) -> None:
        self.repository = repository
        self._condition = threading.Condition()

    def emit(self, task_id: str, **event) -> dict:
        value = self.repository.append_event(task_id, **event)
        with self._condition:
            self._condition.notify_all()
        return value

    def stream(
        self,
        task_id: str,
        after_sequence: int = 0,
        heartbeat_seconds: float = 12.0,
    ) -> Iterator[str]:
        sequence = after_sequence
        heartbeat_at = monotonic()
        while True:
            task = self.repository.get_task(task_id)
            if task is None:
                yield _sse("error", {"message": "任务不存在"})
                return
            events = self.repository.events_after(task_id, sequence)
            for event in events:
                sequence = event["sequence"]
                yield _sse(event["event_type"], event, event_id=sequence)
            if task["status"] in TERMINAL_STATES and not events:
                return
            now = monotonic()
            if now - heartbeat_at >= heartbeat_seconds:
                yield ": heartbeat\n\n"
                heartbeat_at = now
            with self._condition:
                self._condition.wait(timeout=0.5)


def _sse(event: str, value: object, event_id: int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(value, ensure_ascii=False, default=str)
    lines.extend(f"data: {line}" for line in payload.splitlines())
    return "\n".join(lines) + "\n\n"
