"""Deterministic supervisor signals, live facts, settling and closeout races."""

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import api_adapter, checklist, coordinator, supervisor, verification
from agent.call_state import ActionResult, InMemoryStateStore
from agent.workflows import assistance, claim_intake, endings, identity, review_claim, safety
from supervisor_helpers import after_safety, safe_generate, wait_closeout
from test_api_outage import FakeBridge, PROFILE, call_fields, adapter

SAFE = {'danger': False, 'role': 'unverified', 'multiple_incidents': False, 'inconsistent': False, 'wants_human': False}


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
    assert "never ask about safety again" in field.description
    assert 'Greet' in task.objective
    flow.call['safety_answer'] = 'safe'
    coordinator.on_task_complete(flow.call, safety.TASK_ID, flow.bridge)
    flow.client.list_policies.assert_called_once()
    assert flow.bridge.tasks[-1].task_id == identity.HELP_TASK_ID
    assert flow.bridge.tasks[-1].fields[0].question == identity.HELP_QUESTION
    flow.call['request_type'] = coordinator.REPORT_OWN_LOSS
    coordinator.on_task_complete(flow.call, identity.HELP_TASK_ID, flow.bridge)
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
        flow.bridge.read_script.assert_called_once_with(flow.call, checklist.ROLE_LINE)
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
    changed.assert_called_once_with(flow.call, flow.state, flow.bridge)
    flow.state.open_task = review_claim.REVIEW_TASK_ID
    flow.call['location'] = 'Stale review field'
    claim_intake.capture_live(flow.call, flow.state, flow.bridge)
    assert flow.state.live_keys == ()
    assert flow.state.fact('location') == 'New location'


def test_live_times_validate_and_completion_closes_window(flow, monkeypatch):
    monkeypatch.setattr(supervisor, 'on_fact_changed', Mock())
    flow.state.verified_profile = PROFILE
    claim_intake.start(flow.call, flow.bridge, flow.state)
    flow.state.set_fact('occurred_at', None)
    flow.state.live_keys = ('occurred_at',)
    flow.state.live_seen = {}
    for invalid in ('tomorrow', '2999-01-01T12:00:00'):
        flow.call['occurred_at'] = invalid
        claim_intake.capture_live(flow.call, flow.state, flow.bridge)
        assert flow.state.fact('occurred_at') is None
    flow.call['occurred_at'] = '2026-01-01T12:00:00'
    claim_intake.capture_live(flow.call, flow.state, flow.bridge)
    assert flow.state.fact('occurred_at') == flow.call['occurred_at']
    coordinator.on_task_complete(flow.call, claim_intake.TASK_ID, flow.bridge)
    assert flow.state.live_keys == ()


def test_collection_live_keys_are_only_fields_sent(flow):
    collect(flow)
    flow.state.set_fact('location', None)
    claim_intake.start(flow.call, flow.bridge, flow.state)
    assert flow.state.live_keys == ('location',)
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
            supervisor.on_fact_changed(flow.call, flow.state, flow.bridge)
            join_workers(flow)
    assert flow.state.nudge_count == 1
    assert 'private caller details' in flow.bridge.instructions[0]
    assert 'private caller details' not in caplog.text
    assert any(item[0] == ('description', 'location') for item in flow.state.raised)


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
    assert schemas == [supervisor.DESCRIPTION_SCHEMA, supervisor.SUMMARY_SCHEMA, supervisor.FACTS_SCHEMA]
    assert list(flow.state.results)[-4:] == ['write_summary', 'final_consistency', 'save_draft', 'start_review']
    assert flow.state.fact('incident_summary') == 'Rear-ended at a light.'
    assert 'incident_summary' not in claim_intake.CLAIM_FIELDS
    flow.client.update_claim_draft.assert_called_once()


@pytest.mark.parametrize('pending', ['summary', 'consistency'])
@pytest.mark.parametrize('stop', ['danger', 'role'])
def test_late_closeout_after_priority_stop_changes_nothing(flow, pending, stop):
    collect(flow)
    entered, release = threading.Event(), threading.Event()
    def generate(prompt, json_schema):
        if json_schema == (supervisor.SUMMARY_SCHEMA if pending == 'summary' else supervisor.FACTS_SCHEMA):
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
        if json_schema == (supervisor.SUMMARY_SCHEMA if pending == 'summary' else supervisor.FACTS_SCHEMA):
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
        if json_schema == (supervisor.SUMMARY_SCHEMA if pending == 'summary' else supervisor.FACTS_SCHEMA):
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
    assert any(item[0] == ('location', 'occurred_at') for item in flow.state.raised)
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
    acquired = []
    def generate(prompt, json_schema):
        success = flow.state.lock.acquire(timeout=0.2)
        acquired.append(success)
        if success:
            flow.state.lock.release()
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    with flow.state.lock:
        supervisor.on_fact_changed(flow.call, flow.state, flow.bridge)
    join_workers(flow)
    assert acquired == [True]


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


