"""Opt-in real-model conversations; no phone call or SMS is initiated."""

import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic

import pytest

pytestmark = pytest.mark.agent_sim
ROOT = Path(__file__).resolve().parents[1]
SCENARIO_TIMEOUT = 90
EVENT_PREFIX = 'SIM_EVENT '
FIRST_TURN = (
    'I want to report a loss on my own policy. My policy number is GEC 0001. '
    'Name is Avery Morgan. My phone number is 628-268-8753.'
)
CLAIM_TURN = (
    'Someone rear-ended my 2021 Toyota Camry yesterday around 3pm at Main and 5th '
    'in San Jose. It was a collision.'
)
SCENARIOS = {
    'one_turn_verified': [FIRST_TURN],
    'claim_filled_once': [FIRST_TURN, CLAIM_TURN, "No, that's everything for now."],
    'wrong_identity': [
        'I want to report a loss on my own policy. My policy number is GEC-0001. '
        'Name is Jordan Smith. My phone number is 555-555-0100.',
    ],
    'unknown_policy': [
        'I want to report a loss on my own policy. My policy number is GEC-9999. '
        'Name is Jordan Smith. My phone number is 555-555-0100.',
    ],
    'detour_then_claim': [
        'I want to report a claim on my own policy.',
        "I don't know my policy number.",
        'I need a tow truck.',
        'I want to return to the claim on my own policy. My policy number is '
        'G-E-C0001. Name is Avery Morgan. My phone number is 628-268-8753.',
    ],
}


@pytest.mark.parametrize('scenario', [
    'one_turn_verified', 'claim_filled_once', 'wrong_identity', 'unknown_policy',
    'detour_then_claim',
])
def test_call_path(scenario):
    # The child owns all credentials, imports, network connections and SDK
    # threads. Killing it also bounds wait_for_turn and test().__exit__ joins.
    started = monotonic()
    timed_out = False
    try:
        result = subprocess.run(
            [sys.executable, '-u', str(Path(__file__).resolve()), scenario],
            cwd=ROOT, capture_output=True, text=True, timeout=SCENARIO_TIMEOUT,
        )
        output = result.stdout
        errors = result.stderr
    except subprocess.TimeoutExpired as error:
        timed_out = True
        output = error.stdout or b''
        errors = error.stderr or b''
        output = output.decode() if isinstance(output, bytes) else output
        errors = errors.decode() if isinstance(errors, bytes) else errors

    events = [json.loads(line[len(EVENT_PREFIX):]) for line in output.splitlines()
              if line.startswith(EVENT_PREFIX)]
    snapshots = [event for event in events if event['kind'] == 'snapshot']
    latest = snapshots[-1] if snapshots else {}
    actions = latest.get('actions', [])
    transcript = latest.get('transcript', '(no transcript received)')
    tasks = [event['task'] for event in events if event['kind'] == 'task']
    validations = [event['result'] for event in events if event['kind'] == 'validation']
    outcomes = [event['message'] for event in events if event['kind'] == 'outcome']
    print(f'\n{scenario}: duration={monotonic() - started:.2f}s')
    print(f'executed_actions={actions!r}\ntasks={tasks!r}')
    print(f'validation_results={validations!r}\noutcome_logs={outcomes!r}')
    print(transcript)
    if errors:
        print(errors)
    assert not timed_out, f'Scenario exceeded its {SCENARIO_TIMEOUT}s hard limit'
    assert result.returncode == 0, 'Scenario worker failed; see transcript and error above'

    if scenario in ('one_turn_verified', 'detour_then_claim'):
        assert 'Policy completion verified' in outcomes
        assert 'confirm-request' in tasks
    elif scenario == 'claim_filled_once':
        assert 'Claim draft saved' in outcomes
        assert 'claim-details-saved' in tasks
        assert tasks.count('verify-policy') == 1
    elif scenario == 'wrong_identity':
        assert 'Policy completion mismatch' in outcomes
        assert 'policy-verification-mismatch' in tasks
        assert 'confirm-request' not in tasks
        agent_lines = '\n'.join(line for line in transcript.splitlines() if line.startswith('[agent]:'))
        assert 'avery' not in agent_lines.casefold()
        assert '8753' not in agent_lines
    else:
        assert 'Policy validation not found' in outcomes
        assert 'Policy completion verified' not in outcomes
        assert 'confirm-request' not in tasks
        assert any(event['kind'] == 'asked_again' for event in events)


