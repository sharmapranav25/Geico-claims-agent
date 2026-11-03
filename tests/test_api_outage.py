"""Deterministic outage routing, real connection failures, and local callbacks."""

import importlib.util
import json
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent import api_adapter, coordinator
from supervisor_helpers import safe_generate, after_safety, completion
from agent.call_state import InMemoryStateStore
from agent.workflows import assistance, claim_intake, policy_lookup, identity

PROFILE = SimpleNamespace(
    policy_number='1001', name='Michael Johnson', phone='628-268-8753',
    vehicles=(SimpleNamespace(vehicle_id='V01-1', year=2021, make='Toyota', model='Camry'),),
)


def future_time():
    value = (datetime.now(assistance.CALLBACK_ZONE) + timedelta(days=7)).replace(
        hour=10, minute=0, second=0, microsecond=0,
    )
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value


def call_fields():
    return {
        'session_id': 'outage-session', 'policy_number': '10 01',
        'caller_name': 'Michael Johnson', 'caller_phone': '628-268-8753',
        'request_type': coordinator.REPORT_OWN_LOSS, 'vehicle': '2021 Toyota Camry',
        'occurred_at': '2026-11-01T15:00:00-08:00', 'location': 'Main and 5th',
        'description': 'Rear-ended at a light.', 'loss_type': 'collision',
        'wants_representative': 'yes', 'wants_callback': 'yes',
        'callback_time': future_time().isoformat(), 'callback_phone': '628-268-8753',
    }


class FakeBridge:
    generate = staticmethod(safe_generate)

    def hang_up(self, call, final_line):
        self.instructions.append(final_line)

    def __init__(self):
        self.tasks = []
        self.instructions = []
        self.infos = []
        self.validators = {}
        self.completions = {}
        self.actions = {}

    def set_task(self, call, task_id, objective, todos=(), *, fields=(), say=(), completion_criteria=""):
        self.tasks.append(SimpleNamespace(task_id=task_id, objective=objective, fields=fields, say=say, completion_criteria=completion_criteria))

    def add_info(self, call, label, info):
        self.infos.append((label, info))

    def register_question(self, agent, handler):
        self.question = handler

    def send_instruction(self, call, instruction):
        self.instructions.append(instruction)

    def get_field(self, call, key):
        return call.get(key)

    def get_session_id(self, call):
        return call['session_id']

    def register_validator(self, agent, key, handler):
        self.validators[key] = handler

    def register_task_complete(self, agent, task_id, handler):
        self.completions[task_id] = completion(self, handler)

    def register_actions(self, agent, choices, handlers):
        self.actions.update(handlers)


@pytest.fixture
def callback_path(monkeypatch, tmp_path):
    path = tmp_path / 'callbacks.jsonl'
    monkeypatch.setenv('CALLBACK_FILE', str(path))
    monkeypatch.setattr(coordinator, 'STORE', InMemoryStateStore())
    return path


@pytest.fixture
def unavailable_api(monkeypatch):
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    monkeypatch.setenv('MOCK_API_URL', f'http://127.0.0.1:{port}')
    monkeypatch.setenv('NO_PROXY', '*')
    monkeypatch.setenv('no_proxy', '*')


