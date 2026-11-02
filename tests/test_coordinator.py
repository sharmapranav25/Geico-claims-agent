"""Offline routing/dispatch tests using the adapter's SDK bridge."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from guava import SuggestedAction
from guava.commands import SetTaskCommand, SendInstructionCommand
from guava.testing import MockCall


@pytest.fixture
def adapter(monkeypatch):
    spec = importlib.util.spec_from_file_location('routing_adapter_test', Path(__file__).resolve().parents[1] / 'src/agent/guava_adapter.py')
    module = importlib.util.module_from_spec(spec)
    with patch('dotenv.load_dotenv'), patch('guava.agent.Client'):
        spec.loader.exec_module(module)
    from agent.call_state import InMemoryStateStore
    monkeypatch.setattr(module.coordinator, 'STORE', InMemoryStateStore())
    monkeypatch.setattr(module.coordinator.assistance, 'check_api', Mock(return_value=[]))
    return module


def test_call_starts_in_verification(adapter):
    from agent.workflows import policy_lookup

    call = MockCall()
    adapter.coordinator.start_call(call, adapter.SDK_BRIDGE)
    command, = call._command_queue
    assert isinstance(command, SetTaskCommand)
    assert command.task_id == policy_lookup.TASK_ID
    assert {item.key for item in command.action_items} == {'policy_number', 'caller_name', 'caller_phone'}
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
    return SimpleNamespace(policy_number='GEC-0001', vehicles=(vehicle,))


def test_claim_cannot_start_without_verification(adapter):
    call = MockCall()
    call.set_field('request_type', adapter.coordinator.REPORT_OWN_LOSS)
    adapter.coordinator.on_request_confirmed(call, adapter.SDK_BRIDGE)
    assert call._command_queue == []


@pytest.mark.parametrize('request_type,task', [('report_own_loss', 'collect-claim'), ('other_request', 'unsupported-request')])
def test_verified_caller_routes_on_recorded_request(adapter, request_type, task):
    call = MockCall()
    adapter.coordinator.state_for(call, adapter.SDK_BRIDGE).verified_profile = profile()
    adapter.coordinator.on_verified(call, profile(), adapter.SDK_BRIDGE)
    assert call._command_queue[-1].task_id == adapter.coordinator.REQUEST_TASK_ID
    call.set_field('request_type', request_type)
    adapter.coordinator.on_request_confirmed(call, adapter.SDK_BRIDGE)
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
