"""Review current claim facts and capture approval, corrections or additions."""

import logging

from .. import speech
from ..bridge import FieldDefinition
from . import claim_intake

logger = logging.getLogger(__name__)
REVIEW_TASK_ID = 'review-claim'
MAX_CORRECTIONS = 5
# Restarting an open task under its own ID left its read-back unspoken, so each re-read gets a new ID.
TASK_IDS = (REVIEW_TASK_ID, *(f'{REVIEW_TASK_ID}-{number}' for number in range(1, MAX_CORRECTIONS + 1)))
READ_BACK_START = "So here's what I have."
LOSS_LINES = {'collision': ' This was a collision.', 'theft': ' This was a theft.', 'weather': ' This was weather damage.', 'other': " I've put the type of loss down as other."}


def review_line(fields, summary=None):
    date = speech.spoken_date(fields['occurred_at'])
    incident = (summary or fields['description']).rstrip('. ')
    loss = LOSS_LINES.get(fields.get('loss_type'), '')
    return (f"{READ_BACK_START} It was your {fields['vehicle']}, on {date}, "
            f"at {fields['location'].rstrip('. ')}. {incident}.{loss} "
            "Would you like to add anything, or should I file this claim?")


def start_review(call, bridge, state):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    logger.info('Claim draft saved')
    fields = claim_intake.claim_facts(state)
    line = state.review_prefix + review_line(fields, state.fact('incident_summary'))
    state.review_prefix = ''
    state.open_task = REVIEW_TASK_ID
    state.review_seen = (bridge.get_field(call, 'corrected_field'), bridge.get_field(call, 'corrected_value'))
    state.review_decision_seen = bridge.get_field(call, 'submission_decision')
    state.review_line_start = len(state.lines)
    task_id = f'{REVIEW_TASK_ID}-{state.corrections}' if state.corrections else REVIEW_TASK_ID
    bridge.set_task(call, task_id, say=[line],
        completion_criteria='Complete as soon as the decision is recorded.',
        fields=[FieldDefinition('submission_decision', 'Whether the caller explicitly approved submitting these details.',
                                field_type='multiple_choice', choices=('approve', 'change', 'add', 'decline')),
                FieldDefinition('corrected_field', 'The detail to change.', field_type='multiple_choice',
                                choices=claim_intake.CLAIM_FIELDS, required=False),
                FieldDefinition('corrected_value', 'The new value or additional details.', required=False)],
        objective="Record approve if the caller says to file it or gives any clear yes, such as yes, sure, go ahead or that is good. If they want to change a detail, record change plus which detail and its new value. "
        "If they want to add to what happened, record add and put the new details in corrected_value. "
        "If they only say no to adding, ask 'Should I file this claim?' once and record their answer. Once they have answered, never ask again or ask them to confirm anything. Silence or uncertainty is decline. "
        "After recording a change or addition, say only that you are updating it. Never read the details back yourself; the application does that. "
        "Do not say a claim was submitted. " + KEEP_CONTEXT_RULE + CALL_ENDING_RULE)
    return fields


def _unapplied_change(call, state, bridge):
    field, value = bridge.get_field(call, 'corrected_field'), bridge.get_field(call, 'corrected_value')
    return value is not None and bool(str(value).strip()) and (field, value) != state.review_seen


def change_pending(call, state, bridge):
    # The task can stay open after a change is recorded, so completion alone would lose it.
    if state.open_task != REVIEW_TASK_ID or state.stopped or state.claim_blocked:
        return False
    decision = bridge.get_field(call, 'submission_decision')
    if decision == 'approve':
        return decision != state.review_decision_seen and _read_back_spoken(state)
    if decision == 'change' and not bridge.get_field(call, 'corrected_field'):
        return False
    return decision in ('change', 'add') and _unapplied_change(call, state, bridge)


def _read_back_spoken(state):
    return any(line[0] == 'agent' and READ_BACK_START in line[1] for line in state.lines[state.review_line_start:])


def apply_pending(call, state, bridge):
    if not change_pending(call, state, bridge):
        return False
    state.mark_done(REVIEW_TASK_ID)
    capture(call, state, bridge)
    return True


def capture(call, state, bridge):
    decision = bridge.get_field(call, 'submission_decision')
    field = bridge.get_field(call, 'corrected_field')
    value = bridge.get_field(call, 'corrected_value')
    # Approval never covers a change that was recorded but not read back.
    if decision == 'approve' and _unapplied_change(call, state, bridge):
        decision = 'change' if field != state.review_seen[0] else 'add'
    state.open_task = None
    state.review_seen = (field, value)
    if decision in ('change', 'add') and state.corrections >= MAX_CORRECTIONS:
        state.review_decision = 'decline'
    elif decision == 'change':
        claim_intake.apply_correction(state, field, value)
    elif decision == 'add':
        claim_intake.apply_addition(state, value)
    else:
        state.review_decision = decision
