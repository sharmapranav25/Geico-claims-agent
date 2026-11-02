# Prompt 1 — ordered checklist baseline

Start: 2026-11-02 14:58:15 PST (recorded with `date` before editing; retained across clarification pauses).

Baseline only. The KPI is unproven until a measured addition is completed and tested.

## Change scope

- Coordination: added `src/agent/checklist.py` with the specified 11 ordered checks; `coordinator.py` now registers all eight completion tasks and sends each completion through the checklist; `call_state.py` records completed task IDs.
- Workflows: `policy_lookup.py`, `claim_intake.py`, and `assistance.py` expose `TASK_IDS` and register only validators. Their action builders, task IDs, objectives, spoken lines and existing log messages remain unchanged. Policy-number normalization and its field use the requested digit-only behavior and wording.
- Tests: updated seed numbers/names, spoken-number cases and registration setup; added `tests/test_checklist.py` for ordering, first-open selection, priority, no adapter effects during evaluation, waiting, an inserted check, happy/outage action logs, adapter isolation and AST boundaries.
- Unrelated changes: none. No edits to `guava_adapter.py`, the mock API source, or the single-path expected task lists. No commits, pushes, phone calls or golden/model tests.

## Existing test changes

All data edits replace old seeded identities/numbers with the new ones, retaining equivalent case, punctuation, word-order, minor-spelling and mismatch examples. Shared fixture changes also affect consumers whose test bodies were unchanged.

| File | Existing tests/helpers changed and reason |
| --- | --- |
| `tests/test_action_loop.py` | `PROFILE`, `call`, `test_verified_path_records_fetch_before_announcement`: seed data. `test_completing_twice_does_not_repeat_actions`: shared completion setup. `test_request_action_is_gated_and_runs_once`: new completion entry point. |
| `tests/test_api_adapter.py` | `test_list_and_read_seeded_policy`, `test_write_rejects_dict_or_profile`, `test_live_draft_round_trip_and_submit`, `test_raw_forbidden_field_and_adapter_error_mapping`, `test_live_invalid_vehicle_is_wrapped`, `test_live_submission_reference_replay_adds_one_claim`, `test_submission_body_only_when_reference_given`: seed numbers/names and corresponding expected URLs only. |
| `tests/test_api_outage.py` | `PROFILE`, `call_fields`: seed data only. |
| `tests/test_api_reachability.py` | `local_api`, `test_reachable_api_returns_typed_result`, `test_connection_refused_raises_unavailable`, `test_nonresponding_api_times_out_within_bound`, `test_bad_response_never_returns_result`: seed data/URLs. `test_unavailable_api_does_not_escape_workflow_or_verify`: data and shared registration setup. |
| `tests/test_call_paths.py` | `IDENTITY`/scenario data, `run_scenario`, `test_call_path`: new seed identities, spoken number and protected-name assertion data. These model tests were not run. File retained. |
| `tests/test_claim_intake.py` | `PROFILE`, `FIELDS`, `test_complete_saves_draft_with_chosen_vehicle_and_never_submits`: seed data. `registered`: shared completion setup. |
| `tests/test_coordinator.py` | `profile`, `test_question_answers_only_from_state`, `test_verified_policy_context_precedes_request_task`: seed data. `test_claim_cannot_start_without_verification`, `test_verified_caller_routes_on_recorded_request`: new completion entry point. |
| `tests/test_golden_evals.py` | Scenario data and `test_golden` policy-number extraction regex: new seed format. Golden tests were not run. |
| `tests/test_guava_adapter.py` | `test_bridge_adds_info`, `test_policy_validation_failure_retries_without_completion`: policy-number data. `test_policy_callbacks_normalize_through_sdk_dispatch`: new names and specified four spoken-number cases. |
| `tests/test_policy_lookup.py` | `test_live_policy_number_validation`, `test_live_identity_matching`: seed data. `test_registered_completion_branches_and_fields`: data, shared registration setup and the explicitly approved registration-inventory assertion change. Validator assertion remains exactly `{'policy_number'}`. |
| `tests/test_policy_lookup_offline.py` | `register`: shared registration setup. `api`, `PROFILE`, `test_verification_with_fake_bridge`, `test_api_failures_do_not_verify`, `test_outcome_logs_exclude_caller_values`, `test_name_allows_small_differences`, `test_different_name_does_not_match`, `test_phone_allows_formatting_only`, `test_verification_uses_sensitive_digit_sequence_phone`, `test_phone_digits_must_match`: identity/number data. `test_normalize_spoken_policy_number`, `test_invalid_policy_number_is_not_repaired`, `test_policy_digits_are_never_repaired`: digit-only normal/not-repaired examples. |
| `tests/test_single_path.py` | `run_call`: caller-name data only. All expected task lists and behavior assertions remain unchanged. |

