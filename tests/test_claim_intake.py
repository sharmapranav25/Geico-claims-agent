"""Claim intake decisions tested without Guava or a running HTTP service."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent import api_adapter, checklist, coordinator
from agent.call_state import CallState
from agent.workflows import claim_intake

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
        self.end_call_success = Mock()

    def set_task(self, call, task_id, objective, todos=(), *, fields=(), say=(), completion_criteria=""):
        self.tasks.append((task_id, objective, fields))
        self.say.append(tuple(say))
        self.completion_criteria[task_id] = completion_criteria

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
    state = CallState('test-session', verified_profile=profile)
    bridge.state = state
    claim_intake.register(object(), bridge)
    coordinator.register_completions(object(), bridge, lambda call: state, checklist.Routes(
        Mock(), lambda call: bridge.set_task(call, 'assistance-offer', ''),
    ))
    return bridge


def test_start_offers_only_this_policys_vehicles():
    bridge = FakeBridge()
    claim_intake.start({}, bridge, PROFILE)
    task_id, _, fields = bridge.tasks[0]
    assert task_id == claim_intake.TASK_ID
    vehicle = next(field for field in fields if field.key == 'vehicle')
    assert vehicle.choices == ('2021 Toyota Camry', '2022 Honda Civic')
    assert vehicle.question == 'Which vehicle was involved?'


def test_occurred_at_rejects_future_with_fixed_now():
    from datetime import datetime

    now = datetime.fromisoformat('2026-11-02T12:00:00-08:00')
    assert claim_intake.validate_occurred_at('2026-11-01T15:00:00-08:00', now=now) is True
    valid, reason = claim_intake.validate_occurred_at('2026-11-02T15:00:00-08:00', now=now)
    assert valid is False
    assert 'has not happened yet' in reason


def test_single_vehicle_is_used_without_a_field(api):
    profile = SimpleNamespace(policy_number=PROFILE.policy_number, vehicles=VEHICLES[:1])
    bridge = registered(profile)
    call = {key: value for key, value in FIELDS.items() if key != 'vehicle'}
    claim_intake.start(call, bridge, profile)
    _, objective, fields = bridge.tasks[-1]
    assert 'vehicle' not in {field.key for field in fields}
    assert 'The vehicle on this policy is 2021 Toyota Camry.' in objective
    assert 'Do not ask which vehicle' in objective
    bridge.completions[claim_intake.TASK_ID](call)
    assert api.update_claim_draft.call_args.args[1].vehicle_id == 'V01-1'
    assert '2021 Toyota Camry' in bridge.say[-1][0]
    assert bridge.state.result('start_review').data['vehicle'] == '2021 Toyota Camry'


def test_collection_and_review_completion_criteria(api):
    bridge = registered()
    call = dict(FIELDS)
    claim_intake.start(call, bridge, PROFILE)
    bridge.completions[claim_intake.TASK_ID](call)
    assert bridge.completion_criteria[claim_intake.TASK_ID] == (
        'Complete as soon as every field is recorded. Do not ask for extra confirmation; '
        'the application reads the details back next.'
    )
    assert bridge.completion_criteria[claim_intake.REVIEW_TASK_ID] == 'Complete as soon as the decision is recorded.'


def test_review_line_strips_trailing_periods_and_spaces():
    line = claim_intake.review_line(dict(FIELDS, location='Main and 5th... ', description='Rear-ended at a light. . '))
    assert '..' not in line
    assert 'Where: Main and 5th. What happened: Rear-ended at a light. Type of loss:' in line


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
    ('yesterday', False), ('', False), (None, False),
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
    assert line == claim_intake.review_line(claim_intake.current_claim_fields(call, bridge, PROFILE))
    for key in claim_intake.CLAIM_FIELDS:
        value = call[key] if key != 'occurred_at' else 'Sunday, November 1 at 3:00 PM'
        assert value in line
    assert 'has been submitted' not in line
    assert bridge.state.result('start_review').data == {key: call[key] for key in claim_intake.CLAIM_FIELDS}


@pytest.mark.parametrize('decision', ['decline', None])
def test_no_explicit_approval_does_not_submit(api, decision):
    bridge, call = reviewed_call(api)
    call['submission_decision'] = decision
    bridge.completions[claim_intake.REVIEW_TASK_ID](call)
    assert bridge.tasks[-1][0] == 'claim-not-submitted'
    api.submit_claim_draft.assert_not_called()


def test_approve_submits_once_with_current_review_and_real_number(api, caplog):
    bridge, call = reviewed_call(api)
    call['submission_decision'] = 'approve'
    with caplog.at_level('INFO', logger=claim_intake.__name__):
        bridge.completions[claim_intake.REVIEW_TASK_ID](call)
        reference = bridge.state.submission_reference
        bridge.completions[claim_intake.REVIEW_TASK_ID](call)
    assert reference.startswith('sub-')
    assert bridge.state.submission_reference == reference
    api.submit_claim_draft.assert_called_once_with(PROFILE.policy_number, submission_reference=reference)
    assert bridge.state.approved_fields == {key: call[key] for key in claim_intake.CLAIM_FIELDS}
    assert bridge.tasks[-1][0] == claim_intake.SUBMITTED_TASK_ID
    assert bridge.say[-1] == (f'Your claim has been submitted. Your claim number is CLM-A1B2C3D4. {claim_intake.PORTAL_LINE} Is there anything else you need?',)
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
        call[changed_field] = 'changed'
    bridge.completions[claim_intake.REVIEW_TASK_ID](call)
    api.submit_claim_draft.assert_not_called()
    assert bridge.tasks[-1][0] == 'claim-not-submitted'


def test_unverified_state_cannot_submit(api):
    bridge = registered(profile=None)
    bridge.completions[claim_intake.REVIEW_TASK_ID](dict(FIELDS, submission_decision='approve'))
    api.submit_claim_draft.assert_not_called()
    assert bridge.tasks == []


@pytest.mark.parametrize('error,task', [
    (api_adapter.APIUnavailableError('private transport detail'), 'assistance-offer'),
    (api_adapter.APIResponseError('private response detail'), 'assistance-offer'),
    (api_adapter.ClaimValidationError('private validation detail', 400), 'claim-submit-failed'),
])
def test_submission_failure_never_announces_claim(api, caplog, error, task):
    bridge, call = reviewed_call(api)
    call['submission_decision'] = 'approve'
    api.submit_claim_draft.side_effect = error
    with caplog.at_level('INFO', logger=claim_intake.__name__):
        bridge.completions[claim_intake.REVIEW_TASK_ID](call)
    assert bridge.tasks[-1][0] == task
    assert 'Claim submitted' not in caplog.messages
    assert bridge.say[-1] == ()
    assert 'private' not in caplog.text
    assert bridge.state.result('submit_claim').ok is False


@pytest.mark.parametrize('needs_help', ['yes', 'no'])
def test_submitted_call_closes_once_only_when_done(api, needs_help):
    bridge, call = reviewed_call(api)
    call.update(submission_decision='approve', needs_more_help=needs_help)
    bridge.completions[claim_intake.REVIEW_TASK_ID](call)
    bridge.completions[claim_intake.SUBMITTED_TASK_ID](call)
    bridge.completions[claim_intake.SUBMITTED_TASK_ID](call)
    if needs_help == 'no':
        bridge.end_call_success.assert_called_once_with(call)
    else:
        bridge.end_call_success.assert_not_called()
        assert bridge.tasks[-1][0] == 'claim-more-help'
