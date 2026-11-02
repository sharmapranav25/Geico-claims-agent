"""Live policy verification plus offline SDK dispatch with real API reads."""
import sys
from pathlib import Path
from unittest.mock import patch

import guava
import pytest
from guava.commands import SetTaskCommand
from guava.testing import MockCall

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent import api_adapter, coordinator
from agent.call_state import CallState
from agent.workflows import policy_lookup

try:
    with api_adapter.APIAdapter() as probe:
        probe.list_policies()
except api_adapter.APIUnavailableError:
    pytest.skip('mock API not running at MOCK_API_URL', allow_module_level=True)


def test_live_policy_number_validation():
    assert policy_lookup.validate_policy_number('GEC-0001') is True
    result = policy_lookup.validate_policy_number('UNKNOWN')
    assert result[0] is False and 'not found' in result[1]


@pytest.mark.parametrize('name,phone,expected', [
    ('  AVERY   MORGAN ', '+1 (628) 268-8753', True),
    ('Different Person', '628-268-8753', False),
    ('Avery Morgan', '555-555-5555', False),
    ('', '628-268-8753', False),
    ('Avery Morgan', '', False),
])
def test_live_identity_matching(name, phone, expected):
    with api_adapter.APIAdapter() as api:
        profile = api.get_policy('GEC-0001')
    assert policy_lookup.check_identity_match(profile, name, phone) is expected


def test_registered_completion_branches_and_fields():
    with patch('guava.agent.Client'):
        from agent.guava_adapter import SDKBridge
        agent = guava.Agent(name='Test')
    bridge = SDKBridge()
    policy_lookup.register(agent, bridge, lambda call: CallState(call.id),
                           lambda call, profile: coordinator.on_verified(call, profile, bridge))
    assert set(agent._on_validate_handlers) == {'policy_number'}
    assert set(agent._on_task_complete_handlers) == {policy_lookup.TASK_ID}
    commands = []
    for caller_name in ('Avery Morgan', 'Another Person'):
        call = MockCall()
        policy_lookup.start(call, bridge)
        task, = call._command_queue
        assert isinstance(task, SetTaskCommand)
        assert {f.key for f in task.action_items} == {'policy_number', 'caller_name', 'caller_phone'}
        assert all(f.required for f in task.action_items)
        assert coordinator.CALL_ENDING_RULE in task.objective
        call.set_field('policy_number', 'GEC-0001')
        call.set_field('caller_name', caller_name)
        call.set_field('caller_phone', '(628) 268-8753')
        assert agent._on_validate_handlers['policy_number'](call, 'GEC-0001') is True
        agent._on_task_complete_handlers[policy_lookup.TASK_ID](call)
        assert len(call._command_queue) == 2
        result = call._command_queue[-1]
        assert isinstance(result, SetTaskCommand)
        assert coordinator.CALL_ENDING_RULE in result.objective
        commands.append(result)
    assert commands[0].task_id == coordinator.REQUEST_TASK_ID
    assert 'Identity is verified for policy GEC-0001' in commands[0].objective
    assert commands[1].task_id == 'policy-verification-mismatch'
    assert 'could not be verified' in commands[1].objective
