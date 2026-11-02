"""Workflow decisions tested without Guava or a running HTTP service."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent import api_adapter, coordinator
from agent.workflows import policy_lookup


class FakeBridge:
    def __init__(self):
        self.tasks = []
        self.validators = {}
        self.completions = {}

    def set_task(self, call, task_id, objective, todos=(), *, fields=()):
        self.tasks.append((call, task_id, objective, fields))

    def get_field(self, call, key):
        return call.get(key)

    def register_validator(self, agent, key, handler):
        self.validators[key] = handler

    def register_task_complete(self, agent, task_id, handler):
        self.completions[task_id] = handler


@pytest.fixture
def api(monkeypatch):
    client = Mock()
    client.get_policy.return_value = SimpleNamespace(
        policy_number='GEC-0001', name='Avery Morgan', phone='628-268-8763',
    )
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    return client


@pytest.mark.parametrize('name,phone,task', [
    ('  AVERY  MORGAN ', '+1 (628) 268-8763', 'policy-verified-placeholder'),
    ('Another Person', '628-268-8763', 'policy-verification-mismatch'),
    ('Avery Morgan', '555-555-5555', 'policy-verification-mismatch'),
])
def test_verification_with_fake_bridge(api, name, phone, task):
    bridge = FakeBridge()
    call = {'policy_number': ' GEC-0001 ', 'caller_name': name, 'caller_phone': phone}
    policy_lookup.register(object(), bridge)
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
    api.get_policy.assert_called_with('GEC-0001')


@pytest.mark.parametrize('error,reason', [
    (api_adapter.PolicyNotFoundError('missing', 404), 'not found'),
    (api_adapter.APIUnavailableError('offline'), 'service could not confirm'),
    (api_adapter.APIResponseError('bad response'), 'service could not confirm'),
])
def test_api_failures_do_not_verify(api, error, reason):
    api.get_policy.side_effect = error
    bridge = FakeBridge()
    policy_lookup.register(object(), bridge)
    result = bridge.validators['policy_number']({}, 'GEC-0001')
    assert result[0] is False and reason in result[1]
    bridge.completions[policy_lookup.TASK_ID]({'policy_number': 'GEC-0001'})
    assert bridge.tasks[-1][1] == 'policy-verification-unavailable'
    assert 'Do not claim verification succeeded' in bridge.tasks[-1][2]


def test_blank_policy_does_not_call_api(api):
    assert policy_lookup.validate_policy_number(' ')[0] is False
    api.get_policy.assert_not_called()
