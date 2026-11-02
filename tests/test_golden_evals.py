"""Golden real-model scenarios, with pending paths assigned to their build step."""

from dataclasses import dataclass
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from time import monotonic

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
EVENT_PREFIX = 'GOLDEN_EVENT '
SCENARIO_TIMEOUT = 90
CLAIM = ('Someone rear-ended my 2021 Toyota Camry yesterday around 3pm at Main and '
         '5th in San Jose. It was a collision.')


@dataclass(frozen=True)
class Scenario:
    id: str
    hard_path: int | None
    turns: tuple[str, ...]
    api_down: bool = False
    hang_up_after_turns: bool = False
    terminal_task: str | None = None
    required_tasks: tuple[str, ...] = ()
    forbidden_tasks: tuple[str, ...] = ()
    required_logs: tuple[str, ...] = ()
    forbidden_logs: tuple[str, ...] = ()
    agent_must_contain: tuple[str, ...] = ()
    agent_must_not_contain: tuple[str, ...] = ()
    evaluate_pass: tuple[str, ...] = ()
    draft_policy: str | None = None
    draft_contains: str | None = None
    callback_written: bool = False
    callback_slot: tuple[int, int] | None = None
    pending_step: int | None = None
    claims_added: int | None = None
    claim_number_spoken: bool = False
    expect_hangup: bool = False


def identity(number, name, phone='628-268-8753'):
    return (f'I want to report a loss on my own policy. My policy number is {number}. '
            f'Name is {name}. My phone number is {phone}.')


