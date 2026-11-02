"""Live HTTP integration tests; submitting a test claim changes mock API state."""

import importlib.util
import os
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

spec = importlib.util.spec_from_file_location(
    "api_adapter_under_test", Path(__file__).resolve().parents[1] / "src/agent/api_adapter.py"
)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

# Only an unavailable external service skips this module; contract failures fail.
try:
    with api.APIAdapter() as probe:
        probe.list_policies()
except api.APIUnavailableError:
    pytest.skip("mock API not running at MOCK_API_URL", allow_module_level=True)


@pytest.fixture
def adapter():
    with api.APIAdapter() as client:
        yield client


def test_list_and_read_seeded_policy(adapter):
    policies = adapter.list_policies()
    assert len(policies) == 15
    assert any(p.policy_number == "GEC-0001" and p.name == "Avery Morgan" for p in policies)
    profile = adapter.get_policy("GEC-0001")
    assert isinstance(profile, api.PolicyProfile)
    assert profile.policy_number == "GEC-0001"
    assert profile.name == "Avery Morgan"
    assert profile.phone == "628-268-8753"
    assert profile.vehicles[0].vehicle_id == "V01-1"
    assert profile.drivers[0].name == profile.name
    assert profile.beneficiaries
    assert profile.claims[0].claim_number == "CLM-SEED-0001"
    assert adapter.get_policy("GEC-0007").claims_against[0].filed_under_policy == "GEC-0001"
    with pytest.raises(ValidationError):
        profile.name = "Changed"
    with pytest.raises(ValidationError):
        profile.vehicles[0].make = "Changed"
    assert isinstance(profile.vehicles, tuple)


def test_editable_field_set_is_exact():
    assert set(api.ClaimDraftUpdate.model_fields) == {
        "reporting_intent", "caller_role", "represented_person", "caller_name", "caller_phone",
        "occurred_at", "discovered_at", "location", "description", "vehicle_id", "loss_type",
        "other_party", "police_report", "witnesses", "evidence_link_sent",
    }


@pytest.mark.parametrize("field", [
    "policy_number", "name", "phone", "vehicles", "drivers", "beneficiaries",
    "claims", "claims_against", "draft_id", "status", "claim_number", "submitted_at",
])
def test_update_rejects_read_only_fields(field):
    with pytest.raises(ValidationError):
        api.ClaimDraftUpdate(**{field: "forbidden"})


def test_write_rejects_dict_or_profile(adapter):
    for value in ({"name": "Changed"}, adapter.get_policy("GEC-0001")):
        with pytest.raises(TypeError, match="ClaimDraftUpdate"):
            adapter.update_claim_draft("GEC-0001", value)


def test_live_draft_round_trip_and_submit(adapter):
    # GEC-0013 has no activity in the seed. Reruns append a new test claim.
    number = "GEC-0013"
    before = adapter.get_policy(number)
    assert before.open_draft is None, "Existing draft: restart the mock API before testing"
    update = api.ClaimDraftUpdate(
        reporting_intent="A", caller_role="policyholder", caller_name=before.name,
        caller_phone=before.phone, occurred_at="2026-10-31T10:00:00-07:00",
        location="Adapter integration test lot", description="Test scrape while parked.",
        vehicle_id=before.vehicles[0].vehicle_id, loss_type="collision",
        police_report={"filed": False}, witnesses=[], evidence_link_sent=False,
    )
    draft = adapter.update_claim_draft(number, update)
    assert draft.status == "draft"
    assert draft.loss.caller_name == before.name
    assert draft.loss.loss_type == "collision"
    assert draft.loss.police_report.filed is False
    updated = adapter.update_claim_draft(number, api.ClaimDraftUpdate(description="Updated test scrape."))
    assert updated.draft_id == draft.draft_id
    assert updated.loss.location == update.location
    assert updated.loss.description == "Updated test scrape."
    claim = adapter.submit_claim_draft(number)
    assert claim.status == "submitted" and claim.claim_number.startswith("CLM-")
    assert claim.draft_id == draft.draft_id
    after = adapter.get_policy(number)
    assert after.open_draft is None
    assert len(after.claims) == len(before.claims) + 1
    for field in ("policy_number", "name", "phone", "vehicles", "drivers", "beneficiaries"):
        assert getattr(after, field) == getattr(before, field)
    with pytest.raises(api.ClaimValidationError) as error:
        adapter.submit_claim_draft(number)
    assert error.value.status_code == 400
    assert error.value.detail == "No open draft to submit"


def test_raw_forbidden_field_and_adapter_error_mapping(adapter):
    before = adapter.get_policy("GEC-0014")
    with httpx.Client(base_url=os.environ["MOCK_API_URL"], timeout=5) as client:
        response = client.post("/policies/GEC-0014/claims/draft", json={"name": "Changed"})
    assert response.status_code == 422
    with pytest.raises(api.ClaimValidationError) as error:
        api._response_json(response)
    assert error.value.status_code == 422
    assert error.value.detail == response.json()["detail"]
    assert adapter.get_policy("GEC-0014") == before


def test_live_invalid_vehicle_is_wrapped(adapter):
    with pytest.raises(api.ClaimValidationError) as error:
        adapter.update_claim_draft("GEC-0014", api.ClaimDraftUpdate(vehicle_id="V01-1"))
    assert error.value.status_code == 400
    assert error.value.detail == "vehicle_id must reference a vehicle on this policy"


def test_unknown_policy(adapter):
    with pytest.raises(api.PolicyNotFoundError) as error:
        adapter.get_policy("UNKNOWN-POLICY")
    assert error.value.status_code == 404
    assert error.value.detail == "Policy not found"


def test_live_submission_reference_replay_adds_one_claim(adapter):
    from uuid import uuid4

    number = 'GEC-0015'
    before = adapter.get_policy(number)
    adapter.update_claim_draft(number, api.ClaimDraftUpdate(
        occurred_at='2026-11-01T15:00:00-08:00', location='Test lot',
        description='Replay test', vehicle_id=before.vehicles[0].vehicle_id,
    ))
    reference = f'sub-{uuid4().hex}'
    first = adapter.submit_claim_draft(number, submission_reference=reference)
    replay = adapter.submit_claim_draft(number, submission_reference=reference)
    assert first == replay
    assert first.submission_reference == reference
    assert len(adapter.get_policy(number).claims) == len(before.claims) + 1
    assert adapter.get_policy(number).open_draft is None


@pytest.mark.parametrize('reference', [None, 'sub-request-body'])
def test_submission_body_only_when_reference_given(adapter, monkeypatch, reference):
    from unittest.mock import Mock

    request = Mock()
    monkeypatch.setattr(adapter, '_request', request)
    adapter.submit_claim_draft('GEC-0015', submission_reference=reference)
    assert request.call_args.args == ('POST', 'policies/GEC-0015/claims/draft/submit', api.SubmittedClaim)
    assert request.call_args.kwargs == ({} if reference is None else {'json': {'submission_reference': reference}})
