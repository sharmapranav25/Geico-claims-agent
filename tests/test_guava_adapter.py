"""Offline checks for the Step 1 lifecycle handlers."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from guava import AcceptCall
from guava.commands import SendInstructionCommand, SetTaskCommand
from guava.testing import MockCall


@pytest.fixture
def adapter(monkeypatch):
    # Import a fresh adapter without reading credentials or constructing a live
    # client (Client.__init__ performs an HTTP deprecation-status check).
    monkeypatch.delenv("GUAVA_API_KEY", raising=False)
    monkeypatch.delenv("GUAVA_AGENT_NUMBER", raising=False)
    path = Path(__file__).resolve().parents[1] / "src/agent/guava_adapter.py"
    spec = importlib.util.spec_from_file_location("guava_adapter_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch("dotenv.load_dotenv"), patch("guava.agent.Client"):
        spec.loader.exec_module(module)
    return module


def test_on_call_received_accepts_call(adapter):
    call = MockCall()

    action = adapter.on_call_received(call.call_info)

    assert isinstance(action, AcceptCall)


def test_on_call_start_delegates_to_coordinator(adapter, monkeypatch):
    start = Mock()
    monkeypatch.setattr(adapter.coordinator, "start_routing", start)
    call = MockCall()
    adapter.on_call_start(call)
    start.assert_called_once_with(call, adapter.SDK_BRIDGE)


def test_end_call_success_enqueues_sign_off(adapter):
    call = MockCall()

    adapter.end_call_success(call)

    assert len(call._command_queue) == 1
    command = call._command_queue[0]
    assert isinstance(command, SendInstructionCommand)
    assert "I hope you have a great day." in command.instruction


@pytest.mark.parametrize("reason", ["", "The requested task is not implemented."])
def test_end_call_unresolved_enqueues_sign_off(adapter, reason):
    call = MockCall()

    adapter.end_call_unresolved(call, reason)

    assert len(call._command_queue) == 1
    command = call._command_queue[0]
    assert isinstance(command, SendInstructionCommand)
    assert "I'm sorry I could not help you, but I need to hang up now." in command.instruction
    if reason:
        assert reason in command.instruction


def test_build_agent_configures_identity_and_registers_handlers(adapter):
    with patch("guava.agent.Client"), patch.object(adapter.coordinator, "register", wraps=adapter.coordinator.register) as register:
        custom = adapter.build_agent(name="Sam", organization="Test Organization", purpose="Test purpose", voice="test-voice")
    register.assert_called_once_with(custom, adapter.SDK_BRIDGE)
    assert (custom._name, custom._organization, custom._purpose, custom._voice) == (
        "Sam", "Test Organization", "Test purpose", "test-voice",
    )
    assert custom._on_call_received is adapter.on_call_received
    assert custom._on_call_start is adapter.on_call_start
    assert custom._on_caller_speech is adapter.on_caller_speech
    assert custom._on_agent_speech is adapter.on_agent_speech
    assert set(custom._on_action_handlers) == set(adapter.coordinator.INTENT_CHOICES)


def test_build_agent_default_and_overridden_pronunciations(adapter):
    assert adapter.agent._tts_replacements == {"GEICO": "Guy-co"}
    with patch("guava.agent.Client"):
        custom = adapter.build_agent(pronunciations={"GEICO": "guy-koh", "ACME": "Ack-mee"})
        disabled = adapter.build_agent(pronunciations={})
    assert custom._tts_replacements == {"GEICO": "guy-koh", "ACME": "Ack-mee"}
    # The SDK normalizes an explicit empty mapping to None.
    assert disabled._tts_replacements is None


def test_bridge_translates_fields_and_reads_values(adapter):
    from agent.bridge import FieldDefinition

    call = MockCall()
    adapter.SDK_BRIDGE.set_task(
        call, 'example', 'Collect test values', todos=[('intro', 'Explain the task')],
        fields=[FieldDefinition('required_name', 'Caller name'),
                FieldDefinition('optional_note', 'Optional note', required=False)],
    )
    command, = call._command_queue
    assert isinstance(command, SetTaskCommand)
    assert command.task_id == 'example' and command.objective == 'Collect test values'
    todo, required, optional = command.action_items
    assert todo.item_type == 'todo' and todo.key == 'intro'
    assert (required.key, required.description, required.field_type, required.required) == (
        'required_name', 'Caller name', 'text', True,
    )
    assert optional.key == 'optional_note' and optional.required is False
    assert call._field_keys_by_task_id['example'] == ['required_name', 'optional_note']
    call.set_field('required_name', 'Alex')
    assert adapter.SDK_BRIDGE.get_field(call, 'required_name') == 'Alex'
    assert adapter.SDK_BRIDGE.get_field(call, 'missing') is None


def test_bridge_registers_validation_and_completion(adapter):
    from guava.events import TaskCompletedEvent
    from agent.bridge import FieldDefinition

    call = MockCall()
    validator = Mock(return_value=(False, 'Please repeat the name'))
    completed = Mock()
    adapter.SDK_BRIDGE.register_validator(adapter.agent, 'test_name', validator)
    adapter.SDK_BRIDGE.register_task_complete(adapter.agent, 'test-task', completed)
    adapter.SDK_BRIDGE.set_task(call, 'test-task', 'Test', fields=[FieldDefinition('test_name', 'Name')])
    call.set_field('test_name', 'Alex')
    adapter.agent._dispatch_event(call, TaskCompletedEvent(task_id='test-task'))
    validator.assert_called_once_with(call, 'Alex')
    completed.assert_not_called()
    assert len(call._command_queue) == 2  # SDK, not the workflow, retries validation.
    validator.return_value = True
    adapter.agent._dispatch_event(call, TaskCompletedEvent(task_id='test-task'))
    completed.assert_called_once_with(call)
