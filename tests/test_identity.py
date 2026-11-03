"""Identity read-back, bounded retry, privacy and volunteered facts."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agent import api_adapter, coordinator
from agent.workflows import identity
from test_api_outage import FakeBridge, call_fields, callback_path, PROFILE


@pytest.fixture
def flow(monkeypatch, callback_path):
    bridge, call = FakeBridge(), call_fields()
    call.pop('request_type')
    client = Mock()
    client.get_policy.return_value = PROFILE
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    coordinator.register(object(), bridge)
    coordinator.start_call(call, bridge)
    return SimpleNamespace(call=call, bridge=bridge, client=client, state=coordinator.state_for(call, bridge))


def complete(flow, task):
    flow.bridge.completions[task](flow.call)


def confirm(flow, answer='yes'):
    flow.call['identity_confirmed'] = answer
    complete(flow, identity.CONFIRM_TASK_ID)


def test_readback_and_duplicate_completion_fetch_once(flow):
    assert next(f for f in flow.bridge.tasks[-1].fields if f.key == 'caller_name').question == identity.NAME_QUESTION
    complete(flow, identity.TASK_ID)
    line, = flow.bridge.tasks[-1].say
    assert '1, 0, 0, 1' in line and '6, 2, 8, 2, 6, 8, 8, 7, 5, 3' in line and 'Michael Johnson' in line
    before = len(flow.bridge.tasks)
    complete(flow, identity.TASK_ID)
    assert len(flow.bridge.tasks) == before
    flow.client.get_policy.assert_not_called()
    confirm(flow)
    confirm(flow)
    flow.client.get_policy.assert_called_once_with('1001')
    assert flow.state.verified_profile is PROFILE


@pytest.mark.parametrize('answer', ['no', 'yes'])
def test_retries_limit_and_privacy(flow, answer):
    flow.call['caller_name'] = 'Different Person'
    flow.call['caller_phone'] = '555-555-0100'
    for attempt in range(identity.MAX_IDENTITY_RETRIES + 1):
        complete(flow, identity.TASK_ID)
        confirm(flow, answer)
        task = flow.bridge.tasks[-1]
        if attempt < identity.MAX_IDENTITY_RETRIES:
            assert task.task_id == identity.TASK_ID
            assert 'Greet' not in task.objective and coordinator.KEEP_CONTEXT_RULE not in task.objective
            expected = identity.SPELL_NAME_QUESTION if answer == 'yes' else identity.NAME_QUESTION
            assert next(f.question for f in task.fields if f.key == 'caller_name') == expected
        else:
            assert task.say == [identity.FAILED_LINE]
    before = len(flow.bridge.tasks)
    confirm(flow, answer)
    assert len(flow.bridge.tasks) == before
    assert flow.state.identity_retries == identity.MAX_IDENTITY_RETRIES
    assert flow.client.get_policy.call_count == (3 if answer == 'yes' else 0)
    for task in flow.bridge.tasks:
        objective = task.objective.replace("Never use the phrase 'against the policy'.", '')
        assert 'against the policy' not in objective
        assert 'Michael Johnson' not in objective
        for line in task.say:
            assert 'against the policy' not in line and 'Michael Johnson' not in line
            assert PROFILE.phone not in line and identity._digits(PROFILE.phone) not in line
        # The phone read back is caller-provided; no stored policy phone is disclosed on failure.
        assert PROFILE.phone not in objective


def test_spelling_persists_after_no(flow):
    flow.call['caller_name'] = 'Different Person'
    complete(flow, identity.TASK_ID)
    confirm(flow)
    flow.call['caller_name'] = 'Michael Johnson'
    complete(flow, identity.TASK_ID)
    assert 'M i c h a e l , J o h n s o n' in flow.bridge.tasks[-1].say[0]
    confirm(flow, 'no')
    assert next(f.question for f in flow.bridge.tasks[-1].fields if f.key == 'caller_name') == identity.SPELL_NAME_QUESTION


def test_fetch_outage_announces_once(flow):
    flow.client.get_policy.side_effect = api_adapter.APIUnavailableError('offline')
    complete(flow, identity.TASK_ID)
    confirm(flow)
    before = len(flow.bridge.tasks)
    confirm(flow)
    assert len(flow.bridge.tasks) == before
    assert flow.bridge.tasks[-1].task_id == 'assistance-offer'
    assert flow.state.verified_profile is None


@pytest.mark.parametrize('time,expected', [('2026-01-01T12:00:00', '2026-01-01T12:00:00'), ('last week', None)])
def test_volunteered_facts_survive_empty_retry(flow, time, expected):
    flow.call.update(request_type=coordinator.REPORT_OWN_LOSS, stated_time=time)
    complete(flow, identity.TASK_ID)
    for key in ('request_type', 'description', 'location', 'loss_type'):
        assert flow.state.fact(key) == flow.call[key]
    assert flow.state.fact('occurred_at') == expected
    assert flow.state.identity_retries == 0
    assert flow.state.verified_profile is None
    confirm(flow, 'no')
    prior = flow.state.facts_dict()
    for key in ('request_type', 'description', 'location', 'loss_type', 'stated_time'):
        flow.call[key] = None
    complete(flow, identity.TASK_ID)
    assert flow.state.facts_dict() == prior
