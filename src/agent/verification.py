"""Pure verification rules; no adapters, conversation, logging or state writes."""

import re
from datetime import datetime, time
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

NAME_MATCH_THRESHOLD = 0.85
CALLBACK_ZONE = ZoneInfo('America/Los_Angeles')


def identity_matches(profile, caller_name: str, caller_phone: str) -> bool:
    # Require BOTH: all seeded policies share a phone number. Names allow case,
    # punctuation, word order and small spelling differences; phone digits must
    # match exactly, apart from punctuation and an optional US country code.
    def name(value):
        words = re.sub(r"[^\w\s]", "", value.casefold()).split() if isinstance(value, str) else []
        return " ".join(sorted(words))

    def phone(value):
        digits = re.sub(r"\D", "", value) if isinstance(value, str) else ""
        return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits

    stated_name, stated_phone = name(caller_name), phone(caller_phone)
    if not (stated_name and stated_phone) or stated_phone != phone(profile.phone):
        return False
    return (SequenceMatcher(None, stated_name, name(profile.name)).ratio() >= NAME_MATCH_THRESHOLD
            or SequenceMatcher(None, re.sub(r'[^a-z]', '', caller_name.casefold()),
                               re.sub(r'[^a-z]', '', profile.name.casefold())).ratio() >= NAME_MATCH_THRESHOLD)


def parse_occurred_at(value):
    # The API requires a timezone; a time without one is the caller's local time.
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def validate_occurred_at(value, now=None):
    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    parsed = parse_occurred_at(value)
    if parsed is None:
        return False, "Ask the caller again when the loss happened, including the date and approximate time."
    now = now if now is not None else datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    if parsed > now:
        return False, "That time has not happened yet. Ask the caller for the date and time of the loss."
    return True


def missing_required(facts, required_keys):
    return tuple(key for key in required_keys if facts.get(key) is None
                 or (isinstance(facts.get(key), str) and not facts[key].strip()))


def correction_is_valid(field, value, vehicle_labels, loss_types):
    if field == 'vehicle':
        return value in vehicle_labels
    if field == 'loss_type':
        return value in loss_types
    if not isinstance(value, str) or not value.strip():
        return False
    if field == 'occurred_at':
        return validate_occurred_at(value) is True
    return field in ('location', 'description')


def approval_is_valid(decision, reviewed_facts, current_facts):
    return decision == 'approve' and reviewed_facts is not None and reviewed_facts == current_facts


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


