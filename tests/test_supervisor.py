"""Deterministic supervisor signals, live facts, settling and closeout races."""

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import api_adapter, checklist, coordinator, supervisor
from agent.call_state import ActionResult, InMemoryStateStore
from agent.workflows import assistance, claim_intake, identity, review_claim, safety
from supervisor_helpers import after_safety, safe_generate, wait_closeout
from test_api_outage import FakeBridge, PROFILE, call_fields, adapter

SAFE = {'danger': False, 'role': 'unverified', 'multiple_incidents': False, 'inconsistent': False}


def until(check):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.001)
    assert check(), 'Background result did not arrive'


@pytest.fixture
def flow(monkeypatch):
    monkeypatch.setattr(coordinator, 'STORE', InMemoryStateStore())
    monkeypatch.setattr(supervisor, 'SETTLE_SECONDS', 0.015)
    monkeypatch.setattr(supervisor, 'TIMEOUT_SECONDS', 0.05)
    client = Mock()
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    bridge, call = FakeBridge(), call_fields()
    state = after_safety(coordinator.state_for(call, bridge))
    state.record(ActionResult('check_api', 'api', True))
    state.record(ActionResult('start_verification', 'guava', True))
    workers = []
    start = supervisor._start_worker

    def track(work):
        worker = start(work)
        workers.append(worker)
        return worker

    monkeypatch.setattr(supervisor, '_start_worker', track)
    result = SimpleNamespace(call=call, state=state, bridge=bridge, client=client, workers=workers)
    yield result
    with state.lock:
        state.ended = True
        for timer in state.speech_timers.values():
            timer.cancel()
    for worker in workers:
        worker.join(1)
        assert not worker.is_alive()


def join_workers(flow):
    for worker in flow.workers:
        worker.join(2)
        assert not worker.is_alive()


def classify(flow, text, answer, utterance_id='line'):
    flow.bridge.generate = Mock(return_value=json.dumps(SAFE | answer))
    coordinator.on_speech(flow.call, 'caller', text, utterance_id, flow.bridge)
    until(lambda: bool(flow.workers))
    join_workers(flow)


def collect(flow):
    flow.state.verified_profile = PROFILE
    flow.state.facts = tuple((key, flow.call[key]) for key in claim_intake.CLAIM_FIELDS)
    flow.state.mark_done(claim_intake.TASK_ID)


def test_safety_precedes_api_and_exact_question(flow):
    flow.state.completed_tasks.clear()
    flow.state.results.clear()
    coordinator.start_call(flow.call, flow.bridge)
    assert list(flow.state.results) == ['safety_question']
    flow.client.list_policies.assert_not_called()
    task, = flow.bridge.tasks
    assert task.task_id == safety.TASK_ID
    field, = task.fields
    assert field.question == safety.QUESTION
    assert field.choices == ('safe', 'not_safe')
    assert "a bare yes or no is unclear" in field.description
    assert 'Greet' in task.objective
    flow.call['safety_answer'] = 'safe'
    coordinator.on_task_complete(flow.call, safety.TASK_ID, flow.bridge)
    flow.client.list_policies.assert_called_once()
    assert flow.bridge.tasks[-1].task_id == identity.TASK_ID
    assert 'Do not greet' in flow.bridge.tasks[-1].objective


def test_bare_no_fake_recorded_not_safe_hangs_up(flow):
    supervisor.record(flow.state, 'caller', 'no', 'no')
    flow.call['safety_answer'] = 'not_safe'
    coordinator.on_task_complete(flow.call, safety.TASK_ID, flow.bridge)
    assert flow.state.stopped
    assert flow.bridge.instructions == [safety.LINE_911 + ' Nothing has been submitted.']
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    assert len(flow.bridge.instructions) == 1
    flow.client.list_policies.assert_not_called()


@pytest.mark.parametrize('text,answer,stopped,blocked', [
    ('My passenger is bleeding and needs help now', {'danger': True}, True, False),
    ("I'm calling for my mother", {'role': 'someone_else'}, False, True),
    ('My wife was driving', {}, False, False),
    ('I was injured last week but need no urgent help', {}, False, False),
    ('It is my policy', {'role': 'owner'}, False, False),
])
def test_root_classification_routes_fake_answers(flow, text, answer, stopped, blocked):
    classify(flow, text, answer)
    assert flow.state.stopped is stopped
    assert flow.state.claim_blocked is blocked
    if blocked:
        assert flow.bridge.tasks[-1].say == [checklist.ROLE_LINE]
    flow.client.submit_claim_draft.assert_not_called()
    prompt = flow.bridge.generate.call_args.args[0]
    assert 'A family member involved' in prompt and 'past injury' in prompt
    assert 'self-correction' in prompt and text in prompt


