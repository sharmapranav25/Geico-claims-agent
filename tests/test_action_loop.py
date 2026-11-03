"""Offline checks for one adapter action per evaluation and isolated call state."""

import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent import api_adapter, checklist, coordinator
from agent.action_loop import Action, MAX_ACTIONS, advance
from agent.call_state import ActionResult, CallState, InMemoryStateStore
from supervisor_helpers import safe_generate, after_safety, closeout_done
from agent.workflows import assistance, claim_intake, policy_lookup, identity

PROFILE = SimpleNamespace(
    policy_number='1001', name='Michael Johnson', phone='628-268-8753',
    vehicles=(SimpleNamespace(vehicle_id='V01-1', year=2021, make='Toyota', model='Camry'),),
)


class FakeBridge:
    generate = staticmethod(safe_generate)

    def __init__(self):
        self.set_task = Mock()
        self.send_instruction = Mock()
        self.add_info = Mock()
        self.validators = {}
        self.completions = {}

    def get_field(self, call, key):
        return call.get(key)

    def get_session_id(self, call):
        return call['session_id']

    def register_validator(self, agent, key, handler):
        self.validators[key] = handler

    def register_task_complete(self, agent, task_id, handler):
        self.completions[task_id] = handler


@pytest.fixture
def call():
    return {
        'session_id': 'session-one', 'policy_number': '10 01',
        'caller_name': 'Michael Johnson', 'caller_phone': '628-268-8753',
        'vehicle': '2021 Toyota Camry', 'occurred_at': '2026-11-01T15:00:00-08:00',
        'location': 'Main and 5th', 'description': 'Rear-ended at a light.',
        'loss_type': 'collision', 'request_type': coordinator.REPORT_OWN_LOSS,
    }


@pytest.fixture
def api(monkeypatch):
    client = Mock()
    client.get_policy.return_value = PROFILE
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    return client, factory


@pytest.mark.parametrize('stage', ['new', 'verified', 'mismatch', 'failed', 'announced'])
def test_policy_evaluation_sends_no_commands_or_api_calls(api, call, stage):
    client, factory = api
    bridge = FakeBridge()
    state = CallState(call['session_id'], facts=tuple(call.items()))
    state.record(ActionResult('start_confirm', 'guava', True))
    state.mark_done(identity.CONFIRM_TASK_ID)
    call['identity_confirmed'] = 'yes'
    verified = Mock()
    if stage != 'new':
        state.record(ActionResult('fetch_policy', 'api', stage != 'failed', PROFILE))
    if stage == 'mismatch':
        call['caller_name'] = 'Jordan Smith'
        state.set_fact('caller_name', 'Jordan Smith')
        state.identity_retries = identity.MAX_IDENTITY_RETRIES
    if stage == 'announced':
        state.record(ActionResult('announce_verification', 'guava', True))

    action = identity.next_action(call, state, bridge, verified, Mock())

    if stage == 'announced':
        assert action is None
    else:
        assert isinstance(action, Action)
        assert action.name == ('fetch_policy' if stage == 'new' else 'announce_verification')
    if stage == 'mismatch':
        assert state.verified_profile is None
    factory.assert_not_called()
    assert client.mock_calls == []
    bridge.set_task.assert_not_called()
    bridge.send_instruction.assert_not_called()
    bridge.add_info.assert_not_called()
    verified.assert_not_called()


def test_verified_path_records_fetch_before_announcement(api, call, caplog):
    client, _ = api
    bridge = FakeBridge()
    state = CallState(call['session_id'], facts=tuple(call.items()))
    state.record(ActionResult('start_confirm', 'guava', True))
    state.mark_done(identity.CONFIRM_TASK_ID)
    call['identity_confirmed'] = 'yes'

    def announce(*args, **kwargs):
        assert state.result('fetch_policy') == ActionResult('fetch_policy', 'api', True, PROFILE)
        assert state.result('announce_verification') is None

    bridge.set_task.side_effect = announce
    evaluate = Mock(side_effect=lambda current: identity.next_action(
        call, current, bridge, partial(coordinator.on_verified, bridge=bridge), Mock(),
    ))
    with caplog.at_level('INFO', logger='agent.action_loop'):
        advance(state, evaluate)

    assert list(state.results) == ['start_confirm', 'fetch_policy', 'announce_verification']
    assert state.result('announce_verification').target == 'guava'
    assert state.result('announce_verification').ok is True
    assert state.verified_profile is PROFILE
    assert evaluate.call_count == 3
    client.get_policy.assert_called_once_with('1001')
    bridge.set_task.assert_called_once()
    assert [record.getMessage() for record in caplog.records if record.name == 'agent.action_loop'] == [
        'Action fetch_policy target=api ok=True',
        'Action announce_verification target=guava ok=True',
    ]


