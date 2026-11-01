"""Offline checks for the Step 1 lifecycle handlers."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from guava import AcceptCall, SuggestedAction, Todo
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


def test_on_call_start_enqueues_routing_task(adapter):
    call = MockCall()

    adapter.on_call_start(call)

    assert len(call._command_queue) == 1
    command = call._command_queue[0]
    assert isinstance(command, SetTaskCommand)
    assert command.task_id == adapter.INTEGRATION_TASK_ID
    assert command.objective.strip()
    assert command.objective == adapter.INTEGRATION_OBJECTIVE
    assert len(command.action_items) == 1
    assert isinstance(command.action_items[0], Todo)
    assert command.action_items[0].key == "clarify-request"
    assert "clarifying question" in command.objective
    assert adapter.CALL_ENDING_RULE in command.objective


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


@pytest.mark.parametrize("result", [
    None,
    [SuggestedAction(key="intent_a", description="Own/relevant policy loss")],
    [SuggestedAction(key="intent_a"), SuggestedAction(key="intent_b")],
])
def test_action_request_preserves_classifier_result(adapter, monkeypatch, result):
    classify = Mock(return_value=result)
    monkeypatch.setattr(adapter.IntentRecognizer, "classify", classify)
    call = MockCall()
    assert adapter.agent._on_action_requested(call, "I need to report damage") is result
    classify.assert_called_once_with("I need to report damage")
    assert call._command_queue == []


def test_claim_branches_enqueue_distinct_placeholder_tasks(adapter):
    commands = []
    for key, label in (("intent_a", "Intent A"), ("intent_b", "Intent B")):
        call = MockCall()
        adapter.agent._on_action_handlers[key](call)
        assert len(call._command_queue) == 1
        command = call._command_queue[0]
        assert isinstance(command, SetTaskCommand)
        assert label + " selected" in command.objective
        assert "not implemented yet" in command.objective
        assert adapter.CALL_ENDING_RULE in command.objective
        commands.append(command)
    assert commands[0].task_id != commands[1].task_id
    assert commands[0].objective != commands[1].objective


def test_immediate_danger_or_other_sends_instruction(adapter):
    call = MockCall()
    adapter.agent._on_action_handlers["immediate_danger_or_other"](call)
    assert len(call._command_queue) == 1
    command = call._command_queue[0]
    assert isinstance(command, SendInstructionCommand)
    assert "local emergency services" in command.instruction
    assert "No escalation or transfer path is implemented" in command.instruction
    assert adapter.CALL_ENDING_RULE in command.instruction


def test_build_agent_configures_identity_and_registers_handlers(adapter, monkeypatch):
    classify = Mock(return_value=None)
    monkeypatch.setattr(adapter.IntentRecognizer, "classify", classify)
    with patch("guava.agent.Client"):
        custom = adapter.build_agent(
            name="Sam", organization="Test Organization", purpose="Route test requests", voice="test-voice",
        )
    assert (custom._name, custom._organization, custom._purpose, custom._voice) == (
        "Sam", "Test Organization", "Route test requests", "test-voice",
    )
    assert adapter.agent._name == "Alex"
    assert custom._on_call_received is adapter.on_call_received
    assert custom._on_call_start is adapter.on_call_start
    assert custom._on_caller_speech is adapter.on_caller_speech
    assert custom._on_agent_speech is adapter.on_agent_speech
    assert set(custom._on_action_handlers) == set(adapter.INTENT_CHOICES)
    assert custom._on_action_requested(MockCall(), "unclear") is None
    classify.assert_called_once_with("unclear")
    for key in custom._on_action_handlers:
        call = MockCall()
        custom._on_action_handlers[key](call)
        assert len(call._command_queue) == 1
    assert custom._on_escalate is None
    assert custom._on_question is None
    assert custom._on_task_complete_generic is None
    assert custom._on_task_complete_handlers == {}
    assert custom._search_query_handlers == {}


def test_build_agent_default_and_overridden_pronunciations(adapter):
    assert adapter.agent._tts_replacements == {"GEICO": "Guy-co"}
    with patch("guava.agent.Client"):
        custom = adapter.build_agent(pronunciations={"GEICO": "guy-koh", "ACME": "Ack-mee"})
        disabled = adapter.build_agent(pronunciations={})
    assert custom._tts_replacements == {"GEICO": "guy-koh", "ACME": "Ack-mee"}
    # The SDK normalizes an explicit empty mapping to None.
    assert disabled._tts_replacements is None
