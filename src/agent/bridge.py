"""SDK-independent definitions for the call operations used today."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

ValidationResult = Literal[True] | tuple[Literal[False], str]
CallHandler = Callable[[Any], str | None]
Validator = Callable[[Any, Any], ValidationResult]


@dataclass(frozen=True)
class FieldDefinition:
    key: str
    description: str
    field_type: Literal["text", "multiple_choice", "digit_sequence"] = "text"
    required: bool = True
    choices: Sequence[str] = ()
    question: str = ""
    sensitive: bool = False


class CallBridge(Protocol):
    """Call and agent references are opaque to application behavior."""

    def set_task(self, call: Any, task_id: str, objective: str,
                 todos: Sequence[tuple[str, str]] = (), *,
                 fields: Sequence[FieldDefinition] = (), say: Sequence[str] = (),
                 completion_criteria: str = "") -> None: ...

    def add_info(self, call: Any, label: str, info: Any) -> None: ...

    def register_question(self, agent: Any, handler: Callable[[Any, str], str]) -> None: ...

    def send_instruction(self, call: Any, instruction: str) -> None: ...

    def end_call_success(self, call: Any) -> None: ...

    def hang_up(self, call: Any, final_line: str) -> None: ...

    def get_field(self, call: Any, key: str) -> Any: ...

    def get_session_id(self, call: Any) -> str: ...

    def register_actions(self, agent: Any, choices: Mapping[str, str],
                         handlers: Mapping[str, CallHandler]) -> None: ...

    def register_validator(self, agent: Any, key: str, handler: Validator) -> None: ...

    def register_task_complete(self, agent: Any, task_id: str, handler: CallHandler) -> None: ...
