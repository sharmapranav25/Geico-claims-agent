"""Collect, review and submit claims for a verified caller."""

import logging
from datetime import datetime
from uuid import uuid4

from .. import api_adapter
from ..action_loop import Action
from ..bridge import CallBridge, FieldDefinition

logger = logging.getLogger(__name__)

TASK_ID = "collect-claim"
REVIEW_TASK_ID = "review-claim"
SUBMITTED_TASK_ID = "claim-submitted"
TASK_IDS = (TASK_ID, REVIEW_TASK_ID, SUBMITTED_TASK_ID)
CLAIM_FIELDS = ("vehicle", "occurred_at", "location", "description", "loss_type")
PORTAL_LINE = "You can upload supporting photos and documents later through your GEICO claims portal."
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


def validate_occurred_at(value, now=None):
    parsed = parse_occurred_at(value)
    if parsed is None:
        return False, "Ask the caller again when the loss happened, including the date and approximate time."
    now = now if now is not None else datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    if parsed > now:
        return False, "That time has not happened yet. Ask the caller for the date and time of the loss."
    return True


def start(call, bridge: CallBridge, profile):
    from ..coordinator import CALL_ENDING_RULE, KEEP_CONTEXT_RULE

    today = datetime.now().astimezone().date().isoformat()
    single_vehicle = len(profile.vehicles) == 1
    vehicle_rule = (f'The vehicle on this policy is {vehicle_label(profile.vehicles[0])}. '
                    'Do not ask which vehicle; the caller confirms it in the read-back. ') if single_vehicle else ''
    bridge.set_task(call,
        TASK_ID,
        objective=(
            "Fill out the loss report with the verified caller. Record anything "
            "they already described without asking again, and ask only for what "
            "is still missing. Do not decide fault, coverage or payment, and do "
            "not say a claim was created or submitted. Do not ask if there is anything else, close the call, "
            "or say the report is filed or passed along. " + vehicle_rule + KEEP_CONTEXT_RULE + CALL_ENDING_RULE
        ),
        completion_criteria="Complete as soon as every field is recorded. Do not ask for extra confirmation; the application reads the details back next.",
        fields=([] if single_vehicle else [
            FieldDefinition(
                key="vehicle", description="Once the caller names a vehicle, record it immediately and do not ask again.",
                question="Which vehicle was involved?",
                field_type="multiple_choice",
                choices=tuple(vehicle_label(vehicle) for vehicle in profile.vehicles),
            ),
        ]) + [
            FieldDefinition(
                key="occurred_at",
                question="What date and time did it happen?",
                description=f"The date and time the loss happened. Today is {today}. If the caller gives only a time or only a day, ask for the missing part. Never assume a date. Record it as ISO 8601, like 2026-10-31T15:00:00.",
            ),
            FieldDefinition(key="location", description="Where the loss happened."),
            FieldDefinition(key="description", description="Everything the caller said about what happened, combined. Keep earlier details when they add more."),
            FieldDefinition(
                key="loss_type", description="The kind of loss.",
                field_type="multiple_choice", choices=LOSS_TYPES,
            ),
        ],
    )


def current_claim_fields(call, bridge, profile):
    return {key: vehicle_label(profile.vehicles[0]) if key == 'vehicle' and len(profile.vehicles) == 1
            else bridge.get_field(call, key) for key in CLAIM_FIELDS}


def review_line(fields):
    date = parse_occurred_at(fields['occurred_at']).strftime('%A, %B %-d at %-I:%M %p')
    return (f"Here is what I have. Vehicle: {fields['vehicle']}. When: {date}. "
            f"Where: {fields['location'].rstrip('. ')}. What happened: {fields['description'].rstrip('. ')}. "
            f"Type of loss: {fields['loss_type']}. Would you like me to submit this claim?")


def _start_review(call, bridge, profile):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    logger.info('Claim draft saved')
    fields = current_claim_fields(call, bridge, profile)
    bridge.set_task(call, REVIEW_TASK_ID, say=[review_line(fields)],
        completion_criteria="Complete as soon as the decision is recorded.",
        fields=[FieldDefinition('submission_decision', 'Whether the caller explicitly approved submitting these details.',
                                field_type='multiple_choice', choices=('approve', 'decline'))],
        objective=("Record approve only if the caller clearly says yes to submitting. Silence, uncertainty or a request "
                   "to change anything is decline. If they want to change a detail, say corrections are not available yet. "
                   "Do not say a claim was submitted. " + KEEP_CONTEXT_RULE + CALL_ENDING_RULE),
    )
    return fields


def _announce_failed(call, bridge):
    from ..coordinator import CALL_ENDING_RULE

    logger.info('Claim draft save failed')
    bridge.set_task(call,
        "claim-save-failed",
        objective=(
            "The loss details could not be saved. Say so honestly. Do not "
            "claim anything was saved or submitted, or that you can "
            "transfer the caller. " + CALL_ENDING_RULE
        ),
    )


