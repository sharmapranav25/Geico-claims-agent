"""Offline checks for the Step 1 lifecycle handlers."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

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


def test_on_call_start_enqueues_placeholder_task(adapter):
    call = MockCall()

    adapter.on_call_start(call)

    assert len(call._command_queue) == 1
    command = call._command_queue[0]
    assert isinstance(command, SetTaskCommand)
    assert command.task_id == adapter.INTEGRATION_TASK_ID
    assert command.objective.strip()
    assert command.objective == adapter.INTEGRATION_OBJECTIVE
    assert command.action_items == []


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