Only registration assertion changed: `test_registered_completion_branches_and_fields` now expects `set(coordinator.COMPLETION_TASKS)` instead of `{policy_lookup.TASK_ID}`, as explicitly approved. No other registration-inventory assertion required a change. No behavior assertion was weakened or removed.

## Regression results and actual command output

1. Mock API repo: `.venv/bin/python -m pytest test_app.py` exited 0.

```text
============================= test session starts ==============================
platform darwin -- Python 3.12.4, pytest-9.2.0, pluggy-1.7.0
rootdir: /Users/pranavsharma/Files/personal projects/GEICO Claims Mock API
plugins: anyio-4.16.0
collected 42 items

test_app.py ..........................................                   [100%]
======================== 42 passed, 2 warnings in 0.48s ========================
```

Warnings: StarletteDeprecationWarning for using httpx with starlette.testclient (suggests httpx2); PytestCacheWarning because the sandbox could not write the mock repo's `.pytest_cache/v/cache/nodeids` (Operation not permitted). Neither failed a test.

2. Mock API repo: `.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000`.

The existing listener (PID 69952, cwd verified as the mock API repo) was stopped and restarted as requested. Initial sandboxed `kill` failed with `operation not permitted`; escalation succeeded. A read-only `ps` inspection was also denied; `lsof` established the process cwd instead.

First startup exited 3:

```text
INFO:     Started server process [81175]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
ERROR:    [Errno 1] error while attempting to bind on address ('127.0.0.1', 8000): operation not permitted
INFO:     Waiting for application shutdown.
INFO:     Application shutdown complete.
```

Escalated startup succeeded:

```text
INFO:     Started server process [81260]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

PID 81260 was stopped after the suite; `lsof -nP -iTCP:8000 -sTCP:LISTEN` then returned no listener. No mock API code was changed.

3. Agent repo: `uv run pytest`.

First sandboxed attempt exited 2 before running tests:

```text
error: Could not acquire lock
  cause: Could not create temporary file
  cause: Operation not permitted (os error 1) at path "/Users/pranavsharma/.cache/uv/.tmpEqpSWu"
