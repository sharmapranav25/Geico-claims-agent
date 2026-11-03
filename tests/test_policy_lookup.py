"""Live policy verification plus offline SDK dispatch with real API reads."""
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import guava
import pytest
from guava.commands import SetTaskCommand
from guava.testing import MockCall

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent import api_adapter, checklist, coordinator, verification
from agent.call_state import CallState
from supervisor_helpers import after_safety, safe_generate
from agent.workflows import policy_lookup, identity

try:
    with api_adapter.APIAdapter() as probe:
        probe.list_policies()
except api_adapter.APIUnavailableError:
    pytest.skip('mock API not running at MOCK_API_URL', allow_module_level=True)


def test_live_policy_number_validation():
    assert policy_lookup.validate_policy_number('1001') is True
    result = policy_lookup.validate_policy_number('UNKNOWN')
    assert result[0] is False and "couldn't find" in result[1]


@pytest.mark.parametrize('name,phone,expected', [
    ('  MICHAEL   JOHNSON ', '+1 (628) 268-8753', True),
    ('Different Person', '628-268-8753', False),
    ('Michael Johnson', '555-555-5555', False),
    ('', '628-268-8753', False),
    ('Michael Johnson', '', False),
])
def test_live_identity_matching(name, phone, expected):
    with api_adapter.APIAdapter() as api:
        profile = api.get_policy('1001')
    assert verification.identity_matches(profile, name, phone) is expected


def test_registered_completion_branches_and_fields():
    with patch('guava.agent.Client'):
        from agent.guava_adapter import SDKBridge
        agent = guava.Agent(name='Test')
    bridge = SDKBridge()
    bridge.generate = safe_generate
    states = {}
    policy_lookup.register(agent, bridge)
    coordinator.register_completions(agent, bridge, lambda call: states.setdefault(call.id, after_safety(CallState(call.id, identity_retries=identity.MAX_IDENTITY_RETRIES))), checklist.Routes(
        lambda call, profile: coordinator.on_verified(call, profile, bridge), Mock(),
    ))
    assert set(agent._on_validate_handlers) == {'policy_number'}
    assert set(agent._on_task_complete_handlers) == set(coordinator.COMPLETION_TASKS)
    commands = []
    for caller_name in ('Michael Johnson', 'Another Person'):
        call = MockCall()
        identity.start(call, bridge)
        task, = call._command_queue
        assert isinstance(task, SetTaskCommand)
        assert {f.key for f in task.action_items} == {f.key for f in identity.identity_fields()}
        assert all(f.required for f in task.action_items[:3])
        assert coordinator.CALL_ENDING_RULE in task.objective
        call.set_field('policy_number', '1001')
        call.set_field('caller_name', caller_name)
        call.set_field('caller_phone', '(628) 268-8753')
        assert agent._on_validate_handlers['policy_number'](call, '1001') is True
        agent._on_task_complete_handlers[identity.TASK_ID](call)
        call.set_field('identity_confirmed', 'yes')
        agent._on_task_complete_handlers[identity.CONFIRM_TASK_ID](call)
        assert len(call._command_queue) == (4 if caller_name == 'Michael Johnson' else 3)
        result = call._command_queue[-1]
        assert isinstance(result, SetTaskCommand)
        assert coordinator.CALL_ENDING_RULE in result.objective
        commands.append(result)
    assert commands[0].task_id == coordinator.REQUEST_TASK_ID
    assert 'Identity is verified for policy 1001' in commands[0].objective
    assert commands[1].task_id == 'policy-verification-mismatch'
    assert commands[1].action_items[0].statement == identity.FAILED_LINE
