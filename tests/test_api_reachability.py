"""Scenarios test case 1: real localhost transport, independent of the mock API."""

import json
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent import api_adapter
from agent.call_state import CallState
from agent.workflows import policy_lookup


@pytest.fixture(autouse=True)
def local_network_only(monkeypatch):
    # Even a developer's configured HTTP proxy must not receive these requests.
    monkeypatch.setenv('NO_PROXY', '*')
    monkeypatch.setenv('no_proxy', '*')


@pytest.fixture
def unavailable_api(monkeypatch):
    # Close an OS-assigned ephemeral port before connecting. A bound but
    # non-listening socket can cause a connect timeout on macOS instead.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    monkeypatch.setenv('MOCK_API_URL', f'http://127.0.0.1:{port}')


@pytest.fixture
def local_api(monkeypatch):
    response = {
        'status': 200,
        'body': json.dumps({
            'policy_number': 'GEC-0001', 'name': 'Avery Morgan',
            'phone': '628-268-8753', 'vehicles': [], 'drivers': [],
            'beneficiaries': [], 'claims': [], 'open_draft': None,
            'claims_against': [],
        }).encode(),
        'stall': False,
        'paths': [],
    }
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            response['paths'].append(self.path)
            if response['stall']:
                # Send no bytes. This safety release also prevents a missing
                # client timeout from hanging pytest indefinitely.
                release.wait(12)
                return
            self.send_response(response['status'])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response['body'])))
            self.end_headers()
            self.wfile.write(response['body'])

        def log_message(self, format, *args):
            pass

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        monkeypatch.setenv('MOCK_API_URL', f'http://127.0.0.1:{server.server_port}')
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        try:
            yield response
        finally:
            release.set()
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive(), 'Local API server did not stop'


def test_reachable_api_returns_typed_result(local_api):
    with api_adapter.APIAdapter() as api:
        result = api.get_policy('GEC-0001')

    assert isinstance(result, api_adapter.PolicyProfile)
    assert result.policy_number == 'GEC-0001'
    assert result.name == 'Avery Morgan'
    assert result.phone == '628-268-8753'
    assert local_api['paths'] == ['/policies/GEC-0001']


def test_connection_refused_raises_unavailable(unavailable_api):
    with api_adapter.APIAdapter() as api:
        with pytest.raises(api_adapter.APIUnavailableError, match='Mock API request failed') as error:
            api.get_policy('GEC-0001')

    assert isinstance(error.value.__cause__, httpx.ConnectError)


def test_nonresponding_api_times_out_within_bound(local_api):
    local_api['stall'] = True
    with api_adapter.APIAdapter() as api:
        started = monotonic()
        with pytest.raises(api_adapter.APIUnavailableError, match='Mock API request failed') as error:
            api.get_policy('GEC-0001')
        elapsed = monotonic() - started

    # Exercise the unchanged five-second production timeout, with three seconds
    # of scheduling headroom. The server's 12-second safety release exceeds this.
    assert elapsed < 8.0, f'Request took {elapsed:.2f}s; expected less than 8s'
    assert isinstance(error.value.__cause__, httpx.ReadTimeout)
    assert local_api['paths'] == ['/policies/GEC-0001']


@pytest.mark.parametrize('status,body,message', [
    (500, b'{"detail":"Service failed"}', 'Service failed'),
    (200, b'not JSON', 'API returned invalid JSON'),
    (200, b'{"policy_number":"GEC-0001"}', 'API response does not match the expected schema'),
], ids=['http-500', 'invalid-json', 'wrong-schema'])
def test_bad_response_never_returns_result(local_api, status, body, message):
    local_api.update(status=status, body=body)
    with api_adapter.APIAdapter() as api:
        with pytest.raises(api_adapter.APIResponseError, match=message) as error:
            api.get_policy('GEC-0001')

    assert error.value.status_code == status
    assert local_api['paths'] == ['/policies/GEC-0001']


class FakeBridge:
    def __init__(self):
        self.tasks = []
        self.validators = {}
        self.completions = {}

    def set_task(self, call, task_id, objective, todos=(), *, fields=(), completion_criteria=""):
        self.tasks.append(task_id)

    def get_field(self, call, key):
        return call.get(key)

    def register_validator(self, agent, key, handler):
        self.validators[key] = handler

    def register_task_complete(self, agent, task_id, handler):
        self.completions[task_id] = handler


def test_unavailable_api_does_not_escape_workflow_or_verify(unavailable_api):
    bridge = FakeBridge()
    call = {
        'policy_number': 'GEC-0001', 'caller_name': 'Avery Morgan',
        'caller_phone': '628-268-8753',
    }
    state = CallState('test-session')
    policy_lookup.register(object(), bridge, lambda call: state,
                           on_verified=lambda call, profile: bridge.set_task(call, 'verified', ''),
                           on_unavailable=lambda call: bridge.set_task(call, 'assistance-offer', ''))
    policy_lookup.start(call, bridge)

    # No injected exceptions: both registered callbacks use the real adapter.
    result = bridge.validators['policy_number'](call, call['policy_number'])
    assert result is True
    assert bridge.completions[policy_lookup.TASK_ID](call) is None
    assert bridge.tasks == [policy_lookup.TASK_ID, 'assistance-offer']
    assert 'verified' not in bridge.tasks
    # Detailed failure wording is covered by test_api_failures_do_not_verify.