```

Escalated rerun exited 0:

```text
collected 2317 items / 22 deselected / 2295 selected
===================== 2295 passed, 22 deselected in 7.60s ======================
```

No test failures occurred. The 22 deselected tests are excluded by the configured `not agent_sim and not golden` selection. `git diff --check` passed. The full agent test stdout is supplied with the handoff.

## Coverage and limits

“Built now” below means the stated routing/gate only, not the full Scenarios behavior. Empty checks are not covered merely because the order or happy-path test visits them. Test names refer to the repository's tests.

| Check / safeguard | Status and implementation | Evidence / remaining gap |
| --- | --- | --- |
| 1 No immediate safety concern | Empty until Prompt 3: `check_safety` returns PASS. Existing exception: `coordinator.on_immediate_danger_or_other` sends instructions outside Action.run. | No safety pause/resume coverage. `test_placeholder_content_unchanged` preserves existing wording only; it does not cover this check. |
| 2 Clear communication | Empty until Prompt 3: `check_communication` returns PASS. | No communication-barrier coverage. |
| 3 Clear intent and role | Built now: completion gate in `check_intent_and_role`; already enforced for the existing request conversation by `coordinator.on_verified`. Changed intent and unsupported roles await Prompt 3; facts await Prompt 2. | `test_completed_handler_waits_without_restarting`, `test_verified_policy_context_precedes_request_task`; no full role/change coverage. |
| 4 One task handled at a time | Empty until Prompt 3: `check_one_task` returns PASS. The one-action evaluation mechanism is built now, a separate property. | No multiple-request/incident handling coverage. `test_pass_continues_and_first_open_check_stops` and `test_priority_leaves_lower_check_pending` cover selection only. |
| 5 Within supported scope | Built now: routing in `check_scope`; already enforced for own-loss vs other request by `coordinator.next_request_action` and assistance `next_*` functions. Existing exception: `on_intent_b` directly sets a task outside Action.run until Prompt 3. | `test_every_decision_selects_one_path`, `test_verified_caller_routes_on_recorded_request`, `test_outage_check_action_sequence`. Unsupported-role/changed-intent behavior remains incomplete; direct intent dispatch is an exception, not checklist coverage. |
| 6 Required verification confirmed | Built now: completion/wait gate; already enforced by `policy_lookup.next_action` and `check_identity_match`. | `test_verification_with_fake_bridge`, `test_api_failures_do_not_verify`, `test_completed_handler_waits_without_restarting`, `test_happy_path_check_action_sequence`. Read-back/retries/spelling await Prompt 2. |
| 7 Required information available | Built now: collection/review gate; existing fields/date validation and save gating in `claim_intake.start`, `validate_occurred_at`, `next_action`. | `test_complete_saves_draft_with_chosen_vehicle_and_never_submits`, `test_unverified_call_saves_nothing`, `test_occurred_at_validation`. Full required/optional gap handling awaits Prompt 2. |
| 8 Consistent information | Built now: review-completion gate; existing read-back in `claim_intake._start_review`. | `test_review_speaks_every_field_without_claiming_submission`. Conflict resolution/corrections await Prompt 2; no claim of full consistency coverage. |
| 9 Permission given | Built now: routing; already enforced by `claim_intake.next_submit_action` against the current reviewed values and explicit approve. | `test_no_explicit_approval_does_not_submit`, `test_missing_or_changed_review_cannot_submit`, `test_unverified_state_cannot_submit`. Full correction/withdrawal lifecycle awaits later safeguards work in Prompt 2. |
| 10 Clear successful result | Built now: result routing; already enforced by `claim_intake.next_submit_action` and `assistance.next_callback_action`. Existing exception: save_callback remains a local file action touching neither adapter; this is not API-backed callback coverage. | `test_submission_failure_never_announces_claim`, `test_callback_write_precedes_confirmation`, `test_each_action_touches_at_most_one_adapter`. Uncertain submission status/retry handling remains deferred; not covered. |
| 11 Explain the result and close | Built now: submitted-task completion gate; already enforced by `claim_intake._announce_claim` and `next_close_action`. | `test_submitted_call_closes_once_only_when_done`, `test_happy_path_check_action_sequence`. No real telephony verification. |
| Call stays connected | No continuity/recovery implementation built now. Existing close gate in `claim_intake.next_close_action` only. Dropped-call recovery/SMS/resume explicitly out of scope, not promised for a later prompt. | `test_submitted_call_closes_once_only_when_done` covers deliberate close only. No disconnect/reconciliation coverage. |
| No coverage, fault or payment decision/promise | Existing workflow instructions in `claim_intake.start` and `_announce_claim`; not a deterministic semantic enforcement check. | No dedicated semantic coverage in this offline run; model/golden tests not run. |
| No claim number/save/SMS/callback claimed without evidence | Already enforced for current programmed announcements by `claim_intake.next_action`, `next_submit_action`, `coordinator.on_question`, `assistance.next_callback_action`. Local callback is the explicit existing exception to adapter-only actions. | `test_failed_save_is_not_reported_as_saved`, `test_submission_failure_never_announces_claim`, `test_question_answers_only_from_state`, `test_callback_write_precedes_confirmation`. Does not prove arbitrary model speech; known live callback issue remains. SMS is out of scope. |
| No duplicate filing | Already enforced for sequential repeated completion in `claim_intake.next_submit_action` using stored results and submission reference; broader protection is incomplete. | `test_approve_submits_once_with_current_review_and_real_number`, `test_live_submission_reference_replay_adds_one_claim`. Concurrent completion, uncertain result and returning-call duplicates are not proven; claim-already-exists work awaits Prompt 4. |
| Nothing filed without approval of current information | Already enforced in `claim_intake.next_submit_action`: explicit approve plus equality with reviewed fields. | `test_no_explicit_approval_does_not_submit`, `test_missing_or_changed_review_cannot_submit`. Full editable-facts/correction lifecycle awaits Prompt 2. |

## Known limitations and explicit exceptions

- `on_intent_b`'s set_task and `on_immediate_danger_or_other`'s send_instruction remain outside Action.run by explicit approval. Prompt 3 moves these under the checklist; the structure test does not require that migration now.
- `save_callback` remains target `local`, writing the gitignored outage callback file by Pranav's decision. It touches neither adapter. The isolation test allows local actions and verifies they touch neither; it does not present this as API-backed callback coverage.
- Checks 1, 2 and 4 remain empty. Other checks retain the limited existing behavior, not future facts, verification, role, safety or uncertainty handling.
- Existing validators remain SDK-managed. Evaluation selects actions without bridge commands/API calls, but existing workflow builders may update local state (for example verified_profile or approval/reference state); they were deliberately preserved.
- The harness's known live callback issue remains: model conversation may stick or speak a premature callback claim even though the local write is code-gated. Offline tests do not establish speech/telephony behavior.
- No new concurrent-event locking, uncertain-submission reconciliation, dropped-call recovery or cross-call duplicate protection was added.
- The inserted-check test demonstrates the extension point mechanically. It is not a separately implemented, timed feature addition and does not prove the KPI.

## Future measured addition recipe

1. Record a new start time and define the situation and expected behavior.
2. Write its workflow, exposing TASK_IDS and action-building functions; keep SDK calls behind the bridge and API calls in the workflow's API actions.
3. Add its task IDs to coordinator.COMPLETION_TASKS and add or extend an ordered check.
4. Register any needed validators, then drive the addition through registered completion handlers; check priority, waiting, adapter isolation and unchanged existing paths.
5. Run the required regressions and record end time, files/components changed and unrelated changes. Only a completed, tested, measured addition can be assessed as KPI evidence.

Blockers at completion: none. Clarifications approved the two existing exceptions and the one completion-inventory assertion change. No behavior assertion changes were needed.

End: 2026-11-02 15:02:56 PST (date command; includes clarification pauses).

# Prompt 2 — facts, identity, review and uncertain submission

Start: 2026-11-02 15:13:20 PST (original `date` reading, retained across clarification pauses).

Baseline only. The KPI is unproven until a measured addition is completed and tested.

## Scope and extension impact

- Mock API repository, branch `submission-lookup`: changed `app.py`, `test_app.py`, and `README.md`. Added a locked, policy-scoped by-reference claim lookup returning a deep copy or 404, with found/missing/cross-policy tests and one endpoint-table row. Left untracked `terminal-commands.md` alone.
- Agent repository, branch `facts-and-verification`: changed `api_adapter.py`, `call_state.py`, `checklist.py`, `coordinator.py`, `workflows/policy_lookup.py`, `workflows/assistance.py`, `workflows/claim_intake.py`, `pyproject.toml`, tests and this log. Added `verification.py`, `workflows/identity.py`, `workflows/review_claim.py`, `workflows/submit_claim.py`, and `tests/test_call_state.py`, `tests/test_identity.py`, `tests/test_verification.py`. Deleted `tests/test_call_paths.py` and removed the agent_sim marker as explicitly requested.
- Facts live per call as a tuple. Identity captures volunteered facts, reads identity back, and retries with spelling after a mismatch. Claim intake owns later claim-fact writes, review captures corrections/additions, and submission checks uncertain results before one same-reference retry.
- New checklist checks: **0**. Existing check functions changed: **7** (3, 6, 7, 8, 9, 10, 11). Check 5's routing logic is unchanged; checks 1, 2 and 4 only regain their Prompt 3 comments. The new behavior lives in workflows and the existing check extension points; coordination gains the specified capture dispatch and duplicate-completion guard.
- Unrelated changes: none intended. No SDK adapter changes, branch switches, commits, pushes, phone calls or golden/model execution. Existing intent-command and local callback exceptions remain.

## Accepted clarifications

- Exact identity objectives retain `Never use the phrase 'against the policy'.` Privacy tests remove that exact sentence before testing objectives, while testing Say lines without an exclusion.
- Intake omits correction fields too when every CLAIM_FIELD exists; otherwise correction fields accompany the remaining fields. Both cases have tests.
- The completion guard skips mark/capture for an already-completed task but always reevaluates the checklist.
- The duplicate-review-add test was explicitly withdrawn. Replacement tests verify duplicate approve submits once, one add appends once/counts once, and duplicate verify-policy selects start_confirm once.

## Commands and actual results

1. Mock API: `.venv/bin/python -m pytest test_app.py`, exit 0.

```text
============================= test session starts ==============================
platform darwin -- Python 3.12.4, pytest-9.2.0, pluggy-1.7.0
rootdir: /Users/pranavsharma/Files/personal projects/GEICO Claims Mock API
plugins: anyio-4.16.0
collected 44 items

