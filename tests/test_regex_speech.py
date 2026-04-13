"""Tests for regex pattern to spoken description."""

from wednesday_tts.normalize.regex_speech import (
    normalize_hotkeys,
    normalize_html_tags,
    regex_to_speech,
)


def test_basic_regex_spoken():
    result = regex_to_speech(r'\d+')
    assert "digit" in result
    assert "one or more" in result


def test_lookahead():
    result = regex_to_speech(r'(?=foo)')
    assert "lookahead" in result


def test_lookbehind():
    result = regex_to_speech(r'(?<!bar)')
    assert "negative lookbehind" in result


def test_char_class():
    result = regex_to_speech(r'[a-z]')
    assert "one of" in result


def test_quantifier_range():
    result = regex_to_speech(r'x{2,4}')
    assert "2 to 4 times" in result


def test_word_boundary():
    result = regex_to_speech(r'\bword\b')
    assert "word-boundary" in result


def test_html_tag():
    result = normalize_html_tags("<div>")
    assert "div tag" in result


def test_html_closing_tag():
    result = normalize_html_tags("</div>")
    assert "end div" in result


def test_html_self_closing():
    result = normalize_html_tags("<br/>")
    assert "self closing br" in result


def test_hotkey():
    result = normalize_hotkeys("Ctrl+C")
    assert result == "Control C"


def test_hotkey_combo():
    result = normalize_hotkeys("Ctrl+Shift+P")
    assert "Control" in result
    assert "Shift" in result
    assert "P" in result
