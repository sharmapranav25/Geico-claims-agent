"""Intent A policy lookup and mock policyholder identity verification."""

import re
from typing import Literal

from .. import api_adapter
from ..bridge import CallBridge, FieldDefinition

TASK_ID = "verify-policy-intent-a"
# Required caller-provided inputs; these do not modify the policy profile.
CHECKLIST = [
    FieldDefinition(key="policy_number", description="Ask for the policy number for the relevant GEICO policy.", field_type="text", required=True),
    FieldDefinition(key="caller_name", description="Ask the caller for their own full name, not someone else's name.", field_type="text", required=True),
    FieldDefinition(key="caller_phone", description="Ask the caller for their own phone number on the policy.", field_type="text", required=True),
]


def validate_policy_number(policy_number: str) -> Literal[True] | tuple[Literal[False], str]:
    # Return validation feedback; the SDK handles retrying the task.
    if not isinstance(policy_number, str) or not policy_number.strip():
        return False, "Please ask the caller for their policy number."
    try:
        with api_adapter.APIAdapter() as api:
            api.get_policy(policy_number.strip())
    except api_adapter.PolicyNotFoundError:
        return False, "That policy number was not found. Please ask the caller to check and repeat it."
    except api_adapter.APIError:
        return False, "The policy service could not confirm the number right now. Explain the service issue; do not say the policy is invalid."
    return True


def check_identity_match(profile, caller_name: str, caller_phone: str) -> bool:
    # Require BOTH: all seeded policies share a phone number. Case/spacing and
    # phone punctuation (plus an optional US country code) are not identity differences.
    def name(value):
        return " ".join(value.casefold().split()) if isinstance(value, str) else ""

    def phone(value):
        digits = re.sub(r"\D", "", value) if isinstance(value, str) else ""
        return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits

    stated_name, stated_phone = name(caller_name), phone(caller_phone)
    return bool(stated_name and stated_phone and stated_name == name(profile.name) and stated_phone == phone(profile.phone))


def start(call, bridge: CallBridge):
    from ..coordinator import CALL_ENDING_RULE

    bridge.set_task(call,
        TASK_ID,
        objective=(
            "For Intent A, locate the relevant policy and check the caller's own "
            "name and phone against it. This demo supports the policyholder only; "
            "being a representative does not establish authority. Ask naturally "
            "for the three verification fields. Do not disclose profile details "
            "or claim verification succeeded before the application confirms it. "
            "Do not create a claim or collect loss facts yet. " + CALL_ENDING_RULE
        ),
        fields=CHECKLIST,
    )


def register(agent, bridge: CallBridge):
    # Register at agent setup, not every time a caller enters the workflow.
    def on_validate_policy_number(call, value):
        return validate_policy_number(value)

    def on_policy_complete(call):
        from ..coordinator import CALL_ENDING_RULE

        # Read this call's fields and fetch a fresh profile before matching identity.
        number = bridge.get_field(call, "policy_number")
        try:
            with api_adapter.APIAdapter() as api:
                profile = api.get_policy(number.strip() if isinstance(number, str) else "")
        except api_adapter.APIError:
            bridge.set_task(call,
                "policy-verification-unavailable",
                objective=(
                    "Policy verification could not be completed because the policy "
                    "could not be retrieved. Do not claim verification succeeded "
                    "or disclose policy details. This demo has no human-assistance "
                    "handoff yet; do not claim to transfer or escalate. " + CALL_ENDING_RULE
                ),
            )
            return
        if check_identity_match(profile, bridge.get_field(call, "caller_name"), bridge.get_field(call, "caller_phone")):
            bridge.set_task(call,
                "policy-verified-placeholder",
                objective=(
                    f"Identity is verified for policy {profile.policy_number} in this mock demo. "
                    "Acknowledge successful verification. The real claim-start and "
                    "information-collection workflow is not implemented yet. Do not "
                    "invent claim actions or claim to create, save, or submit a claim. " + CALL_ENDING_RULE
                ),
            )
        else:
            bridge.set_task(call,
                "policy-verification-mismatch",
                objective=(
                    "Explain that the caller's identity could not be verified against "
                    "the policy. Do not reveal the name or phone on file. The demo "
                    "does not yet have a human-assistance handoff; do not claim to "
                    "transfer or escalate, or to create or submit a claim. " + CALL_ENDING_RULE
                ),
            )

    bridge.register_validator(agent, "policy_number", on_validate_policy_number)
    bridge.register_task_complete(agent, TASK_ID, on_policy_complete)