test_app.py ............................................                 [100%]
======================== 44 passed, 2 warnings in 0.77s ========================
```

Warnings: StarletteDeprecationWarning for httpx with starlette.testclient (suggesting httpx2); PytestCacheWarning because the sandbox could not write `.pytest_cache/v/cache/nodeids` in the mock repo. No API test failures.

2. Mock API: `.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000`. No existing listener was present, so a fresh process loaded the updated endpoint. Startup used sandbox escalation for localhost binding.

```text
INFO:     Started server process [99135]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Stopped PID 99135 after testing; the subsequent port-8000 lsof check returned no listener.

3. Agent: `uv run pytest`, with escalation for cache access and localhost tests.

First run, exit 1:

```text
collected 2373 items / 18 deselected / 2355 selected
=============== 385 failed, 1970 passed, 18 deselected in 9.55s ================
```

Failure causes:

- 384 single-path matrix cases retained the old assertion that action names never repeat during an entire call. Prompt 2 explicitly clears and repeats identity actions on retries. The test now asserts that a result name is absent from state when recorded, non-retry names remain unique, and retry-action counts respect the specified limit. Per-callback duplicate checks remain intact.
- `test_verified_policy_context_precedes_request_task` expected only add_info/set_task in a wrapped bridge's call log. The specified `state_for` lookup adds get_session_id between them. Updated that exact order expectation.

