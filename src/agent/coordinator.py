"""Call flow and deterministic gates: verify, confirm the request, fill the claim."""

from functools import partial

from .action_loop import Action, advance
from .bridge import CallBridge, FieldDefinition
from .call_state import InMemoryStateStore
from .workflows import assistance, claim_intake, policy_lookup

DEFAULT_PURPOSE = "Verify the caller's GEICO policy, then help them report an auto loss."
REQUEST_TASK_ID = "confirm-request"
REPORT_OWN_LOSS = "report_own_loss"
OTHER_REQUEST = "other_request"
# Shared guardrail appended to workflow objectives.
CALL_ENDING_RULE = (
    "Never end or offer to end the call unless the caller has explicitly "
    "confirmed there is nothing else they need. Do not treat silence or your "
    "own assessment that the task is finished as that confirmation. When the "
    "caller explicitly confirms they are done, acknowledge it. The application "
    "should then end the call through end_call_success if the task was completed "
    "or end_call_unresolved otherwise. These helpers are not wired into this "
    "placeholder task yet; do not independently hang up."
)
# Keeps every later instruction from re-asking what the caller already gave.
KEEP_CONTEXT_RULE = (
    "Use everything the caller has already said in this call. Never re-ask for "
    "or re-confirm something they already gave unless it was unclear or the "
    "application rejected it. Never say you are looking something up or ask the "
    "caller to wait; record what you have so the application can check it. "
)
# Descriptions help the classifier distinguish whose loss is being reported.
INTENT_CHOICES = {
    "intent_a": (
        "A loss reported under the policy covering the person or vehicle concerned. "
        "The caller may be the policyholder or reporting on their behalf. Another "
        "driver's involvement does not by itself make this Intent A or B."
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

STORE = InMemoryStateStore()


def state_for(call, bridge):
    return STORE.get(bridge.get_session_id(call))


def end_call(call, bridge):
    STORE.delete(bridge.get_session_id(call))


def next_start_action(call, state, bridge):
    check = state.result('check_api')
    if check is None:
        return assistance.check_api_action()
    if state.result('start_verification') is not None or state.result('start_assistance') is not None:
        return None
    if check.ok:
        return Action('start_verification', 'guava', lambda: policy_lookup.start(call, bridge))
    return Action('start_assistance', 'guava', lambda: assistance.start(call, bridge))


def start_call(call, bridge: CallBridge):
    advance(state_for(call, bridge), lambda state: next_start_action(call, state, bridge))


def on_verified(call, profile, bridge: CallBridge) -> None:
    bridge.set_task(call,
        REQUEST_TASK_ID,
        objective=(
            f"Identity is verified for policy {profile.policy_number} in this mock "
            "demo; tell the caller. Then record what they are calling about. If "
            "they already said, record it without asking. " + KEEP_CONTEXT_RULE + CALL_ENDING_RULE
        ),
        fields=[FieldDefinition(
            key="request_type",
            description="Whether the caller is reporting a loss on their own policy, or something else.",
            field_type="multiple_choice",
            choices=(REPORT_OWN_LOSS, OTHER_REQUEST),
        )],
    )


def next_request_action(call, state, bridge):
    if state.verified_profile is None:
        return None
    if state.result('start_request') is not None:
        return None

    def start_request():
        if bridge.get_field(call, "request_type") == REPORT_OWN_LOSS:
            claim_intake.start(call, bridge, state.verified_profile)
            return
        bridge.set_task(call,
            "unsupported-request",
            objective=(
                "This demo only opens new loss reports for verified policyholders. "
                "Explain that limit, and guide them to the appropriate service without "
                "inventing contact details or claiming to transfer them. " + CALL_ENDING_RULE
            ),
        )

    return Action('start_request', 'guava', start_request)


def on_request_confirmed(call, bridge: CallBridge) -> None:
    advance(state_for(call, bridge), lambda state: next_request_action(call, state, bridge))


def on_intent_a(call) -> str:
    # Never restart a task here: repeating set_task discards its collected progress.
    return "Noted. Continue your current task without restarting it or re-asking for anything already given."


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
        "intent_a": on_intent_a,
        "intent_b": partial(on_intent_b, bridge=bridge),
        "immediate_danger_or_other": partial(on_immediate_danger_or_other, bridge=bridge),
    })
    get_state = partial(state_for, bridge=bridge)
    on_unavailable = partial(assistance.start, bridge=bridge)
    policy_lookup.register(agent, bridge, get_state, partial(on_verified, bridge=bridge), on_unavailable)
    bridge.register_task_complete(agent, REQUEST_TASK_ID, partial(on_request_confirmed, bridge=bridge))
    claim_intake.register(agent, bridge, get_state, on_unavailable)
    assistance.register(agent, bridge, get_state)


# Deferred: real Intent B and assistance workflows, on_escalate (no handoff),
# on_question (no knowledge source), and searchable fields/on_search_query.
# Future API-backed work belongs in workflow files, not in this coordinator.
# Hangup triggers still await deterministic caller-completion confirmation.