def test_danger_wins_over_role_and_nudges(flow):
    classify(flow, 'help', {'danger': True, 'role': 'someone_else', 'inconsistent': True, 'multiple_incidents': True})
    assert flow.state.stopped
    assert not flow.state.claim_blocked
    assert flow.bridge.tasks == []
    assert len(flow.bridge.instructions) == 1


def test_role_clears_access_and_only_callback_remains(flow, monkeypatch):
    flow.state.verified_profile = PROFILE
    flow.state.approved_fields = {'location': 'approved'}
    flow.state.review_decision = 'approve'
    save = Mock()
    monkeypatch.setattr(assistance, 'save_callback', save)
    classify(flow, 'for my mother', {'role': 'someone_else'})
    assert flow.state.verified_profile is flow.state.approved_fields is flow.state.review_decision is None
    coordinator.on_task_complete(flow.call, claim_intake.TASK_ID, flow.bridge)
    assert not flow.state.done(claim_intake.TASK_ID)
    coordinator.on_task_complete(flow.call, assistance.CALLBACK_OFFER_TASK_ID, flow.bridge)
    assert flow.bridge.tasks[-1].task_id == assistance.CALLBACK_TASK_ID
    coordinator.on_task_complete(flow.call, assistance.CALLBACK_TASK_ID, flow.bridge)
    save.assert_called_once()
    flow.client.submit_claim_draft.assert_not_called()


@pytest.mark.parametrize('uncertain', [False, True])
def test_role_after_filing_or_uncertainty_preserves_access(flow, uncertain):
    flow.state.verified_profile = PROFILE
    flow.state.approved_fields = {'location': 'approved'}
    flow.state.review_decision = 'approve'
    flow.state.record(ActionResult('submit_claim', 'api', not uncertain,
        api_adapter.APIUnavailableError('timeout') if uncertain else SimpleNamespace(claim_number='claim')))
    classify(flow, 'for my mother', {'role': 'someone_else'})
    assert not flow.state.claim_blocked
    assert flow.state.verified_profile is PROFILE
    assert flow.state.approved_fields == {'location': 'approved'}
    assert flow.state.review_decision == 'approve'
    assert flow.bridge.tasks == []


@pytest.mark.parametrize('status,line', [('filed', 'Your claim has been submitted.'),
    ('uncertain', "I can't confirm whether your claim was filed."), ('failed', 'Nothing has been submitted.')])
def test_danger_ending_uses_real_status(flow, status, line):
    data = SimpleNamespace(claim_number='claim') if status == 'filed' else (
        api_adapter.APIUnavailableError('timeout') if status == 'uncertain' else api_adapter.ClaimValidationError('rejected', 400))
    flow.state.record(ActionResult('submit_claim', 'api', status == 'filed', data))
    classify(flow, 'urgent help', {'danger': True})
    assert flow.bridge.instructions == [safety.LINE_911 + ' ' + line]


def test_record_revisions_and_agent_context(flow):
    flow.bridge.generate = Mock(return_value=json.dumps(SAFE))
    coordinator.on_speech(flow.call, 'agent', 'Does anyone need urgent help?', None, flow.bridge)
    coordinator.on_speech(flow.call, 'caller', 'yes', 'same', flow.bridge)
    coordinator.on_speech(flow.call, 'caller', 'no', 'same', flow.bridge)
    until(lambda: bool(flow.workers))
    join_workers(flow)
    assert flow.state.lines == [('agent', 'Does anyone need urgent help?', None), ('caller', 'no', 'same')]
    flow.bridge.generate.assert_called_once()
    assert 'Does anyone need urgent help?' in flow.bridge.generate.call_args.args[0]
    coordinator.on_speech(flow.call, 'caller', 'no', 'same', flow.bridge)
    until(lambda: not flow.state.speech_timers)
    assert flow.bridge.generate.call_count == 1


