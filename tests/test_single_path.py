"""Decision matrix and conflict checks through registered call handlers."""

import itertools
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_api_outage import FakeBridge as BaseBridge, PROFILE, adapter, call_fields, callback_path
from agent import api_adapter, coordinator
from agent.call_state import ActionResult, CallState
from agent.workflows import assistance, claim_intake, policy_lookup


class FakeBridge(BaseBridge):
    def end_call_success(self, call):
        self.tasks.append(SimpleNamespace(task_id='end-call-success'))


COMBINATIONS = list(itertools.product(
    (True, False), (True, False), (True, False), (True, False),
    (coordinator.REPORT_OWN_LOSS, coordinator.OTHER_REQUEST), (True, False),
    ('yes', 'no'), ('yes', 'no'), ('approve', 'decline'), (True, False), ('yes', 'no'),
))


def run_call(monkeypatch, options):
    api_up, policy_found, name_ok, phone_ok, request_type, save_ok, wants_rep, wants_callback, decision, submit_ok, needs_more_help = options
    bridge = FakeBridge()
    call = call_fields() | {
        'caller_name': 'Michael Johnson' if name_ok else 'Jordan Smith',
        'caller_phone': '628-268-8753' if phone_ok else '555-555-0100',
        'request_type': request_type, 'wants_representative': wants_rep, 'wants_callback': wants_callback,
        'submission_decision': decision, 'needs_more_help': needs_more_help,
    }
    client = Mock()
    client.list_policies.side_effect = None if api_up else api_adapter.APIUnavailableError('offline')
    client.get_policy.side_effect = None if policy_found else api_adapter.PolicyNotFoundError('missing', 404)
    client.get_policy.return_value = PROFILE
    # api_up is fixed for the call; a rejected save here is a validation error.
    # Mid-call transport loss is tested separately in test_api_outage.
    client.update_claim_draft.side_effect = None if save_ok else api_adapter.ClaimValidationError('invalid', 400)
    client.submit_claim_draft.side_effect = None if submit_ok else api_adapter.ClaimValidationError('invalid', 400)
    client.submit_claim_draft.return_value = SimpleNamespace(claim_number='CLM-A1B2C3D4')
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    coordinator.register(object(), bridge)
    state = coordinator.state_for(call, bridge)
    recorded = []
    command_count = 0
    original_record = state.record

    def record(result):
        nonlocal command_count
        assert result.target in {'guava', 'api', 'local'}
        count = len(bridge.tasks) + len(bridge.instructions) + len(bridge.infos)
        if result.target in {'api', 'local'}:
            assert count == command_count
        recorded.append(result.name)
        command_count = count
        original_record(result)

    state.record = record

    def invoke(handler, added_info=0):
        before = len(bridge.tasks)
        before_info = len(bridge.infos)
        handler(call)
        assert len(bridge.tasks) == before + 1
        assert len(bridge.infos) == before_info + added_info
        count = len(recorded)
        handler(call)
        assert len(bridge.tasks) == before + 1
        assert len(bridge.infos) == before_info + added_info
        assert len(recorded) == count
        assert bridge.instructions == []
        bridge.actions['intent_a'](call)
        assert len(bridge.tasks) == before + 1
        assert len(bridge.infos) == before_info + added_info
        assert bridge.instructions == []

    bridge.actions['intent_a'](call)
    assert bridge.tasks == bridge.instructions == bridge.infos == []
    invoke(partial(coordinator.start_call, bridge=bridge))
    if not api_up:
        invoke(bridge.completions[assistance.OFFER_TASK_ID])
        if wants_rep == 'yes':
            invoke(bridge.completions[assistance.CALLBACK_OFFER_TASK_ID])
            if wants_callback == 'yes':
                assert bridge.validators['callback_time'](call, call['callback_time']) is True
                assert bridge.validators['callback_phone'](call, call['callback_phone']) is True
                invoke(bridge.completions[assistance.CALLBACK_TASK_ID])
    else:
        result = bridge.validators['policy_number'](call, call['policy_number'])
        if policy_found:
            assert result is True
            invoke(bridge.completions[policy_lookup.TASK_ID], added_info=int(name_ok and phone_ok))
            if name_ok and phone_ok:
                invoke(bridge.completions[coordinator.REQUEST_TASK_ID])
                if request_type == coordinator.REPORT_OWN_LOSS:
                    assert bridge.validators['occurred_at'](call, call['occurred_at']) is True
                    invoke(bridge.completions[claim_intake.TASK_ID])
                    if save_ok:
                        invoke(bridge.completions[claim_intake.REVIEW_TASK_ID])
                        if decision == 'approve' and submit_ok:
                            invoke(bridge.completions[claim_intake.SUBMITTED_TASK_ID])
        else:
            assert result[0] is False
    assert len(recorded) == len(set(recorded))
    assert client.submit_claim_draft.call_count <= 1
    assert not client.submit_claim_draft.called or decision == 'approve'
    return [task.task_id for task in bridge.tasks], state


