"""Session-scoped call state and its in-memory store."""

from dataclasses import dataclass, field
from threading import Lock, RLock
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
    lock: RLock = field(default_factory=RLock, repr=False)
    ended: bool = False
    stopped: bool = False
    claim_blocked: bool = False
    lines: list = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    pending_nudges: list = field(default_factory=list)
    nudge_count: int = 0
    raised: set = field(default_factory=set)
    live_seen: dict = field(default_factory=dict)
    live_keys: tuple = ()
    open_task: str | None = None
    closeout_generation: int = 0
    summary_ready: bool = False
    consistency_ready: bool = False
    loss_type_corrected: bool = False
    details_ready: bool = False
    details_question: str | None = None
    review_seen: tuple = (None, None)
    review_decision_seen: str | None = None
    review_line_start: int = 0
    speech_timers: dict = field(default_factory=dict, repr=False)
    pending_lines: set = field(default_factory=set, repr=False)
    classified_lines: set = field(default_factory=set, repr=False)
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
