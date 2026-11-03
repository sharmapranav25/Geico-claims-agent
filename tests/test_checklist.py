"""Ordered checklist selection and registered-handler regression evidence."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import api_adapter, checklist, coordinator, verification
from agent.action_loop import Action
from agent.call_state import CallState
from agent.workflows import assistance, claim_intake, policy_lookup, identity, review_claim, safety, submit_claim
from test_api_outage import PROFILE, call_fields, callback_path
from test_single_path import FakeBridge
from supervisor_helpers import wait_closeout


EXPECTED_CHECKS = [
    ('1', 'Danger'), ('ending', 'Acknowledge the outcome and close'), ('2', 'Intent or role'),
    ('3', 'Within supported scope'), ('4', 'Conflicts and multiple incidents'), ('5', 'Required verification'),
    ('6', 'Required information'), ('7', 'Consistent information'), ('8', 'Permission given'),
    ('9', 'Clear successful result'), ('10', 'Explain the result and close'),
]


def test_check_order():
    assert [(check.number, check.title) for check in checklist.CHECKS] == EXPECTED_CHECKS


@pytest.mark.parametrize('outcome_kind', ['action', 'waiting'])
def test_pass_continues_and_first_open_check_stops(monkeypatch, outcome_kind):
    action = Action('selected', 'guava', Mock())
    outcome = action if outcome_kind == 'action' else None
    passed, opened, later = Mock(return_value=checklist.PASS), Mock(return_value=outcome), Mock()
    monkeypatch.setattr(checklist, 'CHECKS', (
        checklist.Check('1', 'passed', passed), checklist.Check('2', 'open', opened),
        checklist.Check('3', 'pending', later),
    ))
    assert checklist.evaluate(None, None, None, None) is outcome
    passed.assert_called_once()
    opened.assert_called_once()
    later.assert_not_called()
    action.run.assert_not_called()


def test_priority_leaves_lower_check_pending(monkeypatch):
    first = Action('safety', 'guava', Mock())
    sixth = Action('verification', 'api', Mock())
    safety, verification = Mock(return_value=first), Mock(return_value=sixth)
    monkeypatch.setattr(checklist, 'CHECKS', (
        checklist.Check('1', 'safety', safety), checklist.Check('6', 'verification', verification),
    ))
    assert checklist.evaluate(None, None, None, None) is first
    verification.assert_not_called()
    safety.return_value = checklist.PASS
    assert checklist.evaluate(None, None, None, None) is sixth
    verification.assert_called_once()
    first.run.assert_not_called()
    sixth.run.assert_not_called()


@pytest.fixture
def flow(monkeypatch, callback_path):
    bridge = FakeBridge()
    call = call_fields() | {'identity_confirmed': 'yes', 'submission_decision': 'approve', 'ending_ack': 'done', 'safety_answer': 'safe'}
    call.pop('request_type')
    client = Mock()
    client.get_policy.return_value = PROFILE
    client.submit_claim_draft.return_value = SimpleNamespace(claim_number='CLM-TEST')
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(api_adapter, 'APIAdapter', factory)
    coordinator.register(object(), bridge)
    return SimpleNamespace(call=call, bridge=bridge, client=client, factory=factory,
                           state=coordinator.state_for(call, bridge))


def finish(flow, *tasks):
    for task in tasks:
        flow.bridge.completions[task](flow.call)


def happy_path(flow):
    coordinator.start_call(flow.call, flow.bridge)
    finish(flow, safety.TASK_ID)
    finish(flow, identity.TASK_ID, identity.CONFIRM_TASK_ID)
    flow.call['request_type'] = coordinator.REPORT_OWN_LOSS
    finish(flow, coordinator.REQUEST_TASK_ID, claim_intake.TASK_ID,
           review_claim.REVIEW_TASK_ID, submit_claim.SUBMITTED_TASK_ID)


def outage_path(flow):
    flow.client.list_policies.side_effect = api_adapter.APIUnavailableError('offline')
    coordinator.start_call(flow.call, flow.bridge)
    finish(flow, safety.TASK_ID)
    finish(flow, assistance.OFFER_TASK_ID, assistance.CALLBACK_OFFER_TASK_ID, assistance.CALLBACK_TASK_ID)


def selected(caplog):
    return [(record.args[0], record.args[2]) for record in caplog.records
            if record.name == checklist.__name__]


def test_evaluate_sends_no_commands_or_api_calls(flow):
    routes = coordinator.default_routes(flow.bridge)
    for _ in range(2):
        action = checklist.evaluate(flow.call, flow.state, flow.bridge, routes)
        assert action.name == 'safety_question'
    assert flow.bridge.tasks == flow.bridge.instructions == flow.bridge.infos == []
    flow.factory.assert_not_called()
    assert flow.state.results == {}


def test_completed_handler_waits_without_restarting(flow):
    coordinator.start_call(flow.call, flow.bridge)
    finish(flow, safety.TASK_ID)
    finish(flow, identity.TASK_ID)
    commands = (list(flow.bridge.tasks), list(flow.bridge.instructions), list(flow.bridge.infos))
    calls, results = list(flow.client.mock_calls), dict(flow.state.results)
    finish(flow, identity.TASK_ID)
    assert (flow.bridge.tasks, flow.bridge.instructions, flow.bridge.infos) == commands
    assert flow.client.mock_calls == calls
    assert flow.state.results == results
    assert flow.state.done(identity.TASK_ID)
    assert flow.bridge.tasks[-1].task_id == identity.CONFIRM_TASK_ID


def test_extra_check_runs_after_verification_in_registered_flow(flow, monkeypatch, caplog):
    def extra(call, state, bridge, routes):
        if state.result('extra_situation') is not None:
            return checklist.PASS
        return Action('extra_situation', 'guava', lambda: bridge.send_instruction(call, 'Extra situation.'))

    checks = checklist.CHECKS
    monkeypatch.setattr(checklist, 'CHECKS', checks[:6] + (checklist.Check('5a', 'Extra situation', extra),) + checks[6:])
    with caplog.at_level('INFO', logger=checklist.__name__):
        happy_path(flow)
    sequence = selected(caplog)
    position = sequence.index(('5a', 'extra_situation'))
    assert sequence[position - 1] == ('3', 'start_request')
    assert sequence[position + 1] == ('6', 'check_details')
    assert flow.bridge.instructions == ['Extra situation.']
    assert flow.state.result('extra_situation').ok
    assert flow.bridge.tasks[-1].task_id == 'hang-up'


def test_happy_path_check_action_sequence(flow, caplog):
    with caplog.at_level('INFO', logger=checklist.__name__):
        happy_path(flow)
    assert selected(caplog) == [
        ('1', 'safety_question'), ('5', 'check_api'), ('5', 'start_verification'), ('5', 'start_confirm'), ('5', 'fetch_policy'),
        ('5', 'announce_verification'), ('3', 'start_request'), ('6', 'check_details'), ('6', 'write_summary'), ('6', 'final_consistency'), ('6', 'save_draft'),
        ('6', 'start_review'), ('8', 'submit_claim'), ('9', 'announce_submission'), ('ending', 'hang_up'),
    ]


def test_outage_check_action_sequence(flow, caplog):
    with caplog.at_level('INFO', logger=checklist.__name__):
        outage_path(flow)
    assert selected(caplog) == [
        ('1', 'safety_question'), ('5', 'check_api'), ('5', 'start_assistance'), ('3', 'answer_offer'),
        ('3', 'answer_callback_offer'), ('3', 'save_callback'), ('3', 'announce_callback'),
    ]


@pytest.mark.parametrize('path', [happy_path, outage_path])
def test_each_action_touches_at_most_one_adapter(flow, monkeypatch, path):
    evaluate = checklist.evaluate
    seen = []

    def observed(*args):
        action = evaluate(*args)
        if action is None:
            return None

        def run():
            before_commands = (list(flow.bridge.tasks), list(flow.bridge.instructions), list(flow.bridge.infos))
            before_api = list(flow.factory.mock_calls)
            try:
                return action.run()
            finally:
                commands = (flow.bridge.tasks, flow.bridge.instructions, flow.bridge.infos)
                if action.target in {'api', 'local'}:
                    assert commands == before_commands
                if action.target in {'guava', 'local'}:
                    assert flow.factory.mock_calls == before_api
                seen.append(action.target)

        return Action(action.name, action.target, run, action.failure_types)

    monkeypatch.setattr(checklist, 'evaluate', observed)
    path(flow)
    assert 'guava' in seen and 'api' in seen
    assert ('local' in seen) is (path is outage_path)


def test_source_boundaries_and_completion_inventory():
    source = Path(__file__).resolve().parents[1] / 'src/agent'
    for path in source.rglob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == 'set_fact':
                    assert path.name in {'identity.py', 'claim_intake.py'}
                if node.func.attr == 'set_task':
                    assert path.name != 'policy_lookup.py'
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == 'verified_profile':
                        assert path.name in {'identity.py', 'checklist.py'}
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
                if 'advance' in names:
                    assert path.name == 'coordinator.py'
                if (node.module or '').split('.')[0] == 'guava':
                    assert path.name == 'guava_adapter.py'
                if path.name in {'coordinator.py', 'checklist.py', 'identity.py', 'verification.py', 'review_claim.py'}:
                    assert 'api_adapter' not in names
                    assert 'api_adapter' not in (node.module or '').split('.')
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split('.')[0] == 'guava':
                        assert path.name == 'guava_adapter.py'
                    if path.name in {'coordinator.py', 'checklist.py', 'identity.py', 'verification.py', 'review_claim.py'}:
                        assert 'api_adapter' not in alias.name.split('.')
            elif isinstance(node, ast.Call) and path.parent.name == 'workflows':
                assert not (isinstance(node.func, ast.Attribute) and node.func.attr == 'register_task_complete')
                assert not (isinstance(node.func, ast.Name) and node.func.id == 'register_task_complete')
    for workflow in (identity, claim_intake, review_claim, safety, submit_claim, assistance):
        assert set(workflow.TASK_IDS) <= set(coordinator.COMPLETION_TASKS)


def test_volunteered_request_skips_start_request(flow, caplog):
    flow.call['request_type'] = coordinator.REPORT_OWN_LOSS
    with caplog.at_level('INFO', logger=checklist.__name__):
        coordinator.start_call(flow.call, flow.bridge)
        finish(flow, safety.TASK_ID)
        finish(flow, identity.TASK_ID, identity.CONFIRM_TASK_ID, claim_intake.TASK_ID,
               review_claim.REVIEW_TASK_ID, submit_claim.SUBMITTED_TASK_ID)
    assert ('3', 'start_request') not in selected(caplog)
    assert coordinator.REQUEST_TASK_ID not in [task.task_id for task in flow.bridge.tasks]
    assert flow.bridge.tasks[-1].task_id == 'hang-up'


def test_correction_repeats_save_and_review(flow, caplog):
    with caplog.at_level('INFO', logger=checklist.__name__):
        coordinator.start_call(flow.call, flow.bridge)
        finish(flow, safety.TASK_ID)
        finish(flow, identity.TASK_ID, identity.CONFIRM_TASK_ID)
        flow.call['request_type'] = coordinator.REPORT_OWN_LOSS
        finish(flow, coordinator.REQUEST_TASK_ID, claim_intake.TASK_ID)
        flow.call.update(submission_decision='change', corrected_field='location', corrected_value='Oak Street')
        finish(flow, review_claim.REVIEW_TASK_ID)
    sequence = selected(caplog)
    assert sequence.count(('6', 'save_draft')) == sequence.count(('6', 'start_review')) == 2
    assert ('8', 'submit_claim') not in sequence


def test_timeout_checks_submission_in_check_ten(flow, caplog):
    flow.client.submit_claim_draft.side_effect = api_adapter.APIUnavailableError('timeout')
    flow.client.get_claim_by_reference.return_value = SimpleNamespace(claim_number='CLM-FOUND')
    with caplog.at_level('INFO', logger=checklist.__name__):
        happy_path(flow)
    sequence = selected(caplog)
    index = sequence.index(('9', 'check_submission'))
    assert sequence[index - 1] == ('8', 'submit_claim')
    assert sequence[index + 1] == ('9', 'announce_submission')
