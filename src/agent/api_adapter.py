"""HTTP boundary for read-only policy profiles and editable claim reports."""

import os
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StringConstraints,
    TypeAdapter, ValidationError,
)

load_dotenv()

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ReadOnlyModel(BaseModel):
    # Prevent attribute edits and reject unexpected response fields.
    model_config = ConfigDict(extra="forbid", frozen=True)


class OtherParty(ReadOnlyModel):
    policy_number: Text | None = None
    name: Text | None = None
    phone: Text | None = None
    insurer: Text | None = None
    vehicle_description: Text | None = None


class RepresentedPerson(ReadOnlyModel):
    name: Text | None = None
    relationship: Text | None = None


class PoliceReport(ReadOnlyModel):
    filed: StrictBool | None = None
    report_number: Text | None = None
    department: Text | None = None


class Witness(ReadOnlyModel):
    name: Text | None = None
    phone: Text | None = None


class ClaimDraftUpdate(BaseModel):
    """Only writable loss fields; omitted values are never sent to the server."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    reporting_intent: Literal["A", "B"] | None = None
    caller_role: Literal["policyholder", "representative", "affected_person"] | None = None
    represented_person: RepresentedPerson | None = None
    caller_name: Text | None = None
    caller_phone: Text | None = None
    occurred_at: AwareDatetime | None = None
    discovered_at: AwareDatetime | None = None
    location: Text | None = None
    description: Text | None = None
    vehicle_id: Text | None = None
    loss_type: Literal["collision", "theft", "weather", "other"] | None = None
    other_party: OtherParty | None = None
    police_report: PoliceReport | None = None
    witnesses: list[Witness] = Field(default_factory=list)
    evidence_link_sent: StrictBool = False


class ClaimLoss(ReadOnlyModel):
    """Immutable response snapshot, separate from the writable request model."""

    reporting_intent: Literal["A", "B"] | None = None
    caller_role: Literal["policyholder", "representative", "affected_person"] | None = None
    represented_person: RepresentedPerson | None = None
    caller_name: Text | None = None
    caller_phone: Text | None = None
    occurred_at: AwareDatetime | None = None
    discovered_at: AwareDatetime | None = None
    location: Text | None = None
    description: Text | None = None
    vehicle_id: Text | None = None
    loss_type: Literal["collision", "theft", "weather", "other"] | None = None
    other_party: OtherParty | None = None
    police_report: PoliceReport | None = None
    witnesses: tuple[Witness, ...] = ()
    evidence_link_sent: StrictBool = False


class DraftResponse(ReadOnlyModel):
    draft_id: str
    policy_number: str
    status: Literal["draft"]
    loss: ClaimLoss


class SubmittedClaim(ReadOnlyModel):
    draft_id: str
    policy_number: str
    status: Literal["submitted"]
    loss: ClaimLoss
    claim_number: str
    submitted_at: AwareDatetime
    submission_reference: str | None = None


class Vehicle(ReadOnlyModel):
    vehicle_id: str
    year: int
    make: str
    model: str


class Driver(ReadOnlyModel):
    driver_id: str
    name: str
    relationship: str


class Beneficiary(ReadOnlyModel):
    name: str
    relationship: str


class ClaimAgainst(ReadOnlyModel):
    claim_number: str
    filed_under_policy: str
    status: Literal["submitted"]


class PolicySummary(ReadOnlyModel):
    policy_number: str
    name: str


class PolicyProfile(ReadOnlyModel):
    # Tuples keep nested collections immutable as well as the profile itself.
    policy_number: str
    name: str
    phone: str
    vehicles: tuple[Vehicle, ...]
    drivers: tuple[Driver, ...]
    beneficiaries: tuple[Beneficiary, ...]
    claims: tuple[SubmittedClaim, ...]
    open_draft: DraftResponse | None
    claims_against: tuple[ClaimAgainst, ...]


class APIError(Exception):
    """Base error retaining the server's detail and HTTP status when available."""

    def __init__(self, detail, status_code: int | None = None):
        self.detail = detail
        self.status_code = status_code
        super().__init__(str(detail))


class PolicyNotFoundError(APIError):
    pass


class ClaimValidationError(APIError):
    pass


class APIUnavailableError(APIError):
    pass


class APIResponseError(APIError):
    """Unexpected HTTP status, malformed JSON, or an incompatible response."""


def _response_json(response: httpx.Response):
    """Shared response mapping, also usable when inspecting a raw HTTP response."""
    try:
        data = response.json()
    except ValueError as exc:
        raise APIResponseError("API returned invalid JSON", response.status_code) from exc
    detail = data.get("detail", data) if isinstance(data, dict) else data
    if response.status_code == 404:
        raise PolicyNotFoundError(detail, response.status_code)
    if response.status_code in (400, 422):
        raise ClaimValidationError(detail, response.status_code)
    if not response.is_success:
        raise APIResponseError(detail, response.status_code)
    return data


class APIAdapter:
    """Synchronous client. Use as a context manager or call close() when done."""

    def __init__(self):
        base_url = os.environ.get("MOCK_API_URL", "").strip()
        if not base_url:
            raise ValueError("MOCK_API_URL must be set in the environment or .env")
        self._client = httpx.Client(base_url=base_url.rstrip("/") + "/", timeout=httpx.Timeout(5.0))

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def _request(self, method, path, response_type, **kwargs):
        # Centralize transport errors and response validation for every endpoint.
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise APIUnavailableError("Mock API request failed: " + str(exc)) from exc
        data = _response_json(response)
        try:
            return TypeAdapter(response_type).validate_python(data)
        except ValidationError as exc:
            raise APIResponseError("API response does not match the expected schema", response.status_code) from exc

    @staticmethod
    def _policy_path(policy_number: str) -> str:
        return "policies/" + quote(policy_number, safe="")

    def list_policies(self) -> list[PolicySummary]:
        return self._request("GET", "policies", list[PolicySummary])

    def get_policy(self, policy_number: str) -> PolicyProfile:
        return self._request("GET", self._policy_path(policy_number), PolicyProfile)

    def get_claim_by_reference(self, policy_number: str, reference: str) -> SubmittedClaim:
        return self._request('GET', self._policy_path(policy_number) + '/claims/by-reference/'
                             + quote(reference, safe=''), SubmittedClaim)

    def update_claim_draft(self, policy_number: str, update: ClaimDraftUpdate) -> DraftResponse:
        if type(update) is not ClaimDraftUpdate:
            raise TypeError("update must be a ClaimDraftUpdate")
        # Revalidate at the boundary, including mutable lists or model_copy updates.
        payload = ClaimDraftUpdate.model_validate(update.model_dump(exclude_unset=True))
        return self._request(
            "POST", self._policy_path(policy_number) + "/claims/draft", DraftResponse,
            json=payload.model_dump(mode="json", exclude_unset=True),
        )

    def submit_claim_draft(self, policy_number: str, submission_reference: str | None = None) -> SubmittedClaim:
        # Submission and locking happen on the server; return only its result.
        return self._request(
            "POST", self._policy_path(policy_number) + "/claims/draft/submit", SubmittedClaim,
            **({'json': {'submission_reference': submission_reference}} if submission_reference is not None else {}),
        )
