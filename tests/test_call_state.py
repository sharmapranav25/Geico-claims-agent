"""Facts replace values and invalidate approval only on change."""
from agent.call_state import ActionResult, CallState


def test_fact_replacement_and_approval():
    state = CallState('one')
    assert state.fact('location') is None
    state.set_fact('location', 'Main')
    state.approved_fields = {'location': 'Main'}
    state.set_fact('location', 'Main')
    assert state.approved_fields == {'location': 'Main'}
    state.set_fact('location', 'Oak')
    assert state.approved_fields is None
    assert state.facts == (('location', 'Oak'),)
    assert state.facts_dict() == {'location': 'Oak'}
    assert CallState('two').facts == ()


def test_clear_results_and_done():
    state = CallState('one')
    state.record(ActionResult('save', 'api', True))
    state.record(ActionResult('keep', 'guava', True))
    state.mark_done('review')
    state.mark_done('keep')
    state.clear_results('save', 'missing')
    state.clear_done('review', 'missing')
    assert state.result('save') is None and state.result('keep').ok
    assert not state.done('review') and state.done('keep')
