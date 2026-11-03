"""Call flow and deterministic gates: verify, confirm the request, fill the claim."""

from functools import partial

from . import checklist, supervisor
from .action_loop import Action, advance
from .bridge import CallBridge, FieldDefinition
from .call_state import InMemoryStateStore
from .workflows import assistance, claim_intake, identity, policy_lookup, review_claim, safety, submit_claim

DEFAULT_PURPOSE = "Verify the caller's GEICO policy, then help them report an auto loss."
REQUEST_TASK_ID = "confirm-request"
COMPLETION_TASKS = (*safety.TASK_IDS, REQUEST_TASK_ID, *identity.TASK_IDS, *claim_intake.TASK_IDS, *review_claim.TASK_IDS, *submit_claim.TASK_IDS, *assistance.TASK_IDS)
CAPTURES = {identity.TASK_ID: identity.capture, claim_intake.TASK_ID: claim_intake.capture,
            review_claim.REVIEW_TASK_ID: review_claim.capture}
REPORT_OWN_LOSS = "report_own_loss"
OTHER_REQUEST = "other_request"
# Shared guardrail appended to workflow objectives.
CALL_ENDING_RULE = (
    "Never end or offer to end the call unless the caller has explicitly "
    "confirmed there is nothing else they need. Do not treat silence or your "
    "own assessment that the task is finished as that confirmation. When the "
    "caller explicitly confirms they are done, acknowledge it. The application "
    "ends the call; do not independently hang up."
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
CONTINUE_TASK = "Noted. Continue your current task without restarting it or re-asking for anything already given."


def state_for(call, bridge):
    return STORE.get(bridge.get_session_id(call))


def end_call(call, bridge):
    state = state_for(call, bridge)
    with state.lock:
        state.ended = True
        for timer in state.speech_timers.values():
            timer.cancel()
        state.speech_timers.clear()
        STORE.delete(bridge.get_session_id(call))


def next_start_action(call, state, bridge):
    check = state.result('check_api')
    if check is None:
        return assistance.check_api_action()
    if state.result('start_verification') is not None or state.result('start_assistance') is not None:
        return None
    if check.ok:
        return Action('start_verification', 'guava', lambda: identity.start(call, bridge, state))
    return Action('start_assistance', 'guava', lambda: assistance.start(call, bridge))


def default_routes(bridge):
    return checklist.Routes(partial(on_verified, bridge=bridge), partial(assistance.start, bridge=bridge))


def run_checklist(call, state, bridge, routes=None):
    routes = routes or default_routes(bridge)
    with state.lock:
        if not state.ended:
            advance(state, lambda current: checklist.evaluate(call, current, bridge, routes))


def on_speech(call, role, text, utterance_id, bridge):
    state = state_for(call, bridge)
    with state.lock:
        if state.ended:
            return
        supervisor.record(state, role, text, utterance_id)
        if role == 'caller':
            supervisor.settle(call, state, bridge, text, utterance_id)
        claim_intake.capture_live(call, state, bridge)


def start_call(call, bridge: CallBridge):
    run_checklist(call, state_for(call, bridge), bridge)


def on_task_complete(call, task_id, bridge, get_state=None, routes=None):
    state = get_state(call) if get_state is not None else state_for(call, bridge)
    with state.lock:
        if state.ended or state.stopped:
            return
        if state.claim_blocked and task_id not in assistance.TASK_IDS:
            return
        if not state.done(task_id):
            state.mark_done(task_id)
            capture = CAPTURES.get(task_id)
            if capture is not None:
                capture(call, state, bridge)
            if task_id in (identity.TASK_ID, claim_intake.TASK_ID):
                state.live_keys = ()
                state.open_task = None
        run_checklist(call, state, bridge, routes)


def register_completions(agent, bridge, get_state=None, routes=None):
    for task_id in COMPLETION_TASKS:
        bridge.register_task_complete(agent, task_id, partial(
            on_task_complete, task_id=task_id, bridge=bridge, get_state=get_state, routes=routes,
        ))


def on_verified(call, profile, bridge: CallBridge) -> None:
    bridge.add_info(call, 'verified_policy', {
        'policy_number': profile.policy_number, 'policyholder': profile.name,
        'vehicles': [claim_intake.vehicle_label(vehicle) for vehicle in profile.vehicles],
    })
    state = state_for(call, bridge)
    if state.fact('request_type') == REPORT_OWN_LOSS:
        claim_intake.start(call, bridge, state, say=[identity.VERIFIED_LINE])
        return
    bridge.set_task(call,
        REQUEST_TASK_ID,
        say=[identity.VERIFIED_LINE],
        completion_criteria="Complete as soon as the request type is recorded.",
        objective=(
            f"Identity is verified for policy {profile.policy_number} in this mock "
            "demo. Record what they are calling about. If "
            "they already said, record it without asking. Use the field's question wording exactly. " + KEEP_CONTEXT_RULE + CALL_ENDING_RULE
        ),
        fields=[FieldDefinition(
            key="request_type", question="What are you calling about today?",
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
            claim_intake.start(call, bridge, state)
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


def on_intent_a(call) -> str:
    # Never restart a task here: repeating set_task discards its collected progress.
    return CONTINUE_TASK


def on_intent_b(call, bridge: CallBridge) -> str | None:
    state = state_for(call, bridge)
    if state.verified_profile is not None or state.result('start_assistance') is not None:
        return CONTINUE_TASK
    state.open_task = "intent-b-placeholder"
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


def on_question(call, question, bridge) -> str:
    state = state_for(call, bridge)
    submitted = submit_claim.submitted_claim(state)
    if submitted is not None:
        answer = f'Known facts: the claim has been submitted. Its claim number is {submitted.claim_number}.'
    elif submit_claim.submission_uncertain(state):
        answer = 'Known facts: it is not confirmed whether the claim was filed, and no claim number is available. Do not say it succeeded or failed.'
    elif state.verified_profile is not None:
        answer = ('Known facts: the claim has not been submitted. The details are still being collected and '
                  'will be read back for approval first.')
    else:
        answer = 'Known facts: the caller is not verified, so no policy or claim information can be shared.'
    return answer + ' Use only these facts. If they do not answer the question, say you do not have that information.'


def register(agent, bridge: CallBridge):
    # Connect each route once; workflows own their validation and API access.
    bridge.register_actions(agent, INTENT_CHOICES, {
        "intent_a": on_intent_a,
        "intent_b": partial(on_intent_b, bridge=bridge),
        "immediate_danger_or_other": partial(on_immediate_danger_or_other, bridge=bridge),
    })
    bridge.register_question(agent, partial(on_question, bridge=bridge))
    policy_lookup.register(agent, bridge)
    claim_intake.register(agent, bridge)
    assistance.register(agent, bridge)
    register_completions(agent, bridge)


# Deferred: real Intent B and assistance workflows, on_escalate (no handoff),
# searchable fields/on_search_query.
# Future API-backed work belongs in workflow files, not in this coordinator.
# Hangup triggers still await deterministic caller-completion confirmation.
