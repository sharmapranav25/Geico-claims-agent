"""Offline routing/dispatch tests using the adapter's SDK bridge."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from guava import SuggestedAction
from guava.commands import SetTaskCommand, SendInstructionCommand
from guava.testing import MockCall
from supervisor_helpers import safe_generate, after_safety


@pytest.fixture
def adapter(monkeypatch):
    spec = importlib.util.spec_from_file_location('routing_adapter_test', Path(__file__).resolve().parents[1] / 'src/agent/guava_adapter.py')
    module = importlib.util.module_from_spec(spec)
    with patch('dotenv.load_dotenv'), patch('guava.agent.Client'):
        spec.loader.exec_module(module)
    from agent.call_state import InMemoryStateStore
    monkeypatch.setattr(module.coordinator, 'STORE', InMemoryStateStore())
    monkeypatch.setattr(module.coordinator.assistance, 'check_api', Mock(return_value=[]))
    monkeypatch.setattr(module.SDK_BRIDGE, 'generate', safe_generate)
    return module


def test_call_starts_in_verification(adapter):
    from agent.workflows import policy_lookup, identity

    call = MockCall()
    after_safety(adapter.coordinator.state_for(call, adapter.SDK_BRIDGE))
    adapter.coordinator.start_call(call, adapter.SDK_BRIDGE)
    command, = call._command_queue
    assert isinstance(command, SetTaskCommand)
    assert command.task_id == identity.TASK_ID
    assert {item.key for item in command.action_items} == {field.key for field in identity.identity_fields()}
    assert adapter.coordinator.KEEP_CONTEXT_RULE in command.objective
    assert adapter.coordinator.CALL_ENDING_RULE in command.objective


@pytest.mark.parametrize('result', [None, [SuggestedAction(key='intent_a')], [SuggestedAction(key='intent_a'), SuggestedAction(key='intent_b')]])
def test_classification_passes_through(adapter, monkeypatch, result):
    classify = Mock(return_value=result)
    monkeypatch.setattr(adapter.IntentRecognizer, 'classify', classify)
    assert adapter.agent._on_action_requested(MockCall(), 'report damage') is result
    classify.assert_called_once_with('report damage')


def test_intent_a_never_restarts_a_task(adapter):
    # Repeating set_task mid-collection discarded progress and stalled live calls.
    call = MockCall()
    response = adapter.agent._on_action_handlers['intent_a'](call)
    assert call._command_queue == []
    assert 'without restarting' in response


def profile():
    vehicle = SimpleNamespace(vehicle_id='V01-1', year=2021, make='Toyota', model='Camry')
    return SimpleNamespace(policy_number='1001', name='Michael Johnson', phone='628-268-8753', vehicles=(vehicle,))


@pytest.mark.parametrize('stage,expected', [
    ('unverified', 'Known facts: the caller is not verified, so no policy or claim information can be shared.'),
    ('verified', 'Known facts: the claim has not been submitted. The details are still being collected and will be read back for approval first.'),
    ('submitted', 'Known facts: the claim has been submitted. Its claim number is C, L, M, A, 1, B, 2, C, 3, D, 4. Say the claim number character by character.'),
])
def test_question_answers_only_from_state(adapter, stage, expected):
    from agent.call_state import ActionResult

    call = MockCall()
    state = adapter.coordinator.state_for(call, adapter.SDK_BRIDGE)
    if stage != 'unverified':
        state.verified_profile = profile()
    if stage == 'submitted':
        state.record(ActionResult('submit_claim', 'api', True, SimpleNamespace(claim_number='CLM-A1B2C3D4')))
    bridge = Mock(wraps=adapter.SDK_BRIDGE)
    answer = adapter.coordinator.on_question(call, 'Has my claim been filed?', bridge)
    assert answer == expected + ' Use only these facts. If they do not answer the question, say you do not have that information.'
    assert [entry[0] for entry in bridge.mock_calls] == ['get_session_id']
    assert call._command_queue == []
    assert adapter.agent._on_question(call, 'Has my claim been filed?') == answer
    assert call._command_queue == []
    if stage == 'unverified':
        assert all(value not in answer for value in ('1001', 'Michael Johnson', '628-268-8753', 'Toyota', 'CLM-'))


def test_verified_policy_context_precedes_request_task(adapter):
    bridge = Mock()
    call = object()
    adapter.coordinator.on_verified(call, profile(), bridge)
    assert [entry[0] for entry in bridge.mock_calls] == ['add_info', 'get_session_id', 'set_task']
    bridge.add_info.assert_called_once_with(call, 'verified_policy', {
        'policy_number': '1001', 'policyholder': 'Michael Johnson', 'vehicles': ['2021 Toyota Camry'],
    })
    assert '628-268-8753' not in repr(bridge.add_info.call_args)
    assert bridge.set_task.call_args.args == (call, adapter.coordinator.REQUEST_TASK_ID)
    assert bridge.set_task.call_args.kwargs['completion_criteria'] == 'Complete as soon as the request type is recorded.'


def test_claim_cannot_start_without_verification(adapter):
    call = MockCall()
    call.set_field('request_type', adapter.coordinator.REPORT_OWN_LOSS)
    after_safety(adapter.coordinator.state_for(call, adapter.SDK_BRIDGE))
    adapter.coordinator.on_task_complete(call, adapter.coordinator.REQUEST_TASK_ID, adapter.SDK_BRIDGE)
    assert call._command_queue == []


@pytest.mark.parametrize('request_type,task', [('report_own_loss', 'collect-claim'), ('other_request', 'unsupported-request')])
def test_verified_caller_routes_on_recorded_request(adapter, request_type, task):
    call = MockCall()
    adapter.coordinator.state_for(call, adapter.SDK_BRIDGE).verified_profile = profile()
    adapter.coordinator.on_verified(call, profile(), adapter.SDK_BRIDGE)
    assert call._command_queue[-1].task_id == adapter.coordinator.REQUEST_TASK_ID
    call.set_field('request_type', request_type)
    after_safety(adapter.coordinator.state_for(call, adapter.SDK_BRIDGE))
    adapter.coordinator.on_task_complete(call, adapter.coordinator.REQUEST_TASK_ID, adapter.SDK_BRIDGE)
    assert call._command_queue[-1].task_id == task


def test_placeholder_content_unchanged(adapter):
    call = MockCall()
    adapter.agent._on_action_handlers['intent_b'](call)
    command, = call._command_queue
    assert isinstance(command, SetTaskCommand)
    assert command.task_id == 'intent-b-placeholder'
    assert command.objective == (
        'Intent B selected: damage allegedly caused by a GEICO-insured '
        'party. Acknowledge the request and explain that the real Intent B '
        'workflow is not implemented yet. This is an early routing '
        'integration; do not collect claim details, decide fault or '
        'coverage, or claim to save, submit, or send an evidence link. '
        + adapter.coordinator.CALL_ENDING_RULE
    )
    call = MockCall()
    adapter.agent._on_action_handlers['immediate_danger_or_other'](call)
    command, = call._command_queue
    assert isinstance(command, SendInstructionCommand)
    assert command.instruction == (
        "Immediate danger or other service selected. Acknowledge the caller's "
        'request. If there is immediate danger, guide them to local emergency '
        'services. For another service, clarify what help they need and guide '
        'them to the appropriate service without inventing contact details. '
        'No escalation or transfer path is implemented; do not claim to '
        'dispatch help, transfer the call, or arrange a callback. '
        + adapter.coordinator.CALL_ENDING_RULE
    )


@pytest.mark.parametrize('known', [False, True])
def test_verified_request_fact_routes_with_verified_line(adapter, known):
    from agent.workflows import identity
    from guava import Say

    call = MockCall()
    state = adapter.coordinator.state_for(call, adapter.SDK_BRIDGE)
    state.verified_profile = profile()
    if known:
        state.set_fact('request_type', adapter.coordinator.REPORT_OWN_LOSS)
    adapter.coordinator.on_verified(call, profile(), adapter.SDK_BRIDGE)
    task = call._command_queue[-1]
    assert task.task_id == ('collect-claim' if known else 'confirm-request')
    assert any(isinstance(item, Say) and item.statement == identity.VERIFIED_LINE for item in task.action_items)


@pytest.mark.parametrize('found', [False, True])
def test_question_after_uncertain_submission(adapter, found):
    from agent import api_adapter
    from agent.call_state import ActionResult

    call = MockCall()
    state = adapter.coordinator.state_for(call, adapter.SDK_BRIDGE)
    state.verified_profile = profile()
    state.record(ActionResult('submit_claim', 'api', False, api_adapter.APIUnavailableError('timeout')))
    if found:
        state.record(ActionResult('check_submission', 'api', True, SimpleNamespace(claim_number='CLM-FOUND')))
    answer = adapter.coordinator.on_question(call, 'Was it filed?', adapter.SDK_BRIDGE)
    assert 'has not been submitted' not in answer
    assert ('C, L, M, F, O, U, N, D' in answer) is found
    assert ('not confirmed whether' in answer) is (not found)
