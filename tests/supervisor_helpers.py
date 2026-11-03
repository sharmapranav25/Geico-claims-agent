"""Offline model answers and waiting for background closeout in workflow tests."""

import json
import time

from agent import coordinator
from agent.call_state import ActionResult
from agent.workflows import safety


def safe_generate(prompt, json_schema):
    keys = json_schema['properties']
    if 'summary' in keys:
        description = json.loads(prompt.split('\n')[-1]).get('description')
        return json.dumps({'summary': description or 'An incident was reported.', 'report': description or 'An incident was reported.', 'loss_type': 'collision'})
    if 'role' in keys:
        return json.dumps({'danger': False, 'role': 'unverified', 'multiple_incidents': False, 'inconsistent': False})
    if 'fields' in keys:
        return json.dumps({'inconsistent': False, 'fields': [], 'reason': ''})
    return json.dumps({key: True for key in keys})


def after_safety(state):
    state.mark_done(safety.TASK_ID)
    return state


def closeout_done(state):
    state.summary_ready = state.consistency_ready = True
    for name in ('write_summary', 'final_consistency'):
        state.record(ActionResult(name, 'guava', True))


def wait_closeout(state):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with state.lock:
            pending = (state.result('write_summary') is not None and not state.summary_ready
                       or state.result('final_consistency') is not None and not state.consistency_ready)
            if not pending or state.ended or state.stopped or state.claim_blocked:
                return
        time.sleep(0.001)
    raise AssertionError('Background closeout did not finish')


def completion(bridge, handler):
    def complete(call):
        result = handler(call)
        state = bridge.state if hasattr(bridge, 'state') else coordinator.state_for(call, bridge)
        wait_closeout(state)
        return result
    return complete