def test_distinct_pending_utterance_does_not_drop_danger(flow):
    def generate(prompt, json_schema):
        return json.dumps(SAFE | {'danger': 'bleeding' in prompt.split('\n')[-1]})
    flow.bridge.generate = Mock(side_effect=generate)
    coordinator.on_speech(flow.call, 'caller', 'someone is bleeding', 'first', flow.bridge)
    coordinator.on_speech(flow.call, 'caller', 'my policy', 'second', flow.bridge)
    until(lambda: flow.state.stopped)
    assert flow.bridge.instructions == [safety.LINE_911 + ' Nothing has been submitted.']


def test_answer_after_end_is_ignored_without_new_state(flow):
    entered, release = threading.Event(), threading.Event()
    def generate(prompt, json_schema):
        entered.set()
        release.wait(1)
        return json.dumps(SAFE | {'danger': True})
    flow.bridge.generate = generate
    coordinator.on_speech(flow.call, 'caller', 'danger', 'line', flow.bridge)
    assert entered.wait(1)
    coordinator.end_call(flow.call, flow.bridge)
    release.set()
    join_workers(flow)
    assert flow.state.signals == {}
    assert flow.state.session_id not in coordinator.STORE._states
    assert flow.bridge.instructions == []


@pytest.mark.parametrize('response', ['not json', '[]', '{}', '{"danger": 1}', RuntimeError('private failure')])
def test_ask_failure_is_private_and_call_continues(flow, caplog, response):
    flow.bridge.generate = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
    with caplog.at_level('INFO'):
        assert supervisor.ask(flow.bridge, 'private prompt', supervisor.LINE_SCHEMA, supervisor.check_line) is None
    assert 'Supervisor check failed' in caplog.messages
    assert 'private' not in caplog.text
    assert not flow.state.stopped


def test_timeout_discards_late_answer_and_does_not_block_speech(flow):
    release, entered = threading.Event(), threading.Event()
    def generate(prompt, json_schema):
        entered.set()
        release.wait(1)
        return json.dumps(SAFE | {'danger': True})
    flow.bridge.generate = generate
    started = time.monotonic()
    coordinator.on_speech(flow.call, 'caller', 'urgent', 'line', flow.bridge)
    assert time.monotonic() - started < 0.04
    assert entered.wait(1)
    join_workers(flow)
    assert not flow.state.stopped
    release.set()
    assert flow.state.signals == {}


def test_live_fields_baseline_and_stale_value_do_not_void_approval(flow, monkeypatch):
    changed = Mock()
    monkeypatch.setattr(supervisor, 'on_fact_changed', changed)
    identity.start(flow.call, flow.bridge, flow.state)
    flow.state.set_fact('location', 'Corrected location')
    flow.state.approved_fields = {'location': 'Corrected location'}
    coordinator.on_speech(flow.call, 'agent', 'Checking', None, flow.bridge)
    assert flow.state.fact('location') == 'Corrected location'
    assert flow.state.approved_fields == {'location': 'Corrected location'}
    changed.assert_not_called()
    flow.call['location'] = 'New location'
    coordinator.on_speech(flow.call, 'agent', 'Recorded', None, flow.bridge)
    assert flow.state.fact('location') == 'New location'
    assert flow.state.approved_fields is None
    changed.assert_called_once_with(flow.call, flow.state, flow.bridge, 'location')
    flow.state.open_task = review_claim.REVIEW_TASK_ID
    flow.call['location'] = 'Stale review field'
    claim_intake.capture_live(flow.call, flow.state, flow.bridge)
    assert flow.state.live_keys == ()
    assert flow.state.fact('location') == 'New location'


def test_live_times_validate_and_completion_closes_window(flow, monkeypatch):
    monkeypatch.setattr(supervisor, 'on_fact_changed', Mock())
    identity.start(flow.call, flow.bridge, flow.state)
    for invalid in ('tomorrow', '2999-01-01T12:00:00'):
        flow.call['stated_time'] = invalid
        claim_intake.capture_live(flow.call, flow.state, flow.bridge)
        assert flow.state.fact('occurred_at') is None
    flow.call['stated_time'] = '2026-01-01T12:00:00'
    claim_intake.capture_live(flow.call, flow.state, flow.bridge)
    assert flow.state.fact('occurred_at') == flow.call['stated_time']
    coordinator.on_task_complete(flow.call, identity.TASK_ID, flow.bridge)
    assert flow.state.live_keys == ()