Second run, exit 0:

```text
===================== 2355 passed, 18 deselected in 7.61s ======================
```

After adding the eight requested matrix branches, exit 0:

```text
===================== 2363 passed, 18 deselected in 7.62s ======================
```

Final run after punctuation and failure-privacy coverage, exit 0:

```text
===================== 2367 passed, 18 deselected in 7.59s ======================
```

The 18 golden scenarios were deselected, not executed. A subsequent golden-only driver adjustment excludes the exact identity read-back from “repeated identity question” counts; it was compiled, not run. Python compilation and `git diff --check` pass in the final working tree. Full initial-failure and final-run stdout accompany the handoff.

## Requirement coverage

Passing tests establish the stated deterministic behavior, not arbitrary model compliance or live speech performance.

| Quoted requirement / direction | Enforcing code | Passing evidence and limits |
| --- | --- | --- |
| Step 4: “Failed verification discloses nothing.” | `identity.next_action`, `_retry_or_fail`, `_announce_mismatch`: never announce stored answers on failure; only success sets verified_profile. | `test_retries_limit_and_privacy`, `test_fetch_outage_announces_once`, existing fake/live identity matching tests. Code-owned Say/objective privacy covered; arbitrary generated speech is not proven. |
| Step 4: “Required and optional gaps differ.” | `verification.missing_required`, `claim_intake.next_action`; missing loss_type remains None. | `test_skipped_required_fact_never_saves`, `test_optional_loss_type_stays_none`, `test_dates`. |
| Step 4: “Corrections void approval.” | `CallState.set_fact`, `claim_intake.apply_correction`, `apply_addition`. | `test_fact_replacement_and_approval`, `test_review_changes_and_additions`; actual changed values clear approval, unchanged values retain it as specified. |
| Step 4: “Submission timeouts remain unknown; status is checked using the existing reference before a safe retry.” | `submit_claim.next_submit_action`, `submitted_claim`, `submission_uncertain`; API `get_claim_by_reference`. | `test_uncertain_submission_checks_before_retry`, `test_timeout_checks_submission_in_check_ten`, `test_live_claim_by_reference`, mock lookup tests. |
| Check 7: optional details stay unknown, do not block filing, no invented value. | `claim_intake.claim_facts`/`next_action`, `review_claim.review_line`: None saved and “not provided” read back. | `test_optional_loss_type_stays_none`; model extraction accuracy is not established. |
| Check 7: required fact remains missing after asking → block submission. | `verification.missing_required`, `claim_intake.next_action` routes to announce_missing before draft save. | `test_skipped_required_fact_never_saves` covers every required key. Asking before skipping is a field instruction only, not independently enforced by code. |
| Check 7: explain what is needed and offer assistance; describe saved intake only with evidence, no portal-completion promise. | `claim_intake._announce_missing` names missing facts and says nothing submitted; specified objective disallows portal-completion promises. | Missing-details tests cover blocked save and task selection. **Partial:** the exact requested implementation does not offer assistance here. No automatic assistance route was added. Broader model promises are instruction-only. |
| Check 8: correction updates information, invalidates earlier approval, repeats affected checks. | `apply_correction`/`apply_addition` update facts and clear specified save/review results and review completion; checklist reevaluates. | `test_review_changes_and_additions`, `test_correction_repeats_save_and_review`, `test_corrections_stop_at_limit`. |
| Check 8: read back revised information and obtain fresh approval. | `review_claim.start_review`, `check_consistency`, `submit_claim.next_submit_action`. | `test_review_changes_and_additions` checks new read-back before a subsequent approve; `test_missing_or_changed_review_cannot_submit` blocks stale snapshots. |
| Check 9: approval not withdrawn and current information unchanged when action happens; enforce required checks. | `verification.approval_is_valid` requires approve plus equality with reviewed data; checklist gates verification, information and review before submit. | `test_approval`, `test_no_explicit_approval_does_not_submit`, `test_missing_or_changed_review_cannot_submit`, `test_unverified_state_cannot_submit`. Approval/decline extraction is model-provided; an independent asynchronous withdrawal channel is not implemented. |
| Check 10: status lookup with the same reference before another filing attempt. | `submit_claim.next_submit_action` checks once, retries only on PolicyNotFoundError, reusing the existing reference. | `test_uncertain_submission_checks_before_retry` asserts API call order and reference equality. |
| Check 10: prevent new filing while earlier result unresolved. | Stored submit/check/retry results and announce guard prevent another attempt after unresolved status announcement. | `test_uncertain_submission_checks_before_retry` repeats approve and verifies call counts; retry failure stays uncertain. Returning calls/process restarts remain outside this protection. |
| Check 10: explain known status, no confirmed success/failure while uncertain. | `_announce_status_unknown`; `coordinator.on_question` uses submitted_claim/submission_uncertain. | `test_uncertain_submission_checks_before_retry`, `test_question_after_uncertain_submission`; programmed answers covered, arbitrary model speech not proven. |
| Direction: record volunteered facts, ask only for missing facts, verify before use. | `identity.capture` captures optional facts; verification gate precedes claim intake; `claim_intake.start` omits known fields. | `test_volunteered_facts_survive_empty_retry`, `test_existing_facts_omit_fields_but_keep_correction_fields`, `test_all_facts_present_has_no_fields_and_advances`, `test_volunteered_request_skips_start_request`. Interpreting free speech is model-driven and unverified by offline tests. |
| Direction: identity read-back, retry and spelling after mismatch. | `identity._confirm_identity`, `_retry_or_fail`, pure `verification.identity_matches`. | `test_readback_and_duplicate_completion_fetch_once`, `test_retries_limit_and_privacy`, `test_spelling_persists_after_no`, `test_identity_matches`. Existing policy-existence validator still performs its preliminary API lookup before read-back; identity matching/fetch action waits for yes. |
| Direction: ask whether caller wants to add anything; file only on explicit approval. | `review_claim.review_line`/`capture`, `verification.approval_is_valid`, submission gate. | `test_review_speaks_every_field_without_claiming_submission`, `test_review_changes_and_additions`, `test_no_explicit_approval_does_not_submit`. Distinguishing “no more to add” from filing approval is instructed to the model; golden scenarios were written but not run. |
| One-action rule: “Each checklist item selects and performs only one next action per evaluation. It does not launch multiple actions together.” | `checklist.evaluate` selects one action; coordinator/action_loop records it before reevaluation. | Existing first-open/priority/isolation tests, updated matrix and new branch matrix, correction/timeout checklist sequences. Existing direct intent commands and local callback action remain explicit exceptions. |

