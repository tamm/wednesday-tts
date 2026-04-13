"""Tests for backtick identifier expansion and hash detection."""

from wednesday_tts.normalize.identifiers import (
    normalize_dotted_names,
    normalize_escape_sequences,
    normalize_hashes,
    normalize_identifiers,
    normalize_uuids,
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


# --- normalize_uuids ---

def test_uuid_standard():
    result = normalize_uuids("c3dec37b-1b8d-4471-84e3-009d5f227794")
    assert "UUID ending in" in result
    assert "seven nine four" in result


def test_uuid_in_log_line():
    result = normalize_uuids("GET /preview/c3dec37b-1b8d-4471-84e3-009d5f227794 200 OK")
    assert "UUID ending in" in result
    assert result.startswith("GET /preview/")
    assert result.endswith("200 OK")


def test_uuid_in_backticks_survives_pipeline():
    # normalize_uuids runs before normalize_identifiers; backtick gets stripped after
    from wednesday_tts.normalize.identifiers import normalize_identifiers
    text = normalize_uuids("`c3dec37b-1b8d-4471-84e3-009d5f227794`")
    result = normalize_identifiers(text)
    assert "UUID ending in" in result


def test_non_uuid_hex_not_caught():
    # Plain hex without the 8-4-4-4-12 dash pattern should not be replaced
    result = normalize_uuids("abcdef12")
    assert result == "abcdef12"


# --- normalize_dotted_names ---

def test_dotted_module_attr():
    assert normalize_dotted_names("socket.timeout") == "socket dot timeout"


def test_dotted_chained():
    assert normalize_dotted_names("os.path.join") == "os dot path dot join"


def test_dotted_in_prose():
    result = normalize_dotted_names("Use socket.timeout to handle it.")
    assert "socket dot timeout" in result
    # trailing sentence dot should NOT become "dot"
    assert result.endswith(".")


def test_dotted_standalone_domain():
    assert normalize_dotted_names("example.com") == "example dot com"


def test_dotted_subdomain():
    assert normalize_dotted_names("api.github.com") == "api dot github dot com"


def test_dotted_not_decimal():
    # digit.digit is NOT a dotted name — left alone for the decimal rules
    assert normalize_dotted_names("3.14") == "3.14"


def test_dotted_not_url_path():
    # Already-converted URL text won't re-trigger (no bare dot left)
    assert normalize_dotted_names("ta dot mw slash unwatch") == "ta dot mw slash unwatch"


def test_dotted_not_file_extension_already_converted():
    # After file-ext rule runs, "claude dot markdown" has no bare dot — safe
    assert normalize_dotted_names("claude dot markdown") == "claude dot markdown"
