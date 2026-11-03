"""Golden real-model scenarios for the current call flow."""

from dataclasses import dataclass
from datetime import datetime, timedelta
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
SCENARIO_TIMEOUT = 180
SAFE = "Yes, everyone's safe."
_yesterday = datetime.now() - timedelta(days=1)
# The agent refuses relative dates such as yesterday, so callers give an exact one.
LOSS_DATE = f'{_yesterday:%B} {_yesterday.day}'
INCIDENT = (f'Someone rear-ended me on {LOSS_DATE} at 3 PM at Main and 5th in San Jose. '
            'I was stopped at a red light and the other driver hit my back bumper.')


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
    claims_added: int | None = None
    claim_number_spoken: bool = False
    expect_hangup: bool = False
    gated_turns: tuple[tuple[str, str], ...] = ()
    fallback_turn: str = INCIDENT
    max_asks: tuple[tuple[str, int], ...] = ()
    must_ask: tuple[str, ...] = ()
    identity_confirmation: str = "Yes, that's correct."


def policy_phone(number):
    # Mirrors the mock API seed, where each policy has its own number.
    return f'628-555-{int(number) - 900:04d}'


def identity(number, name, phone=None):
    return (f'I want to report a loss on my own policy. My policy number is {number}. '
            f'Name is {name}. My phone number is {phone or policy_phone(number)}.')


def claim(vehicle):
    return (f'Someone rear-ended my {vehicle} on {LOSS_DATE} at 3 PM at Main and 5th in San Jose. '
            'I was stopped at a red light and the other driver hit my back bumper.')