## Known limitations

- A duplicate review-claim callback arriving after the review has restarted would be treated as a fresh answer. The SDK dispatches one completion per TaskCompletedEvent, the corrections limit bounds it, and the read-back shows the caller any change. This is the explicitly accepted limitation; no duplicate-answer detection was added.
- The guard prevents repeated capture for tasks still marked complete. Specified retry/correction paths clear completion so subsequent task instances can complete.
- Existing `on_intent_b` and `on_immediate_danger_or_other` commands remain outside Action.run until Prompt 3. Outage save_callback remains a local action touching neither adapter.
- Safety, communication, changed intent, multiple requests, unsupported callers and requests for a human remain deferred. Checks 1, 2 and 4 are placeholders, not covered implementations.
- No SMS, resume, saved intake on disk, post-verification identity edits, submitted-claim editing, or existing-claim handling. The backend remains in memory; reference reconciliation does not survive server restart. A returning caller can still duplicate a prior report.
- No new concurrency control for call-state event processing was added. Sequential duplicate completion and backend reference replay are covered; concurrent event behavior is not established.
- Explicit missing-detail assistance is not implemented by the exact requested missing-detail action. Asking/volunteered extraction, retry wording compliance, optional-field skipping, and semantic approval interpretation depend partly on model instructions. Empty-field task advancement and live telephony behavior are not proven by offline tests.
- Existing live callback conversation limitations remain. Golden scenarios and phone calls were not run.
- No new checklist check was required, but this coordinated foundational change is not a separately measured feature addition proving the KPI.

