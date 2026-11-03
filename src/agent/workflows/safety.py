"""Opening safety question and immediate-danger ending."""

from ..action_loop import Action
from ..bridge import FieldDefinition
from . import submit_claim

TASK_ID = 'safety-check'
TASK_IDS = (TASK_ID,)
QUESTION = 'Before we start, is everyone safe, and does anyone need urgent medical help?'
LINE_911 = "If anyone is in immediate danger or needs urgent medical help, please call 911 now. I can't contact emergency services for you. We can continue the claim when you're safe."


def next_action(call, state, bridge):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    if state.signals.get('danger') or bridge.get_field(call, 'safety_answer') == 'not_safe':
        state.stopped = True
        status = 'Nothing has been submitted.'
        if submit_claim.submitted_claim(state) is not None:
            status = 'Your claim has been submitted.'
        elif submit_claim.submission_uncertain(state):
            status = "I can't confirm whether your claim was filed."
        return Action('danger_hangup', 'guava', lambda: bridge.hang_up(call, LINE_911 + ' ' + status))
    if state.result('safety_question') is None and not state.done(TASK_ID):
        return Action('safety_question', 'guava', lambda: bridge.set_task(call, TASK_ID,
            objective='Greet the caller and ask the safety question using its exact wording. Ask nothing else and give no safety instructions. '
                      + KEEP_CONTEXT_RULE + CALL_ENDING_RULE,
            fields=[FieldDefinition('safety_answer',
                "Record safe if everyone is safe and nobody needs urgent help now, including a past injury or someone hurt who does not need urgent help. "
                "Record not_safe if anyone is in danger or needs urgent help now, or the caller is unsure. "
                "The question has two parts, so a bare yes or no is unclear: ask once, 'Just to be sure, is everyone safe right now?', and record from that answer.",
                field_type='multiple_choice', choices=('safe', 'not_safe'), question=QUESTION)],
            completion_criteria='Complete as soon as safety_answer is recorded.'))
    return None