def run_scenario(scenario):
    # Deliberately deferred: default pytest collection needs no key or agent.
    import logging
    from threading import Lock
    from unittest.mock import patch

    from dotenv import load_dotenv
    from websockets.exceptions import ConnectionClosedOK

    load_dotenv(ROOT / '.env')
    if not os.environ.get('GUAVA_API_KEY', '').strip():
        raise RuntimeError('agent_sim requires GUAVA_API_KEY in the environment or .env')
    sys.path.insert(0, str(ROOT / 'src'))
    from agent import api_adapter

    try:
        with api_adapter.APIAdapter() as api:
            api.get_policy('GEC-0001')
    except (api_adapter.APIError, ValueError) as error:
        raise RuntimeError('agent_sim requires a reachable mock API at MOCK_API_URL') from error

    import guava
    from guava.testing.session import TestSession
    from agent.guava_adapter import build_agent

    lock = Lock()
    tasks = []
    outcomes = []

    def emit(kind, **values):
        with lock:
            print(EVENT_PREFIX + json.dumps({'kind': kind, **values}), flush=True)

    class OutcomeHandler(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            outcomes.append(message)
            emit('outcome', message=message)

    workflow_logger = logging.getLogger('agent.workflows')
    workflow_logger.setLevel(logging.INFO)
    workflow_logger.addHandler(OutcomeHandler())
    agent = build_agent()
    original_set_task = guava.Call.set_task
    original_recv = TestSession.recv

    def observe_task(call, task_id, *args, **kwargs):
        result = original_set_task(call, task_id, *args, **kwargs)
        tasks.append(task_id)
        emit('task', task=task_id)
        return result

    def snapshot(session):
        emit('snapshot', actions=list(session.executed_actions),
             transcript=session.get_transcript(), termination_reason=session.termination_reason)

    def observe_recv(session):
        event = original_recv(session)
        snapshot(session)
        return event

    with patch.object(guava.Call, 'set_task', observe_task), patch.object(TestSession, 'recv', observe_recv):
        with agent.test() as session:
            try:
                session.wait_for_turn()
                try:
                    for turn in SCENARIOS[scenario]:
                        session.say(turn)
                        session.wait_for_turn()
                except ConnectionClosedOK:
                    return  # The agent ended the call; results are in the events.
                # Answer safety clarification and repeat all supplied fields,
                # preserving the exact initial report and policy-number format.
                # This accommodates question order and re-asks without changing
                # the caller's identity or inventing missing loss information.
                for attempt in range(6):
                    if any(task in tasks for task in (
                        'claim-details-saved' if scenario == 'claim_filled_once' else 'confirm-request',
                        'policy-verification-mismatch', 'policy-verification-unavailable',
                    )):
                        break
                    if scenario == 'unknown_policy' and 'Policy validation not found' in outcomes:
                        # Supplemental model judgment only; deterministic
                        # action/validation/no-verification checks remain above.
                        session.evaluate(pass_criteria=[
                            'After the unknown policy lookup, the agent asks the caller '
                            'to check or repeat their policy number.',
                        ])
                        emit('asked_again')
                        break
                    session.say('Everyone is safe; there is no immediate danger. '
                                + SCENARIOS[scenario][-1])
                    session.wait_for_turn()
            finally:
                snapshot(session)


if __name__ == '__main__':
    run_scenario(sys.argv[1])
