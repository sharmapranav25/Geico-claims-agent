"""Code-owned claim facts, collection and draft saving."""

import logging
from datetime import datetime

from .. import api_adapter, verification
from ..action_loop import Action
from ..bridge import CallBridge, FieldDefinition

logger = logging.getLogger(__name__)
TASK_ID = 'collect-claim'
TASK_IDS = (TASK_ID,)
CLAIM_FIELDS = ('vehicle', 'occurred_at', 'location', 'description', 'loss_type')
LOSS_TYPES = ('collision', 'theft', 'weather', 'other')
REQUIRED_FACTS = ('vehicle', 'occurred_at', 'location', 'description')


def vehicle_label(vehicle) -> str:
    return f"{vehicle.year} {vehicle.make} {vehicle.model}"


def claim_facts(state):
    return {key: state.fact(key) for key in CLAIM_FIELDS}


def start(call, bridge, state, say=()):
    from ..coordinator import CALL_ENDING_RULE, KEEP_CONTEXT_RULE

    profile = state.verified_profile
    if len(profile.vehicles) == 1:
        state.set_fact('vehicle', vehicle_label(profile.vehicles[0]))
    facts = claim_facts(state)
    bridge.add_info(call, 'recorded_facts', {key: value for key, value in facts.items() if value})
    today = datetime.now().astimezone().date().isoformat()
    skip = ' Ask for this. Skip it only if the caller says they cannot provide it.'
    fields = [
        FieldDefinition('vehicle', 'Once the caller names a vehicle, record it immediately and do not ask again.',
                        question='Which vehicle was involved?', field_type='multiple_choice',
                        choices=tuple(vehicle_label(vehicle) for vehicle in profile.vehicles)),
        FieldDefinition('occurred_at', f'The date and time the loss happened. Today is {today}. '
                        'If the caller gives only a time or only a day, ask for the missing part. Never assume a date. '
                        'Record it as ISO 8601, like 2026-10-31T15:00:00.' + skip,
                        question='What date and time did it happen?', required=False),
        FieldDefinition('location', 'Where the loss happened.' + skip, question='Where did it happen?', required=False),
        FieldDefinition('description', 'Everything the caller said about what happened, combined. Keep earlier details when they add more.' + skip,
                        question='What happened?', required=False),
        FieldDefinition('loss_type', 'The kind of loss.', question='What kind of loss was it: collision, theft, weather or other?',
                        field_type='multiple_choice', choices=LOSS_TYPES, required=False),
    ]
    fields = [field for field in fields if not facts[field.key]]
    if fields:
        correction = ' Record this only if the caller changes something they already gave. Never ask for it.'
        fields += [FieldDefinition('corrected_field', 'The detail changed.' + correction,
                                   field_type='multiple_choice', choices=CLAIM_FIELDS, required=False),
                   FieldDefinition('corrected_value', 'The new value.' + correction, required=False)]
    bridge.set_task(call, TASK_ID, say=say, fields=fields,
        completion_criteria='Complete as soon as every field is recorded. Do not ask for extra confirmation; the application reads the details back next.',
        objective='Fill out the loss report with the verified caller. Record anything '
        'they already described without asking again, and ask only for what '
        'is still missing. Do not decide fault, coverage or payment, and do '
        'not say a claim was created or submitted. Do not ask if there is anything else, close the call, '
        'or say the report is filed or passed along. ' + KEEP_CONTEXT_RULE + CALL_ENDING_RULE)


def capture(call, state, bridge):
    for key in CLAIM_FIELDS:
        value = bridge.get_field(call, key)
        if value is not None and str(value).strip():
            state.set_fact(key, value)
    field, value = bridge.get_field(call, 'corrected_field'), bridge.get_field(call, 'corrected_value')
    labels = tuple(vehicle_label(vehicle) for vehicle in state.verified_profile.vehicles) if state.verified_profile else ()
    if field and value and verification.correction_is_valid(field, value, labels, LOSS_TYPES):
        state.set_fact(field, value)
        logger.info('Claim fact corrected')


def apply_correction(state, field, value):
    from . import review_claim

    state.corrections += 1
    labels = tuple(vehicle_label(vehicle) for vehicle in state.verified_profile.vehicles)
    if verification.correction_is_valid(field, value, labels, LOSS_TYPES):
        logger.info('Claim fact corrected')
        state.set_fact(field, value)
        state.clear_results('save_draft', 'start_review', 'announce_save')
    else:
        logger.info('Claim correction rejected')
        state.clear_results('start_review')
        state.review_prefix = "I couldn't use that change. "
    state.clear_done(review_claim.REVIEW_TASK_ID)


def apply_addition(state, value):
    from . import review_claim

    state.corrections += 1
    if isinstance(value, str) and value.strip():
        previous = (state.fact('description') or '').rstrip('.!? ')
        state.set_fact('description', previous + '. ' + value.strip().lstrip('.!? '))
        logger.info('Claim details added')
        state.clear_results('save_draft', 'start_review', 'announce_save')
    else:
        logger.info('Claim correction rejected')
        state.clear_results('start_review')
        state.review_prefix = "I couldn't use that change. "
    state.clear_done(review_claim.REVIEW_TASK_ID)


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
    missing = verification.missing_required(claim_facts(state), REQUIRED_FACTS)
    if state.result('announce_missing') is not None:
        return None
    if missing:
        return Action('announce_missing', 'guava', lambda: _announce_missing(call, bridge, missing))
    saved = state.result('save_draft')
    if saved is None:
        def save_draft():
            profile = state.verified_profile
            chosen = state.fact('vehicle')
            vehicle_id = next((v.vehicle_id for v in profile.vehicles if vehicle_label(v) == chosen), None)
            update = api_adapter.ClaimDraftUpdate(
                reporting_intent="A", caller_role="policyholder",
                caller_name=state.fact("caller_name"),
                caller_phone=state.fact("caller_phone"),
                occurred_at=verification.parse_occurred_at(state.fact("occurred_at")),
                location=state.fact("location"),
                description=state.fact("description"),
                vehicle_id=vehicle_id,
                loss_type=state.fact("loss_type"),
            )
            with api_adapter.APIAdapter() as api:
                return api.update_claim_draft(profile.policy_number, update)

        return Action('save_draft', 'api', save_draft, (api_adapter.APIError, ValueError))
    if state.result('start_review') is not None or state.result('announce_save') is not None:
        return None
    if saved.ok:
        from . import review_claim
        return Action('start_review', 'guava', lambda: review_claim.start_review(call, bridge, state))
    def announce_save():
        if isinstance(saved.data, (api_adapter.APIUnavailableError, api_adapter.APIResponseError)):
            logger.info('Claim draft save failed')
            return on_unavailable(call)
        return _announce_failed(call, bridge)

    return Action('announce_save', 'guava', announce_save)


def _announce_missing(call, bridge, missing):
    from ..coordinator import CALL_ENDING_RULE

    labels = {'vehicle': 'the vehicle', 'occurred_at': 'the date and time',
              'location': 'the location', 'description': 'what happened'}
    readable = ', '.join(labels[key] for key in missing)
    logger.info('Claim missing required details')
    bridge.set_task(call, 'claim-missing-details',
        say=[f"I can't open the claim without {readable}. Nothing has been submitted."],
        objective='Explain nothing was submitted. Do not promise the portal can finish it. ' + CALL_ENDING_RULE)


def register(agent, bridge: CallBridge):
    def on_validate_occurred_at(call, value):
        return verification.validate_occurred_at(value)

    bridge.register_validator(agent, 'occurred_at', on_validate_occurred_at)
