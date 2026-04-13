"""Tests for homographs.py — disambiguation of 'read' for TTS."""

from wednesday_tts.normalize.homographs import fix_read_homograph

# ---------------------------------------------------------------------------
# Pattern A — passive voice (be-family + read)
# ---------------------------------------------------------------------------


def test_was_read_passive():
    assert fix_read_homograph("it was read aloud") == "it was red aloud"


def test_were_read_passive():
    assert fix_read_homograph("they were read carefully") == "they were red carefully"


def test_is_read_passive():
    assert fix_read_homograph("it is read daily") == "it is red daily"


def test_are_read_passive():
    assert fix_read_homograph("they are read often") == "they are red often"


def test_am_read_passive():
    assert fix_read_homograph("I am read widely") == "I am red widely"


def test_be_read_passive():
    assert fix_read_homograph("to be read later") == "to be red later"


def test_been_read_passive():
    assert fix_read_homograph("it has been read") == "it has been red"


def test_being_read_passive():
    assert fix_read_homograph("it is being read now") == "it is being red now"


# ---------------------------------------------------------------------------
# Pattern B — perfect aspect (have-family + read)
# ---------------------------------------------------------------------------


def test_has_read_perfect():
    assert fix_read_homograph("she has read it") == "she has red it"


def test_have_read_perfect():
    assert fix_read_homograph("I have read the book") == "I have red the book"


def test_had_read_perfect():
    assert fix_read_homograph("he had read the report") == "he had red the report"


# ---------------------------------------------------------------------------
# Pattern C — modal passive (modal + be + read)
# ---------------------------------------------------------------------------


def test_can_be_read():
    assert fix_read_homograph("it can be read here") == "it can be red here"


def test_could_be_read():
    assert fix_read_homograph("it could be read") == "it could be red"


def test_will_be_read():
    assert fix_read_homograph("it will be read tomorrow") == "it will be red tomorrow"


def test_would_be_read():
    assert fix_read_homograph("it would be read") == "it would be red"


def test_shall_be_read():
    assert fix_read_homograph("it shall be read") == "it shall be red"


def test_should_be_read():
    assert fix_read_homograph("it should be read") == "it should be red"


def test_may_be_read():
    assert fix_read_homograph("it may be read") == "it may be red"


def test_might_be_read():
    assert fix_read_homograph("it might be read") == "it might be red"


def test_must_be_read():
    assert fix_read_homograph("it must be read") == "it must be red"


# ---------------------------------------------------------------------------
# Remaining 'read' -> present tense / imperative -> 'reed'
# ---------------------------------------------------------------------------


def test_present_tense_read():
    assert fix_read_homograph("I read every day") == "I reed every day"


def test_imperative_read():
    assert fix_read_homograph("Read the docs") == "Reed the docs"


def test_read_the_file():
    assert fix_read_homograph("Please read the file") == "Please reed the file"


def test_standalone_read():
    assert fix_read_homograph("read") == "reed"


def test_capitalised_read_imperative():
    assert fix_read_homograph("Read carefully") == "Reed carefully"


# ---------------------------------------------------------------------------
# Hyphenated forms — should NOT be converted (negative lookahead (?!-))
# ---------------------------------------------------------------------------


def test_read_hyphenated_unchanged():
    assert fix_read_homograph("read-only") == "read-only"


def test_was_read_hyphenated_unchanged():
    assert fix_read_homograph("was read-only") == "was read-only"


def test_have_read_hyphenated_unchanged():
    assert fix_read_homograph("have read-only access") == "have read-only access"


# ---------------------------------------------------------------------------
# Case insensitivity — patterns A/B/C are case-insensitive
# ---------------------------------------------------------------------------


def test_uppercase_was_read():
    assert fix_read_homograph("IT WAS READ") == "IT WAS red"


def test_mixed_case_has_read():
    assert fix_read_homograph("She Has Read it") == "She Has red it"


# ---------------------------------------------------------------------------
# Sentences with multiple occurrences
# ---------------------------------------------------------------------------


def test_multiple_reads_in_sentence():
    result = fix_read_homograph("Read the docs after you have read them once")
    # First "Read" is imperative -> "Reed"
    assert result.startswith("Reed")
    # "have read" is perfect -> "have red"
    assert "have red" in result


def test_no_read_passthrough():
    assert (
        fix_read_homograph("She wrote and edited the document")
        == "She wrote and edited the document"
    )