def test_conflict_on_a_single_fact_is_ignored(flow):
    collect(flow)
    def generate(prompt, json_schema):
        if json_schema == supervisor.FACTS_SCHEMA:
            return json.dumps({'inconsistent': True, 'fields': ['location'], 'reason': 'PRIVATE REASON'})
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    wait_closeout(flow.state)
    assert flow.bridge.tasks[-1].say[0].startswith("So here's what I have.")
    assert flow.bridge.instructions == []


def test_blocked_caller_ending_still_hangs_up(flow):
    classify(flow, "I'm calling for my mother", {'role': 'someone_else'})
    flow.call['wants_callback'] = 'no'
    coordinator.on_task_complete(flow.call, assistance.CALLBACK_OFFER_TASK_ID, flow.bridge)
    assert flow.state.ending == 'assistance_closed'
    flow.call['ending_ack'] = 'done'
    coordinator.on_task_complete(flow.call, 'assistance-closed', flow.bridge)
    flow.bridge.hang_up.assert_called_once_with(flow.call, endings.FINAL_LINES['assistance_closed'])


def test_danger_wins_over_an_open_ending(flow):
    flow.state.ending = 'declined'
    classify(flow, 'someone needs help now', {'danger': True})
    assert flow.bridge.instructions == [safety.LINE_911 + ' Nothing has been submitted.']


def test_model_proposes_loss_type_and_collection_never_asks(flow):
    flow.state.verified_profile = PROFILE
    claim_intake.start(flow.call, flow.bridge, flow.state)
    task = flow.bridge.tasks[-1]
    assert 'loss_type' not in [field.key for field in task.fields]
    assert 'Never question or comment on a date' in task.objective and 'Today is' in task.objective
    claim_intake.store_summary(flow.state, 'A summary.', 'theft')
    assert flow.state.fact('loss_type') == 'theft'


def test_caller_loss_type_correction_outlasts_the_model(flow):
    flow.state.verified_profile = PROFILE
    claim_intake.apply_correction(flow.state, 'loss_type', 'theft')
    claim_intake.store_summary(flow.state, 'A summary.', 'collision')
    assert flow.state.fact('loss_type') == 'theft'


def reach_review(flow):
    collect(flow)
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    wait_closeout(flow.state)
    assert flow.bridge.tasks[-1].task_id in review_claim.TASK_IDS


def reviews(flow):
    return [task for task in flow.bridge.tasks if task.task_id in review_claim.TASK_IDS]


def test_change_is_saved_and_read_back_while_the_task_stays_open(flow):
    reach_review(flow)
    flow.call.update(submission_decision='change', corrected_field='loss_type', corrected_value='other')
    coordinator.on_speech(flow.call, 'agent', 'Updating that.', None, flow.bridge)
    until(lambda: len(reviews(flow)) == 2)
    assert flow.state.fact('loss_type') == 'other'
    assert "type of loss down as other" in reviews(flow)[-1].say[0]
    assert flow.client.update_claim_draft.call_args.args[1].loss_type == 'other'
    coordinator.on_speech(flow.call, 'agent', 'Still reading.', None, flow.bridge)
    time.sleep(0.05)
    assert len(reviews(flow)) == 2 and flow.state.corrections == 1


def test_a_partly_recorded_change_waits_for_its_field(flow):
    reach_review(flow)
    flow.call.update(submission_decision='change', corrected_value='other')
    flow.call.pop('corrected_field', None)
    assert not review_claim.change_pending(flow.call, flow.state, flow.bridge)
    flow.call['corrected_field'] = 'loss_type'
    assert review_claim.change_pending(flow.call, flow.state, flow.bridge)


