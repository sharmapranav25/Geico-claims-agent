"""Code-owned claim facts, collection and draft saving."""

import logging

from .. import api_adapter, speech, verification
from . import endings
from ..action_loop import Action
from ..bridge import CallBridge, FieldDefinition

logger = logging.getLogger(__name__)
TASK_ID = 'collect-claim'
TASK_IDS = (TASK_ID,)
CLAIM_FIELDS = ('vehicle', 'occurred_at', 'location', 'description', 'loss_type')
LOSS_TYPES = ('collision', 'theft', 'weather', 'other')
REQUIRED_FACTS = ('vehicle', 'occurred_at', 'location', 'description')
NOT_ON_POLICY = 'not on this policy'


def vehicle_label(vehicle) -> str:
    return f"{vehicle.year} {vehicle.make} {vehicle.model}"


def claim_facts(state):
    return {key: state.fact(key) for key in CLAIM_FIELDS}


def start(call, bridge, state, say=()):
    from ..coordinator import CALL_ENDING_RULE, KEEP_CONTEXT_RULE

    profile = state.verified_profile
    labels = tuple(vehicle_label(vehicle) for vehicle in profile.vehicles)
    facts = claim_facts(state)
    bridge.add_info(call, 'recorded_facts', {key: value for key, value in facts.items() if value})
    skip = ' Ask for this. Skip it only if the caller says they cannot provide it.'
    fields = [
        FieldDefinition('vehicle', 'The vehicle involved must match a choice exactly, including the year, make and model. Never assume. '
                        'If the caller names anything different, tell them right away what this policy shows and ask which they mean, before asking anything else. '
                        f"Record '{NOT_ON_POLICY}' if they confirm it is none of them.",
                        question=f'I see your {labels[0]} on this policy. Was that the vehicle involved?' if len(labels) == 1 else 'Which vehicle was involved?',
                        field_type='multiple_choice', choices=(*labels, NOT_ON_POLICY)),
        FieldDefinition('occurred_at', f'The date and time the loss happened. {speech.date_context()} '
                        'If the caller gives only a time or only a day, ask for the missing part. Never assume a date. '
                        'Record it as ISO 8601, like 2026-10-31T15:00:00.' + skip,
                        question='What date and time did it happen?', required=False),
        FieldDefinition('location', 'Where the loss happened.' + skip, question='Where did it happen?', required=False),
        FieldDefinition('description', "Everything the caller said about what happened, combined. Keep earlier details when they add more. "
                        "Record what happened in the caller's own words, for example 'my mother', not 'the caller's mother'." + skip,
                        question='What happened?', required=False),
    ]
    fields = [field for field in fields if not facts[field.key]]
    keep_description = bool(fields and facts['description'])
    if keep_description:
        fields.append(FieldDefinition('description',
            "The caller already described this. Update it only if they add details, combining old and new in the caller's own words. Never ask for it from scratch.",
            required=False))
    if fields:
        correction = ' Record this only if the caller changes something they already gave. Never ask for it.'
        fields += [FieldDefinition('corrected_field', 'The detail changed.' + correction,
                                   field_type='multiple_choice', choices=CLAIM_FIELDS, required=False),
                   FieldDefinition('corrected_value', 'The new value.' + correction, required=False)]
    state.open_task = TASK_ID
    state.live_keys = tuple(field.key for field in fields if not field.key.startswith('corrected_'))
    state.live_seen = {key: bridge.get_field(call, key) for key in state.live_keys}
    bridge.set_task(call, TASK_ID, say=say, fields=fields,
        completion_criteria='Complete as soon as every field you need to ask for is recorded. Do not ask for extra confirmation; the application reads the details back next.',
        objective='Fill out the loss report with the verified caller. Record anything '
        'they already described without asking again, and ask only for what '
        'is still missing. Do not decide fault, coverage or payment, and do '
        'not say a claim was created or submitted. Do not ask if there is anything else, close the call, '
        'or say the report is filed or passed along. ' + KEEP_CONTEXT_RULE + CALL_ENDING_RULE)
    if keep_description:
        supervisor.check_description(call, state, bridge)