@pytest.fixture
def adapter(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'src/agent/guava_adapter.py'
    spec = importlib.util.spec_from_file_location('outage_adapter_test', path)
    module = importlib.util.module_from_spec(spec)
    with patch('dotenv.load_dotenv'), patch('guava.agent.Client'):
        spec.loader.exec_module(module)
    monkeypatch.setattr(coordinator, 'STORE', InMemoryStateStore())
    monkeypatch.setattr(module.SDK_BRIDGE, 'generate', safe_generate)
    return module


def registered():
    bridge = FakeBridge()
    coordinator.register(object(), bridge)
    return bridge


def test_down_start_uses_say_and_never_verifies(adapter, unavailable_api, callback_path):
    from guava import Say
    from guava.testing import MockCall

    call = MockCall()
    after_safety(coordinator.state_for(call, adapter.SDK_BRIDGE))
    coordinator.start_call(call, adapter.SDK_BRIDGE)
    state = coordinator.state_for(call, adapter.SDK_BRIDGE)
    assert list(state.results) == ['check_api', 'start_assistance']
    assert state.result('check_api').ok is False
    command, = call._command_queue
    assert command.task_id == assistance.OFFER_TASK_ID
    assert isinstance(command.action_items[0], Say)
    assert command.action_items[0].statement == assistance.UNAVAILABLE_LINE
    assert state.result('start_verification') is None
    assert not callback_path.exists()


def test_up_start_has_no_say(adapter, monkeypatch):
    from guava import Say
    from guava.testing import MockCall

    monkeypatch.setattr(assistance, 'check_api', Mock(return_value=[]))
    call = MockCall()
    after_safety(coordinator.state_for(call, adapter.SDK_BRIDGE))
    coordinator.start_call(call, adapter.SDK_BRIDGE)
    command, = call._command_queue
    assert command.task_id == identity.TASK_ID
    assert not any(isinstance(item, Say) for item in command.action_items)
    assert coordinator.state_for(call, adapter.SDK_BRIDGE).result('start_verification').ok


@pytest.mark.parametrize('wants_rep,wants_callback,expected', [
    ('no', 'no', ['assistance-offer', 'assistance-closed']),
    ('yes', 'no', ['assistance-offer', 'callback-offer', 'assistance-closed']),
    ('yes', 'yes', ['assistance-offer', 'callback-offer', 'collect-callback']),
])
def test_assistance_choices(unavailable_api, callback_path, caplog, wants_rep, wants_callback, expected):
    bridge = registered()
    call = call_fields() | {'wants_representative': wants_rep, 'wants_callback': wants_callback}
    with caplog.at_level('INFO', logger=assistance.__name__):
        after_safety(coordinator.state_for(call, bridge))
        coordinator.start_call(call, bridge)
        bridge.completions[assistance.OFFER_TASK_ID](call)
        if wants_rep == 'yes':
            bridge.completions[assistance.CALLBACK_OFFER_TASK_ID](call)
    assert [task.task_id for task in bridge.tasks] == expected
    assert not callback_path.exists()
    assert all('how can i help' not in task.objective.lower() for task in bridge.tasks)
    if wants_rep == 'yes':
        assert bridge.tasks[1].say == [assistance.NO_REP_LINE]
        assert 'Never say a callback is noted' in bridge.tasks[1].objective
        assert 'Giving a callback time counts as yes.' in bridge.tasks[1].fields[0].description
        assert 'Representative check none available' in caplog.messages
        if wants_callback == 'yes':
            assert 'Never say a callback is noted' in bridge.tasks[2].objective


@pytest.mark.parametrize('write_fails', [False, True])
def test_callback_write_precedes_confirmation(unavailable_api, callback_path, monkeypatch, caplog, write_fails):
    bridge = registered()
    call = call_fields()
    after_safety(coordinator.state_for(call, bridge))
    coordinator.start_call(call, bridge)
    bridge.completions[assistance.OFFER_TASK_ID](call)
    bridge.completions[assistance.CALLBACK_OFFER_TASK_ID](call)
    assert bridge.validators['callback_time'](call, call['callback_time']) is True
    assert bridge.validators['callback_phone'](call, call['callback_phone']) is True
    original = bridge.set_task

    def observe_task(*args, **kwargs):
        saved = coordinator.state_for(call, bridge).result('save_callback')
        assert saved.ok is not write_fails
        if not write_fails:
            assert callback_path.exists()
        original(*args, **kwargs)

    bridge.set_task = observe_task
    if write_fails:
        monkeypatch.setattr(assistance, 'save_callback', Mock(side_effect=OSError('disk full')))
    with patch.object(assistance.os, 'fsync', wraps=assistance.os.fsync) as fsync:
        with caplog.at_level('INFO', logger=assistance.__name__):
            bridge.validators['callback_time'](call, 'Saturday at 8 PM')
            bridge.completions[assistance.CALLBACK_TASK_ID](call)
        assert fsync.call_count == (not write_fails)
    assert bridge.tasks[-1].task_id == ('callback-save-failed' if write_fails else 'callback-saved')
    assert all('how can i help' not in task.objective.lower() for task in bridge.tasks)
    if write_fails:
        assert not callback_path.exists()
        assert 'Callback saved' not in caplog.messages
    else:
        records = [json.loads(line) for line in callback_path.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]['session_id'] == call['session_id']
        assert records[0]['callback_time'] == call['callback_time']
        assert records[0]['callback_phone'] == call['callback_phone']
        assert datetime.fromisoformat(records[0]['created_at']).tzinfo is not None
    assert call['callback_phone'] not in caplog.text
    assert call['callback_time'] not in caplog.text
    assert 'Saturday at 8 PM' not in caplog.text


def test_callback_time_invalid_at_save_writes_nothing(unavailable_api, callback_path):
    bridge = registered()
    call = call_fields()
    after_safety(coordinator.state_for(call, bridge))
    coordinator.start_call(call, bridge)
    bridge.completions[assistance.OFFER_TASK_ID](call)
    bridge.completions[assistance.CALLBACK_OFFER_TASK_ID](call)
    assert bridge.validators['callback_time'](call, call['callback_time']) is True
    call['callback_time'] = (datetime.now(assistance.CALLBACK_ZONE) - timedelta(days=1)).isoformat()

    bridge.completions[assistance.CALLBACK_TASK_ID](call)

    saved = coordinator.state_for(call, bridge).result('save_callback')
    assert saved.ok is False
    assert isinstance(saved.data, ValueError)
    assert not callback_path.exists()
    assert bridge.tasks[-1].task_id == 'callback-save-failed'
    assert all(task.task_id != 'callback-saved' for task in bridge.tasks)


@pytest.mark.parametrize('value,valid', [
    ('2026-11-02T09:00:00', True), ('2026-11-02T16:00:00', True),
    ('2026-11-06T12:00:00', True), ('2026-11-02T12:00:00-05:00', True),
    ('2026-11-02T08:59:00', False), ('2026-11-02T16:00:01', False),
    ('2026-11-07T12:00:00', False), ('2026-11-08T12:00:00', False),
    ('2026-10-30T12:00:00', False), ('tomorrow', False), ('', False), (None, False),
    ('2026-11-02T19:01:00-05:00', False),
])
def test_callback_time_window(value, valid):
    result = assistance.parse_callback_time(value, datetime(2026, 11, 2, 8, tzinfo=assistance.CALLBACK_ZONE))
    assert (result is not None) is valid
    if valid:
        assert result.tzinfo == assistance.CALLBACK_ZONE


@pytest.mark.parametrize('value,valid', [
    ('628-268-8753', True), ('+1 (628) 268-8753', True), ('555555555', False),
    ('26282688753', False), ('', False), (None, False),
])
def test_callback_phone(value, valid):
    assert assistance.valid_callback_phone(value) is valid


def test_outage_validator_allows_assistance_but_never_verifies(unavailable_api, callback_path):
    bridge = registered()
    call = call_fields()
    assert bridge.validators['policy_number'](call, call['policy_number']) is True
    after_safety(coordinator.state_for(call, bridge))
    bridge.completions[identity.TASK_ID](call)
    call['identity_confirmed'] = 'yes'
    bridge.completions[identity.CONFIRM_TASK_ID](call)
    assert [task.task_id for task in bridge.tasks] == ['confirm-identity', 'assistance-offer']
    assert coordinator.state_for(call, bridge).verified_profile is None


@pytest.mark.parametrize('error,expected', [
    (api_adapter.APIUnavailableError('offline'), 'assistance-offer'),
    (api_adapter.APIResponseError('bad response'), 'assistance-offer'),
    (api_adapter.ClaimValidationError('invalid', 400), 'claim-save-failed'),
])
def test_midcall_save_routes_by_error(callback_path, monkeypatch, error, expected):
    client = Mock()
    client.update_claim_draft.side_effect = error
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    bridge = registered()
    call = call_fields()
    after_safety(coordinator.state_for(call, bridge)).verified_profile = PROFILE
    bridge.completions[claim_intake.TASK_ID](call)
    assert [task.task_id for task in bridge.tasks] == [expected]
    client.submit_claim_draft.assert_not_called()
