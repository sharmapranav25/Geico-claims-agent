"""Caller identity collection, read-back, retries and verification."""

import logging

from .. import speech, verification
from ..action_loop import Action
from ..bridge import FieldDefinition
from . import endings, policy_lookup

logger = logging.getLogger(__name__)
TASK_ID = 'verify-policy'
CONFIRM_TASK_ID = 'confirm-identity'
TASK_IDS = (TASK_ID, CONFIRM_TASK_ID)
MAX_IDENTITY_RETRIES = 2
POLICY_QUESTION = 'What is your policy number?'
NAME_QUESTION = 'What is your full name?'
SPELL_NAME_QUESTION = 'Please spell your full name for me.'
PHONE_QUESTION = 'What is your phone number?'
HELP_QUESTION = 'How can I help you today?'
RETRY_LINE = "Hmm, I couldn't verify those details. Let's try that one more time."
FAILED_LINE = "I'm sorry, I still couldn't verify those details, so I can't open a claim on this call."
VERIFIED_LINE = "Thanks so much, you're all verified."


def identity_fields(spell_name=False, ask_request=False):
    from ..coordinator import REPORT_OWN_LOSS, OTHER_REQUEST

    volunteer = ' Record this only if the caller volunteers it. Never ask for it.'
    request = [FieldDefinition('request_type', 'What the caller wants: report_own_loss if they are reporting a loss or opening a claim, otherwise other_request.',
                               question=HELP_QUESTION, field_type='multiple_choice', choices=(REPORT_OWN_LOSS, OTHER_REQUEST), required=False)] if ask_request else []
    return request + [
        FieldDefinition('policy_number', 'The policy number, digits only. Do not record letters.',
                        question=POLICY_QUESTION, field_type='digit_sequence', sensitive=True),
        FieldDefinition('caller_name', 'Have the caller spell it letter by letter, then record it as normal words, like Jessica Brown.'
                        if spell_name else "The caller's own full name, recorded as normal words.",
                        question=SPELL_NAME_QUESTION if spell_name else NAME_QUESTION),
        FieldDefinition('caller_phone', "Ask the caller for their own phone number on the policy.",
                        question=PHONE_QUESTION, field_type='digit_sequence', sensitive=True),
        *([] if ask_request else [FieldDefinition('request_type', 'The request type.' + volunteer, required=False,
                                                  field_type='multiple_choice', choices=(REPORT_OWN_LOSS, OTHER_REQUEST))]),
        FieldDefinition('description', "What happened. Record what happened in the caller's own words, for example 'my mother', not 'the caller's mother'." + volunteer, required=False),
        FieldDefinition('location', 'Where it happened.' + volunteer, required=False),
        FieldDefinition('loss_type', 'The kind of loss.' + volunteer, required=False,
                        field_type='multiple_choice', choices=LOSS_TYPES),
        FieldDefinition('stated_time', f'The stated loss time as ISO 8601, like 2026-10-31T15:00:00. {speech.date_context()}'
                        + volunteer, required=False),
    ]


def start(call, bridge, state=None):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE, state_for

    state = state if state is not None else state_for(call, bridge)
    _start_live(call, state, bridge)

    bridge.set_task(call, TASK_ID, fields=identity_fields(ask_request=True),
        completion_criteria='Complete as soon as the policy number, name and phone are recorded. Do not read them back or re-confirm them.',
        objective="Do not greet the caller again. Collect the policy number, name and phone using each field's question wording exactly. "
        "If the caller already gave one, record it and skip that question. Ask only the three identity questions. "
        "Do not ask about the loss or discuss it until the application tells you to. Say nothing about what the policy contains. "
        "Never use the phrase 'against the policy'. This demo supports the policyholder only. "
        "If the caller lacks their policy number, explain this demo cannot look it up without it. " + KEEP_CONTEXT_RULE + CALL_ENDING_RULE)


def _start_live(call, state, bridge):
    state.open_task = TASK_ID
    state.live_keys = ('description', 'location', 'loss_type', 'stated_time')
    state.live_seen = {key: bridge.get_field(call, key) for key in state.live_keys}