## Existing test change inventory

The following names are every existing test/helper whose AST changed, grouped by file. Import-only moves accompany these changes. New tests are additional coverage, not replacements for unrelated assertions.

- `tests/test_action_loop.py`: Moved identity rules/actions, seeded confirmed identity and fact snapshots, and adjusted the pre-confirmation duplicate-completion expectation; action sequencing/isolation assertions retained. Existing names: `test_policy_evaluation_sends_no_commands_or_api_calls`, `test_verified_path_records_fetch_before_announcement`, `test_policy_actions_call_only_their_target`, `test_failed_fetch_records_exception_then_announces`, `test_claim_save_records_result_before_announcement`, `test_completing_twice_does_not_repeat_actions`, `test_start_call_records_verification_once`.

- `tests/test_api_adapter.py`: No existing test body changed; added live by-reference lookup coverage using policy 1015. Existing names: none.

- `tests/test_api_outage.py`: Moved identity task/start names and inserted confirmation before lookup-outage completion; callback expectations unchanged. Existing names: `test_up_start_has_no_say`, `test_outage_validator_allows_assistance_but_never_verifies`.

- `tests/test_api_reachability.py`: FakeBridge accepts Say, identity start moved, and confirmation precedes the outage announcement. Existing names: `FakeBridge`, `test_unavailable_api_does_not_escape_workflow_or_verify`.