def test_collection_live_keys_are_only_fields_sent(flow):
    collect(flow)
    flow.state.set_fact('location', None)
    claim_intake.start(flow.call, flow.bridge, flow.state)
    assert flow.state.live_keys == ('location', 'description')
    assert flow.state.live_seen == {key: flow.call[key] for key in flow.state.live_keys}
    join_workers(flow)


def test_root_conflicts_once_per_utterance_multiple_once_per_call(flow):
    classify(flow, 'two incidents and a contradiction', {'inconsistent': True, 'multiple_incidents': True})
    assert len(flow.bridge.instructions) == 2
    classify(flow, 'another contradiction', {'inconsistent': True, 'multiple_incidents': True}, 'second')
    until(lambda: flow.state.nudge_count == 3)
    assert flow.bridge.instructions.count(supervisor.MULTIPLE_NUDGE) == 1
    assert flow.bridge.instructions.count(supervisor.CONFLICT_NUDGE) == 2


def test_live_conflict_dedupes_fields_and_never_logs_reason(flow, caplog):
    answer = {'inconsistent': True, 'fields': ['description', 'location'], 'reason': 'private caller details'}
    flow.bridge.generate = Mock(return_value=json.dumps(answer))
    with caplog.at_level('INFO'):
        for fields in (['description', 'location'], ['location', 'description']):
            answer['fields'] = fields
            flow.bridge.generate.return_value = json.dumps(answer)
            supervisor.on_fact_changed(flow.call, flow.state, flow.bridge, 'location')
            join_workers(flow)
    assert flow.state.nudge_count == 1
    assert 'private caller details' in flow.bridge.instructions[0]
    assert 'private caller details' not in caplog.text
    assert ('description', 'location') in flow.state.raised


@pytest.mark.parametrize('involved,expected', [(False, 3), (True, 4)])
def test_description_nudges_once_and_other_actions_only_if_involved(flow, involved, expected):
    flow.state.open_task = claim_intake.TASK_ID
    def generate(prompt, json_schema):
        if json_schema == supervisor.DESCRIPTION_SCHEMA:
            return json.dumps({key: involved if key == 'other_party_involved' else False for key in json_schema['properties']})
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    for _ in range(2):
        supervisor.on_fact_changed(flow.call, flow.state, flow.bridge, 'description')
        join_workers(flow)
    assert flow.state.nudge_count == expected
    assert ('other_party_actions' in flow.state.raised) is involved
    assert not flow.state.claim_blocked


def test_closeout_order_summary_final_check_then_save(flow):
    collect(flow)
    schemas = []
    def generate(prompt, json_schema):
        schemas.append(json_schema)
        assert flow.state.lock.acquire(timeout=0.2), 'call lock held during generate'
        flow.state.lock.release()
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    wait_closeout(flow.state)
    assert schemas == [supervisor.SUMMARY_SCHEMA, supervisor.FACTS_SCHEMA]
    assert list(flow.state.results)[-4:] == ['write_summary', 'final_consistency', 'save_draft', 'start_review']
    assert flow.state.fact('incident_summary') == 'An incident was reported.'
    assert 'incident_summary' not in claim_intake.CLAIM_FIELDS
    flow.client.update_claim_draft.assert_called_once()


@pytest.mark.parametrize('pending', ['summary', 'consistency'])
@pytest.mark.parametrize('stop', ['danger', 'role'])
def test_late_closeout_after_priority_stop_changes_nothing(flow, pending, stop):
    collect(flow)
    entered, release = threading.Event(), threading.Event()
    def generate(prompt, json_schema):
        if (json_schema == supervisor.SUMMARY_SCHEMA) == (pending == 'summary'):
            entered.set()
            release.wait(1)
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    assert entered.wait(1)
    with flow.state.lock:
        flow.state.signals['danger' if stop == 'danger' else 'someone_else'] = True
        coordinator.run_checklist(flow.call, flow.state, flow.bridge)
        snapshot = (flow.state.facts, flow.state.summary_ready, flow.state.consistency_ready, dict(flow.state.results))
    release.set()
    join_workers(flow)
    assert (flow.state.facts, flow.state.summary_ready, flow.state.consistency_ready, flow.state.results) == snapshot
    flow.client.update_claim_draft.assert_not_called()
    assert flow.state.stopped if stop == 'danger' else flow.state.claim_blocked


