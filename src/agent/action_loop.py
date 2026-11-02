"""Evaluate, perform one adapter action, record its result, and evaluate again."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .call_state import ActionResult, CallState

logger = logging.getLogger(__name__)
MAX_ACTIONS = 10


@dataclass(frozen=True)
class Action:
    name: str
    target: Literal['guava', 'api']
    run: Callable[[], Any]
    failure_types: tuple[type[Exception], ...] = ()


def advance(state: CallState, evaluate: Callable[[CallState], Action | None]) -> None:
    for _ in range(MAX_ACTIONS):
        action = evaluate(state)
        if action is None:
            return
        if state.result(action.name) is not None:
            raise RuntimeError(f'Action already recorded: {action.name}')
        try:
            data = action.run()
        except action.failure_types as error:
            result = ActionResult(action.name, action.target, False, error)
        else:
            result = ActionResult(action.name, action.target, True, data)
        state.record(result)
        logger.info('Action %s target=%s ok=%s', result.name, result.target, result.ok)
    raise RuntimeError(f'Action limit reached: {MAX_ACTIONS}')