SCENARIOS = (
    Scenario('happy_path', None, (SAFE, identity('1010', 'Amanda Clark'), claim('2021 Kia Telluride')),
             gated_turns=(("review-claim", "Yes, please submit it."), ("claim-submitted", "No, that's everything.")),
             max_asks=(("vehicle|telluride", 1), ("policy number", 0), ("phone number", 0)),
             required_tasks=('review-claim', 'claim-submitted'),
             required_logs=('Policy completion verified', 'Claim draft saved', 'Claim submitted'),
             claims_added=1, claim_number_spoken=True, expect_hangup=True,
             agent_must_contain=('add anything', 'claims portal')),
    Scenario('hard_1_role', 1, (SAFE, "I'm calling for my mother, William Harris. Her policy number is 1011. "
                              f"Her phone is {policy_phone('1011')}. I need to report that her car was stolen.",),
             terminal_task='callback-offer', forbidden_tasks=('confirm-request', 'collect-claim'),
             agent_must_contain=('only open a claim for the policyholder',)),
    Scenario('hard_1_intent_changes', 1, (SAFE, identity('1012', 'Jennifer Lewis'),
             'Actually, never mind that. I want to cancel a claim I already submitted instead.'),
             terminal_task='unsupported-request', forbidden_logs=('Claim draft saved',)),
    Scenario('hard_2_scope', 2, (SAFE, 'I want to cancel a claim I already submitted. My policy number is 1012. '
                              f"Name is Jennifer Lewis. My phone number is {policy_phone('1012')}.",),
             terminal_task='unsupported-request', forbidden_tasks=('collect-claim',)),
    Scenario('hard_3_identity', 3, (SAFE, identity('1009', 'Jordan Smith', '555-555-0100'),),
             gated_turns=(('verify-policy#2', identity('1009', 'Jordan Smith', '555-555-0100')),
                          ('verify-policy#3', identity('1009', 'Jordan Smith', '555-555-0100'))),
             fallback_turn=identity('1009', 'Jordan Smith', '555-555-0100'),
             terminal_task='policy-verification-mismatch', forbidden_tasks=('confirm-request',),
             agent_must_contain=("still couldn't verify",),
             agent_must_not_contain=('John Anderson', '0109', 'against the policy')),
    Scenario('hard_4_missing_info', 4, (SAFE, identity('1010', 'Amanda Clark'),
             "My Kia Telluride was hit in a parking lot at the mall, but I honestly don't remember the date.",
             "I really can't provide the date."),
             fallback_turn="I really can't provide the date.", terminal_task='claim-missing-details',
             forbidden_logs=('Claim draft saved',), claims_added=0),
    Scenario('hard_5_correction', 5, (SAFE, identity('1011', 'William Harris'), claim('2021 Nissan Altima')),
             gated_turns=(('review-claim', 'Change the location to Oak Street.'),
                          ('review-claim-1', "No, please don't submit anything.")),
             terminal_task='claim-not-submitted',
             required_logs=('Claim fact corrected',), draft_policy='1011', draft_contains='Oak',
             claims_added=0, agent_must_contain=('oak street',)),
    Scenario('hard_6_permission', 6, (SAFE, identity('1012', 'Jennifer Lewis'), claim('2021 Toyota Sienna')),
             gated_turns=(("review-claim", "No, please don't submit anything."),),
             terminal_task='claim-not-submitted', required_tasks=('review-claim',),
             required_logs=('Claim draft saved', 'Claim not submitted'), forbidden_logs=('Claim submitted',),
             claims_added=0, agent_must_contain=('add anything',)),
    Scenario('no_repeat_multi_vehicle', None, (SAFE, identity('1008', 'Ashley Moore'), 'It was my 2021 Honda CR-V.'),
             gated_turns=(("review-claim", "No, please don't submit anything."),),
             required_tasks=('review-claim',), claims_added=0, max_asks=(("which vehicle|cr-v|f-150", 1),)),
    Scenario('date_not_assumed', None, (SAFE, identity('1009', 'John Anderson'),
             'Someone rear-ended my 2021 Ford F-150 at 3 PM at Main and 5th in San Jose. '
             'I was stopped at a red light and the other driver hit my back bumper.',
             'It was yesterday.', f'It was {LOSS_DATE} at 3 PM.'),
             gated_turns=(("review-claim", "No, please don't submit anything."),),
             required_tasks=('review-claim',), claims_added=0, must_ask=('date|what day|which day',),
             agent_must_contain=('exact date and time',)),
    Scenario('hard_7_api_down', 7, (SAFE, 'Yes, please.', "Yes, I'd like to schedule a callback.", f"Next Tuesday at 10 AM. Call me at {policy_phone('1010')}."),
             api_down=True, terminal_task='callback-saved',
             required_tasks=('assistance-offer', 'callback-offer', 'collect-callback'), forbidden_tasks=('verify-policy',),
             required_logs=('Representative check none available', 'Callback saved'),
             agent_must_not_contain=('how can i help',), callback_written=True),
    Scenario('hard_7_callback_window', 7, (SAFE, 'Yes, please.', "Yes, I'd like to schedule a callback.",
             f"Saturday at 8 PM. Call me at {policy_phone('1010')}.", 'Okay, next Tuesday at 10 AM.'),
             api_down=True, terminal_task='callback-saved',
             required_logs=('Callback saved',), callback_written=True, callback_slot=(1, 10)),
    Scenario('hard_7_time_as_answer', 7, (SAFE, 'Yes, please.', f"Next Tuesday at 10 AM. Call me at {policy_phone('1010')}."),
             api_down=True, terminal_task='callback-saved',
             required_tasks=('assistance-offer', 'callback-offer', 'collect-callback'),
             required_logs=('Callback saved',), callback_written=True, callback_slot=(1, 10)),
    Scenario('spelled_name_identity', None,
             (SAFE, "I want to report a loss on my own policy. My policy number is 1006. My name is spelled J E S S I C A B R O W N. "
              f"My phone number is {policy_phone('1006')}.",),
             required_logs=('Policy completion verified',), required_tasks=('confirm-identity',)),
    Scenario('volunteered_facts', None,
             (SAFE, 'Hi, I got rear-ended on Main Street and I want to start a claim.',
              f"My policy number is 1010. My name is Amanda Clark. My phone number is {policy_phone('1010')}.",
              f'It happened {LOSS_DATE} at 3 PM in my 2021 Kia Telluride. I was stopped at a red light and the other driver hit my back bumper.'),
             gated_turns=(('review-claim', "No, please don't submit anything."),),
             forbidden_tasks=('confirm-request',), agent_must_contain=('add anything',),
             max_asks=(('what happened|where did|where was', 0),)),
    Scenario('hard_9_multiple', 9, (SAFE, identity('1010', 'Amanda Clark'),
             'I have two separate accidents to report, one yesterday and one last month.'),
             evaluate_pass=('The agent asks which incident to handle first and keeps them separate.',)),
    Scenario('hard_10_communication', 10, (SAFE, identity('1011', 'William Harris'),
             "Sorry, I can't understand you. Can you please speak slower?"),
             evaluate_pass=('The agent adapts its communication and does not pretend the caller understood.',)),
    Scenario('hard_11_safety', 11, (SAFE, identity('1012', 'Jennifer Lewis'),
             'I just crashed and my passenger is bleeding badly right now.'),
             expect_hangup=True, agent_must_contain=('call 911 now',), forbidden_logs=('Claim draft saved',)),
)


