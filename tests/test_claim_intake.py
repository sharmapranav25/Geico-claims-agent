"""Claim intake decisions tested without Guava or a running HTTP service."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent import api_adapter
from agent.call_state import CallState
from agent.workflows import claim_intake

VEHICLES = (
    SimpleNamespace(vehicle_id='V01-1', year=2021, make='Toyota', model='Camry'),
    SimpleNamespace(vehicle_id='V01-2', year=2022, make='Honda', model='Civic'),
)
PROFILE = SimpleNamespace(policy_number='GEC-0001', vehicles=VEHICLES)
FIELDS = {
    'caller_name': 'Avery Morgan', 'caller_phone': '628-268-8753', 'vehicle': '2022 Honda Civic',
    'occurred_at': '2026-11-01T15:00:00', 'location': 'Main and 5th',
    'description': 'Rear-ended at a light.', 'loss_type': 'collision',
}


class FakeBridge:
    def __init__(self):
        self.tasks = []
        self.validators = {}
        self.completions = {}

    def set_task(self, call, task_id, objective, todos=(), *, fields=()):
        self.tasks.append((task_id, objective, fields))

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
    claim_intake.register(object(), bridge, lambda call: state,
                          lambda call: bridge.set_task(call, 'assistance-offer', ''))
    return bridge


def test_start_offers_only_this_policys_vehicles():
    bridge = FakeBridge()
    claim_intake.start({}, bridge, PROFILE)
    task_id, _, fields = bridge.tasks[0]
    assert task_id == claim_intake.TASK_ID
    vehicle = next(field for field in fields if field.key == 'vehicle')
    assert vehicle.choices == ('2021 Toyota Camry', '2022 Honda Civic')


def test_complete_saves_draft_with_chosen_vehicle_and_never_submits(api):
    bridge = registered()
    bridge.completions[claim_intake.TASK_ID](dict(FIELDS))
    policy_number, update = api.update_claim_draft.call_args.args
    assert policy_number == 'GEC-0001'
    assert update.vehicle_id == 'V01-2'
    assert update.caller_role == 'policyholder'
    assert update.occurred_at.tzinfo is not None
    api.submit_claim_draft.assert_not_called()
    assert bridge.tasks[-1][0] == 'claim-details-saved'
    assert 'not a submitted claim' in bridge.tasks[-1][1]


@pytest.mark.parametrize('error', [
    api_adapter.APIUnavailableError('offline'), api_adapter.ClaimValidationError('bad', 400),
])
def test_failed_save_is_not_reported_as_saved(api, error):
    api.update_claim_draft.side_effect = error
    bridge = registered()
    bridge.completions[claim_intake.TASK_ID](dict(FIELDS))
    expected = 'assistance-offer' if isinstance(error, api_adapter.APIUnavailableError) else 'claim-save-failed'
    assert bridge.tasks[-1][0] == expected
    assert all(task[0] != 'claim-details-saved' for task in bridge.tasks)


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