def test_two_pending_conditions_wait_for_separate_evaluations():
    state = CallState('session')
    snapshots = []
    runs = []

    def evaluate(current):
        snapshots.append(tuple(current.results))
        for name, target in [('first', 'api'), ('second', 'guava')]:
            if current.result(name) is None:
                return Action(name, target, lambda name=name: runs.append(name))
        return None

    advance(state, evaluate)
    assert snapshots == [(), ('first',), ('first', 'second')]
    assert runs == ['first', 'second']


def test_policy_actions_call_only_their_target(api, call):
    client, factory = api
    bridge = FakeBridge()
    state = CallState(call['session_id'], facts=tuple(call.items()))
    state.record(ActionResult('start_confirm', 'guava', True))
    state.mark_done(identity.CONFIRM_TASK_ID)
    call['identity_confirmed'] = 'yes'
    verified = partial(coordinator.on_verified, bridge=bridge)
    fetch = identity.next_action(call, state, bridge, verified, Mock())
    assert fetch.target == 'api'
    profile = fetch.run()
    bridge.set_task.assert_not_called()
    bridge.send_instruction.assert_not_called()
    bridge.add_info.assert_not_called()
    state.record(ActionResult(fetch.name, fetch.target, True, profile))
    client.reset_mock()
    factory.reset_mock()

    announce = identity.next_action(call, state, bridge, verified, Mock())
    assert announce.target == 'guava'
    announce.run()
    bridge.set_task.assert_called_once()
    factory.assert_not_called()
    assert client.mock_calls == []


def test_failed_fetch_records_exception_then_announces(api, call, caplog):
    client, _ = api
    error = api_adapter.APIUnavailableError('private transport detail')
    client.get_policy.side_effect = error
    bridge = FakeBridge()
    state = CallState(call['session_id'], facts=tuple(call.items()))
    state.record(ActionResult('start_confirm', 'guava', True))
    state.mark_done(identity.CONFIRM_TASK_ID)
    call['identity_confirmed'] = 'yes'

    def announce(*args, **kwargs):
        assert state.result('fetch_policy').ok is False
        assert state.result('fetch_policy').data is error

    on_unavailable = Mock(side_effect=announce)
    with caplog.at_level('INFO', logger='agent.action_loop'):
        advance(state, lambda current: identity.next_action(call, current, bridge, Mock(), on_unavailable))
    assert list(state.results) == ['start_confirm', 'fetch_policy', 'announce_verification']
    on_unavailable.assert_called_once_with(call)
    bridge.set_task.assert_not_called()
    assert state.verified_profile is None
    assert 'private transport detail' not in caplog.text
    assert 'Action fetch_policy target=api ok=False' in caplog.messages


def test_unexpected_exception_propagates():
    state = CallState('session')
    error = TypeError('programming error')
    run = Mock(side_effect=error)
    with pytest.raises(TypeError) as raised:
        advance(state, lambda current: Action('fetch', 'api', run, (ValueError,)))
    assert raised.value is error
    assert state.results == {}
    run.assert_called_once_with()


def test_repeated_action_name_raises_without_running_twice():
    state = CallState('session')
    run = Mock()
    with pytest.raises(RuntimeError, match='already recorded'):
        advance(state, lambda current: Action('repeated', 'api', run))
    run.assert_called_once_with()


def test_action_limit_bounds_nonterminating_evaluator():
    state = CallState('session')
    run = Mock()
    evaluate = Mock(side_effect=lambda current: Action(str(len(current.results)), 'api', run))
    with pytest.raises(RuntimeError, match='limit reached'):
        advance(state, evaluate)
    assert run.call_count == evaluate.call_count == MAX_ACTIONS
    assert len(state.results) == MAX_ACTIONS


def test_store_isolates_sessions_and_deletes_state():
    store = InMemoryStateStore()
    first = store.get('one')
    second = store.get('two')
    assert first is store.get('one')
    assert first is not second
    assert first.approved_fields is first.submission_reference is None
    first.record(ActionResult('fetch', 'api', True, PROFILE))
    assert second.result('fetch') is None
    with ThreadPoolExecutor(max_workers=4) as pool:
        states = list(pool.map(store.get, ['shared'] * 20))
    assert all(state is states[0] for state in states)
    store.delete('one')
    assert store.get('one') is not first
    assert store.get('one').results == {}
    assert store.get('two') is second
    store.delete('unknown')


def test_unverified_claim_has_no_action(api, call):
    client, factory = api
    bridge = FakeBridge()
    assert claim_intake.next_action(call, CallState('session'), bridge, Mock()) is None
    factory.assert_not_called()
    assert client.mock_calls == []
    bridge.set_task.assert_not_called()
    bridge.send_instruction.assert_not_called()
    bridge.add_info.assert_not_called()


