"""Tests for backtick identifier expansion and hash detection."""

from wednesday_tts.normalize.identifiers import (
    normalize_identifiers, normalize_escape_sequences, normalize_hashes,
    pattern_descriptor_to_speech,
)


def test_snake_case():
    result = normalize_identifiers("`my_var`")
    assert result == "my var"


def test_snake_case_with_acronym():
    result = normalize_identifiers("`api_url`")
    assert "Ae pee eye" in result
    assert "you ar el" in result


def test_stderr():
    # stderr has no underscore — expand_identifier_part not called, returns as-is
    result = normalize_identifiers("`stderr`")
    assert result == "stderr"


def test_fn_ctx():
    result = normalize_identifiers("`fn_ctx`")
    assert "function" in result
    assert "context" in result


def test_single_letter():
    # Single char: pattern_descriptor_to_speech treats it as "1 characters"
    result = normalize_identifiers("`x`")
    assert result == "1 characters"


def test_no_underscore_passthrough():
    result = normalize_identifiers("`hello world`")
    assert result == "hello world"


def test_git_hash():
    result = normalize_identifiers("`f4c5c15`")
    assert "hash ending in" in result


def test_sha256_prefixed_hash():
    result = normalize_identifiers("`sha256:abc123def456`")
    assert "sha256 hash ending in" in result


def test_all_digits_not_hash():
    # All digits — not a hash (no letters)
    result = normalize_identifiers("`1234567`")
    assert "hash" not in result


def test_all_alpha_not_hash():
    # All letters — not a hash (no digits)
    result = normalize_identifiers("`deadbeef`")
    assert "hash" not in result


def test_repeated_char_pattern():
    result = pattern_descriptor_to_speech("xxxx xxxx xxxx xxxx")
    assert "4 blocks of 4" in result


def test_repeated_char_single_block():
    result = pattern_descriptor_to_speech("****")
    assert "4 characters" in result


def test_repeated_char_dashes():
    result = pattern_descriptor_to_speech("xxxx-xxxx")
    assert "blocks" in result
    assert "dashes" in result


def test_escape_sequences():
    assert "new line" in normalize_escape_sequences("\\n")
    assert "double new line" in normalize_escape_sequences("\\n\\n")
    assert "tab" in normalize_escape_sequences("\\t")


def test_hash_number_reference():
    result = normalize_hashes("PR #579")
    assert "number 579" in result


def test_c_sharp():
    result = normalize_hashes("I use C#")
    assert "see sharp" in result


def test_f_sharp():
    result = normalize_hashes("F# is cool")
    assert "eff sharp" in result


def test_heading_hash_preserved():
    # Heading hashes (followed by space at line start) should be preserved
    result = normalize_hashes("# Title")
    assert result == "# Title"
