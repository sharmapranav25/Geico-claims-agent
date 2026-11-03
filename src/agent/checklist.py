"""Ordered coordination checks.

To add a situation, write its workflow with TASK_IDS, add its task IDs to
coordinator.COMPLETION_TASKS, and add or extend a check. Background detection
only records signals or nudges and re-runs this checklist; it never routes directly.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .action_loop import Action
from .bridge import FieldDefinition
from .workflows import endings, assistance, claim_intake, identity, review_claim, safety, submit_claim

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


ROLE_LINE = "I can only open a claim for the policyholder on this call, but I can have a person call you back. Would you like that?"
HUMAN_LINE = "I can't transfer you to a person on this call, but I can have someone call you back. Would you like that?"


def check_danger(call, state, bridge, routes):
    if state.stopped:
        return None
    action = safety.next_action(call, state, bridge)
    if action is not None:
        return action
    return PASS if state.done(safety.TASK_ID) else None


def check_role(call, state, bridge, routes):
    from .coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    if (state.signals.get('someone_else') or state.signals.get('human')) and submit_claim.submitted_claim(state) is None and not submit_claim.submission_uncertain(state):
        if not state.claim_blocked:
            state.claim_blocked = True
            state.verified_profile = None
            state.approved_fields = None
            state.review_decision = None
            state.open_task = assistance.CALLBACK_OFFER_TASK_ID
            # Hangup is only an instruction, so a caller still asking is answered even after an ending began.
            state.ending = None
            state.clear_results('hang_up', 'ending_more')
            def offer_callback():
                # Word for word and at once: a task's own line can lose to what the model was already saying.
                bridge.read_script(call, ROLE_LINE if state.signals.get('someone_else') else HUMAN_LINE)
                bridge.set_task(call, assistance.CALLBACK_OFFER_TASK_ID,
                fields=[FieldDefinition('wants_callback', 'Whether the caller wants a callback. Giving a callback time counts as yes.',
                                        field_type='multiple_choice', choices=('yes', 'no'))],
                objective='The caller was just told the limit of this call and asked whether they want a callback. Do not repeat that, and never say you can transfer or connect them. '
                          'Record their answer. Do not collect policy or claim details or disclose protected information. '
                          'Never say a callback is noted or saved before the application confirms it. ' + KEEP_CONTEXT_RULE + CALL_ENDING_RULE)
            return Action('role_callback_offer', 'guava', offer_callback)
        if state.ending:
            return PASS
        if state.done(assistance.CALLBACK_TASK_ID):
            return assistance.next_callback_action(call, state, bridge)
        if state.done(assistance.CALLBACK_OFFER_TASK_ID):
            return assistance.next_callback_offer_action(call, state, bridge)
        return None
    from .coordinator import REQUEST_TASK_ID, REPORT_OWN_LOSS
    if state.verified_profile is None or state.result('announce_verification') is None:
        return PASS
    return PASS if state.done(REQUEST_TASK_ID) or state.fact('request_type') == REPORT_OWN_LOSS else None


def check_scope(call, state, bridge, routes):
    from .coordinator import REPORT_OWN_LOSS, REQUEST_TASK_ID, next_request_action
    # Assistance is entered from verification, collection and result checks; the latest finished step decides.
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


def check_nudges(call, state, bridge, routes):
    if state.pending_nudges:
        instruction = state.pending_nudges.pop(0)
        state.nudge_count += 1
        return Action(f'nudge_{state.nudge_count}', 'guava', lambda: bridge.send_instruction(call, instruction))
    return PASS


def check_verification(call, state, bridge, routes):
    from .coordinator import next_start_action
    if state.verified_profile is not None:
        return PASS
    if not state.done(identity.TASK_ID):
        return None if (state.completed_tasks - {safety.TASK_ID, identity.HELP_TASK_ID}) or state.result('identity_retry') is not None else next_start_action(call, state, bridge)
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
    Check('1', 'Danger', check_danger),
    Check('2', 'Intent or role', check_role),
    Check('ending', 'Acknowledge the outcome and close',
          lambda call, state, bridge, routes: endings.next_action(call, state, bridge) if state.ending else PASS),
    Check('3', 'Within supported scope', check_scope),
    Check('4', 'Conflicts and multiple incidents', check_nudges),
    Check('5', 'Required verification', check_verification),
    Check('6', 'Required information', check_required_information),
    Check('7', 'Consistent information', check_consistency),
    Check('8', 'Permission given', check_permission),
    Check('9', 'Clear successful result', check_result),
    Check('10', 'Explain the result and close', check_close),
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
