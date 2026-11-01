"""Minimal Guava integration for phone and local call smoke tests."""

import argparse
import logging
import os

from dotenv import load_dotenv
import guava
from guava import logging_utils
from guava.events import AgentSpeechEvent, CallerSpeechEvent

load_dotenv()

logger = logging.getLogger(__name__)

INTEGRATION_TASK_ID = "integration-test"
INTEGRATION_OBJECTIVE = (
    "Greet the caller and explain that this is an early voice integration test. "
    "The claims workflow is not implemented yet. Briefly acknowledge whatever "
    "the caller says without collecting claim details or claiming to perform "
    "any claims actions. "
    "Never end or offer to end the call unless the caller has explicitly "
    "confirmed there is nothing else they need. Do not treat silence or your "
    "own assessment that the task is finished as that confirmation. When the "
    "caller explicitly confirms they are done, acknowledge it. The application "
    "should then end the call through end_call_success if the task was completed "
    "or end_call_unresolved otherwise. These helpers are not wired into this "
    "placeholder task yet; do not independently hang up."
)

agent = guava.Agent(
    name="Alex",
    organization="GEICO",
    purpose="Test the voice connection for a future claims intake application.",
)


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


@agent.on_call_received
def on_call_received(call_info: guava.CallInfo) -> guava.IncomingCallAction:
    logger.info("Call accepted")
    return guava.AcceptCall()


@agent.on_call_start
def on_call_start(call: guava.Call) -> None:
    call.set_task(INTEGRATION_TASK_ID, objective=INTEGRATION_OBJECTIVE)
    logger.info("Call started; task_id=%s", INTEGRATION_TASK_ID)


@agent.on_caller_speech
def on_caller_speech(call: guava.Call, event: CallerSpeechEvent) -> None:
    logger.info("Caller utterance: %s", event.utterance)


@agent.on_agent_speech
def on_agent_speech(call: guava.Call, event: AgentSpeechEvent) -> None:
    logger.info("Agent utterance: %s", event.utterance)


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
