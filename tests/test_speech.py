"""Offline spoken formatting and local date context."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agent import speech
from agent.call_state import CallState
from agent.workflows import claim_intake, identity
from test_claim_intake import PROFILE, FakeBridge


@pytest.mark.parametrize('value,expected', [(1120, '1, 1, 2, 0'), ('CLM-A1', 'C, L, M, A, 1'),
    ('+1 (628) 268-8753', '1, 6, 2, 8, 2, 6, 8, 8, 7, 5, 3'), ('--', '')])
def test_spell_out(value, expected):
    assert speech.spell_out(value) == expected


def test_date_context():
    assert speech.date_context(datetime(2026, 11, 2, 12)) == (
        'Today is Monday, November 2, 2026. Yesterday was Sunday, November 1, 2026.')
    assert speech.date_context(datetime(2027, 1, 1, 12)) == (
        'Today is Friday, January 1, 2027. Yesterday was Thursday, December 31, 2026.')


def test_date_context_uses_local_day():
    now = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)
    local = now.astimezone()
    assert speech.date_context(now) == speech.date_context(local.replace(tzinfo=None))


def test_date_context_defaults_to_local_now():
    now = datetime(2026, 11, 2, 12).astimezone()
    with patch.object(speech, 'datetime') as clock:
        clock.now.return_value = now
        assert speech.date_context() == speech.date_context(now)


@pytest.mark.parametrize('value,expected', [
    ('2026-11-01T17:00:00', 'Sunday, November 1 at 5 PM'),
    ('  2026-11-01T17:00:00  ', 'Sunday, November 1 at 5 PM'),
    ('2026-11-01T17:05:00', 'Sunday, November 1 at 5:05 PM'),
    ('2026-11-01T00:00:00', 'Sunday, November 1 at 12 AM'),
])
def test_spoken_date(value, expected):
    assert speech.spoken_date(value) == expected


def test_collection_includes_date_context_iso_and_own_words(monkeypatch):
    context = 'Today is Monday, November 2, 2026. Yesterday was Sunday, November 1, 2026.'
    monkeypatch.setattr(speech, 'date_context', lambda: context)
    bridge = FakeBridge()
    claim_intake.start({}, bridge, CallState('test', verified_profile=PROFILE))
    for fields, time_key in [(identity.identity_fields(), 'stated_time'),
                             (bridge.tasks[-1][2], 'occurred_at')]:
        descriptions = {field.key: field.description for field in fields}
        assert context in descriptions[time_key]
        assert 'ISO 8601' in descriptions[time_key]
        assert "Record what happened in the caller's own words, for example 'my mother', not 'the caller's mother'." in descriptions['description']
