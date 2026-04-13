"""Tests for dictionary loading and application."""

import json
import os
import tempfile

from wednesday_tts.normalize.dictionary import (
    apply_dictionary,
    load_dictionary,
    load_filenames_dict,
)


def test_apply_dictionary_basic(sample_dictionary):
    result = apply_dictionary("The API returned JSON", sample_dictionary)
    assert "Ae pee eye" in result
    assert "jason" in result


def test_apply_dictionary_case_sensitive(sample_dictionary):
    # "API" is case-sensitive — lowercase "api" should not match
    result = apply_dictionary("the api returned data", sample_dictionary)
    assert "Ae pee eye" not in result
    assert "api" in result


def test_apply_dictionary_case_insensitive(sample_dictionary):
    # "TTS" has case_sensitive: false
    result = apply_dictionary("the tts service", sample_dictionary)
    assert "teetee ess" in result


def test_apply_dictionary_word_boundary(sample_dictionary):
    # "API" should not match inside "RAPID"
    result = apply_dictionary("RAPID changes", sample_dictionary)
    assert "Ae pee eye" not in result


def test_apply_dictionary_literal():
    entries = [
        {"pattern": "C#", "replacement": "see sharp", "case_sensitive": True, "literal": True},
    ]
    result = apply_dictionary("I use C# daily", entries)
    assert "see sharp" in result


def test_load_dictionary_from_file():
    data = {
        "replacements": [
            {"pattern": "npm", "pocket": "en pee em"},
            {"pattern": "API", "replacement": "ay pee eye"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(data, f)
        path = f.name

    try:
        result = load_dictionary(path, backend="pocket")
        assert len(result) == 2
        # "npm" has a pocket-specific replacement
        npm_entry = next(e for e in result if e["pattern"] == "npm")
        assert npm_entry["replacement"] == "en pee em"
        # "API" falls back to universal "replacement"
        api_entry = next(e for e in result if e["pattern"] == "API")
        assert api_entry["replacement"] == "ay pee eye"
    finally:
        os.unlink(path)


def test_load_dictionary_missing_file():
    result = load_dictionary("/nonexistent/path.json")
    assert result == []


def test_load_filenames_dict():
    data = {"extensions": {"py": "pie", "js": "jay ess"}}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(data, f)
        path = f.name

    try:
        result = load_filenames_dict(path)
        assert result["py"] == "pie"
        assert result["js"] == "jay ess"
    finally:
        os.unlink(path)