def test_approval_never_files_over_an_unapplied_change(flow):
    reach_review(flow)
    flow.call.update(submission_decision='approve', corrected_field='loss_type', corrected_value='theft')
    coordinator.on_task_complete(flow.call, review_claim.REVIEW_TASK_ID, flow.bridge)
    wait_closeout(flow.state)
    flow.client.submit_claim_draft.assert_not_called()
    assert flow.state.fact('loss_type') == 'theft'
    assert len(reviews(flow)) == 2
    coordinator.on_task_complete(flow.call, review_claim.REVIEW_TASK_ID, flow.bridge)
    flow.client.submit_claim_draft.assert_called_once()
    assert flow.client.update_claim_draft.call_args.args[1].loss_type == 'theft'


def missing_details(flow, involved=False):
    def generate(prompt, json_schema):
        if json_schema == supervisor.DESCRIPTION_SCHEMA:
            return json.dumps({key: involved if key == 'other_party_involved' else key == 'what_happened' for key in json_schema['properties']})
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate


@pytest.mark.parametrize('involved,question', [
    (False, 'Can you tell me a bit more about how it happened and what you were doing at the time?'),
    (True, 'Can you tell me a bit more about how it happened, what you were doing at the time and what the other party did?'),
])
def test_missing_details_get_one_question_before_the_read_back(flow, involved, question):
    missing_details(flow, involved)
    collect(flow)
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    wait_closeout(flow.state)
    until(lambda: flow.bridge.tasks and flow.bridge.tasks[-1].task_id == claim_intake.DETAILS_TASK_ID)
    assert flow.bridge.tasks[-1].fields[0].question == question
    assert flow.state.result('write_summary') is None
    flow.call['more_details'] = 'I was backing out slowly.'
    coordinator.on_task_complete(flow.call, claim_intake.DETAILS_TASK_ID, flow.bridge)
    wait_closeout(flow.state)
    assert flow.state.fact('description').endswith('I was backing out slowly.')
    assert flow.bridge.tasks[-1].task_id in review_claim.TASK_IDS


def test_caller_with_nothing_to_add_moves_on_and_is_not_asked_again(flow):
    missing_details(flow)
    collect(flow)
    description = flow.state.fact('description')
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    until(lambda: flow.bridge.tasks and flow.bridge.tasks[-1].task_id == claim_intake.DETAILS_TASK_ID)
    flow.call['more_details'] = 'None.'
    coordinator.on_task_complete(flow.call, claim_intake.DETAILS_TASK_ID, flow.bridge)
    wait_closeout(flow.state)
    assert flow.state.fact('description') == description
    claim_intake.apply_addition(flow.state, 'More words.')
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    wait_closeout(flow.state)
    assert [task.task_id for task in flow.bridge.tasks].count(claim_intake.DETAILS_TASK_ID) == 1


def test_details_check_failure_never_blocks_the_read_back(flow):
    flow.bridge.generate = Mock(side_effect=RuntimeError('offline'))
    reach_review(flow)
    assert claim_intake.DETAILS_TASK_ID not in [task.task_id for task in flow.bridge.tasks]


def test_summary_only_sees_what_happened(flow):
    prompts = []
    def generate(prompt, json_schema):
        prompts.append((json_schema, prompt))
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    reach_review(flow)
    summary = next(prompt for schema, prompt in prompts if schema == supervisor.SUMMARY_SCHEMA)
    assert json.loads(summary.split('\n')[-1]) == {'description': flow.state.fact('description')}


def test_claim_files_third_person_report_while_read_back_says_you(flow):
    def generate(prompt, json_schema):
        if json_schema == supervisor.SUMMARY_SCHEMA:
            return json.dumps({'summary': 'You were hit.', 'report': 'The policyholder was hit.', 'loss_type': 'collision'})
        return safe_generate(prompt, json_schema)
    flow.bridge.generate = generate
    reach_review(flow)
    assert flow.client.update_claim_draft.call_args.args[1].description == 'The policyholder was hit.'
    assert 'You were hit.' in reviews(flow)[-1].say[0]


def test_failed_summary_files_the_callers_words(flow):
    flow.bridge.generate = Mock(side_effect=RuntimeError('offline'))
    reach_review(flow)
    assert flow.client.update_claim_draft.call_args.args[1].description == flow.state.fact('description')


def test_conflict_can_be_raised_again_after_the_facts_change(flow):
    collect(flow)
    answer = {'inconsistent': True, 'fields': ['description', 'vehicle'], 'reason': 'private'}
    assert supervisor._raise_conflict(flow.state, answer)
    assert not supervisor._raise_conflict(flow.state, answer)
    flow.state.set_fact('vehicle', '2022 Honda Civic')
    assert supervisor._raise_conflict(flow.state, answer)


