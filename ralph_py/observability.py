"""Observability - structured progress logging for factory runs."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _iso_now() -> str:
    """Return current time as ISO 8601 string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class ComponentTiming:
    """Timing data for a single component."""

    component_id: str
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0
    iteration_count: int = 0
    verification_duration: float = 0.0
    review_duration: float = 0.0


class ProgressLog:
    """Append-only JSONL event log.

    Each event is a single JSON line. JSONL is crash-safe since each line
    is a complete object - partial writes only lose the last event.
    """

    def __init__(self, log_path: Path) -> None:
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def emit(
        self,
        event_type: str,
        component_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Append one JSON event line to the log."""
        event: dict[str, Any] = {
            "ts": _iso_now(),
            "event": event_type,
        }
        if component_id is not None:
            event["component"] = component_id
        if data:
            event["data"] = data
        with open(self._path, "a") as f:
            f.write(json.dumps(event) + "\n")

    def factory_started(self, project_name: str, component_count: int) -> None:
        self.emit("factory_started", data={
            "project": project_name,
            "components": component_count,
        })

    def component_started(self, component_id: str) -> None:
        self.emit("component_started", component_id=component_id)

    def component_completed(
        self, component_id: str, duration: float, iterations: int,
    ) -> None:
        self.emit("component_completed", component_id=component_id, data={
            "duration_seconds": round(duration, 2),
            "iterations": iterations,
        })

    def component_failed(self, component_id: str, error: str) -> None:
        self.emit("component_failed", component_id=component_id, data={
            "error": error,
        })

    def component_retrying(
        self, component_id: str, attempt: int, reason: str,
    ) -> None:
        self.emit("component_retrying", component_id=component_id, data={
            "attempt": attempt,
            "reason": reason,
        })

    def verification_result(
        self,
        component_id: str,
        passed: bool,
        check_names: list[str] | None = None,
        failures: list[str] | None = None,
        duration: float = 0.0,
    ) -> None:
        self.emit("verification_result", component_id=component_id, data={
            "passed": passed,
            "checks": check_names or [],
            "failures": failures or [],
            "duration_seconds": round(duration, 2),
        })

    def review_result(
        self,
        component_id: str,
        passed: bool,
        mode: str = "",
        fail_count: int = 0,
        advisory_count: int = 0,
        duration: float = 0.0,
    ) -> None:
        self.emit("review_result", component_id=component_id, data={
            "passed": passed,
            "mode": mode,
            "fail_count": fail_count,
            "advisory_count": advisory_count,
            "duration_seconds": round(duration, 2),
        })

    def contract_result(
        self,
        tier: int,
        passed: bool,
        breaker: str | None = None,
        duration: float = 0.0,
    ) -> None:
        self.emit("contract_result", data={
            "tier": tier,
            "passed": passed,
            "breaker": breaker,
            "duration_seconds": round(duration, 2),
        })

    def factory_completed(
        self,
        completed: int,
        failed: int,
        skipped: int,
        duration: float = 0.0,
    ) -> None:
        self.emit("factory_completed", data={
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "duration_seconds": round(duration, 2),
        })

    def read_events(self) -> list[dict[str, Any]]:
        """Read all events from the log. Useful for testing."""
        if not self._path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(self._path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events


class NullProgressLog(ProgressLog):
    """No-op progress log for when logging is disabled."""

    def __init__(self) -> None:
        # Do not call super().__init__() since we have no path
        self._path = Path("/dev/null")

    def emit(
        self,
        event_type: str,
        component_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        pass