@pytest.mark.parametrize('pending', ['summary', 'consistency'])
def test_old_closeout_answer_after_correction_is_dropped(flow, pending):
    collect(flow)
    entered, release = threading.Event(), threading.Event()
    def generate(prompt, json_schema):
        if (json_schema == supervisor.SUMMARY_SCHEMA) == (pending == 'summary'):
            entered.set()
            release.wait(1)
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    assert entered.wait(1)
    with flow.state.lock:
        claim_intake.apply_correction(flow.state, 'location', 'Corrected street')
        snapshot = flow.state.facts
    release.set()
    join_workers(flow)
    assert flow.state.facts == snapshot
    assert flow.state.closeout_generation == 1
    assert not flow.state.summary_ready and not flow.state.consistency_ready
    assert flow.state.result('write_summary') is flow.state.result('final_consistency') is None
    flow.client.update_claim_draft.assert_not_called()
    flow.bridge.generate = safe_generate
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    wait_closeout(flow.state)
    assert flow.state.summary_ready and flow.state.consistency_ready
    assert flow.client.update_claim_draft.call_args.args[1].location == 'Corrected street'


@pytest.mark.parametrize('pending', ['summary', 'consistency'])
def test_closeout_timeout_falls_back_and_reaches_save(flow, pending):
    collect(flow)
    release = threading.Event()
    def generate(prompt, json_schema):
        if (json_schema == supervisor.SUMMARY_SCHEMA) == (pending == 'summary'):
            release.wait(1)
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    try:
        coordinator.run_checklist(flow.call, flow.state, flow.bridge)
        wait_closeout(flow.state)
        flow.client.update_claim_draft.assert_called_once()
        assert flow.state.summary_ready and flow.state.consistency_ready
        if pending == 'summary':
            assert flow.state.fact('incident_summary') == flow.call['description']
    finally:
        release.set()


def test_final_conflict_only_speaks_field_labels_and_dedupes_live_conflict(flow):
    collect(flow)
    def generate(prompt, json_schema):
        if json_schema == supervisor.FACTS_SCHEMA:
            return json.dumps({'inconsistent': True, 'fields': ['location', 'occurred_at'], 'reason': 'PRIVATE REASON'})
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    wait_closeout(flow.state)
    assert flow.bridge.tasks[-1].say[0].startswith("One thing to check: the location and the date and time don't seem to match. Which is right? ")
    assert 'PRIVATE REASON' not in flow.bridge.tasks[-1].say[0]
    assert ('location', 'occurred_at') in flow.state.raised
    with flow.state.lock:
        claim_intake.apply_addition(flow.state, 'More detail')
        coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    wait_closeout(flow.state)
    assert not flow.bridge.tasks[-1].say[0].startswith('One thing to check:')
    assert flow.state.closeout_generation == 1


def test_ask_timeout_returns_none_and_late_success_is_discarded(flow):
    release = threading.Event()
    def generate(prompt, json_schema):
        release.wait(1)
        return json.dumps(SAFE)
    flow.bridge.generate = generate
    started = time.monotonic()
    try:
        assert supervisor.ask(flow.bridge, 'line', supervisor.LINE_SCHEMA, supervisor.check_line) is None
        assert time.monotonic() - started < 0.3
    finally:
        release.set()


def test_live_checks_generate_without_holding_call_lock(flow):
    flow.state.open_task = claim_intake.TASK_ID
    acquired = []
    def generate(prompt, json_schema):
        success = flow.state.lock.acquire(timeout=0.2)
        acquired.append(success)
        if success:
            flow.state.lock.release()
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    with flow.state.lock:
        supervisor.on_fact_changed(flow.call, flow.state, flow.bridge, 'description')
    join_workers(flow)
    assert acquired == [True, True]


@pytest.mark.parametrize('log_speech', [None, '', '1'])
def test_speech_hooks_delegate_without_logging_text(adapter, monkeypatch, caplog, log_speech):
    if log_speech is None:
        monkeypatch.delenv('LOG_SPEECH', raising=False)
    else:
        monkeypatch.setenv('LOG_SPEECH', log_speech)
    speech = Mock()
    monkeypatch.setattr(coordinator, 'on_speech', speech)
    call = object()
    event = SimpleNamespace(utterance='private caller details', utterance_id='line')
    with caplog.at_level('INFO'):
        adapter.on_caller_speech(call, event)
        adapter.on_agent_speech(call, event)
    assert speech.call_args_list[0].args == (call, 'caller', event.utterance, 'line', adapter.SDK_BRIDGE)
    assert speech.call_args_list[1].args == (call, 'agent', event.utterance, None, adapter.SDK_BRIDGE)
    if log_speech:
        assert caplog.messages == [f'Caller utterance: {event.utterance}', f'Agent utterance: {event.utterance}']
    else:
        assert event.utterance not in caplog.text


