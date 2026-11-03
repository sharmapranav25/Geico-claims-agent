"""Every outcome waits for acknowledgement and hangs up once through code."""

from unittest.mock import Mock

import pytest

from agent import checklist, coordinator
from agent.action_loop import advance
from agent.call_state import CallState
from agent.workflows import assistance, claim_intake, endings, identity, submit_claim
from test_api_outage import FakeBridge


def start_outcome(name, call, state, bridge):
    if name == 'filed':
        submit_claim._announce_claim(call, bridge, state, 'CLM-A1')
    elif name == 'missing_details':
        claim_intake._announce_missing(call, bridge, state, ['location'])
    elif name == 'verification_failed':
        identity._announce_mismatch(call, bridge, state)
    elif name == 'unsupported':
        state.verified_profile = object()
        coordinator.next_request_action(call, state, bridge).run()
    else:
        handler = {
            'declined': submit_claim._announce_not_submitted,
            'status_unknown': submit_claim._announce_status_unknown,
            'save_failed': claim_intake._announce_failed,
            'submit_failed': submit_claim._announce_submit_failed,
            'callback_saved': assistance._announce_callback_saved,
            'callback_failed': assistance._announce_callback_failed,
            'assistance_closed': assistance._close_assistance,
        }[name]
        handler(call, bridge, state)


@pytest.mark.parametrize('name', endings.FINAL_LINES)
@pytest.mark.parametrize('answers', [('done',), ('more', 'done'), ('more', 'more')])
def test_each_ending_hangs_up_exactly_once(name, answers):
    call, state, bridge = {}, CallState('ending'), FakeBridge()
    coordinator.register_completions(object(), bridge, lambda call: state)
    start_outcome(name, call, state, bridge)
    assert state.ending == name
    task = bridge.tasks[-1]
    assert task.task_id == endings.ENDING_TASKS[name]
    assert task.task_id in coordinator.COMPLETION_TASKS
    assert coordinator.CALL_ENDING_RULE in task.objective
    field, = task.fields
    assert field.key == 'ending_ack' and field.required
    assert field.field_type == 'multiple_choice' and field.choices == ('done', 'more')
    assert field.question == ('' if name == 'filed' else 'Is there anything else you need?')
    assert task.completion_criteria == 'Complete as soon as the answer is recorded.'
    routes = checklist.Routes(Mock(), Mock())
    advance(state, lambda state: checklist.evaluate(call, state, bridge, routes))
    bridge.hang_up.assert_not_called()
    for answer in answers:
        task_id = bridge.tasks[-1].task_id
        call['ending_ack'] = answer
        bridge.completions[task_id](call)
        bridge.completions[task_id](call)
        if answer == 'more' and task_id != endings.MORE_TASK_ID:
            bridge.hang_up.assert_not_called()
            assert bridge.tasks[-1].task_id == endings.MORE_TASK_ID
            assert 'This line can only open new claims for policyholders.' in bridge.tasks[-1].objective
    bridge.hang_up.assert_called_once_with(call, endings.FINAL_LINES[name])
    assert len(bridge.tasks) == len(answers)
    routes.on_verified.assert_not_called()
    routes.on_unavailable.assert_not_called()


def test_endings_have_one_goodbye_and_no_great_day():
    for line in endings.FINAL_LINES.values():
        assert line.lower().count('goodbye') == 1
        assert line.endswith('Goodbye.')
        assert 'great day' not in line.lower()


@pytest.mark.parametrize('answer', [None, '', 'invalid'])
def test_missing_or_invalid_ack_does_not_hang_up(answer):
    state, bridge = CallState('waiting'), FakeBridge()
    endings.start({}, bridge, state, 'filed', endings.ENDING_TASKS['filed'])
    state.mark_done(endings.ENDING_TASKS['filed'])
    assert endings.next_action({'ending_ack': answer}, state, bridge) is None
    bridge.hang_up.assert_not_called()
