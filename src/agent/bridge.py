"""SDK-independent definitions for the call operations used today."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

ValidationResult = Literal[True] | tuple[Literal[False], str]
CallHandler = Callable[[Any], None]
Validator = Callable[[Any, Any], ValidationResult]


@dataclass(frozen=True)
class FieldDefinition:
    key: str
    description: str
    field_type: Literal["text"] = "text"
    required: bool = True


class CallBridge(Protocol):
    """Call and agent references are opaque to application behavior."""

    def set_task(self, call: Any, task_id: str, objective: str,
                 todos: Sequence[tuple[str, str]] = (), *,
                 fields: Sequence[FieldDefinition] = ()) -> None: ...

    def send_instruction(self, call: Any, instruction: str) -> None: ...

    def get_field(self, call: Any, key: str) -> Any: ...

    def register_actions(self, agent: Any, choices: Mapping[str, str],
                         handlers: Mapping[str, CallHandler]) -> None: ...

    def register_validator(self, agent: Any, key: str, handler: Validator) -> None: ...

    def register_task_complete(self, agent: Any, task_id: str, handler: CallHandler) -> None: ...
