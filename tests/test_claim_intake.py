"""Claim intake decisions tested without Guava or a running HTTP service."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent import api_adapter, checklist, coordinator, verification
from agent.call_state import CallState
from agent.workflows import endings, claim_intake, review_claim, submit_claim

VEHICLES = (
    SimpleNamespace(vehicle_id='V01-1', year=2021, make='Toyota', model='Camry'),
    SimpleNamespace(vehicle_id='V01-2', year=2022, make='Honda', model='Civic'),
)
PROFILE = SimpleNamespace(policy_number='1001', vehicles=VEHICLES)
FIELDS = {
    'caller_name': 'Michael Johnson', 'caller_phone': '628-268-8753', 'vehicle': '2022 Honda Civic',
    'occurred_at': '2026-11-01T15:00:00', 'location': 'Main and 5th',
    'description': 'Rear-ended at a light.', 'loss_type': 'collision',
}


class FakeBridge:
    def __init__(self):
        self.tasks = []
        self.validators = {}
        self.completions = {}
        self.say = []
        self.completion_criteria = {}
        self.hang_up = Mock()

    def set_task(self, call, task_id, objective, todos=(), *, fields=(), say=(), completion_criteria=""):
        self.tasks.append((task_id, objective, fields))
        self.say.append(tuple(say))
        self.completion_criteria[task_id] = completion_criteria

    def add_info(self, call, label, info):
        pass

    def get_field(self, call, key):
        return call.get(key)

    def register_validator(self, agent, key, handler):
        self.validators[key] = handler

    def register_task_complete(self, agent, task_id, handler):
        self.completions[task_id] = handler


@pytest.fixture
def api(monkeypatch):
    client = Mock()
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    return client


def registered(profile=PROFILE):
    bridge = FakeBridge()
    state = CallState('test-session', verified_profile=profile, facts=tuple((key, FIELDS[key]) for key in ('caller_name', 'caller_phone')))
    bridge.state = state
    claim_intake.register(object(), bridge)
    coordinator.register_completions(object(), bridge, lambda call: state, checklist.Routes(
        Mock(), lambda call: bridge.set_task(call, 'assistance-offer', ''),
    ))
    return bridge


def test_start_offers_only_this_policys_vehicles():
    bridge = FakeBridge()
    claim_intake.start({}, bridge, CallState('start', verified_profile=PROFILE))
    task_id, _, fields = bridge.tasks[0]
    assert task_id == claim_intake.TASK_ID
    vehicle = next(field for field in fields if field.key == 'vehicle')
    assert vehicle.choices == ('2021 Toyota Camry', '2022 Honda Civic')
    assert vehicle.question == 'Which vehicle was involved?'


def test_occurred_at_rejects_future_with_fixed_now():
    from datetime import datetime

    now = datetime.fromisoformat('2026-11-02T12:00:00-08:00')
    assert verification.validate_occurred_at('2026-11-01T15:00:00-08:00', now=now) is True
    valid, reason = verification.validate_occurred_at('2026-11-02T15:00:00-08:00', now=now)
    assert valid is False
    assert 'has not happened yet' in reason


def test_single_vehicle_is_used_without_a_field(api):
    profile = SimpleNamespace(policy_number=PROFILE.policy_number, vehicles=VEHICLES[:1])
    bridge = registered(profile)
    call = {key: value for key, value in FIELDS.items() if key != 'vehicle'}
    claim_intake.start(call, bridge, bridge.state)
    _, objective, fields = bridge.tasks[-1]
    assert 'vehicle' not in {field.key for field in fields}
    assert bridge.state.fact('vehicle') == '2021 Toyota Camry'
    bridge.completions[claim_intake.TASK_ID](call)
    assert api.update_claim_draft.call_args.args[1].vehicle_id == 'V01-1'
    assert '2021 Toyota Camry' in bridge.say[-1][0]
    assert bridge.state.result('start_review').data['vehicle'] == '2021 Toyota Camry'


def test_collection_and_review_completion_criteria(api):
    bridge = registered()
    call = dict(FIELDS)
    claim_intake.start(call, bridge, bridge.state)
    bridge.completions[claim_intake.TASK_ID](call)
    assert bridge.completion_criteria[claim_intake.TASK_ID] == (
        'Complete as soon as every field is recorded. Do not ask for extra confirmation; '
        'the application reads the details back next.'
    )
    assert bridge.completion_criteria[review_claim.REVIEW_TASK_ID] == 'Complete as soon as the decision is recorded.'


def test_review_line_strips_trailing_periods_and_spaces():
    line = review_claim.review_line(dict(FIELDS, location='Main and 5th... ', description='Rear-ended at a light. . '))
    assert '..' not in line
    assert 'at Main and 5th. Rear-ended at a light. This was a collision.' in line


def test_complete_saves_draft_with_chosen_vehicle_and_never_submits(api):
    bridge = registered()
    bridge.completions[claim_intake.TASK_ID](dict(FIELDS))
    policy_number, update = api.update_claim_draft.call_args.args
    assert policy_number == '1001'
    assert update.vehicle_id == 'V01-2'
    assert update.caller_role == 'policyholder'
    assert update.occurred_at.tzinfo is not None
    api.submit_claim_draft.assert_not_called()
    assert bridge.tasks[-1][0] == 'review-claim'
    assert 'Do not say a claim was submitted' in bridge.tasks[-1][1]
    assert 'claim number' not in bridge.say[-1][0].lower()
    assert 'has been submitted' not in bridge.say[-1][0].lower()


@pytest.mark.parametrize('error', [
    api_adapter.APIUnavailableError('offline'), api_adapter.ClaimValidationError('bad', 400),
])
def test_failed_save_is_not_reported_as_saved(api, error):
    api.update_claim_draft.side_effect = error
    bridge = registered()
    bridge.completions[claim_intake.TASK_ID](dict(FIELDS))
    expected = 'assistance-offer' if isinstance(error, api_adapter.APIUnavailableError) else 'claim-save-failed'
    assert bridge.tasks[-1][0] == expected
    assert all(task[0] != 'review-claim' for task in bridge.tasks)


def test_unverified_call_saves_nothing(api):
    bridge = registered(profile=None)
    bridge.completions[claim_intake.TASK_ID](dict(FIELDS))
    api.update_claim_draft.assert_not_called()
    assert bridge.tasks == []


@pytest.mark.parametrize('value,valid', [
    ('2026-11-01T15:00:00', True), ('2026-11-01T15:00:00-08:00', True),
    ('yesterday', False), ('', True), (None, True),
])
def test_occurred_at_validation(value, valid):
    result = registered().validators['occurred_at']({}, value)
    assert (result is True) is valid


def reviewed_call(api):
    bridge = registered()
    call = dict(FIELDS)
    api.submit_claim_draft.return_value = SimpleNamespace(claim_number='CLM-A1B2C3D4')
    bridge.completions[claim_intake.TASK_ID](call)
    return bridge, call


def test_review_speaks_every_field_without_claiming_submission(api):
    bridge, call = reviewed_call(api)
    line, = bridge.say[-1]
    assert line == review_claim.review_line(claim_intake.claim_facts(bridge.state))
    for key in claim_intake.CLAIM_FIELDS:
        value = call[key] if key != 'occurred_at' else 'Sunday, November 1 at 3 PM'
        assert value in line
    assert 'has been submitted' not in line
    assert bridge.state.result('start_review').data == {key: call[key] for key in claim_intake.CLAIM_FIELDS}


@pytest.mark.parametrize('decision', ['decline', None])
def test_no_explicit_approval_does_not_submit(api, decision):
    bridge, call = reviewed_call(api)
    call['submission_decision'] = decision
    bridge.completions[review_claim.REVIEW_TASK_ID](call)
    assert bridge.tasks[-1][0] == 'claim-not-submitted'
    api.submit_claim_draft.assert_not_called()


def test_approve_submits_once_with_current_review_and_real_number(api, caplog):
    bridge, call = reviewed_call(api)
    call['submission_decision'] = 'approve'
    with caplog.at_level('INFO', logger='agent.workflows'):
        bridge.completions[review_claim.REVIEW_TASK_ID](call)
        reference = bridge.state.submission_reference
        bridge.completions[review_claim.REVIEW_TASK_ID](call)
    assert reference.startswith('sub-')
    assert bridge.state.submission_reference == reference
    api.submit_claim_draft.assert_called_once_with(PROFILE.policy_number, submission_reference=reference)
    assert bridge.state.approved_fields == {key: call[key] for key in claim_intake.CLAIM_FIELDS}
    assert bridge.tasks[-1][0] == submit_claim.SUBMITTED_TASK_ID
    assert bridge.say[-1] == (f'All done, your claim has been filed. Your claim number is C, L, M, A, 1, B, 2, C, 3, D, 4. {submit_claim.PORTAL_LINE} Is there anything else I can help you with?',)
    assert 'Claim submitted' in caplog.messages
    assert 'CLM-A1B2C3D4' not in caplog.text and reference not in caplog.text
    assert call['description'] not in caplog.text


@pytest.mark.parametrize('missing_review,changed_field', [(True, None)] + [(False, key) for key in claim_intake.CLAIM_FIELDS])
def test_missing_or_changed_review_cannot_submit(api, missing_review, changed_field):
    bridge, call = reviewed_call(api)
    call['submission_decision'] = 'approve'
    if missing_review:
        del bridge.state.results['start_review']
    else:
        bridge.state.set_fact(changed_field, 'changed')
    bridge.completions[review_claim.REVIEW_TASK_ID](call)
    api.submit_claim_draft.assert_not_called()
    assert bridge.tasks[-1][0] == 'claim-not-submitted'


def test_unverified_state_cannot_submit(api):
    bridge = registered(profile=None)
    bridge.completions[review_claim.REVIEW_TASK_ID](dict(FIELDS, submission_decision='approve'))
    api.submit_claim_draft.assert_not_called()
    assert bridge.tasks == []


@pytest.mark.parametrize('error,task', [
    (api_adapter.APIUnavailableError('private transport detail'), 'claim-status-unknown'),
    (api_adapter.APIResponseError('private response detail'), 'claim-status-unknown'),
    (api_adapter.ClaimValidationError('private validation detail', 400), 'claim-submit-failed'),
])
def test_submission_failure_never_announces_claim(api, caplog, error, task):
    bridge, call = reviewed_call(api)
    call['submission_decision'] = 'approve'
    api.submit_claim_draft.side_effect = error
    api.get_claim_by_reference.side_effect = api_adapter.APIUnavailableError('unavailable')
    with caplog.at_level('INFO', logger='agent.workflows'):
        bridge.completions[review_claim.REVIEW_TASK_ID](call)
    assert bridge.tasks[-1][0] == task
    assert 'Claim submitted' not in caplog.messages
    assert bridge.say[-1] == (() if task == 'claim-submit-failed' else ("I can't confirm whether your claim was filed, so please don't file it again yet. No claim number is available right now.",))
    assert 'private' not in caplog.text
    assert bridge.state.result('submit_claim').ok is False


@pytest.mark.parametrize('needs_help', ['yes', 'no'])
def test_submitted_call_closes_once_only_when_done(api, needs_help):
    bridge, call = reviewed_call(api)
    call.update(submission_decision='approve', ending_ack='done' if needs_help == 'no' else 'more')
    bridge.completions[review_claim.REVIEW_TASK_ID](call)
    bridge.completions[submit_claim.SUBMITTED_TASK_ID](call)
    bridge.completions[submit_claim.SUBMITTED_TASK_ID](call)
    if needs_help == 'no':
        bridge.hang_up.assert_called_once_with(call, endings.FINAL_LINES['filed'])
    else:
        bridge.hang_up.assert_not_called()
        assert bridge.tasks[-1][0] == endings.MORE_TASK_ID


def test_existing_facts_omit_fields_but_keep_correction_fields(api):
    bridge = registered()
    bridge.state.set_fact('location', 'Already known')
    claim_intake.start({}, bridge, bridge.state)
    fields = {field.key: field for field in bridge.tasks[-1][2]}
    assert 'location' not in fields
    assert {'corrected_field', 'corrected_value'} <= fields.keys()
    assert all(not fields[key].required for key in ('occurred_at', 'description', 'loss_type', 'corrected_field', 'corrected_value'))


def test_all_facts_present_has_no_fields_and_advances(api):
    bridge = registered()
    for key, value in FIELDS.items():
        bridge.state.set_fact(key, value)
    claim_intake.start({}, bridge, bridge.state, say=['Ready.'])
    assert bridge.tasks[-1][2] == []
    assert bridge.say[-1] == ('Ready.',)
    bridge.completions[claim_intake.TASK_ID]({})
    assert api.update_claim_draft.call_args.args[1].location == FIELDS['location']
    assert bridge.tasks[-1][0] == review_claim.REVIEW_TASK_ID


def test_correction_during_collection(api):
    bridge = registered()
    bridge.completions[claim_intake.TASK_ID](dict(FIELDS, corrected_field='location', corrected_value='Oak Street'))
    assert bridge.state.fact('location') == 'Oak Street'
    assert api.update_claim_draft.call_args.args[1].location == 'Oak Street'


@pytest.mark.parametrize('missing', claim_intake.REQUIRED_FACTS)
def test_skipped_required_fact_never_saves(api, missing):
    bridge = registered()
    call = dict(FIELDS)
    call.pop(missing)
    bridge.completions[claim_intake.TASK_ID](call)
    assert bridge.tasks[-1][0] == 'claim-missing-details'
    api.update_claim_draft.assert_not_called()
    api.submit_claim_draft.assert_not_called()


def test_optional_loss_type_stays_none(api):
    bridge = registered()
    call = dict(FIELDS)
    call.pop('loss_type')
    bridge.completions[claim_intake.TASK_ID](call)
    assert api.update_claim_draft.call_args.args[1].loss_type is None
    assert 'This was a' not in bridge.say[-1][0]
    assert 'not provided' not in bridge.say[-1][0]


@pytest.mark.parametrize('decision,value,valid', [('change', 'Oak Street', True), ('change', ' ', False),
    ('add', 'The bumper broke.', True), ('add', ' ', False)])
def test_review_changes_and_additions(api, decision, value, valid):
    bridge, call = reviewed_call(api)
    original = bridge.state.fact('description')
    bridge.state.approved_fields = claim_intake.claim_facts(bridge.state)
    call.update(submission_decision=decision, corrected_field='location', corrected_value=value)
    bridge.completions[review_claim.REVIEW_TASK_ID](call)
    assert bridge.state.corrections == 1
    assert api.update_claim_draft.call_count == (2 if valid else 1)
    assert bridge.tasks[-1][0] == review_claim.REVIEW_TASK_ID
    if valid:
        assert bridge.state.approved_fields is None
        expected = value if decision == 'change' else original.rstrip('. ') + '. ' + value
        assert bridge.state.fact('location' if decision == 'change' else 'description') == expected
        assert value in bridge.say[-1][0]
    else:
        assert bridge.say[-1][0].startswith("I couldn't use that change. ")
        assert bridge.state.fact('description') == original
        assert bridge.state.fact('location') == FIELDS['location']
    call['submission_decision'] = 'approve'
    bridge.completions[review_claim.REVIEW_TASK_ID](call)
    bridge.completions[review_claim.REVIEW_TASK_ID](call)
    api.submit_claim_draft.assert_called_once()
    assert bridge.state.approved_fields == claim_intake.claim_facts(bridge.state)


@pytest.mark.parametrize('decision', ['add', 'change'])
def test_corrections_stop_at_limit(api, decision):
    bridge, call = reviewed_call(api)
    for index in range(review_claim.MAX_CORRECTIONS):
        call.update(submission_decision=decision, corrected_field='location', corrected_value=f'Detail {index}')
        bridge.completions[review_claim.REVIEW_TASK_ID](call)
    before = bridge.state.facts
    bridge.completions[review_claim.REVIEW_TASK_ID](call)
    assert bridge.state.corrections == review_claim.MAX_CORRECTIONS
    assert bridge.state.facts == before
    assert bridge.tasks[-1][0] == 'claim-not-submitted'
    api.submit_claim_draft.assert_not_called()


@pytest.mark.parametrize('status,retry', [('found', False), ('missing', True), ('unavailable', False), ('retry_failed', True)])
def test_uncertain_submission_checks_before_retry(api, status, retry):
    bridge, call = reviewed_call(api)
    claim = SimpleNamespace(claim_number='CLM-RECOVERED')
    error = api_adapter.APIUnavailableError('timeout')
    api.submit_claim_draft.side_effect = [error, error if status == 'retry_failed' else claim]
    if status == 'found':
        api.get_claim_by_reference.return_value = claim
    else:
        api.get_claim_by_reference.side_effect = (api_adapter.PolicyNotFoundError('missing', 404)
            if retry else api_adapter.APIUnavailableError('offline'))
    call['submission_decision'] = 'approve'
    bridge.completions[review_claim.REVIEW_TASK_ID](call)
    bridge.completions[review_claim.REVIEW_TASK_ID](call)
    reference = bridge.state.submission_reference
    api.get_claim_by_reference.assert_called_once_with(PROFILE.policy_number, reference)
    assert api.submit_claim_draft.call_count == 1 + retry
    assert all(entry.kwargs['submission_reference'] == reference for entry in api.submit_claim_draft.call_args_list)
    relevant = [entry[0] for entry in api.mock_calls if entry[0] in ('submit_claim_draft', 'get_claim_by_reference')]
    assert relevant == ['submit_claim_draft', 'get_claim_by_reference'] + (['submit_claim_draft'] if retry else [])
    if status in ('found', 'missing'):
        assert 'C, L, M, R, E, C, O, V, E, R, E, D' in bridge.say[-1][0]
    else:
        assert bridge.tasks[-1][0] == 'claim-status-unknown'
        assert 'CLM-' not in bridge.say[-1][0]
        assert submit_claim.submission_uncertain(bridge.state)


@pytest.mark.parametrize('ending', ['.', '!', '?', '... '])
def test_addition_has_no_doubled_sentence_punctuation(ending):
    bridge = registered()
    bridge.state.set_fact('description', 'First detail' + ending)
    claim_intake.apply_addition(bridge.state, 'Second detail.')
    assert bridge.state.fact('description') == 'First detail. Second detail.'
    assert bridge.state.corrections == 1


@pytest.mark.parametrize('summary', [None, '', 'My car was hit from behind.'])
@pytest.mark.parametrize('loss_type', ['collision', None])
def test_review_uses_summary_without_changing_claim_fields(summary, loss_type):
    fields = dict(FIELDS, loss_type=loss_type)
    state = CallState('summary', facts=tuple(fields.items()) + (('incident_summary', summary),))
    bridge = FakeBridge()
    returned = review_claim.start_review({}, bridge, state)
    incident = (summary or fields['description']).rstrip('. ')
    loss = ' This was a collision.' if loss_type else ''
    assert bridge.say[-1] == (
        "So here's what I have. It was your 2022 Honda Civic, on Sunday, November 1 at 3 PM, "
        f"at Main and 5th. {incident}.{loss} Would you like to add anything, or should I file this claim?",
    )
    assert returned == claim_intake.claim_facts(state)
    assert 'incident_summary' not in returned
    assert 'incident_summary' not in claim_intake.CLAIM_FIELDS


@pytest.mark.parametrize('loss_type,loss', [
    ('collision', ' This was a collision.'),
    ('theft', ' This was a theft.'),
    ('weather', ' This was weather damage.'),
    ('other', ''),
    (None, ''),
])
def test_review_loss_wording(loss_type, loss):
    fields = dict(FIELDS)
    if loss_type is None:
        fields.pop('loss_type')
    else:
        fields['loss_type'] = loss_type
    assert review_claim.review_line(fields) == (
        "So here's what I have. It was your 2022 Honda Civic, on Sunday, November 1 at 3 PM, "
        f"at Main and 5th. Rear-ended at a light.{loss} "
        "Would you like to add anything, or should I file this claim?"
    )
