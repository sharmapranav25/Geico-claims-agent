"""Approved submission, reference reconciliation and closing."""

import logging
from uuid import uuid4

from .. import api_adapter, verification
from ..action_loop import Action
from ..bridge import FieldDefinition
from . import claim_intake

logger = logging.getLogger(__name__)
SUBMITTED_TASK_ID = 'claim-submitted'
TASK_IDS = (SUBMITTED_TASK_ID,)
PORTAL_LINE = "You can upload supporting photos and documents later through your GEICO claims portal."


def next_submit_action(call, state, bridge):
    if state.verified_profile is None:
        return None
    submitted = state.result('submit_claim')
    if submitted is None:
        if state.result('announce_not_submitted') is not None:
            return None
        reviewed = state.result('start_review')
        fields = claim_intake.claim_facts(state)
        if not verification.approval_is_valid(state.review_decision, reviewed.data if reviewed else None, fields):
            return Action('announce_not_submitted', 'guava', lambda: _announce_not_submitted(call, bridge))
        state.approved_fields = fields
        if state.submission_reference is None:
            state.submission_reference = f'sub-{uuid4().hex}'

        def submit_claim():
            with api_adapter.APIAdapter() as api:
                return api.submit_claim_draft(state.verified_profile.policy_number,
                                              submission_reference=state.submission_reference)

        return Action('submit_claim', 'api', submit_claim, (api_adapter.APIError,))
    if state.result('announce_submission') is not None:
        return None

    claim = submitted_claim(state)
    if claim is not None:
        return Action('announce_submission', 'guava', lambda: _announce_claim(call, bridge, claim.claim_number))
    if submission_uncertain(state):
        checked = state.result('check_submission')
        if checked is None:
            def check_submission():
                with api_adapter.APIAdapter() as api:
                    return api.get_claim_by_reference(state.verified_profile.policy_number, state.submission_reference)
            return Action('check_submission', 'api', check_submission, (api_adapter.APIError,))
        if not checked.ok and isinstance(checked.data, api_adapter.PolicyNotFoundError) and state.result('retry_submit') is None:
            def retry_submit():
                with api_adapter.APIAdapter() as api:
                    return api.submit_claim_draft(state.verified_profile.policy_number,
                                                  submission_reference=state.submission_reference)
            return Action('retry_submit', 'api', retry_submit, (api_adapter.APIError,))
        return Action('announce_submission', 'guava', lambda: _announce_status_unknown(call, bridge))
    return Action('announce_submission', 'guava', lambda: _announce_submit_failed(call, bridge))


def submitted_claim(state):
    for name in ('submit_claim', 'check_submission', 'retry_submit'):
        result = state.result(name)
        if result is not None and result.ok:
            return result.data
    return None


def submission_uncertain(state):
    submitted = state.result('submit_claim')
    return (submitted is not None and not submitted.ok
            and isinstance(submitted.data, (api_adapter.APIUnavailableError, api_adapter.APIResponseError))
            and submitted_claim(state) is None)


def _announce_status_unknown(call, bridge):
    from ..coordinator import CALL_ENDING_RULE

    logger.info('Claim submission status unknown')
    bridge.set_task(call, 'claim-status-unknown',
        say=["I can't confirm whether your claim was filed, so please don't file it again yet. No claim number is available right now."],
        objective='Do not say it succeeded or failed. Do not offer to file again. ' + CALL_ENDING_RULE)


def _announce_claim(call, bridge, claim_number):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    logger.info('Claim submitted')
    bridge.set_task(call, SUBMITTED_TASK_ID,
        say=[f'Your claim has been submitted. Your claim number is {claim_number}. {PORTAL_LINE} Is there anything else you need?'],
        fields=[FieldDefinition('needs_more_help', 'Whether the caller needs anything else.',
                                field_type='multiple_choice', choices=('yes', 'no'))],
        objective='Record whether the caller needs anything else. Do not decide fault, coverage or payment. '
                  + KEEP_CONTEXT_RULE + CALL_ENDING_RULE,
    )


def _announce_not_submitted(call, bridge):
    from ..coordinator import CALL_ENDING_RULE

    logger.info('Claim not submitted')
    bridge.set_task(call, 'claim-not-submitted', objective=(
        'Nothing was submitted and there is no claim number. Explain this without pressure. The details are saved '
        'as a draft, which is not a claim. Do not promise a resume text or that the portal can finish it. ' + CALL_ENDING_RULE
    ))


def _announce_submit_failed(call, bridge):
    from ..coordinator import CALL_ENDING_RULE

    logger.info('Claim submission failed')
    bridge.set_task(call, 'claim-submit-failed', objective=(
        'The claim could not be submitted and there is no claim number. Say so honestly. '
        'Do not claim to transfer the caller. ' + CALL_ENDING_RULE
    ))


def next_close_action(call, state, bridge):
    from ..coordinator import CALL_ENDING_RULE

    if state.result('close_call') is not None:
        return None
    if bridge.get_field(call, 'needs_more_help') == 'no':
        return Action('close_call', 'guava', lambda: bridge.end_call_success(call))
    return Action('close_call', 'guava', lambda: bridge.set_task(call, 'claim-more-help', objective=(
        'This demo only opens new loss reports. Explain that limit without inventing contact details '
        'or claiming to transfer the caller. ' + CALL_ENDING_RULE
    )))


