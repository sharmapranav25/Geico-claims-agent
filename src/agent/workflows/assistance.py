"""Outage assistance and locally saved callback requests."""

import json
import logging
import os
import re
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import api_adapter
from ..action_loop import Action, advance
from ..bridge import FieldDefinition

logger = logging.getLogger(__name__)
OFFER_TASK_ID = 'assistance-offer'
CALLBACK_OFFER_TASK_ID = 'callback-offer'
CALLBACK_TASK_ID = 'collect-callback'
UNAVAILABLE_LINE = "I'm sorry, I'm unable to assist you right now. Would you like to talk to a GEICO representative?"
NO_REP_LINE = 'No GEICO representatives are available right now. Would you like to schedule a callback? We call back on weekdays between 9 AM and 4 PM Pacific.'
CALLBACK_ZONE = ZoneInfo('America/Los_Angeles')


def check_api():
    with api_adapter.APIAdapter() as api:
        return api.list_policies()


def check_api_action():
    return Action('check_api', 'api', check_api, (api_adapter.APIError, ValueError))


def parse_callback_time(value, now=None):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    parsed = parsed.replace(tzinfo=CALLBACK_ZONE) if parsed.tzinfo is None else parsed.astimezone(CALLBACK_ZONE)
    now = now if now is not None else datetime.now(CALLBACK_ZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CALLBACK_ZONE)
    if parsed <= now or parsed.weekday() >= 5 or not time(9) <= parsed.time() <= time(16):
        return None
    return parsed


def valid_callback_phone(value):
    if not isinstance(value, str):
        return False
    digits = re.sub(r'\D', '', value)
    return len(digits) == 10 or (len(digits) == 11 and digits.startswith('1'))


def callback_file():
    return Path(os.environ.get('CALLBACK_FILE', Path(__file__).resolve().parents[3] / 'data/callbacks.jsonl'))


def save_callback(record):
    path = callback_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def start(call, bridge):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    bridge.set_task(call, OFFER_TASK_ID, say=[UNAVAILABLE_LINE],
        objective=(
            "The policy system is unavailable. Do not ask how you can help and do not collect "
            "policy or claim details. Record the caller's answer. " + KEEP_CONTEXT_RULE + CALL_ENDING_RULE
        ),
        fields=[FieldDefinition('wants_representative', "Whether the caller wants a GEICO representative.",
                                field_type='multiple_choice', choices=('yes', 'no'))],
    )


def next_offer_action(call, state, bridge):
    if state.result('answer_offer') is not None:
        return None
    if bridge.get_field(call, 'wants_representative') == 'yes':
        logger.info('Representative check none available')
        return Action('answer_offer', 'guava', lambda: _offer_callback(call, bridge))
    return Action('answer_offer', 'guava', lambda: _close_assistance(call, bridge))


def _offer_callback(call, bridge):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    bridge.set_task(call, CALLBACK_OFFER_TASK_ID, say=[NO_REP_LINE],
        objective=("Record whether the caller wants a callback. If they give a time or say they want one, "
                   "record yes without asking again. Never say a callback is noted, scheduled or updated; "
                   "the application confirms that later. " + KEEP_CONTEXT_RULE + CALL_ENDING_RULE),
        fields=[FieldDefinition('wants_callback', 'Whether the caller wants a callback. Giving a callback time counts as yes.',
                                field_type='multiple_choice', choices=('yes', 'no'))],
    )


def next_callback_offer_action(call, state, bridge):
    if state.result('answer_callback_offer') is not None:
        return None
    if bridge.get_field(call, 'wants_callback') == 'yes':
        return Action('answer_callback_offer', 'guava', lambda: _collect_callback(call, bridge))
    return Action('answer_callback_offer', 'guava', lambda: _close_assistance(call, bridge))


def _collect_callback(call, bridge):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    today = datetime.now(CALLBACK_ZONE).strftime('%Y-%m-%d, %A')
    bridge.set_task(call, CALLBACK_TASK_ID,
        objective=("Collect the requested callback time and phone number. Callback times are weekdays "
                   "between 9 AM and 4 PM Pacific. Never say a callback is noted, scheduled, updated or saved; "
                   "the application confirms that after this step. Use any time the caller already gave. "
                   + KEEP_CONTEXT_RULE + CALL_ENDING_RULE),
        fields=[
            FieldDefinition('callback_time', f'When the caller wants the callback. Today in Pacific time is {today}. '
                            'Record it as ISO 8601 with no offset, like 2026-11-03T10:00:00.'),
            FieldDefinition('callback_phone', 'The phone number to call back.'),
        ],
    )


def next_callback_action(call, state, bridge):
    saved = state.result('save_callback')
    if saved is None:
        def write_callback():
            callback_time = parse_callback_time(bridge.get_field(call, 'callback_time'))
            if callback_time is None:
                raise ValueError('Callback time is no longer valid')
            record = {
                'session_id': state.session_id,
                'callback_time': callback_time.isoformat(),
                'callback_phone': bridge.get_field(call, 'callback_phone'),
                'created_at': datetime.now(CALLBACK_ZONE).isoformat(),
            }
            save_callback(record)
            return record

        return Action('save_callback', 'local', write_callback, (OSError, ValueError))
    if state.result('announce_callback') is not None:
        return None
    return Action('announce_callback', 'guava',
                  lambda: _announce_callback_saved(call, bridge) if saved.ok else _announce_callback_failed(call, bridge))


def _announce_callback_saved(call, bridge):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    logger.info('Callback saved')
    bridge.set_task(call, 'callback-saved', objective=(
        'The callback request was saved. Confirm the requested time and phone number to the caller. '
        'Do not promise a specific person will call. ' + KEEP_CONTEXT_RULE + CALL_ENDING_RULE
    ))


def _announce_callback_failed(call, bridge):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    logger.info('Callback save failed')
    bridge.set_task(call, 'callback-save-failed', objective=(
        'The callback request could not be saved. Tell the caller honestly; do not claim otherwise. '
        + KEEP_CONTEXT_RULE + CALL_ENDING_RULE
    ))


def _close_assistance(call, bridge):
    from ..coordinator import KEEP_CONTEXT_RULE, CALL_ENDING_RULE

    bridge.set_task(call, 'assistance-closed', objective=(
        'Acknowledge politely and offer nothing further. Do not claim a callback was requested or saved. '
        + KEEP_CONTEXT_RULE + CALL_ENDING_RULE
    ))


def register(agent, bridge, get_state):
    def on_offer_complete(call):
        advance(get_state(call), lambda state: next_offer_action(call, state, bridge))

    def on_callback_offer_complete(call):
        advance(get_state(call), lambda state: next_callback_offer_action(call, state, bridge))

    def on_callback_complete(call):
        advance(get_state(call), lambda state: next_callback_action(call, state, bridge))

    def validate_time(call, value):
        if parse_callback_time(value) is None:
            logger.info('Callback time rejected')
            return False, 'Callback times are weekdays between 9 AM and 4 PM Pacific. Ask the caller for a time in that window.'
        return True

    def validate_phone(call, value):
        if not valid_callback_phone(value):
            return False, 'Ask the caller for a full 10-digit phone number.'
        return True

    bridge.register_task_complete(agent, OFFER_TASK_ID, on_offer_complete)
    bridge.register_task_complete(agent, CALLBACK_OFFER_TASK_ID, on_callback_offer_complete)
    bridge.register_task_complete(agent, CALLBACK_TASK_ID, on_callback_complete)
    bridge.register_validator(agent, 'callback_time', validate_time)
    bridge.register_validator(agent, 'callback_phone', validate_phone)