def capture_live(call, state, bridge):
    from .. import supervisor
    from . import identity

    if state.open_task not in (identity.TASK_ID, TASK_ID) or state.stopped or state.claim_blocked:
        state.live_keys = ()
        return
    for key in state.live_keys:
        value = bridge.get_field(call, key)
        if value == state.live_seen.get(key) or value is None or not str(value).strip():
            continue
        state.live_seen[key] = value
        if key in ('occurred_at', 'stated_time') and verification.validate_occurred_at(value) is not True:
            continue
        fact_key = 'occurred_at' if key == 'stated_time' else key
        state.set_fact(fact_key, value)
        supervisor.on_fact_changed(call, state, bridge, fact_key)


def capture(call, state, bridge):
    for key in CLAIM_FIELDS:
        value = bridge.get_field(call, key)
        if value is not None and str(value).strip() and value != NOT_ON_POLICY:
            state.set_fact(key, value)
    field, value = bridge.get_field(call, 'corrected_field'), bridge.get_field(call, 'corrected_value')
    labels = tuple(vehicle_label(vehicle) for vehicle in state.verified_profile.vehicles) if state.verified_profile else ()
    if field and value and verification.correction_is_valid(field, value, labels, LOSS_TYPES):
        state.loss_type_corrected = state.loss_type_corrected or field == 'loss_type'
        state.set_fact(field, value)
        logger.info('Claim fact corrected')


def apply_correction(state, field, value):
    from . import review_claim

    state.corrections += 1
    labels = tuple(vehicle_label(vehicle) for vehicle in state.verified_profile.vehicles)
    if verification.correction_is_valid(field, value, labels, LOSS_TYPES):
        logger.info('Claim fact corrected')
        state.loss_type_corrected = state.loss_type_corrected or field == 'loss_type'
        state.set_fact(field, value)
        state.clear_results('save_draft', 'start_review', 'announce_save', 'write_summary', 'final_consistency')
        state.closeout_generation += 1
        state.summary_ready = state.consistency_ready = False
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
        state.clear_results('save_draft', 'start_review', 'announce_save', 'write_summary', 'final_consistency')
        state.closeout_generation += 1
        state.summary_ready = state.consistency_ready = False
    else:
        logger.info('Claim correction rejected')
        state.clear_results('start_review')
        state.review_prefix = "I couldn't use that change. "
    state.clear_done(review_claim.REVIEW_TASK_ID)


def store_summary(state, summary, loss_type=None, report=None):
    state.set_fact('incident_summary', summary)
    # The read-back speaks to the caller; the claim file holds the third-person report.
    state.set_fact('incident_report', report)
    # The model proposes the loss type and the read-back asks the caller; a caller's correction stands.
    if loss_type and not state.loss_type_corrected:
        state.set_fact('loss_type', loss_type)


def _announce_failed(call, bridge, state):
    from ..coordinator import CALL_ENDING_RULE

    logger.info('Claim draft save failed')
    endings.start(call, bridge, state, 'save_failed',
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
        return Action('announce_missing', 'guava', lambda: _announce_missing(call, bridge, state, missing))
    from .. import supervisor
    if state.result('write_summary') is None:
        return Action('write_summary', 'guava', lambda: supervisor.start_summary(call, state, bridge))
    if not state.summary_ready:
        return None
    if state.result('final_consistency') is None:
        return Action('final_consistency', 'guava', lambda: supervisor.start_final_consistency(call, state, bridge))
    if not state.consistency_ready:
        return None
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
                description=state.fact("incident_report") or state.fact("description"),
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
        return _announce_failed(call, bridge, state)

    return Action('announce_save', 'guava', announce_save)


def _announce_missing(call, bridge, state, missing):
    from ..coordinator import CALL_ENDING_RULE

    labels = {'vehicle': 'the vehicle', 'occurred_at': 'the date and time',
              'location': 'the location', 'description': 'what happened'}
    readable = ', '.join(labels[key] for key in missing)
    logger.info('Claim missing required details')
    endings.start(call, bridge, state, 'missing_details', 'claim-missing-details',
        say=[f"I can't open the claim without {readable}. Nothing has been submitted."],
        objective='Explain nothing was submitted. Do not promise the portal can finish it. ' + CALL_ENDING_RULE)


def register(agent, bridge: CallBridge):
    def on_validate_occurred_at(call, value):
        return verification.validate_occurred_at(value)

    bridge.register_validator(agent, 'occurred_at', on_validate_occurred_at)
