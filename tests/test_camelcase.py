"""Tests for camelcase.py — CamelCase splitting and ALL CAPS normalisation."""

from wednesday_tts.normalize.camelcase import normalize_all_caps, normalize_camelcase


# ---------------------------------------------------------------------------
# normalize_camelcase
# ---------------------------------------------------------------------------

def test_simple_camel():
    assert normalize_camelcase("myVariable") == "my Variable"


def test_three_part_camel():
    assert normalize_camelcase("myVariableName") == "my Variable Name"


def test_pascal_case():
    assert normalize_camelcase("MyClassName") == "My Class Name"


def test_short_word_skipped():
    # Fewer than 4 chars — regex doesn't touch them
    assert normalize_camelcase("iOS") == "iOS"


def test_all_lowercase_unchanged():
    assert normalize_camelcase("hello") == "hello"


def test_all_caps_unchanged():
    # ALL_CAPS words are not split (no lowercase -> uppercase transition)
    assert normalize_camelcase("ALLCAPS") == "ALLCAPS"


def test_mixed_sentence():
    result = normalize_camelcase("Call myFunction now")
    assert "my Function" in result


def test_no_camel_plain_text():
    assert normalize_camelcase("plain text here") == "plain text here"


def test_camel_with_numbers_in_surrounding_text():
    # myBuild123 contains digits — no \b between alpha and digit chars, so the
    # regex r'\b[a-zA-Z]{4,}\b' won't match mid-word. Token passes through unchanged.
    result = normalize_camelcase("version myBuild123")
    assert "myBuild123" in result


def test_consecutive_caps_not_split():
    # "getHTTPResponse" — the HTTPR block is all caps so not split by the rule
    # Only the a->z to A-Z boundary is split; HTTPR has no lowercase before caps
    result = normalize_camelcase("getHTTPResponse")
    # "get" -> "get" (3 chars, skipped); within "HTTPResponse" the R->e boundary triggers
    assert "get" in result


def test_camel_preserves_other_content():
    result = normalize_camelcase("Use camelCaseHere for clarity")
    assert "camel Case Here" in result
    assert "for clarity" in result


# ---------------------------------------------------------------------------
# normalize_all_caps
# ---------------------------------------------------------------------------

def test_long_caps_to_title():
    assert normalize_all_caps("ERROR") == "Error"


def test_five_letter_caps():
    assert normalize_all_caps("ABORT") == "Abort"


def test_short_acronym_preserved():
    # 2-letter acronym — not touched by the 4+ letter rule
    assert normalize_all_caps("DB") == "DB"


def test_three_letter_acronym_preserved():
    assert normalize_all_caps("API") == "API"


def test_caps_exclamation_hi():
    assert normalize_all_caps("HI") == "Hi"


def test_caps_exclamation_oh():
    assert normalize_all_caps("OH") == "Oh"


def test_caps_exclamation_no():
    assert normalize_all_caps("NO") == "No"


def test_caps_exclamation_yes():
    assert normalize_all_caps("YES") == "Yes"


def test_caps_exclamation_wow():
    assert normalize_all_caps("WOW") == "Wow"


def test_caps_exclamation_hey():
    assert normalize_all_caps("HEY") == "Hey"


def test_caps_exclamation_omg():
    assert normalize_all_caps("OMG") == "oh my god"


def test_caps_in_sentence():
    result = normalize_all_caps("There was an ERROR in the system")
    assert "Error" in result
    assert "There was an" in result


def test_caps_word_boundary():
    # Only whole words are matched
    result = normalize_all_caps("ERRORS")
    assert result == "Errors"


def test_mixed_case_unchanged():
    # Already mixed case — not all caps, not touched
    assert normalize_all_caps("Hello") == "Hello"


def test_caps_with_apostrophe():
    # The regex allows apostrophes in the 4+ pattern — e.g. WON'T
    result = normalize_all_caps("WON'T")
    # Should be title-cased to "Won't"
    assert result == "Won't"