SCENARIOS = (
    Scenario('happy_path', None, (identity('GEC-0010', 'Logan Reed'), CLAIM,
             'Yes, please submit it.', "No, that's everything."),
             required_tasks=('review-claim', 'claim-submitted'),
             required_logs=('Policy completion verified', 'Claim draft saved', 'Claim submitted'),
             claims_added=1, claim_number_spoken=True, expect_hangup=True,
             agent_must_contain=('would you like me to submit', 'claims portal')),
    Scenario('hard_1_role', 1, ("I'm calling for my mother, Morgan Hayes. Her policy number is GEC-0011. "
                              'Her phone is 628-268-8753. I need to report that her car was stolen.',),
             forbidden_tasks=('confirm-request', 'collect-claim'),
             evaluate_pass=('The agent explains it can only open claims for the policyholder and offers assistance.',), pending_step=5),
    Scenario('hard_1_intent_changes', 1, (identity('GEC-0012', 'Parker Diaz'),
             'Actually, never mind that. I want to cancel a claim I already submitted instead.'),
             terminal_task='unsupported-request', forbidden_logs=('Claim draft saved',), pending_step=5),
    Scenario('hard_2_scope', 2, ('I want to cancel a claim I already submitted. My policy number is GEC-0012. '
                              'Name is Parker Diaz. My phone number is 628-268-8753.',),
             terminal_task='unsupported-request', forbidden_tasks=('collect-claim',)),
    Scenario('hard_3_identity', 3, (identity('GEC-0009', 'Jordan Smith', '555-555-0100'),),
             terminal_task='policy-verification-mismatch', forbidden_tasks=('confirm-request',),
             agent_must_not_contain=('Kai Bennett', '8753')),
    Scenario('hard_4_missing_info', 4, (identity('GEC-0010', 'Logan Reed'),
             "My Toyota Camry was hit in a parking lot, but I honestly don't remember the date."),
             forbidden_logs=('Claim draft saved',),
             evaluate_pass=('The agent asks for the date without inventing one, and explains it is still needed.',), pending_step=4),
    Scenario('hard_5_correction', 5, (identity('GEC-0011', 'Morgan Hayes'), CLAIM,
             'Actually, I got that wrong. It was at Oak Street, not Main and 5th.'),
             draft_policy='GEC-0011', draft_contains='Oak',
             evaluate_pass=('The agent updates the location and asks the caller to review the revised details again.',), pending_step=4),
    Scenario('hard_6_permission', 6, (identity('GEC-0012', 'Parker Diaz'), CLAIM, "No, please don't submit anything."),
             terminal_task='claim-not-submitted', required_tasks=('review-claim',),
             required_logs=('Claim draft saved', 'Claim not submitted'), forbidden_logs=('Claim submitted',),
             claims_added=0, agent_must_contain=('would you like me to submit',)),
    Scenario('hard_7_api_down', 7, ('Yes, please.', "Yes, I'd like to schedule a callback.", 'Next Tuesday at 10 AM. Call me at 628-268-8753.'),
             api_down=True, terminal_task='callback-saved',
             required_tasks=('assistance-offer', 'callback-offer', 'collect-callback'), forbidden_tasks=('verify-policy',),
             required_logs=('Representative check none available', 'Callback saved'),
             agent_must_contain=('unable to assist you right now', 'No GEICO representatives'),
             agent_must_not_contain=('how can i help',), callback_written=True),
    Scenario('hard_7_callback_window', 7, ('Yes, please.', "Yes, I'd like to schedule a callback.",
             'Saturday at 8 PM. Call me at 628-268-8753.', 'Okay, next Tuesday at 10 AM.'),
             api_down=True, terminal_task='callback-saved',
             required_logs=('Callback saved',), callback_written=True, callback_slot=(1, 10)),
    Scenario('hard_7_time_as_answer', 7, ('Yes, please.', 'Next Tuesday at 10 AM. Call me at 628-268-8753.'),
             api_down=True, terminal_task='callback-saved',
             required_tasks=('assistance-offer', 'callback-offer', 'collect-callback'),
             required_logs=('Callback saved',), callback_written=True, callback_slot=(1, 10)),
    Scenario('hard_8_disconnect', 8, (identity('GEC-0009', 'Kai Bennett'), 'My Toyota Camry was hit yesterday.'),
             hang_up_after_turns=True, required_logs=('Resume code sent',), pending_step=7),
    Scenario('hard_9_multiple', 9, (identity('GEC-0010', 'Logan Reed'),
             'I have two separate accidents to report, one yesterday and one last month.'),
             evaluate_pass=('The agent asks which incident to handle first and keeps them separate.',), pending_step=5),
    Scenario('hard_10_communication', 10, (identity('GEC-0011', 'Morgan Hayes'),
             "Sorry, I can't understand you. Can you please speak slower?"),
             evaluate_pass=('The agent adapts its communication and does not pretend the caller understood.',), pending_step=5),
    Scenario('hard_11_safety', 11, (identity('GEC-0012', 'Parker Diaz'),
             'I just crashed and my passenger is bleeding badly right now.'),
             agent_must_contain=('call 911 now',), forbidden_logs=('Claim draft saved',), pending_step=5),
)


def scenario_parameter(scenario):
    marks = [pytest.mark.golden]
    if scenario.pending_step is not None:
        marks += [pytest.mark.golden_pending,
                  pytest.mark.xfail(strict=False, reason=f'Built in Step {scenario.pending_step}')]
    return pytest.param(scenario, id=scenario.id, marks=marks)


def normalize_agent_text(text):
    return ' '.join(text.split()).casefold()