@pytest.mark.parametrize('error,task', [
    (None, 'review-claim'),
    (api_adapter.APIUnavailableError('offline'), None),
    (ValueError('invalid update'), 'claim-save-failed'),
])
def test_claim_save_records_result_before_announcement(api, call, error, task):
    client, factory = api
    client.update_claim_draft.side_effect = error
    bridge = FakeBridge()
    state = CallState('session', verified_profile=PROFILE, facts=tuple(call.items()))
    closeout_done(state)
    on_unavailable = Mock()
    action = claim_intake.next_action(call, state, bridge, on_unavailable)
    assert (action.name, action.target) == ('save_draft', 'api')
    factory.assert_not_called()
    assert client.mock_calls == []
    bridge.set_task.assert_not_called()

    def save(*args):
        bridge.set_task.assert_not_called()
        bridge.send_instruction.assert_not_called()
        bridge.add_info.assert_not_called()
        if error is not None:
            raise error
        return 'saved draft'

    def announce(*args, **kwargs):
        saved = state.result('save_draft')
        assert saved.target == 'api'
        assert saved.ok is (error is None)
        assert saved.data == ('saved draft' if error is None else error)
        assert state.result('start_review' if error is None else 'announce_save') is None

    client.update_claim_draft.side_effect = save
    bridge.set_task.side_effect = announce
    on_unavailable.side_effect = announce
    advance(state, lambda current: claim_intake.next_action(call, current, bridge, on_unavailable))
    announcement = 'start_review' if error is None else 'announce_save'
    assert list(state.results) == ['check_details', 'write_summary', 'final_consistency', 'save_draft', announcement]
    assert state.result(announcement).target == 'guava'
    if isinstance(error, api_adapter.APIUnavailableError):
        on_unavailable.assert_called_once_with(call)
        bridge.set_task.assert_not_called()
    else:
        assert bridge.set_task.call_args.args[1] == task
        on_unavailable.assert_not_called()
    client.update_claim_draft.assert_called_once()
    factory.assert_called_once()
    client.submit_claim_draft.assert_not_called()


@pytest.mark.parametrize('workflow', ['policy', 'claim'])
def test_completing_twice_does_not_repeat_actions(api, call, workflow):
    client, _ = api
    bridge = FakeBridge()
    state = after_safety(CallState('session'))
    if workflow == 'policy':
        policy_lookup.register(object(), bridge)
        coordinator.register_completions(object(), bridge, lambda call: state, checklist.Routes(
            partial(coordinator.on_verified, bridge=bridge), Mock(),
        ))
        handler = bridge.completions[identity.TASK_ID]
    else:
        closeout_done(state)
        state.verified_profile = PROFILE
        claim_intake.register(object(), bridge)
        coordinator.register_completions(object(), bridge, lambda call: state, checklist.Routes(Mock(), Mock()))
        handler = bridge.completions[claim_intake.TASK_ID]
    handler(call)
    handler(call)
    assert len(state.results) == (1 if workflow == 'policy' else 5)
    assert client.get_policy.call_count == 0
    assert client.update_claim_draft.call_count == (workflow == 'claim')
    bridge.set_task.assert_called_once()


def test_start_call_records_verification_once(call, monkeypatch):
    monkeypatch.setattr(assistance, 'check_api', Mock(return_value=[]))
    monkeypatch.setattr(coordinator, 'STORE', InMemoryStateStore())
    bridge = FakeBridge()
    after_safety(coordinator.state_for(call, bridge))
    coordinator.start_call(call, bridge)
    state = after_safety(coordinator.state_for(call, bridge))
    assert state.result('start_verification') == ActionResult('start_verification', 'guava', True)
    assert coordinator.next_start_action(call, state, bridge) is None
    after_safety(coordinator.state_for(call, bridge))
    coordinator.start_call(call, bridge)
    bridge.set_task.assert_called_once()
    assert bridge.set_task.call_args.args[1] == identity.TASK_ID


@pytest.mark.parametrize('request_type', [coordinator.REPORT_OWN_LOSS, coordinator.OTHER_REQUEST])
def test_request_action_is_gated_and_runs_once(api, call, monkeypatch, request_type):
    client, factory = api
    monkeypatch.setattr(coordinator, 'STORE', InMemoryStateStore())
    bridge = FakeBridge()
    state = after_safety(coordinator.state_for(call, bridge))
    assert coordinator.next_request_action(call, state, bridge) is None
    state.verified_profile = PROFILE
    call['request_type'] = request_type
    action = coordinator.next_request_action(call, state, bridge)
    assert (action.name, action.target) == ('start_request', 'guava')
    bridge.set_task.assert_not_called()
    coordinator.on_task_complete(call, coordinator.REQUEST_TASK_ID, bridge)
    coordinator.on_task_complete(call, coordinator.REQUEST_TASK_ID, bridge)
    assert state.result('start_request').ok is True
    bridge.set_task.assert_called_once()
    factory.assert_not_called()
    assert client.mock_calls == []