@pytest.mark.parametrize('options', COMBINATIONS)
def test_every_decision_selects_one_path(monkeypatch, callback_path, options):
    api_up, found, name_ok, phone_ok, request_type, save_ok, wants_rep, wants_callback, decision, submit_ok, needs_more_help = options
    tasks, state = run_call(monkeypatch, options)
    if not api_up:
        expected = ['assistance-offer']
        if wants_rep == 'no':
            expected += ['assistance-closed']
        elif wants_callback == 'no':
            expected += ['callback-offer', 'assistance-closed']
        else:
            expected += ['callback-offer', 'collect-callback', 'callback-saved']
    else:
        expected = ['verify-policy']
        if found:
            if not (name_ok and phone_ok):
                expected += ['policy-verification-mismatch']
            elif request_type == coordinator.OTHER_REQUEST:
                expected += ['confirm-request', 'unsupported-request']
            else:
                expected += ['confirm-request', 'collect-claim', 'review-claim' if save_ok else 'claim-save-failed']
                if save_ok:
                    if decision == 'decline':
                        expected += ['claim-not-submitted']
                    elif not submit_ok:
                        expected += ['claim-submit-failed']
                    else:
                        expected += ['claim-submitted', 'end-call-success' if needs_more_help == 'no' else 'claim-more-help']
    assert tasks == expected
    assert 'collect-claim' not in tasks or (api_up and found and name_ok and phone_ok)
    assert not ('verify-policy' in tasks and 'assistance-offer' in tasks)
    if not (api_up and found and name_ok and phone_ok):
        assert state.verified_profile is None


