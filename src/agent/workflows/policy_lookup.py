"""Policy lookup and policy-number validation."""

import logging
from typing import Literal

from .. import api_adapter
from ..action_loop import Action
from ..bridge import CallBridge

logger = logging.getLogger(__name__)


def normalize_policy_number(value: str) -> str:
    compact = ''.join(character for character in value if character.isalnum()).upper()
    if compact.isdigit():
        return compact
    return value.strip()


def validate_policy_number(policy_number: str) -> Literal[True] | tuple[Literal[False], str]:
    # Return validation feedback; the SDK handles retrying the task.
    if not isinstance(policy_number, str) or not policy_number.strip():
        return False, "Please ask the caller for their policy number."
    try:
        with api_adapter.APIAdapter() as api:
            api.get_policy(normalize_policy_number(policy_number))
    except api_adapter.PolicyNotFoundError:
        logger.info('Policy validation not found')
        return False, "I couldn't find that policy number. Ask the caller to repeat it."
    except api_adapter.APIError:
        logger.info('Policy validation unavailable')
        # An outage is not a bad number. The completion lookup fails and routes to assistance.
        return True
    logger.info('Policy validation passed')
    return True


def find_policy_action(policy_number):
    def fetch_policy():
        with api_adapter.APIAdapter() as api:
            return api.get_policy(normalize_policy_number(policy_number) if isinstance(policy_number, str) else '')

    return Action('fetch_policy', 'api', fetch_policy, (api_adapter.APIError,))


def register(agent, bridge: CallBridge):
    # Register at agent setup, not every time a caller enters the workflow.
    def on_validate_policy_number(call, value):
        return validate_policy_number(value)

    bridge.register_validator(agent, "policy_number", on_validate_policy_number)
