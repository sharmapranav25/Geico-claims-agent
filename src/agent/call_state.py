"""Session-scoped call state and its in-memory store."""

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ActionResult:
    name: str
    target: Literal['guava', 'api', 'local']
    ok: bool
    data: Any = None


@dataclass
class CallState:
    session_id: str
    results: dict[str, ActionResult] = field(default_factory=dict)
    verified_profile: Any = None
    approved_fields: dict[str, Any] | None = None
    submission_reference: str | None = None

    def record(self, result: ActionResult) -> None:
        self.results[result.name] = result

    def result(self, name: str) -> ActionResult | None:
        return self.results.get(name)


class StateStore(Protocol):
    def get(self, session_id: str) -> CallState: ...

    def delete(self, session_id: str) -> None: ...


class InMemoryStateStore:
    def __init__(self):
        self._states: dict[str, CallState] = {}
        self._lock = Lock()

    def get(self, session_id: str) -> CallState:
        with self._lock:
            if session_id not in self._states:
                self._states[session_id] = CallState(session_id)
            return self._states[session_id]

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._states.pop(session_id, None)
