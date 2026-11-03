"""Review current claim facts and capture approval, corrections or additions."""

import logging

from .. import speech
from ..bridge import FieldDefinition
from . import claim_intake

logger = logging.getLogger(__name__)
REVIEW_TASK_ID = 'review-claim'
MAX_CORRECTIONS = 5
LOSS_LINES = {'collision': ' This was a collision.', 'theft': ' This was a theft.', 'weather': ' This was weather damage.'}


def review_line(fields, summary=None):
    date = speech.spoken_date(fields['occurred_at'])
    incident = (summary or fields['description']).rstrip('. ')
    loss = LOSS_LINES.get(fields.get('loss_type'), '')
    return (f"So here's what I have. It was your {fields['vehicle']}, on {date}, "
            f"at {fields['location'].rstrip('. ')}. {incident}.{loss} "
            "Would you like to add anything, or should I file this claim?")


def start_review(call, bridge, state):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    logger.info('Claim draft saved')
    fields = claim_intake.claim_facts(state)
    line = state.review_prefix + review_line(fields, state.fact('incident_summary'))
    state.review_prefix = ''
    bridge.set_task(call, REVIEW_TASK_ID, say=[line],
        completion_criteria='Complete as soon as the decision is recorded.',
        fields=[FieldDefinition('submission_decision', 'Whether the caller explicitly approved submitting these details.',
                                field_type='multiple_choice', choices=('approve', 'change', 'add', 'decline')),
                FieldDefinition('corrected_field', 'The detail to change.', field_type='multiple_choice',
                                choices=claim_intake.CLAIM_FIELDS, required=False),
                FieldDefinition('corrected_value', 'The new value or additional details.', required=False)],
        objective="Record approve if the caller says to file it or gives any clear yes, such as yes, sure, go ahead or that is good. If they want to change a detail, record change plus which detail and its new value. "
        "If they want to add to what happened, record add and put the new details in corrected_value. "
        "If they only say no to adding, ask 'Should I file this claim?' and record their answer. Silence or uncertainty is decline. "
        "Do not say a claim was submitted. " + KEEP_CONTEXT_RULE + CALL_ENDING_RULE)
    return fields


def capture(call, state, bridge):
    decision = bridge.get_field(call, 'submission_decision')
    field = bridge.get_field(call, 'corrected_field')
    value = bridge.get_field(call, 'corrected_value')
    if decision in ('change', 'add') and state.corrections >= MAX_CORRECTIONS:
        state.review_decision = 'decline'
    elif decision == 'change':
        claim_intake.apply_correction(state, field, value)
    elif decision == 'add':
        claim_intake.apply_addition(state, value)
    else:
        state.review_decision = decision