def scenario_parameter(scenario):
    return pytest.param(scenario, id=scenario.id, marks=pytest.mark.golden)


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
        policy_number = re.search(r'\b10\d{2}\b', ' '.join(scenario.turns)).group()
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
    asked_lines = []
    for line in transcript.splitlines():
        if "here's what i have" in line.casefold():
            break
        if 'i have your policy number as' in line.casefold():
            continue  # Confirmation is not a repeated request for the identity values.
        if line.startswith('[agent]:') and '?' in line:
            asked_lines.append(line.removeprefix('[agent]:').strip())
    max_ask_matches = {pattern: [line for line in asked_lines if re.search(pattern, line, re.IGNORECASE)]
                       for pattern, _ in scenario.max_asks}
    must_ask_matches = {pattern: [line for line in asked_lines if re.search(pattern, line, re.IGNORECASE)]
                        for pattern in scenario.must_ask}
    print(f'max_asks matches={max_ask_matches!r}\nmust_ask matches={must_ask_matches!r}')
    duration = monotonic() - started
    print(f'\n{scenario.id}: duration={duration:.2f}s\ntasks={tasks!r}\nlogs={logs!r}\n{transcript}')
    if errors:
        print(errors)
    (tmp_path / 'evidence.json').write_text(json.dumps({
        'scenario': scenario.id, 'duration': duration, 'tasks': tasks, 'logs': logs,
        'transcript': transcript, 'errors': errors, 'timed_out': timed_out, 'events': events,
        'max_ask_matches': max_ask_matches, 'must_ask_matches': must_ask_matches,
    }, indent=2))
    assert not timed_out, f'Scenario exceeded its {SCENARIO_TIMEOUT}s hard limit'
    assert result.returncode == 0, 'Scenario worker failed; see captured diagnostics'
    for pattern, limit in scenario.max_asks:
        assert len(max_ask_matches[pattern]) <= limit, max_ask_matches[pattern]
    for pattern in scenario.must_ask:
        assert must_ask_matches[pattern], f'No asked line matched {pattern!r}'
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
        from agent.workflows import assistance, identity
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
        evaluations = [event for event in events if event['kind'] == 'evaluation']
        assert evaluations and evaluations[-1]['passed'], evaluations
    if scenario.draft_policy:
        from agent import api_adapter
        with api_adapter.APIAdapter() as api:
            draft = api.get_policy(scenario.draft_policy).open_draft
        assert draft is not None
        assert scenario.draft_contains in draft.model_dump_json()
    if scenario.callback_written or scenario.callback_slot is not None:
        from agent.workflows import assistance, identity
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
    task_counts = {}
    confirmed = 0

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
        task_counts[task_id] = task_counts.get(task_id, 0) + 1
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

    def appeared(gate):
        task, separator, occurrence = gate.partition('#')
        return task_counts.get(task, 0) >= (int(occurrence) if separator else 1)

    def answer_identity(session):
        nonlocal confirmed
        if tasks and tasks[-1] == 'confirm-identity' and task_counts['confirm-identity'] > confirmed:
            confirmed = task_counts['confirm-identity']
            session.say(scenario.identity_confirmation)
            snapshot(session)
            session.wait_for_turn()

    with patch.object(guava.Call, 'set_task', observe_task), patch.object(TestSession, 'recv', observe_recv):
        with agent.test() as session:
            try:
                try:
                    session.wait_for_turn()
                    for turn in scenario.turns:
                        answer_identity(session)
                        session.say(turn)
                        snapshot(session)
                        session.wait_for_turn()
                    answer_identity(session)
                    for task_id, turn in scenario.gated_turns:
                        for _ in range(3):
                            answer_identity(session)
                            if appeared(task_id):
                                break
                            session.say(scenario.fallback_turn)
                            snapshot(session)
                            session.wait_for_turn()
                        while not appeared(task_id):
                            answer_identity(session)
                            session.recv()
                        session.say(turn)
                        snapshot(session)
                        session.wait_for_turn()
                    answer_identity(session)
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
                            answer_identity(session)
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