def capture(call, state, bridge):
    state.clear_results('start_confirm', 'identity_retry', 'fetch_policy', 'announce_verification')
    state.clear_done(CONFIRM_TASK_ID)
    for key in ('policy_number', 'caller_name', 'caller_phone', 'request_type', 'description', 'location', 'loss_type'):
        value = bridge.get_field(call, key)
        if value is not None and str(value).strip():
            state.set_fact(key, policy_lookup.normalize_policy_number(value) if key == 'policy_number' else value)
    value = bridge.get_field(call, 'stated_time')
    if value is not None and str(value).strip() and verification.validate_occurred_at(value) is True:
        state.set_fact('occurred_at', value)


def _digits(value):
    return ' '.join(character for character in str(value) if character.isalnum())


def _confirm_identity(call, state, bridge):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    name = state.fact('caller_name')
    if state.spell_name:
        name = ' , '.join(_digits(word) for word in name.split())
    bridge.set_task(call, CONFIRM_TASK_ID,
        say=[f"I have your policy number as {speech.spell_out(state.fact('policy_number'))}, your name as {name}, "
             f"and your phone number as {speech.spell_out(state.fact('caller_phone'))}. Is that right?"],
        fields=[FieldDefinition('identity_confirmed', 'Whether the caller confirms these details.',
                                field_type='multiple_choice', choices=('yes', 'no'))],
        objective='Record whether the caller confirms these details. Say nothing else about them. ' + KEEP_CONTEXT_RULE + CALL_ENDING_RULE,
        completion_criteria='Complete as soon as the answer is recorded.')


def next_action(call, state, bridge, on_verified, on_unavailable):
    if state.result('announce_verification') is not None:
        return None
    if state.result('start_confirm') is None:
        return Action('start_confirm', 'guava', lambda: _confirm_identity(call, state, bridge))
    if not state.done(CONFIRM_TASK_ID):
        return None
    if bridge.get_field(call, 'identity_confirmed') != 'yes':
        return _retry_or_fail(call, state, bridge, spell_name=False)
    lookup = state.result('fetch_policy')
    if lookup is None:
        return policy_lookup.find_policy_action(state.fact('policy_number'))
    if not lookup.ok:
        def announce_unavailable():
            logger.info('Policy completion unavailable')
            return on_unavailable(call)
        return Action('announce_verification', 'guava', announce_unavailable)
    if verification.identity_matches(lookup.data, state.fact('caller_name'), state.fact('caller_phone')):
        state.verified_profile = lookup.data
        def announce_verified():
            logger.info('Policy completion verified')
            return on_verified(call, state.verified_profile)
        return Action('announce_verification', 'guava', announce_verified)
    return _retry_or_fail(call, state, bridge, spell_name=True)


def _retry_or_fail(call, state, bridge, spell_name):
    from ..coordinator import CALL_ENDING_RULE

    if state.result('identity_retry') is not None:
        return None
    if state.identity_retries < MAX_IDENTITY_RETRIES:
        def retry():
            state.identity_retries += 1
            state.spell_name = state.spell_name or spell_name
            state.clear_done(TASK_ID, CONFIRM_TASK_ID)
            logger.info('Policy identity retry')
            _start_live(call, state, bridge)
            bridge.set_task(call, TASK_ID, say=[RETRY_LINE], fields=identity_fields(state.spell_name),
                objective="Collect the policy number, name and phone again using each field's question wording exactly. "
                "Ask for all three again and do not reuse earlier answers. Ask only the three identity questions. "
                "Do not ask about the loss or discuss it until the application tells you to. Do not greet the caller again. "
                "Say nothing about what the policy contains or which detail failed. Never use the phrase 'against the policy'. " + CALL_ENDING_RULE)
        return Action('identity_retry', 'guava', retry)
    return Action('announce_verification', 'guava', lambda: _announce_mismatch(call, bridge, state))


def _announce_mismatch(call, bridge, state):
    from ..coordinator import CALL_ENDING_RULE

    logger.info('Policy completion mismatch')
    endings.start(call, bridge, state, 'verification_failed', 'policy-verification-mismatch', say=[FAILED_LINE],
        objective='Offer nothing further about this policy. Do not reveal any stored detail or which detail failed. '
        'Do not claim to transfer or escalate the caller, or to create or submit a claim. ' + CALL_ENDING_RULE)
