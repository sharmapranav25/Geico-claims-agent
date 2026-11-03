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
