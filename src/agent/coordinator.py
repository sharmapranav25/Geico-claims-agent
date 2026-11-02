"""Routing wording and dispatch; no SDK or API calls."""

from functools import partial

from .bridge import CallBridge
from .workflows import policy_lookup

DEFAULT_PURPOSE = "Identify the caller's auto loss reporting request and route it appropriately."
INTEGRATION_TASK_ID = "request-routing"
# Shared guardrail appended to routing and workflow objectives.
CALL_ENDING_RULE = (
    "Never end or offer to end the call unless the caller has explicitly "
    "confirmed there is nothing else they need. Do not treat silence or your "
    "own assessment that the task is finished as that confirmation. When the "
    "caller explicitly confirms they are done, acknowledge it. The application "
    "should then end the call through end_call_success if the task was completed "
    "or end_call_unresolved otherwise. These helpers are not wired into this "
    "placeholder task yet; do not independently hang up."
)
# Descriptions help the classifier distinguish whose loss is being reported.
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



def start_routing(call, bridge: CallBridge):
    # Begin request clarification without depending on SDK types.
    bridge.set_task(
        call, INTEGRATION_TASK_ID, objective=INTEGRATION_OBJECTIVE,
        todos=[("clarify-request", "Clarify the intended request and choose the appropriate routing action; "
                "prioritize immediate danger and ask for clarification when unclear.")],
    )


def on_intent_b(call, bridge: CallBridge) -> None:
    bridge.set_task(call,
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

def on_immediate_danger_or_other(call, bridge: CallBridge) -> None:
    bridge.send_instruction(call,
        "Immediate danger or other service selected. Acknowledge the caller's "
        "request. If there is immediate danger, guide them to local emergency "
        "services. For another service, clarify what help they need and guide "
        "them to the appropriate service without inventing contact details. "
        "No escalation or transfer path is implemented; do not claim to "
        "dispatch help, transfer the call, or arrange a callback. "
        + CALL_ENDING_RULE
    )



def register(agent, bridge: CallBridge):
    # Connect each route once; workflows own their validation and API access.
    bridge.register_actions(agent, INTENT_CHOICES, {
        "intent_a": partial(policy_lookup.start, bridge=bridge),
        "intent_b": partial(on_intent_b, bridge=bridge),
        "immediate_danger_or_other": partial(on_immediate_danger_or_other, bridge=bridge),
    })
    policy_lookup.register(agent, bridge)


# Deferred: real Intent B and assistance workflows, on_escalate (no handoff),
# on_question (no knowledge source), and searchable fields/on_search_query.
# Future API-backed work belongs in workflow files, not in this coordinator.
# Hangup triggers still await deterministic caller-completion confirmation.
