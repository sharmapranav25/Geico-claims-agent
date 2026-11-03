"""Outcome acknowledgement and one code-owned hangup per call."""

from ..action_loop import Action
from ..bridge import FieldDefinition

FINAL_LINES = {
    'filed': 'Thanks for calling GEICO, and take care. Goodbye.',
    'declined': 'Thanks for calling GEICO. Goodbye.',
    'missing_details': "I'm sorry I couldn't open the claim today. Goodbye.",
    'verification_failed': "I'm sorry I couldn't help more today. Goodbye.",
    'unsupported': "I'm sorry I couldn't help with that request. Goodbye.",
    'status_unknown': "I'm sorry I couldn't confirm the claim's status. Goodbye.",
    'save_failed': "I'm sorry I couldn't save the details today. Goodbye.",
    'submit_failed': "I'm sorry I couldn't file the claim today. Goodbye.",
    'callback_saved': 'Thanks for calling GEICO, and take care. Goodbye.',
    'callback_failed': "I'm sorry I couldn't save your callback request. Goodbye.",
    'assistance_closed': 'Thanks for calling GEICO. Goodbye.',
}
ENDING_TASKS = {
    'filed': 'claim-submitted',
    'declined': 'claim-not-submitted',
    'missing_details': 'claim-missing-details',
    'verification_failed': 'policy-verification-mismatch',
    'unsupported': 'unsupported-request',
    'status_unknown': 'claim-status-unknown',
    'save_failed': 'claim-save-failed',
    'submit_failed': 'claim-submit-failed',
    'callback_saved': 'callback-saved',
    'callback_failed': 'callback-save-failed',
    'assistance_closed': 'assistance-closed',
}
MORE_TASK_ID = 'ending-more'
TASK_IDS = (*ENDING_TASKS.values(), MORE_TASK_ID)


def _ack_field(question='Is there anything else you need?'):
    return FieldDefinition('ending_ack', 'Record done if the caller needs nothing else, or more if they need more help.',
                           field_type='multiple_choice', choices=('done', 'more'), question=question)


def start(call, bridge, state, name, task_id, say=(), objective=''):
    state.ending = name
    question = '' if any('anything else' in line.lower() for line in say) else 'Is there anything else you need?'
    objective += ' When the caller answers, record it and say nothing at all. Do not say goodbye; the application says the final line.'
    bridge.set_task(call, task_id, say=say, objective=objective,
                    fields=[_ack_field(question)],
                    completion_criteria='Complete as soon as the answer is recorded.')


def next_action(call, state, bridge):
    if state.ending is None or state.result('hang_up') is not None:
        return None
    more = state.result('ending_more') is not None
    task_id = MORE_TASK_ID if more else ENDING_TASKS[state.ending]
    if not state.done(task_id):
        return None
    answer = bridge.get_field(call, 'ending_ack')
    if more or answer == 'done':
        return Action('hang_up', 'guava', lambda: bridge.hang_up(call, FINAL_LINES[state.ending]))
    if answer == 'more':
        from ..coordinator import CALL_ENDING_RULE

        return Action('ending_more', 'guava', lambda: bridge.set_task(
            call, MORE_TASK_ID,
            objective='This line can only open new claims for policyholders. ' + CALL_ENDING_RULE,
            fields=[_ack_field()], completion_criteria='Complete as soon as the answer is recorded.'))
    return None