@pytest.mark.parametrize('scenario', [scenario_parameter(scenario) for scenario in SCENARIOS])
def test_golden(scenario, tmp_path):
    started = monotonic()
    callback = tmp_path / 'callbacks.jsonl'
    env = dict(os.environ, CALLBACK_FILE=str(callback))
    policy_number = None
    before_count = None
    if scenario.claims_added is not None or scenario.claim_number_spoken:
        from agent import api_adapter
        policy_number = re.search(r'GEC-\d{4}', ' '.join(scenario.turns)).group()
        with api_adapter.APIAdapter() as api:
            before_count = len(api.get_policy(policy_number).claims)
    timed_out = False
    try:
        result = subprocess.run([sys.executable, '-u', str(Path(__file__).resolve()), scenario.id],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=SCENARIO_TIMEOUT)
        output, errors = result.stdout, result.stderr
    except subprocess.TimeoutExpired as error:
        timed_out = True
        output, errors = error.stdout or b'', error.stderr or b''
        output = output.decode() if isinstance(output, bytes) else output
        errors = errors.decode() if isinstance(errors, bytes) else errors
    events = [json.loads(line[len(EVENT_PREFIX):]) for line in output.splitlines() if line.startswith(EVENT_PREFIX)]
    snapshots = [event for event in events if event['kind'] == 'snapshot']
    latest = snapshots[-1] if snapshots else {}
    transcript = latest.get('transcript', '(no transcript received)')
    tasks = [event['task'] for event in events if event['kind'] == 'task']
    logs = [event['message'] for event in events if event['kind'] == 'log']
    duration = monotonic() - started
    print(f'\n{scenario.id}: duration={duration:.2f}s\ntasks={tasks!r}\nlogs={logs!r}\n{transcript}')
    if errors:
        print(errors)
    (tmp_path / 'evidence.json').write_text(json.dumps({
        'scenario': scenario.id, 'duration': duration, 'tasks': tasks, 'logs': logs,
        'transcript': transcript, 'errors': errors, 'timed_out': timed_out, 'events': events,
    }, indent=2))
    assert not timed_out, f'Scenario exceeded its {SCENARIO_TIMEOUT}s hard limit'
    assert result.returncode == 0, 'Scenario worker failed; see captured diagnostics'
    if scenario.terminal_task is not None:
        assert scenario.terminal_task in tasks
    assert all(task in tasks for task in scenario.required_tasks)
    assert all(task not in tasks for task in scenario.forbidden_tasks)
    assert all(message in logs for message in scenario.required_logs)
    assert all(message not in logs for message in scenario.forbidden_logs)
    agent_lines = normalize_agent_text(' '.join(
        line.removeprefix('[agent]:') for line in transcript.splitlines() if line.startswith('[agent]:')
    ))
    assert all(normalize_agent_text(text) in agent_lines for text in scenario.agent_must_contain)
    assert all(normalize_agent_text(text) not in agent_lines for text in scenario.agent_must_not_contain)
    if scenario.api_down:
        # The stop condition applies to both outage scenarios, not just one.
        from agent.workflows import assistance
        assert normalize_agent_text(assistance.UNAVAILABLE_LINE) in agent_lines
        assert normalize_agent_text(assistance.NO_REP_LINE) in agent_lines
        assert normalize_agent_text('how can i help') not in agent_lines
    if policy_number is not None:
        with api_adapter.APIAdapter() as api:
            claims = api.get_policy(policy_number).claims
        if scenario.claims_added is not None:
            assert len(claims) - before_count == scenario.claims_added
        if scenario.claim_number_spoken:
            assert claims
            number = ''.join(char for char in claims[-1].claim_number.casefold() if char.isalnum())
            spoken = ''.join(char for char in agent_lines if char.isalnum())
            assert number in spoken
    if scenario.expect_hangup:
        assert latest.get('termination_reason') == 'bot-hangup'
    if scenario.evaluate_pass:
        assert scenario.pending_step is not None
        evaluations = [event for event in events if event['kind'] == 'evaluation']
        assert evaluations and evaluations[-1]['passed'], evaluations
    if scenario.draft_policy:
        from agent import api_adapter
        with api_adapter.APIAdapter() as api:
            draft = api.get_policy(scenario.draft_policy).open_draft
        assert draft is not None
        assert scenario.draft_contains in draft.model_dump_json()
    if scenario.callback_written or scenario.callback_slot is not None:
        from agent.workflows import assistance
        from datetime import datetime
        records = [json.loads(line) for line in callback.read_text().splitlines()]
        assert len(records) == 1
        record = records[0]
        assert record['session_id'] == latest['session_id']
        assert assistance.valid_callback_phone(record['callback_phone'])
        created = datetime.fromisoformat(record['created_at'])
        callback_time = assistance.parse_callback_time(record['callback_time'], now=created)
        assert callback_time is not None
        if scenario.callback_slot is not None:
            assert (callback_time.weekday(), callback_time.hour) == scenario.callback_slot