def next_action(call, state, bridge, on_unavailable):
    if state.verified_profile is None:
        return None
    saved = state.result('save_draft')
    if saved is None:
        def save_draft():
            profile = state.verified_profile
            chosen = current_claim_fields(call, bridge, profile)['vehicle']
            vehicle_id = next((v.vehicle_id for v in profile.vehicles if vehicle_label(v) == chosen), None)
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
                return api.update_claim_draft(profile.policy_number, update)

        return Action('save_draft', 'api', save_draft, (api_adapter.APIError, ValueError))
    if state.result('start_review') is not None or state.result('announce_save') is not None:
        return None
    if saved.ok:
        return Action('start_review', 'guava', lambda: _start_review(call, bridge, state.verified_profile))
    def announce_save():
        if isinstance(saved.data, (api_adapter.APIUnavailableError, api_adapter.APIResponseError)):
            logger.info('Claim draft save failed')
            return on_unavailable(call)
        return _announce_failed(call, bridge)

    return Action('announce_save', 'guava', announce_save)


def next_submit_action(call, state, bridge, on_unavailable):
    if state.verified_profile is None:
        return None
    submitted = state.result('submit_claim')
    if submitted is None:
        if state.result('announce_not_submitted') is not None:
            return None
        reviewed = state.result('start_review')
        fields = current_claim_fields(call, bridge, state.verified_profile)
        if reviewed is None or bridge.get_field(call, 'submission_decision') != 'approve' or fields != reviewed.data:
            return Action('announce_not_submitted', 'guava', lambda: _announce_not_submitted(call, bridge))
        state.approved_fields = fields
        if state.submission_reference is None:
            state.submission_reference = f'sub-{uuid4().hex}'

        def submit_claim():
            with api_adapter.APIAdapter() as api:
                return api.submit_claim_draft(state.verified_profile.policy_number,
                                              submission_reference=state.submission_reference)

        return Action('submit_claim', 'api', submit_claim, (api_adapter.APIError,))
    if state.result('announce_submission') is not None:
        return None

    def announce_submission():
        if submitted.ok:
            return _announce_claim(call, bridge, submitted.data.claim_number)
        if isinstance(submitted.data, (api_adapter.APIUnavailableError, api_adapter.APIResponseError)):
            logger.info('Claim submission unavailable')
            return on_unavailable(call)
        return _announce_submit_failed(call, bridge)

    return Action('announce_submission', 'guava', announce_submission)


def _announce_claim(call, bridge, claim_number):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    logger.info('Claim submitted')
    bridge.set_task(call, SUBMITTED_TASK_ID,
        say=[f'Your claim has been submitted. Your claim number is {claim_number}. {PORTAL_LINE} Is there anything else you need?'],
        fields=[FieldDefinition('needs_more_help', 'Whether the caller needs anything else.',
                                field_type='multiple_choice', choices=('yes', 'no'))],
        objective='Record whether the caller needs anything else. Do not decide fault, coverage or payment. '
                  + KEEP_CONTEXT_RULE + CALL_ENDING_RULE,
    )


def _announce_not_submitted(call, bridge):
    from ..coordinator import CALL_ENDING_RULE

    logger.info('Claim not submitted')
    bridge.set_task(call, 'claim-not-submitted', objective=(
        'Nothing was submitted and there is no claim number. Explain this without pressure. The details are saved '
        'as a draft, which is not a claim. Do not promise a resume text or that the portal can finish it. ' + CALL_ENDING_RULE
    ))


def _announce_submit_failed(call, bridge):
    from ..coordinator import CALL_ENDING_RULE

    logger.info('Claim submission failed')
    bridge.set_task(call, 'claim-submit-failed', objective=(
        'The claim could not be submitted and there is no claim number. Say so honestly. '
        'Do not claim to transfer the caller. ' + CALL_ENDING_RULE
    ))


def next_close_action(call, state, bridge):
    from ..coordinator import CALL_ENDING_RULE

    if state.result('close_call') is not None:
        return None
    if bridge.get_field(call, 'needs_more_help') == 'no':
        return Action('close_call', 'guava', lambda: bridge.end_call_success(call))
    return Action('close_call', 'guava', lambda: bridge.set_task(call, 'claim-more-help', objective=(
        'This demo only opens new loss reports. Explain that limit without inventing contact details '
        'or claiming to transfer the caller. ' + CALL_ENDING_RULE
    )))


def register(agent, bridge: CallBridge):
    def on_validate_occurred_at(call, value):
        return validate_occurred_at(value)

    bridge.register_validator(agent, "occurred_at", on_validate_occurred_at)
