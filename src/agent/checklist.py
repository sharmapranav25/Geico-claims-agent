"""Ordered coordination checks.

To add a situation, write its workflow with TASK_IDS, add its task IDs to
coordinator.COMPLETION_TASKS, and add or extend a check.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .workflows import endings, assistance, claim_intake, identity, review_claim, submit_claim

logger = logging.getLogger(__name__)
PASS = object()   # The check is satisfied; continue to the next one.


@dataclass(frozen=True)
class Routes:
    on_verified: Callable
    on_unavailable: Callable


@dataclass(frozen=True)
class Check:
    number: str
    title: str
    run: Callable


def check_safety(call, state, bridge, routes):
    # Built in Prompt 3.
    return PASS


def check_communication(call, state, bridge, routes):
    # Built in Prompt 3.
    return PASS


def check_one_task(call, state, bridge, routes):
    # Built in Prompt 3.
    return PASS


def check_intent_and_role(call, state, bridge, routes):
    from .coordinator import REQUEST_TASK_ID, REPORT_OWN_LOSS
    if state.verified_profile is None or state.result('announce_verification') is None:
        return PASS
    return PASS if state.done(REQUEST_TASK_ID) or state.fact('request_type') == REPORT_OWN_LOSS else None


def check_scope(call, state, bridge, routes):
    from .coordinator import REPORT_OWN_LOSS, REQUEST_TASK_ID, next_request_action
    # Assistance is entered from checks 6, 7 and 10; the latest finished step decides.
    if state.done(assistance.CALLBACK_TASK_ID):
        return assistance.next_callback_action(call, state, bridge)
    if state.done(assistance.CALLBACK_OFFER_TASK_ID):
        return assistance.next_callback_offer_action(call, state, bridge)
    if state.done(assistance.OFFER_TASK_ID):
        return assistance.next_offer_action(call, state, bridge)
    if not state.done(REQUEST_TASK_ID):
        return PASS
    action = next_request_action(call, state, bridge)
    if action is not None:
        return action
    return PASS if bridge.get_field(call, 'request_type') == REPORT_OWN_LOSS else None


def check_verification(call, state, bridge, routes):
    from .coordinator import next_start_action
    if state.verified_profile is not None:
        return PASS
    if not state.done(identity.TASK_ID):
        return None if state.completed_tasks or state.result('identity_retry') is not None else next_start_action(call, state, bridge)
    return identity.next_action(call, state, bridge, routes.on_verified, routes.on_unavailable)


def check_required_information(call, state, bridge, routes):
    if state.done(review_claim.REVIEW_TASK_ID) or state.result('start_review') is not None:
        return PASS
    if not state.done(claim_intake.TASK_ID):
        return None
    action = claim_intake.next_action(call, state, bridge, routes.on_unavailable)
    return action


def check_consistency(call, state, bridge, routes):
    return PASS if state.done(review_claim.REVIEW_TASK_ID) else None


def check_permission(call, state, bridge, routes):
    if state.result('submit_claim') is not None:
        return PASS
    return submit_claim.next_submit_action(call, state, bridge)


def check_result(call, state, bridge, routes):
    action = submit_claim.next_submit_action(call, state, bridge)
    if action is not None:
        return action
    return PASS if submit_claim.submitted_claim(state) is not None else None


def check_close(call, state, bridge, routes):
    if not state.done(submit_claim.SUBMITTED_TASK_ID):
        return None
    return submit_claim.next_close_action(call, state, bridge)


CHECKS = (
    Check('ending', 'Acknowledge the outcome and close',
          lambda call, state, bridge, routes: endings.next_action(call, state, bridge) if state.ending else PASS),
    Check('1', 'No immediate safety concern', check_safety),
    Check('2', 'Clear communication', check_communication),
    Check('3', 'Clear intent and role', check_intent_and_role),
    Check('4', 'One task handled at a time', check_one_task),
    Check('5', 'Within supported scope', check_scope),
    Check('6', 'Required verification confirmed', check_verification),
    Check('7', 'Required information available', check_required_information),
    Check('8', 'Consistent information', check_consistency),
    Check('9', 'Permission given', check_permission),
    Check('10', 'Clear successful result', check_result),
    Check('11', 'Explain the result and close', check_close),
)


def evaluate(call, state, bridge, routes):
    # Returns one Action, or None while waiting. Never runs anything itself.
    for check in CHECKS:
        outcome = check.run(call, state, bridge, routes)
        if outcome is PASS:
            continue
        if outcome is not None:
            logger.info('Check %s %s selected %s', check.number, check.title, outcome.name)
        return outcome
    return None
