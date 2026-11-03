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
    from agent.call_state import InMemoryStateStore
    monkeypatch.setattr(module.coordinator, 'STORE', InMemoryStateStore())
    return module


def test_bridge_session_id_matches_call(adapter):
    call = MockCall()
    assert adapter.SDK_BRIDGE.get_session_id(call) == call.id


def test_bridge_registers_question_handler(adapter):
    handler = Mock(return_value='Known facts')
    adapter.SDK_BRIDGE.register_question(adapter.agent, handler)
    assert adapter.agent._on_question is handler


def test_bridge_adds_info(adapter):
    call = Mock()
    info = {'policy_number': '1001'}
    adapter.SDK_BRIDGE.add_info(call, 'verified_policy', info)
    call.add_info.assert_called_once_with('verified_policy', info)


def test_session_end_deletes_only_its_state(adapter):
    from agent.call_state import ActionResult

    call = MockCall()
    state = adapter.coordinator.state_for(call, adapter.SDK_BRIDGE)
    state.record(ActionResult('start_verification', 'guava', True))
    other = adapter.coordinator.STORE.get('other-session')
    assert adapter.agent._on_session_end is adapter.on_session_end
    adapter.agent._on_session_end(call, object())
    fresh = adapter.coordinator.state_for(call, adapter.SDK_BRIDGE)
    assert fresh is not state
    assert fresh.results == {}
    assert adapter.coordinator.STORE.get('other-session') is other


def test_on_call_received_accepts_call(adapter):
    call = MockCall()

    action = adapter.on_call_received(call.call_info)

    assert isinstance(action, AcceptCall)


def test_on_call_start_delegates_to_coordinator(adapter, monkeypatch):
    start = Mock()
    monkeypatch.setattr(adapter.coordinator, "start_call", start)
    call = MockCall()
    adapter.on_call_start(call)
    start.assert_called_once_with(call, adapter.SDK_BRIDGE)


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
    assert adapter.agent._tts_replacements == {"GEICO": "Guy-co", "911": "nine one one"}
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
        fields=[FieldDefinition('required_name', 'Caller name', question='What is your name?', sensitive=True),
                FieldDefinition('optional_note', 'Optional note', required=False)],
        completion_criteria='Complete once recorded',
    )
    command, = call._command_queue
    assert isinstance(command, SetTaskCommand)
    assert command.task_id == 'example' and command.objective == 'Collect test values'
    todo, required, optional = command.action_items
    assert todo.item_type == 'todo' and todo.key == 'intro'
    assert (required.key, required.description, required.field_type, required.required) == (
        'required_name', 'Caller name', 'text', True,
    )
    assert required.question == 'What is your name?' and required.sensitive is True
    assert command.completion_criteria == 'Complete once recorded'
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


@pytest.mark.parametrize('number', ['1001', '10 01', '10-01', ' 1001 '])
@pytest.mark.parametrize('name,task', [
    ('Michael Johnson', 'confirm-request'),
    ('Jordan Smith', 'verify-policy'),
])
def test_policy_callbacks_normalize_through_sdk_dispatch(adapter, monkeypatch, number, name, task):
    from types import SimpleNamespace

    from guava.events import TaskCompletedEvent
    from agent import api_adapter
    from agent.workflows import policy_lookup, identity

    client = Mock()
    client.get_policy.return_value = SimpleNamespace(
        policy_number='1001', name='Michael Johnson', phone='628-268-8753',
        vehicles=(SimpleNamespace(vehicle_id='V01-1', year=2021, make='Toyota', model='Camry'),),
    )
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    call = MockCall()
    identity.start(call, adapter.SDK_BRIDGE)
    call.set_field('policy_number', number)
    call.set_field('caller_name', name)
    call.set_field('caller_phone', '628-268-8753')

    adapter.agent._dispatch_event(call, TaskCompletedEvent(task_id=identity.TASK_ID))
    call.set_field('identity_confirmed', 'yes')
    adapter.agent._dispatch_event(call, TaskCompletedEvent(task_id=identity.CONFIRM_TASK_ID))

    assert client.get_policy.call_count == 2
    assert all(args.args == ('1001',) for args in client.get_policy.call_args_list)
    assert call._command_queue[-1].task_id == task


def test_policy_validation_failure_retries_without_completion(adapter, monkeypatch, caplog):
    from guava.events import TaskCompletedEvent
    from agent import api_adapter
    from agent.workflows import policy_lookup, identity

    client = Mock()
    client.get_policy.side_effect = api_adapter.PolicyNotFoundError('missing', 404)
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    completed = Mock(wraps=adapter.agent._on_task_complete_handlers[identity.TASK_ID])
    adapter.agent._on_task_complete_handlers[identity.TASK_ID] = completed
    call = MockCall()
    identity.start(call, adapter.SDK_BRIDGE)
    call.set_field('policy_number', '9999')
    call.set_field('caller_name', 'Jordan Smith')
    call.set_field('caller_phone', '555-555-0100')
    with patch.object(call, 'retry_task', wraps=call.retry_task) as retry:
        with caplog.at_level('INFO', logger=policy_lookup.__name__):
            adapter.agent._dispatch_event(call, TaskCompletedEvent(task_id=identity.TASK_ID))
    retry.assert_called_once()
    assert "couldn't find" in retry.call_args.kwargs['reason']
    completed.assert_not_called()
    client.get_policy.assert_called_once_with('9999')
    assert len(call._command_queue) == 2
    assert 'Policy validation not found' in caplog.messages


def test_bridge_hang_up_enqueues_one_sign_off(adapter):
    call = MockCall()
    adapter.SDK_BRIDGE.hang_up(call, 'Thanks for calling GEICO. Goodbye.')
    command, = call._command_queue
    assert isinstance(command, SendInstructionCommand)
    assert 'Say exactly: "Thanks for calling GEICO. Goodbye."' in command.instruction


def test_bridge_hang_up_speaks_the_given_final_line(adapter):
    call = Mock()
    adapter.SDK_BRIDGE.hang_up(call, 'Please call 911 now.')
    call.hangup.assert_called_once_with(final_instructions='Say exactly: "Please call 911 now."')