@pytest.mark.parametrize('line,relative', [
    ('It happened today, five hours ago.', True), ('yesterday at 2 p.m.', True), ('Last night', True),
    ('November first at five p.m.', False), ('yesterday, November 1st at 5', False), ('11/01 at 5', False), ('On Main Street', False),
])
def test_relative_times_are_recognized(line, relative):
    assert verification.is_relative_time(line) is relative


def test_relative_date_is_refused_by_code_and_exact_date_is_accepted(flow):
    claim_intake.register(object(), flow.bridge)
    validate = flow.bridge.validators['occurred_at']
    supervisor.record(flow.state, 'caller', 'It happened today, five hours ago.', 'time')
    valid, message = validate(flow.call, '2026-11-02T01:26:00')
    assert valid is False and verification.EXACT_TIME_LINE in message
    supervisor.record(flow.state, 'caller', 'November first at five p.m.', 'time')
    assert validate(flow.call, '2026-11-01T17:00:00') is True


def test_read_back_correction_with_a_relative_date_is_refused(flow):
    reach_review(flow)
    before = flow.state.fact('occurred_at')
    supervisor.record(flow.state, 'caller', 'It was yesterday', 'fix')
    claim_intake.apply_correction(flow.state, 'occurred_at', '2026-11-01T17:00:00')
    assert flow.state.fact('occurred_at') == before
    assert flow.state.review_prefix == verification.EXACT_TIME_LINE + ' '


def test_recorded_approval_files_at_once_after_the_read_back_was_spoken(flow):
    reach_review(flow)
    flow.call['submission_decision'] = 'approve'
    assert not review_claim.change_pending(flow.call, flow.state, flow.bridge)
    coordinator.on_speech(flow.call, 'agent', "So here's what I have.", None, flow.bridge)
    until(lambda: flow.client.submit_claim_draft.called)
    flow.client.submit_claim_draft.assert_called_once()


def test_stale_approval_cannot_file_a_changed_claim(flow):
    reach_review(flow)
    flow.call.update(submission_decision='approve', corrected_field='loss_type', corrected_value='theft')
    coordinator.on_task_complete(flow.call, review_claim.REVIEW_TASK_ID, flow.bridge)
    wait_closeout(flow.state)
    coordinator.on_speech(flow.call, 'agent', "So here's what I have.", None, flow.bridge)
    time.sleep(0.05)
    flow.client.submit_claim_draft.assert_not_called()


def test_each_re_read_gets_a_new_task_id_that_still_completes_the_review(flow):
    reach_review(flow)
    assert reviews(flow)[0].task_id == review_claim.REVIEW_TASK_ID
    flow.call.update(submission_decision='add', corrected_value='A bumper broke.')
    coordinator.on_speech(flow.call, 'agent', 'Updating that.', None, flow.bridge)
    until(lambda: len(reviews(flow)) == 2)
    new_id = reviews(flow)[1].task_id
    assert new_id == review_claim.REVIEW_TASK_ID + '-1' and new_id in coordinator.COMPLETION_TASKS
    flow.call['submission_decision'] = 'approve'
    coordinator.on_task_complete(flow.call, new_id, flow.bridge)
    flow.client.submit_claim_draft.assert_called_once()


def test_approval_with_a_new_value_but_no_new_field_counts_as_an_addition(flow):
    reach_review(flow)
    flow.state.review_seen = ('vehicle', None)
    flow.call.update(submission_decision='approve', corrected_field='vehicle', corrected_value='a motorcyclist')
    coordinator.on_task_complete(flow.call, review_claim.REVIEW_TASK_ID, flow.bridge)
    wait_closeout(flow.state)
    assert 'a motorcyclist' in flow.state.fact('description')
    assert flow.state.review_prefix == ''


def test_slow_action_answer_cannot_restart_a_blocked_claim(flow):
    assert coordinator.on_intent_a(flow.call, flow.bridge) == coordinator.CONTINUE_TASK
    classify(flow, "I'm calling on behalf of my mother", {'role': 'someone_else'})
    flow.bridge.read_script.assert_called_once_with(flow.call, checklist.ROLE_LINE)
    assert flow.bridge.tasks[-1].task_id == assistance.CALLBACK_OFFER_TASK_ID
    before = len(flow.bridge.tasks)
    for handler in (coordinator.on_intent_a, coordinator.on_intent_b, coordinator.on_immediate_danger_or_other):
        assert 'Stop collecting' in handler(flow.call, flow.bridge)
    assert len(flow.bridge.tasks) == before and flow.bridge.instructions == []
    assert 'reporting on their behalf' not in coordinator.INTENT_CHOICES['intent_a']


