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
    completed_tasks: set[str] = field(default_factory=set)
    verified_profile: Any = None
    approved_fields: dict[str, Any] | None = None
    submission_reference: str | None = None
    ending: str | None = None

    facts: tuple[tuple[str, Any], ...] = ()
    identity_retries: int = 0
    spell_name: bool = False
    review_decision: str | None = None
    corrections: int = 0
    review_prefix: str = ""

    def fact(self, key):
        return dict(self.facts).get(key)

    def set_fact(self, key, value):
        if self.fact(key) != value:
            facts = self.facts_dict()
            facts[key] = value
            self.facts = tuple(facts.items())
            self.approved_fields = None

    def facts_dict(self):
        return dict(self.facts)

    def clear_results(self, *names):
        for name in names:
            self.results.pop(name, None)

    def clear_done(self, *task_ids):
        self.completed_tasks.difference_update(task_ids)

    def mark_done(self, task_id):
        self.completed_tasks.add(task_id)

    def done(self, task_id):
        return task_id in self.completed_tasks

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
