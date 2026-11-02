"""Workflow decisions tested without Guava or a running HTTP service."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent import api_adapter, checklist, coordinator
from agent.call_state import CallState
from agent.workflows import policy_lookup


class FakeBridge:
    def __init__(self):
        self.tasks = []
        self.validators = {}
        self.completions = {}

    def set_task(self, call, task_id, objective, todos=(), *, fields=(), completion_criteria=""):
        self.tasks.append((call, task_id, objective, fields))

    def get_field(self, call, key):
        return call.get(key)

    def register_validator(self, agent, key, handler):
        self.validators[key] = handler

    def register_task_complete(self, agent, task_id, handler):
        self.completions[task_id] = handler


def register(bridge):
    def on_verified(call, profile):
        bridge.set_task(call, 'verified', coordinator.CALL_ENDING_RULE)

    state = CallState('test-session')
    verified = Mock(side_effect=on_verified)
    policy_lookup.register(object(), bridge)
    coordinator.register_completions(object(), bridge, lambda call: state, checklist.Routes(
        verified, lambda call: bridge.set_task(call, 'assistance-offer', ''),
    ))
    return verified


@pytest.fixture
def api(monkeypatch):
    client = Mock()
    client.get_policy.return_value = SimpleNamespace(
        policy_number='1001', name='Michael Johnson', phone='628-268-8753',
    )
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    return client


@pytest.mark.parametrize('name,phone,task', [
    ('  MICHAEL  JOHNSON ', '+1 (628) 268-8753', 'verified'),
    ('Another Person', '628-268-8753', 'policy-verification-mismatch'),
    ('Michael Johnson', '555-555-5555', 'policy-verification-mismatch'),
])
def test_verification_with_fake_bridge(api, name, phone, task):
    bridge = FakeBridge()
    call = {'policy_number': ' 1001 ', 'caller_name': name, 'caller_phone': phone}
    register(bridge)
    policy_lookup.start(call, bridge)
    assert bridge.tasks[0][1] == policy_lookup.TASK_ID
    fields = bridge.tasks[0][3]
    assert [f.key for f in fields] == ['policy_number', 'caller_name', 'caller_phone']
    assert all(f.required for f in fields)
    assert bridge.validators['policy_number'](call, call['policy_number']) is True
    bridge.completions[policy_lookup.TASK_ID](call)
    assert bridge.tasks[-1][1] == task
    assert coordinator.CALL_ENDING_RULE in bridge.tasks[-1][2]
    assert api.get_policy.call_count == 2  # Validation and completion each fetch.
    api.get_policy.assert_called_with('1001')


@pytest.mark.parametrize('error,reason', [
    (api_adapter.PolicyNotFoundError('missing', 404), 'not found'),
    (api_adapter.APIUnavailableError('offline'), 'service could not confirm'),
    (api_adapter.APIResponseError('bad response'), 'service could not confirm'),
])
def test_api_failures_do_not_verify(api, error, reason):
    api.get_policy.side_effect = error
    bridge = FakeBridge()
    verified = register(bridge)
    result = bridge.validators['policy_number']({}, '1001')
    if isinstance(error, api_adapter.PolicyNotFoundError):
        assert result[0] is False and reason in result[1]
    else:
        assert result is True
    bridge.completions[policy_lookup.TASK_ID]({'policy_number': '1001'})
    assert bridge.tasks[-1][1] == 'assistance-offer'
    verified.assert_not_called()


def test_blank_policy_does_not_call_api(api):
    assert policy_lookup.validate_policy_number(' ')[0] is False
    api.get_policy.assert_not_called()


@pytest.mark.parametrize('number', ['1001', '10 01', '10-01', ' 1001 '])
def test_normalize_spoken_policy_number(number):
    assert policy_lookup.normalize_policy_number(number) == '1001'


@pytest.mark.parametrize('number', ['101', '10001', 'OTHER-1001', 'ten oh one'])
def test_invalid_policy_number_is_not_repaired(api, number):
    api.get_policy.side_effect = api_adapter.PolicyNotFoundError('missing', 404)
    assert policy_lookup.normalize_policy_number(number) == number.strip()
    assert policy_lookup.validate_policy_number(number)[0] is False
    api.get_policy.assert_called_once_with(number.strip())


@pytest.mark.parametrize('phase,error,name,expected', [
    ('validation', None, 'Michael Johnson', 'passed'),
    ('validation', api_adapter.PolicyNotFoundError('missing', 404), 'Michael Johnson', 'not found'),
    ('validation', api_adapter.APIUnavailableError('offline'), 'Michael Johnson', 'unavailable'),
    ('completion', None, 'Michael Johnson', 'verified'),
    ('completion', None, 'Jordan Smith', 'mismatch'),
    ('completion', api_adapter.APIUnavailableError('offline'), 'Michael Johnson', 'unavailable'),
])
def test_outcome_logs_exclude_caller_values(api, caplog, phase, error, name, expected):
    api.get_policy.side_effect = error
    bridge = FakeBridge()
    register(bridge)
    call = {'policy_number': '10 01', 'caller_name': name, 'caller_phone': '628-268-8753'}
    with caplog.at_level('INFO', logger=policy_lookup.__name__):
        if phase == 'validation':
            bridge.validators['policy_number'](call, call['policy_number'])
        else:
            bridge.completions[policy_lookup.TASK_ID](call)
    messages = [record.getMessage() for record in caplog.records if record.name == policy_lookup.__name__]
    assert messages == [f'Policy {phase} {expected}']
    for value in (*call.values(), '1001', 'Michael Johnson'):
        assert value not in messages[0]


PROFILE = SimpleNamespace(name='Michael Johnson', phone='628-268-8753')


@pytest.mark.parametrize('name', [
    'Michael Johnson', 'michael johnson', 'MICHAEL  JOHNSON', 'Michael Johnson.', 'Johnson, Michael', 'Michael Johnsen', 'Micheal Johnson',
])
def test_name_allows_small_differences(name):
    assert policy_lookup.check_identity_match(PROFILE, name, '628-268-8753') is True


@pytest.mark.parametrize('name', ['Jordan Smith', 'Michael', 'William Harris', 'Michael Smith', ''])
def test_different_name_does_not_match(name):
    assert policy_lookup.check_identity_match(PROFILE, name, '628-268-8753') is False


@pytest.mark.parametrize('phone', ['628-268-8753', '6282688753', '+1 628 268 8753', '1-628-268-8753', '(628) 268-8753', '628.268.8753'])
def test_phone_allows_formatting_only(phone):
    assert policy_lookup.check_identity_match(PROFILE, 'Michael Johnson', phone) is True


def test_verification_uses_sensitive_digit_sequence_phone(api):
    bridge = FakeBridge()
    verified = register(bridge)
    call = {'policy_number': '1001', 'caller_name': 'Michael Johnson', 'caller_phone': '6282688753'}
    policy_lookup.start(call, bridge)
    fields = {field.key: field for field in bridge.tasks[-1][3]}
    assert fields['caller_phone'].field_type == 'digit_sequence'
    assert fields['caller_phone'].sensitive is True
    assert fields['policy_number'].sensitive is True
    bridge.completions[policy_lookup.TASK_ID](call)
    verified.assert_called_once_with(call, api.get_policy.return_value)


@pytest.mark.parametrize('phone', ['628-268-8763', '628-268-875', '628-268-87533', '2-628-268-8753', ''])
def test_phone_digits_must_match(phone):
    assert policy_lookup.check_identity_match(PROFILE, 'Michael Johnson', phone) is False


@pytest.mark.parametrize('number', ['1002', '10001', '101', 'OTHER-1001'])
def test_policy_digits_are_never_repaired(number):
    assert policy_lookup.normalize_policy_number(number) != '1001'
