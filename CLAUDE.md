# GEICO Voice Agent — Guava FDE Track A

A Guava SDK voice agent for GEICO auto-claims intake. This demo opens claims
for verified policyholders who know their own policy number. Other caller
roles (representative, third party, on-behalf) are recognized and offered
assistance only — no protected disclosure, no claim intake for them in this
build.

## Repo topology

This repo is the voice agent only. The mock policy/claims API is a
**separate standalone repo** (`Geico-claims-mockAPI`), run locally and
reached only over HTTP through `MOCK_API_URL`. Nothing here imports it
directly — if a scope needs a new backend capability, that's a change in the
other repo plus a new method on `api_adapter.py` here, not a shortcut around
the HTTP boundary.

## Setup

```
uv sync
cp .env.example .env   # fill in GUAVA_API_KEY, GUAVA_AGENT_NUMBER
```

`.env` also needs `MOCK_API_URL` (defaults to `http://localhost:8000`). Start
the mock API from its own repo before running the agent locally.

## Running

```
python -m agent.guava_adapter --local    # local test call
python -m agent.guava_adapter --phone    # listen on GUAVA_AGENT_NUMBER
```

## Testing

```
pytest
```

Most tests are live and need the mock API running on `MOCK_API_URL`.
`tests/test_policy_lookup_offline.py` is the exception: it drives workflow
logic through a `FakeBridge` with no Guava SDK and no network call, proving
the workflow layer is independently testable.

## Architecture

```
guava_adapter.py  (SDK plumbing only — the only file that imports `guava`)
      ↓ implements
bridge.py         (CallBridge protocol — the dependency surface below here)
      ↓ used by
coordinator.py    (routing/dispatch wording, no SDK, no API import)
      ↓ routes to
workflows/*.py     (real workflow logic — calls api_adapter directly)
      ↓
api_adapter.py    (HTTP boundary to the mock policy/claims API)
```

Hard rule: workflow wording (objectives, intent choices, routing decisions)
never lives in `guava_adapter.py`. New scope should only ever touch
`coordinator.py` or a `workflows/*.py` file — never the SDK integration file.
This is what makes the extensibility claim checkable rather than asserted.

## Current scope (this sprint)

Per `reference-docs/GEICO Voice Agent - Build Sprint Plan.docx` (the active
execution plan — built independently of the brief's suggested order) and
`reference-docs/Scenarios.docx` (effort labels, path checklist, test cases):

**Built this sprint:**
1. Own-policy claim intake for a caller who knows their policy number:
   verify, confirm vehicle, collect loss facts, read back/correct, explicit
   approval, one confirmed submission.
2. Shared handling: changing intent, multiple requests, communication
   problems, safety, unsupported requests.
3. Mock human availability and saved callback requests (no live transfer or
   outbound calls).
4. Saved intake on disk after verification, plus dropped-call recovery with
   an SMS resume code (biggest risk — depends on unconfirmed Guava SDK
   post-call SMS support).
5. *Optional, time permitting:* a separate new incident reported while a
   claim already exists — this is the sprint's planned KPI/extensibility
   measurement, not proven until actually built and measured.

**Policyholder verification (no OTP):** match caller-stated policy number +
policyholder name + registered mobile against the same mock API policy
record. A simplified demo check, not proof of mobile ownership. Already
implemented in `workflows/policy_lookup.py`.

**Explicitly deferred — do not build without a new authorized step:**
representative/third-party verification and intake, editing or cancelling a
submitted claim, filing with another insurer, deliberate pause-and-resume,
forgotten-policy-number recovery, evidence collection/portal building.
Portal guidance ("upload photos later through your GEICO claims portal") is
allowed to say; building that portal is not.

**One action per checklist evaluation:** every step picks exactly one next
action, targeting exactly one adapter (Guava or API, never both); an
internal state check calls neither. This is one of the two explicit test
cases in `Scenarios.docx` and already governs how `coordinator.py` and
`workflows/*.py` are structured.

## Reference docs (ranked when they conflict)

1. `reference-docs/Guava FDE — Take-Home Project.pdf` — the assignment brief.
2. `reference-docs/GEICO Voice Agent Workflow.docx` — full target behavior.
3. `reference-docs/GEICO Voice Agent - Build Sprint Plan.docx` +
   `reference-docs/Scenarios.docx` — the current execution plan and its test
   cases. These supersede the earlier build-order doc.
4. This file and `codex-harness.md` — process and current state.

## Process

Build process, roles (Claude plans/reviews, Codex implements), and the
per-step review loop live in `codex-harness.md` at the repo root (gitignored,
local only — not part of this file).