@pytest.mark.parametrize('evaluator,stage', [
    ('start', 'new'), ('start', 'ok'), ('start', 'failed'), ('start', 'done'),
    ('policy', 'new'), ('policy', 'ok'), ('policy', 'mismatch'), ('policy', 'failed'), ('policy', 'done'),
    ('request', 'new'), ('request', 'ok'), ('request', 'done'),
    ('claim', 'unverified'), ('claim', 'new'), ('claim', 'ok'), ('claim', 'failed'), ('claim', 'done'),
    ('offer', 'yes'), ('offer', 'no'), ('offer', 'done'),
    ('callback_offer', 'yes'), ('callback_offer', 'no'), ('callback_offer', 'done'),
    ('callback', 'new'), ('callback', 'ok'), ('callback', 'failed'), ('callback', 'done'),
])
def test_evaluators_are_repeatable_without_actions(monkeypatch, callback_path, evaluator, stage):
    call = call_fields()
    bridge = FakeBridge()
    state = CallState('evaluation')
    factory = Mock()
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    write = Mock()
    monkeypatch.setattr(assistance, 'save_callback', write)
    verified, unavailable = Mock(), Mock()
    if evaluator == 'start':
        if stage != 'new':
            state.record(ActionResult('check_api', 'api', stage != 'failed'))
        if stage == 'done':
            state.record(ActionResult('start_verification', 'guava', True))
        evaluate = coordinator.next_start_action
    elif evaluator == 'policy':
        if stage != 'new':
            state.record(ActionResult('fetch_policy', 'api', stage != 'failed', PROFILE))
        if stage == 'mismatch':
            call['caller_phone'] = '555-555-0100'
        if stage == 'done':
            state.record(ActionResult('announce_verification', 'guava', True))
        evaluate = partial(policy_lookup.next_action, on_verified=verified, on_unavailable=unavailable)
    elif evaluator == 'request':
        if stage != 'new':
            state.verified_profile = PROFILE
        if stage == 'done':
            state.record(ActionResult('start_request', 'guava', True))
        evaluate = coordinator.next_request_action
    elif evaluator == 'claim':
        if stage != 'unverified':
            state.verified_profile = PROFILE
        if stage in ('ok', 'failed', 'done'):
            state.record(ActionResult('save_draft', 'api', stage != 'failed', api_adapter.APIUnavailableError('offline')))
        if stage == 'done':
            state.record(ActionResult('announce_save', 'guava', True))
        evaluate = partial(claim_intake.next_action, on_unavailable=unavailable)
    elif evaluator in ('offer', 'callback_offer'):
        call['wants_representative'] = call['wants_callback'] = stage
        if stage == 'done':
            state.record(ActionResult('answer_offer' if evaluator == 'offer' else 'answer_callback_offer', 'guava', True))
        evaluate = assistance.next_offer_action if evaluator == 'offer' else assistance.next_callback_offer_action
    else:
        if stage != 'new':
            state.record(ActionResult('save_callback', 'local', stage != 'failed'))
        if stage == 'done':
            state.record(ActionResult('announce_callback', 'guava', True))
        evaluate = assistance.next_callback_action
    first = evaluate(call, state, bridge)
    second = evaluate(call, state, bridge)
    assert (first.name if first else None) == (second.name if second else None)
    assert bridge.tasks == bridge.instructions == bridge.infos == []
    factory.assert_not_called()
    write.assert_not_called()
    verified.assert_not_called()
    unavailable.assert_not_called()


def test_two_classifier_suggestions_do_not_change_task(adapter, monkeypatch):
    from guava import SuggestedAction
    from guava.testing import MockCall

    suggestions = [SuggestedAction(key='intent_a'), SuggestedAction(key='intent_b')]
    monkeypatch.setattr(adapter.IntentRecognizer, 'classify', Mock(return_value=suggestions))
    call = MockCall()
    assert adapter.agent._on_action_requested(call, 'two requests') == suggestions
    assert call._command_queue == []


def test_failed_callback_validator_retries_once_without_completion(adapter):
    from guava.events import TaskCompletedEvent
    from guava.testing import MockCall

    call = MockCall()
    assistance._collect_callback(call, adapter.SDK_BRIDGE)
    call.set_field('callback_time', 'Saturday at 8 PM')
    call.set_field('callback_phone', '628-268-8753')
    completed = Mock(wraps=adapter.agent._on_task_complete_handlers[assistance.CALLBACK_TASK_ID])
    adapter.agent._on_task_complete_handlers[assistance.CALLBACK_TASK_ID] = completed
    from unittest.mock import patch
    with patch.object(call, 'retry_task', wraps=call.retry_task) as retry:
        adapter.agent._dispatch_event(call, TaskCompletedEvent(task_id=assistance.CALLBACK_TASK_ID))
    retry.assert_called_once()
    completed.assert_not_called()
    assert len(call._command_queue) == 2


@pytest.mark.parametrize('stage', ['verified', 'outage'])
def test_intent_b_does_not_replace_active_task(callback_path, stage):
    bridge = FakeBridge()
    call = call_fields()
    coordinator.register(object(), bridge)
    state = coordinator.state_for(call, bridge)
    if stage == 'verified':
        state.verified_profile = PROFILE
    else:
        state.record(ActionResult('start_assistance', 'guava', True))
    assert bridge.actions['intent_b'](call) == coordinator.on_intent_a(call)
    assert bridge.tasks == bridge.instructions == bridge.infos == []
