"""Deterministic mock policyholder verification, the first step of every call."""

import logging
import re
from difflib import SequenceMatcher
from typing import Literal

from .. import api_adapter
from ..action_loop import Action, advance
from ..bridge import CallBridge, FieldDefinition

logger = logging.getLogger(__name__)

TASK_ID = "verify-policy"
# Tolerates a transcription slip in a name, not a different name.
NAME_MATCH_THRESHOLD = 0.85
# Required caller-provided inputs; these do not modify the policy profile.
CHECKLIST = [
    FieldDefinition(key="policy_number", description="Ask for the policy number for the relevant GEICO policy.", field_type="text", required=True),
    FieldDefinition(key="caller_name", description="Ask the caller for their own full name, not someone else's name.", field_type="text", required=True),
    FieldDefinition(key="caller_phone", description="Ask the caller for their own phone number on the policy.", field_type="text", required=True),
]


def normalize_policy_number(value: str) -> str:
    compact = ''.join(character for character in value if character.isalnum()).upper()
    if re.fullmatch(r'GEC[0-9]{4}', compact):
        return 'GEC-' + compact[3:]
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
        return False, "That policy number was not found. Please ask the caller to check and repeat it."
    except api_adapter.APIError:
        logger.info('Policy validation unavailable')
        return False, "The policy service could not confirm the number right now. Explain the service issue; do not say the policy is invalid."
    logger.info('Policy validation passed')
    return True


def check_identity_match(profile, caller_name: str, caller_phone: str) -> bool:
    # Require BOTH: all seeded policies share a phone number. Names allow case,
    # punctuation, word order and small spelling differences; phone digits must
    # match exactly, apart from punctuation and an optional US country code.
    def name(value):
        words = re.sub(r"[^\w\s]", "", value.casefold()).split() if isinstance(value, str) else []
        return " ".join(sorted(words))

    def phone(value):
        digits = re.sub(r"\D", "", value) if isinstance(value, str) else ""
        return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits

    stated_name, stated_phone = name(caller_name), phone(caller_phone)
    if not (stated_name and stated_phone) or stated_phone != phone(profile.phone):
        return False
    return SequenceMatcher(None, stated_name, name(profile.name)).ratio() >= NAME_MATCH_THRESHOLD


def start(call, bridge: CallBridge):
    from ..coordinator import CALL_ENDING_RULE, KEEP_CONTEXT_RULE

    bridge.set_task(call,
        TASK_ID,
        objective=(
            "Greet the caller and collect their policy number, full name and the "
            "phone number on the policy. They may give these before you ask or all "
            "at once: record each one as soon as you hear it. This demo supports "
            "the policyholder only; being a representative does not establish "
            "authority. Do not disclose profile details or claim verification "
            "succeeded before the application confirms it. Do not collect loss "
            "facts yet. If the caller does not have their policy number, explain "
            "this demo cannot look it up without one. " + KEEP_CONTEXT_RULE + CALL_ENDING_RULE
        ),
        fields=CHECKLIST,
    )


def _announce_mismatch(call, bridge):
    from ..coordinator import CALL_ENDING_RULE

    logger.info('Policy completion mismatch')
    bridge.set_task(call,
        "policy-verification-mismatch",
        objective=(
            "Explain that the caller's identity could not be verified against "
            "the policy. Do not reveal the name or phone on file. The demo "
            "does not yet have a human-assistance handoff; do not claim to "
            "transfer or escalate, or to create or submit a claim. " + CALL_ENDING_RULE
        ),
    )


def _announce_unavailable(call, bridge):
    from ..coordinator import CALL_ENDING_RULE

    logger.info('Policy completion unavailable')
    bridge.set_task(call,
        "policy-verification-unavailable",
        objective=(
            "Policy verification could not be completed because the policy "
            "could not be retrieved. Do not claim verification succeeded "
            "or disclose policy details. This demo has no human-assistance "
            "handoff yet; do not claim to transfer or escalate. " + CALL_ENDING_RULE
        ),
    )


def next_action(call, state, bridge, on_verified):
    lookup = state.result('fetch_policy')
    if lookup is None:
        def fetch_policy():
            number = bridge.get_field(call, "policy_number")
            with api_adapter.APIAdapter() as api:
                return api.get_policy(normalize_policy_number(number) if isinstance(number, str) else "")

        return Action('fetch_policy', 'api', fetch_policy, (api_adapter.APIError,))
    if state.result('announce_verification') is not None:
        return None
    if not lookup.ok:
        return Action('announce_verification', 'guava', lambda: _announce_unavailable(call, bridge))
    if check_identity_match(lookup.data, bridge.get_field(call, "caller_name"), bridge.get_field(call, "caller_phone")):
        state.verified_profile = lookup.data
    if state.verified_profile is not None:
        def announce_verified():
            logger.info('Policy completion verified')
            return on_verified(call, state.verified_profile)

        return Action('announce_verification', 'guava', announce_verified)
    return Action('announce_verification', 'guava', lambda: _announce_mismatch(call, bridge))


def register(agent, bridge: CallBridge, get_state, on_verified):
    # Register at agent setup, not every time a caller enters the workflow.
    def on_validate_policy_number(call, value):
        return validate_policy_number(value)

    def on_policy_complete(call):
        advance(get_state(call), lambda state: next_action(call, state, bridge, on_verified))

    bridge.register_validator(agent, "policy_number", on_validate_policy_number)
    bridge.register_task_complete(agent, TASK_ID, on_policy_complete)
