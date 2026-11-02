"""Collect loss facts for a verified caller and save them as a claim draft."""

import logging
from datetime import datetime

from .. import api_adapter
from ..bridge import CallBridge, FieldDefinition

logger = logging.getLogger(__name__)

TASK_ID = "collect-claim"
LOSS_TYPES = ("collision", "theft", "weather", "other")


def vehicle_label(vehicle) -> str:
    return f"{vehicle.year} {vehicle.make} {vehicle.model}"


def parse_occurred_at(value):
    # The API requires a timezone; a time without one is the caller's local time.
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def start(call, bridge: CallBridge, profile):
    from ..coordinator import CALL_ENDING_RULE, KEEP_CONTEXT_RULE

    today = datetime.now().astimezone().date().isoformat()
    bridge.set_task(call,
        TASK_ID,
        objective=(
            "Fill out the loss report with the verified caller. Record anything "
            "they already described without asking again, and ask only for what "
            "is still missing. Do not decide fault, coverage or payment, and do "
            "not say a claim was created or submitted. " + KEEP_CONTEXT_RULE + CALL_ENDING_RULE
        ),
        fields=[
            FieldDefinition(
                key="vehicle", description="Which vehicle on the policy was involved.",
                field_type="multiple_choice",
                choices=tuple(vehicle_label(vehicle) for vehicle in profile.vehicles),
            ),
            FieldDefinition(
                key="occurred_at",
                description=f"When the loss happened. Today is {today}. Record it as ISO 8601, like 2026-10-31T15:00:00.",
            ),
            FieldDefinition(key="location", description="Where the loss happened."),
            FieldDefinition(key="description", description="What happened, in the caller's words."),
            FieldDefinition(
                key="loss_type", description="The kind of loss.",
                field_type="multiple_choice", choices=LOSS_TYPES,
            ),
        ],
    )


def register(agent, bridge: CallBridge, get_profile):
    def on_validate_occurred_at(call, value):
        if parse_occurred_at(value) is None:
            return False, "Ask the caller again when the loss happened, including the date and approximate time."
        return True

    def on_claim_complete(call):
        from ..coordinator import CALL_ENDING_RULE

        profile = get_profile(call)
        if profile is None:
            return
        chosen = bridge.get_field(call, "vehicle")
        vehicle_id = next((v.vehicle_id for v in profile.vehicles if vehicle_label(v) == chosen), None)
        try:
            update = api_adapter.ClaimDraftUpdate(
                reporting_intent="A", caller_role="policyholder",
                caller_name=bridge.get_field(call, "caller_name"),
                caller_phone=bridge.get_field(call, "caller_phone"),
                occurred_at=parse_occurred_at(bridge.get_field(call, "occurred_at")),
                location=bridge.get_field(call, "location"),
                description=bridge.get_field(call, "description"),
                vehicle_id=vehicle_id,
                loss_type=bridge.get_field(call, "loss_type"),
            )
            with api_adapter.APIAdapter() as api:
                api.update_claim_draft(profile.policy_number, update)
        except (api_adapter.APIError, ValueError):
            logger.info('Claim draft save failed')
            bridge.set_task(call,
                "claim-save-failed",
                objective=(
                    "The loss details could not be saved. Say so honestly. Do not "
                    "claim anything was saved or submitted, or that you can "
                    "transfer the caller. " + CALL_ENDING_RULE
                ),
            )
            return
        logger.info('Claim draft saved')
        bridge.set_task(call,
            "claim-details-saved",
            objective=(
                "The loss details are saved as a draft. Tell the caller that. It is "
                "not a submitted claim and has no claim number yet; reviewing and "
                "submitting is not implemented, so do not claim to do it. " + CALL_ENDING_RULE
            ),
        )

    bridge.register_validator(agent, "occurred_at", on_validate_occurred_at)
    bridge.register_task_complete(agent, TASK_ID, on_claim_complete)
