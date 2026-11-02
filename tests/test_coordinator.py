"""Offline routing/dispatch tests using the adapter's SDK bridge."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from guava import SuggestedAction
from guava.commands import SetTaskCommand, SendInstructionCommand
from guava.testing import MockCall


@pytest.fixture
def adapter():
    spec = importlib.util.spec_from_file_location('routing_adapter_test', Path(__file__).resolve().parents[1] / 'src/agent/guava_adapter.py')
    module = importlib.util.module_from_spec(spec)
    with patch('dotenv.load_dotenv'), patch('guava.agent.Client'):
        spec.loader.exec_module(module)
    return module


def test_start_routing(adapter):
    call = MockCall()
    adapter.coordinator.start_routing(call, adapter.SDK_BRIDGE)
    command, = call._command_queue
    assert isinstance(command, SetTaskCommand)
    assert command.task_id == adapter.coordinator.INTEGRATION_TASK_ID
    assert command.objective == adapter.coordinator.INTEGRATION_OBJECTIVE
    assert command.action_items[0].key == 'clarify-request'


@pytest.mark.parametrize('result', [None, [SuggestedAction(key='intent_a')], [SuggestedAction(key='intent_a'), SuggestedAction(key='intent_b')]])
def test_classification_passes_through(adapter, monkeypatch, result):
    classify = Mock(return_value=result)
    monkeypatch.setattr(adapter.IntentRecognizer, 'classify', classify)
    assert adapter.agent._on_action_requested(MockCall(), 'report damage') is result
    classify.assert_called_once_with('report damage')


def test_intent_a_dispatches_to_workflow(adapter, monkeypatch):
    start = Mock()
    monkeypatch.setattr(adapter.coordinator.policy_lookup, 'start', start)
    with patch('guava.agent.Client'):
        agent = adapter.build_agent()
    call = MockCall()
    agent._on_action_handlers['intent_a'](call)
    start.assert_called_once_with(call, bridge=adapter.SDK_BRIDGE)


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