- `tests/test_checklist.py`: Updated workflow ownership/structure rules, happy-path confirmation action, waiting task and shared flow setup; added volunteered/correction/timeout sequences. Existing names: `flow`, `happy_path`, `test_completed_handler_waits_without_restarting`, `test_happy_path_check_action_sequence`, `test_source_boundaries_and_completion_inventory`.

- `tests/test_claim_intake.py`: Moved review/submit/date names; state-backed facts and start signatures; optional blank-date semantics; correction snapshots and uncertain-status expectations; FakeBridge supports recorded_facts info. Existing names: `FakeBridge`, `registered`, `test_start_offers_only_this_policys_vehicles`, `test_occurred_at_rejects_future_with_fixed_now`, `test_single_vehicle_is_used_without_a_field`, `test_collection_and_review_completion_criteria`, `test_review_line_strips_trailing_periods_and_spaces`, `test_occurred_at_validation`, `test_review_speaks_every_field_without_claiming_submission`, `test_no_explicit_approval_does_not_submit`, `test_approve_submits_once_with_current_review_and_real_number`, `test_missing_or_changed_review_cannot_submit`, `test_unverified_state_cannot_submit`, `test_submission_failure_never_announces_claim`, `test_submitted_call_closes_once_only_when_done`.

- `tests/test_coordinator.py`: Identity optional-field inventory and required get_session_id read in on_verified; new volunteered-request and uncertainty question tests added. Existing names: `test_call_starts_in_verification`, `test_verified_policy_context_precedes_request_task`.

- `tests/test_golden_evals.py`: Updated scenarios, occurrence-aware gated driver and automatic identity confirmation; read-back excluded from repeated identity-question counts. Not run. Existing names: `Scenario`, `test_golden`, `run_scenario`.

- `tests/test_guava_adapter.py`: Moved identity start/task names, dispatched confirmation, expected first mismatch retry, and updated exact validator failure wording. Existing names: `test_policy_callbacks_normalize_through_sdk_dispatch`, `test_policy_validation_failure_retries_without_completion`.

- `tests/test_policy_lookup.py`: Moved matching/identity names, confirmation and retry-limit setup, optional fields, prescribed validator wording and mismatch Say line. Existing names: `test_live_policy_number_validation`, `test_live_identity_matching`, `test_registered_completion_branches_and_fields`.

- `tests/test_policy_lookup_offline.py`: Moved identity and matching ownership, FakeBridge Say support, confirmed/limited retry setup, optional identity fields and completion logger ownership. Existing names: `FakeBridge`, `register`, `test_verification_with_fake_bridge`, `test_api_failures_do_not_verify`, `test_outcome_logs_exclude_caller_values`, `test_name_allows_small_differences`, `test_different_name_does_not_match`, `test_phone_allows_formatting_only`, `test_verification_uses_sensitive_digit_sequence_phone`, `test_phone_digits_must_match`.

- `tests/test_single_path.py`: Inserted confirm-identity and bounded retries, delayed request_type until verification, tracked recorded_facts add_info, allowed explicitly cleared action names with bounded counts, and seeded pure evaluators with facts/confirmation. Existing names: `run_call`, `test_every_decision_selects_one_path`, `test_evaluators_are_repeatable_without_actions`.

- `tests/test_call_paths.py`: deleted in full as explicitly requested; all its agent_sim scenarios retired in favor of golden scenarios.
- Mock API `test_app.py`: existing tests unchanged; added `test_claim_by_reference_found_and_isolated` and `test_claim_by_reference_missing`.

Blockers at completion: none. The declared limitations above remain outside this implementation.

End: 2026-11-02 15:23:27 PST (date command; elapsed interval includes clarification pauses).