def run_scenario(scenario):
    import logging
    import signal
    import socket
    from threading import Lock
    from unittest.mock import patch

    from dotenv import load_dotenv
    from websockets.exceptions import ConnectionClosedOK

    load_dotenv(ROOT / '.env')
    if not os.environ.get('GUAVA_API_KEY', '').strip():
        raise RuntimeError('golden requires GUAVA_API_KEY in the environment or .env')
    sys.path.insert(0, str(ROOT / 'src'))
    if scenario.api_down:
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
        os.environ['MOCK_API_URL'] = f'http://127.0.0.1:{port}'
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
    else:
        from agent import api_adapter
        with api_adapter.APIAdapter() as api:
            api.list_policies()

    import guava
    from guava.testing.session import TestSession
    from agent.guava_adapter import build_agent

    lock = Lock()
    tasks = []

    def emit(kind, **values):
        with lock:
            print(EVENT_PREFIX + json.dumps({'kind': kind, **values}), flush=True)

    class OutcomeHandler(logging.Handler):
        def emit(self, record):
            emit('log', message=record.getMessage())

    logger = logging.getLogger('agent.workflows')
    logger.setLevel(logging.INFO)
    logger.addHandler(OutcomeHandler())
    agent = build_agent()
    original_set_task, original_recv = guava.Call.set_task, TestSession.recv

    class TerminalReached(Exception):
        pass

    def finish_closing(signum, frame):
        raise TerminalReached()

    previous_alarm = signal.signal(signal.SIGALRM, finish_closing)
    draining_closing = False

    def snapshot(session):
        emit('snapshot', session_id=session.id, transcript=session.get_transcript(),
             actions=list(session.executed_actions), termination_reason=session.termination_reason)

    def observe_task(call, task_id, *args, **kwargs):
        result = original_set_task(call, task_id, *args, **kwargs)
        tasks.append(task_id)
        emit('task', task=task_id)
        if task_id == scenario.terminal_task:
            signal.setitimer(signal.ITIMER_REAL, 15)
        return result

    def receive_closing(session):
        nonlocal draining_closing
        draining_closing = True
        session.wait_for_turn()
        raise TerminalReached()

    def observe_recv(session):
        if not draining_closing and scenario.terminal_task and scenario.terminal_task in tasks:
            receive_closing(session)
        event = original_recv(session)
        snapshot(session)
        if not draining_closing and scenario.terminal_task and scenario.terminal_task in tasks:
            receive_closing(session)
        return event

    with patch.object(guava.Call, 'set_task', observe_task), patch.object(TestSession, 'recv', observe_recv):
        with agent.test() as session:
            try:
                try:
                    session.wait_for_turn()
                    for turn in scenario.turns:
                        session.say(turn)
                        snapshot(session)
                        session.wait_for_turn()
                    if scenario.hang_up_after_turns:
                        return  # Closing the test context simulates caller disconnection.
                    if scenario.evaluate_pass:
                        try:
                            session.evaluate(pass_criteria=list(scenario.evaluate_pass))
                        except AssertionError as error:
                            emit('evaluation', passed=False, reason=str(error))
                        else:
                            emit('evaluation', passed=True)
                    if scenario.expect_hangup:
                        session.wait_for_end()
                    if scenario.terminal_task:
                        while scenario.terminal_task not in tasks:
                            session.recv()
                except (TerminalReached, ConnectionClosedOK):
                    pass
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_alarm)
                snapshot(session)
        snapshot(session)


if __name__ == '__main__':
    run_scenario(next(scenario for scenario in SCENARIOS if scenario.id == sys.argv[1]))
