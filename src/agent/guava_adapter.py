"""Minimal Guava integration for phone and local call smoke tests."""

import argparse
import logging
import os

from dotenv import load_dotenv
import guava
from guava import logging_utils
from guava.events import AgentSpeechEvent, CallerSpeechEvent
from guava.helpers.llm import IntentRecognizer

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_PURPOSE = "Identify the caller's auto loss reporting request and route it appropriately."
INTEGRATION_TASK_ID = "request-routing"
CALL_ENDING_RULE = (
    "Never end or offer to end the call unless the caller has explicitly "
    "confirmed there is nothing else they need. Do not treat silence or your "
    "own assessment that the task is finished as that confirmation. When the "
    "caller explicitly confirms they are done, acknowledge it. The application "
    "should then end the call through end_call_success if the task was completed "
    "or end_call_unresolved otherwise. These helpers are not wired into this "
    "placeholder task yet; do not independently hang up."
)
INTENT_CHOICES = {
    "intent_a": (
        "A loss reported under the policy covering the person or vehicle concerned. "
        "The caller may be the policyholder or reporting on their behalf. Another "
        "driver's involvement does not by itself make this Intent B."
    ),
    "intent_b": (
        "Damage allegedly caused by a GEICO-insured person or vehicle, reported by "
        "the affected person or their representative. The caller may also have "
        "their own GEICO policy; that alone does not make this Intent A."
    ),
    "immediate_danger_or_other": (
        "An immediate danger or urgent safety need, or a request for a different "
        "service rather than reporting an auto loss, such as roadside assistance "
        "or existing-claim servicing. Immediate danger takes priority."
    ),
}
INTEGRATION_OBJECTIVE = (
    "Greet the caller and identify their request and any immediate safety needs. "
    "Your first job is routing: immediate danger or another service; Intent A, "
    "a loss under the policy covering the person or vehicle concerned; or Intent B, "
    "damage allegedly caused by a GEICO-insured party. Establish whose loss is "
    "being reported and which policy or insured party it concerns. Do not infer "
    "the branch solely from 'with', 'against', or the caller having GEICO insurance. "
    "Use the available actions once the intended report is clear. If no action "
    "matches or the request is ambiguous, ask a short clarifying question, such as "
    "whose loss they are reporting and which policy or insured party it concerns. "
    "Do not guess a branch. Recognizing a branch does not authorize claims intake. "
    "The real workflows are not implemented yet; do not collect claim details "
    "or claim to look up, save, submit, transfer, or send anything. "
    + CALL_ENDING_RULE
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


def on_call_received(call_info: guava.CallInfo) -> guava.IncomingCallAction:
    logger.info("Call accepted")
    return guava.AcceptCall()


def on_call_start(call: guava.Call) -> None:
    call.set_task(
        INTEGRATION_TASK_ID,
        objective=INTEGRATION_OBJECTIVE,
        checklist=[guava.Todo(
            "Clarify the intended request and choose the appropriate routing action; "
            "prioritize immediate danger and ask for clarification when unclear.",
            key="clarify-request",
        )],
    )
    logger.info("Call started; task_id=%s", INTEGRATION_TASK_ID)


def on_caller_speech(call: guava.Call, event: CallerSpeechEvent) -> None:
    logger.info("Caller utterance: %s", event.utterance)


def on_agent_speech(call: guava.Call, event: AgentSpeechEvent) -> None:
    logger.info("Agent utterance: %s", event.utterance)


def build_agent(
    name: str = "Alex",
    organization: str = "GEICO",
    purpose: str = DEFAULT_PURPOSE,
    voice: str | None = None,
    pronunciations: dict[str, str] | None = None,
) -> guava.Agent:
    """Build an independently configured agent with routing-only handlers."""
    agent = guava.Agent(
        name=name, organization=organization, purpose=purpose, voice=voice,
        pronunciations={"GEICO": "Guy-co"} if pronunciations is None else pronunciations,
    )
    intent_recognizer = IntentRecognizer(dict(INTENT_CHOICES))
    agent.on_call_received(on_call_received)
    agent.on_call_start(on_call_start)
    agent.on_caller_speech(on_caller_speech)
    agent.on_agent_speech(on_agent_speech)

    @agent.on_action_request
    def on_action_request(call: guava.Call, request: str):
        return intent_recognizer.classify(request)

    @agent.on_action("intent_a")
    def on_intent_a(call: guava.Call) -> None:
        call.set_task(
            "intent-a-placeholder",
            objective=(
                "Intent A selected: a loss under the policy covering the person "
                "or vehicle concerned. Acknowledge the request and explain that "
                "the real Intent A workflow is not implemented yet. This is an "
                "early routing integration; do not collect claim details or "
                "claim to verify, look up, save, or submit anything. "
                + CALL_ENDING_RULE
            ),
        )

    @agent.on_action("intent_b")
    def on_intent_b(call: guava.Call) -> None:
        call.set_task(
            "intent-b-placeholder",
            objective=(
                "Intent B selected: damage allegedly caused by a GEICO-insured "
                "party. Acknowledge the request and explain that the real Intent B "
                "workflow is not implemented yet. This is an early routing "
                "integration; do not collect claim details, decide fault or "
                "coverage, or claim to save, submit, or send an evidence link. "
                + CALL_ENDING_RULE
            ),
        )

    @agent.on_action("immediate_danger_or_other")
    def on_immediate_danger_or_other(call: guava.Call) -> None:
        call.send_instruction(
            "Immediate danger or other service selected. Acknowledge the caller's "
            "request. If there is immediate danger, guide them to local emergency "
            "services. For another service, clarify what help they need and guide "
            "them to the appropriate service without inventing contact details. "
            "No escalation or transfer path is implemented; do not claim to "
            "dispatch help, transfer the call, or arrange a callback. "
            + CALL_ENDING_RULE
        )

    # Deferred: on_escalate needs a real escalation path; on_question needs a
    # knowledge source; on_task_complete needs a real multi-step workflow.
    # on_search_query / searchable Field and API adapter calls must wait for
    # the coordinator to mediate API access. Hangup helpers remain unwired
    # until coordinator state can enforce the caller's completion confirmation.
    return agent


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
