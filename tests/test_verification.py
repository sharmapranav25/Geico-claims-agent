"""Pure verification rules for identity, time, gaps and approval."""
import ast
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from agent import verification


def test_standard_library_only():
    tree = ast.parse(Path(verification.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split('.')[0] in sys.stdlib_module_names for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module.split('.')[0] in sys.stdlib_module_names


@pytest.mark.parametrize('name,expected', [('Jessica Brown', True), ('Jesica Brown', True),
    ('J E S S I C A B R O W N', True), ('Another Person', False)])
def test_identity_matches(name, expected):
    profile = SimpleNamespace(name='Jessica Brown', phone='628-268-8753')
    assert verification.identity_matches(profile, name, '+1 (628) 268-8753') is expected
    assert verification.identity_matches(profile, name, '5555555555') is False


@pytest.mark.parametrize('value,valid', [(None, True), (' ', True), ('tomorrow', False),
    ('2026-11-01T12:00:00-08:00', True), ('2026-11-03T12:00:00-08:00', False)])
def test_dates(value, valid):
    assert (verification.validate_occurred_at(value, datetime.fromisoformat('2026-11-02T12:00:00-08:00')) is True) is valid


def test_missing_required():
    assert verification.missing_required({'a': None, 'b': ' ', 'c': 'value'}, ('a', 'b', 'c', 'd')) == ('a', 'b', 'd')


@pytest.mark.parametrize('field,value,valid', [('vehicle', 'Camry', True), ('vehicle', 'Other', False),
    ('loss_type', 'collision', True), ('loss_type', 'bad', False), ('location', 'Oak', True),
    ('description', ' ', False), ('occurred_at', None, False), ('occurred_at', '2026-01-01T12:00:00', True),
    ('occurred_at', '2999-01-01T12:00:00', False), ('caller_name', 'Name', False)])
def test_correction(field, value, valid):
    assert verification.correction_is_valid(field, value, ('Camry',), ('collision',)) is valid


@pytest.mark.parametrize('decision,reviewed,current,valid', [('approve', {'a': 1}, {'a': 1}, True),
    ('approve', None, {}, False), ('decline', {}, {}, False), ('approve', {'a': 1}, {'a': 2}, False)])
def test_approval(decision, reviewed, current, valid):
    assert verification.approval_is_valid(decision, reviewed, current) is valid
