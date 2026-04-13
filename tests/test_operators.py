"""Tests for operator normalization."""

from wednesday_tts.normalize.operators import (
    normalize_negative_numbers,
    normalize_operators,
    normalize_word_dash_number,
)


def test_triple_equals():
    assert "equals" in normalize_operators("===")


def test_not_equals():
    assert "not equals" in normalize_operators("!==")


def test_double_equals():
    assert "equals" in normalize_operators("==")


def test_less_than_or_equal():
    assert "less than or equal" in normalize_operators("<=")


def test_greater_than_or_equal():
    assert "greater than or equal" in normalize_operators(">=")


def test_fat_arrow():
    result = normalize_operators("a => b")
    assert "to" in result


def test_plus_equals():
    assert "plus equals" in normalize_operators("+=")


def test_minus_equals():
    assert "minus equals" in normalize_operators("-=")


def test_standalone_equals():
    result = normalize_operators("x = 5")
    assert "equals" in result


def test_negative_number():
    result = normalize_negative_numbers("-3.5")
    assert "negative" in result


def test_negative_not_subtraction():
    # Subtraction (space before minus) should NOT trigger
    result = normalize_negative_numbers("x - 3")
    assert "negative" not in result


def test_word_dash_number():
    result = normalize_word_dash_number("vm-01")
    assert result == "vm 01"


def test_word_dash_number_node():
    result = normalize_word_dash_number("node-18")
    assert result == "node 18"