def test_sdk_generate_is_only_a_pass_through(adapter, monkeypatch):
    generate = Mock(return_value='{}')
    monkeypatch.setattr(adapter, 'generate', generate)
    schema = {'type': 'object'}
    assert adapter.SDKBridge.generate('prompt', schema) == '{}'
    generate.assert_called_once_with('prompt', json_schema=schema)


def test_identity_description_waits_for_collection_and_additions_capture_live(flow):
    schemas = []
    def generate(prompt, json_schema):
        schemas.append(json_schema)
        if json_schema == supervisor.DESCRIPTION_SCHEMA:
            return json.dumps({key: key != 'owner_role' for key in json_schema['properties']})
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    identity.start(flow.call, flow.bridge, flow.state)
    flow.call['description'] = 'A car hit my parked car.'
    with flow.state.lock:
        claim_intake.capture_live(flow.call, flow.state, flow.bridge)
    join_workers(flow)
    assert flow.state.fact('description') == flow.call['description']
    assert schemas == [supervisor.FACTS_SCHEMA]
    assert flow.bridge.instructions == []

    flow.state.verified_profile = PROFILE
    with flow.state.lock:
        claim_intake.start(flow.call, flow.bridge, flow.state)
    join_workers(flow)
    assert schemas.count(supervisor.DESCRIPTION_SCHEMA) == 1
    assert len(flow.bridge.instructions) == 1
    task = flow.bridge.tasks[-1]
    field = next(field for field in task.fields if field.key == 'description')
    assert field.required is False and field.question == ''
    assert field.description == (
        "The caller already described this. Update it only if they add details, combining old and new in the caller's own words. Never ask for it from scratch."
    )
    flow.call['description'] += ' I was away from the vehicle.'
    with flow.state.lock:
        claim_intake.capture_live(flow.call, flow.state, flow.bridge)
    join_workers(flow)
    assert flow.state.fact('description') == 'A car hit my parked car. I was away from the vehicle.'
    assert schemas.count(supervisor.DESCRIPTION_SCHEMA) == 2
    assert len(flow.bridge.instructions) == 1


def test_complete_collection_does_not_keep_description_or_start_nudge(flow, monkeypatch):
    collect(flow)
    check = Mock()
    monkeypatch.setattr(supervisor, 'check_description', check)
    claim_intake.start(flow.call, flow.bridge, flow.state)
    assert flow.bridge.tasks[-1].fields == []
    assert flow.state.live_keys == ()
    check.assert_not_called()


def test_descriptive_answer_after_collection_closes_is_dropped(flow):
    entered, release = threading.Event(), threading.Event()
    flow.state.open_task = claim_intake.TASK_ID
    flow.state.set_fact('description', 'An incident.')
    def generate(prompt, json_schema):
        entered.set()
        release.wait(1)
        return json.dumps({key: False for key in json_schema['properties']})
    flow.bridge.generate = generate
    supervisor.check_description(flow.call, flow.state, flow.bridge)
    assert entered.wait(1)
    with flow.state.lock:
        flow.state.open_task = review_claim.REVIEW_TASK_ID
    release.set()
    join_workers(flow)
    assert flow.state.raised == set()
    assert flow.state.pending_nudges == []
    assert flow.bridge.instructions == []


def test_final_single_field_conflict_uses_singular_wording(flow):
    collect(flow)
    def generate(prompt, json_schema):
        if json_schema == supervisor.FACTS_SCHEMA:
            return json.dumps({'inconsistent': True, 'fields': ['location'], 'reason': 'PRIVATE REASON'})
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    wait_closeout(flow.state)
    assert flow.bridge.tasks[-1].say[0].startswith("One thing to check: the location doesn't seem right. Is that correct? ")
    assert 'PRIVATE REASON' not in flow.bridge.tasks[-1].say[0]
