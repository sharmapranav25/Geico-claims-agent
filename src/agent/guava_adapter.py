"""Guava SDK plumbing and phone/local CLI entrypoint."""

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import guava
from guava import logging_utils
from guava.events import AgentSpeechEvent, CallerSpeechEvent
from guava.helpers.llm import IntentRecognizer

# Support both package imports and the existing direct-script CLI command.
if __package__:
    from . import coordinator
    from .bridge import CallBridge
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent import coordinator
    from agent.bridge import CallBridge

load_dotenv()

logger = logging.getLogger(__name__)

class SDKBridge(CallBridge):
    """Generic SDK operations used by SDK-free dispatch code."""

    @staticmethod
    def set_task(call, task_id, objective, todos=(), *, fields=()):
        call.set_task(
            task_id, objective=objective,
            checklist=[guava.Todo(description, key=key) for key, description in todos] + [
                guava.Field(key=field.key, description=field.description,
                            field_type=field.field_type, required=field.required)
                for field in fields
            ],
        )

    @staticmethod
    def send_instruction(call, instruction):
        call.send_instruction(instruction)

    @staticmethod
    def get_field(call, key):
        return call.get_field(key)

    @staticmethod
    def register_validator(agent, key, handler):
        agent.on_validate(key)(handler)

    @staticmethod
    def register_task_complete(agent, task_id, handler):
        agent.on_task_complete(task_id)(handler)

    @staticmethod
    def register_actions(agent, choices, handlers):
        # The coordinator supplies choices; this bridge handles SDK registration.
        recognizer = IntentRecognizer(dict(choices))

        @agent.on_action_request
        def on_action_request(call, request):
            # Preserve None and multiple matches so the dialogue can clarify.
            return recognizer.classify(request)

        for key, handler in handlers.items():
            agent.on_action(key)(handler)


SDK_BRIDGE = SDKBridge()


def end_call_success(call: guava.Call) -> None:
    """End a completed task after the caller has confirmed they are done."""
    call.hangup(
        final_instructions='End on a positive note. Say exactly: "I hope you have a great day."'
    )


def end_call_unresolved(call: guava.Call, reason: str = "") -> None:
    """End an unresolved task after the caller has confirmed they are done."""
    instructions = (
        'Apologize and end the call. Say exactly: '
        '"I\'m sorry I could not help you, but I need to hang up now."'
    )
    if reason:
        instructions += f" Context only (do not change the spoken apology): {reason}"
    call.hangup(final_instructions=instructions)


def on_call_received(call_info: guava.CallInfo) -> guava.IncomingCallAction:
    logger.info("Call accepted")
    return guava.AcceptCall()


def on_call_start(call: guava.Call) -> None:
    coordinator.start_routing(call, SDK_BRIDGE)
    logger.info("Call started")


def on_caller_speech(call: guava.Call, event: CallerSpeechEvent) -> None:
    logger.info("Caller utterance: %s", event.utterance)


def on_agent_speech(call: guava.Call, event: AgentSpeechEvent) -> None:
    logger.info("Agent utterance: %s", event.utterance)


def build_agent(
    name: str = "Alex",
    organization: str = "GEICO",
    purpose: str = coordinator.DEFAULT_PURPOSE,
    voice: str | None = None,
    pronunciations: dict[str, str] | None = None,
) -> guava.Agent:
    """Build an independently configured agent and connect dispatch handlers."""
    agent = guava.Agent(
        name=name, organization=organization, purpose=purpose, voice=voice,
        pronunciations={"GEICO": "Guy-co"} if pronunciations is None else pronunciations,
    )
    # Attach lifecycle callbacks once for each configured agent.
    agent.on_call_received(on_call_received)
    agent.on_call_start(on_call_start)
    agent.on_caller_speech(on_caller_speech)
    agent.on_agent_speech(on_agent_speech)

    coordinator.register(agent, SDK_BRIDGE)
    return agent


# Default instance used by the phone/local CLI.
agent = build_agent()


if __name__ == "__main__":
    logging_utils.configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    channel = parser.add_mutually_exclusive_group(required=True)
    channel.add_argument("--phone", action="store_true", help="Listen for inbound calls.")
    channel.add_argument("--local", action="store_true", help="Start a local test call.")
    args = parser.parse_args()

    required = ("GUAVA_API_KEY", "GUAVA_AGENT_NUMBER") if args.phone else ("GUAVA_API_KEY",)
    for variable in required:
        if not os.environ.get(variable, "").strip():
            parser.error(f"{variable} must be set in the environment or .env")

    if args.phone:
        agent.listen_phone(os.environ["GUAVA_AGENT_NUMBER"])
    else:
        agent.call_local()