def test_asking_for_a_human_gets_the_limit_and_only_a_callback(flow):
    classify(flow, 'I want to talk to a human', {'wants_human': True})
    flow.bridge.read_script.assert_called_once_with(flow.call, checklist.HUMAN_LINE)
    assert flow.state.claim_blocked
    assert flow.bridge.tasks[-1].task_id == assistance.CALLBACK_OFFER_TASK_ID
    assert 'connect' not in checklist.HUMAN_LINE.lower()
    assert 'Stop collecting' in coordinator.on_intent_a(flow.call, flow.bridge)


def test_asking_for_a_human_after_filing_changes_nothing(flow):
    flow.state.record(ActionResult('submit_claim', 'api', True, SimpleNamespace(claim_number='claim')))
    classify(flow, 'Can I talk to a person', {'wants_human': True})
    assert not flow.state.claim_blocked
    flow.bridge.read_script.assert_not_called()


def at_help_question(flow):
    flow.state.results.clear()
    flow.state.clear_done(identity.HELP_TASK_ID)
    flow.state.record(ActionResult('check_api', 'api', True))
    coordinator.run_checklist(flow.call, flow.state, flow.bridge)
    assert flow.bridge.tasks[-1].task_id == identity.HELP_TASK_ID


def test_identity_questions_wait_for_the_classifier(flow):
    at_help_question(flow)
    release = threading.Event()
    def generate(prompt, json_schema):
        release.wait(1)
        return json.dumps(SAFE)
    flow.bridge.generate = generate
    coordinator.on_speech(flow.call, 'caller', 'I want to open a claim', 'help', flow.bridge)
    coordinator.on_task_complete(flow.call, identity.HELP_TASK_ID, flow.bridge)
    assert flow.bridge.tasks[-1].task_id == identity.HELP_TASK_ID
    release.set()
    until(lambda: flow.bridge.tasks[-1].task_id == identity.TASK_ID)
    assert flow.state.pending_lines == set()


def test_asking_for_a_human_never_reaches_the_identity_questions(flow):
    at_help_question(flow)
    flow.bridge.generate = Mock(return_value=json.dumps(SAFE | {'wants_human': True}))
    coordinator.on_speech(flow.call, 'caller', 'I want to talk to a human', 'help', flow.bridge)
    coordinator.on_task_complete(flow.call, identity.HELP_TASK_ID, flow.bridge)
    until(lambda: flow.state.claim_blocked)
    join_workers(flow)
    assert identity.TASK_ID not in [task.task_id for task in flow.bridge.tasks]
    flow.bridge.read_script.assert_called_once_with(flow.call, checklist.HUMAN_LINE)


def test_failed_classification_does_not_hold_the_call(flow):
    at_help_question(flow)
    flow.bridge.generate = Mock(side_effect=RuntimeError('offline'))
    coordinator.on_speech(flow.call, 'caller', 'I want to open a claim', 'help', flow.bridge)
    coordinator.on_task_complete(flow.call, identity.HELP_TASK_ID, flow.bridge)
    until(lambda: flow.bridge.tasks[-1].task_id == identity.TASK_ID)


@pytest.mark.parametrize('hung_up', [False, True])
def test_asking_for_a_human_during_an_ending_still_gets_the_callback(flow, hung_up):
    collect(flow)
    flow.state.ending = 'missing_details'
    if hung_up:
        flow.state.record(ActionResult('hang_up', 'guava', True))
    classify(flow, 'I want to talk to a human', {'wants_human': True})
    flow.bridge.read_script.assert_called_once_with(flow.call, checklist.HUMAN_LINE)
    assert flow.state.ending is None and flow.state.result('hang_up') is None
    flow.call.update(wants_callback='no', ending_ack='done')
    coordinator.on_task_complete(flow.call, assistance.CALLBACK_OFFER_TASK_ID, flow.bridge)
    assert flow.state.ending == 'assistance_closed'
    coordinator.on_task_complete(flow.call, 'assistance-closed', flow.bridge)
    flow.bridge.hang_up.assert_called_once_with(flow.call, endings.FINAL_LINES['assistance_closed'])

