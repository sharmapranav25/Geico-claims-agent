"""SDK-independent formatting for spoken numbers and dates."""

from datetime import datetime, timedelta


def spell_out(value) -> str:
    return ', '.join(character for character in str(value) if character.isalnum())


def date_context(now=None) -> str:
    today = (now or datetime.now().astimezone()).astimezone().date()
    yesterday = today - timedelta(days=1)
    return (f"Today is {today.strftime('%A, %B %-d, %Y')}. "
            f"Yesterday was {yesterday.strftime('%A, %B %-d, %Y')}.")


def spoken_date(value) -> str:
    date = datetime.fromisoformat(value.strip()) if isinstance(value, str) else value
    time_format = '%-I %p' if date.minute == 0 else '%-I:%M %p'
    return date.strftime('%A, %B %-d at ' + time_format)
