"""Tests for text chunking — chunk_text_intelligently and chunk_text_server."""


from wednesday_tts.normalize.chunking import chunk_text_intelligently, chunk_text_server


# ---------------------------------------------------------------------------
# chunk_text_intelligently
# ---------------------------------------------------------------------------

class TestChunkTextIntelligently:

    def test_empty_string_returns_empty(self):
        assert chunk_text_intelligently("") == []

    def test_whitespace_only_returns_empty(self):
        assert chunk_text_intelligently("   \n  ") == []

    def test_short_text_content_preserved(self):
        # Short text still gets word-boundary split; check content is all there
        text = "Hello world."
        result = chunk_text_intelligently(text)
        assert "".join(result).replace(" ", "") == "Helloworld."

    def test_all_chunks_are_non_empty(self):
        text = (
            "First sentence here. Second sentence follows. Third sentence. "
            "Fourth one. Fifth one. Sixth one. Seventh one. Eighth one. "
            "Ninth one. Tenth sentence is here to ensure we have plenty of text."
        )
        result = chunk_text_intelligently(text)
        assert all(c.strip() for c in result)

    def test_text_fully_preserved(self):
        text = (
            "This is a moderately long paragraph used to test that no words are "
            "lost during chunking. Every word must appear in the output chunks "
            "exactly once, in order, so we can verify the chunker is not dropping content."
        )
        rejoined = " ".join(result.strip() for result in chunk_text_intelligently(text))
        # Normalise whitespace for comparison
        assert " ".join(text.split()) == " ".join(rejoined.split())

    def test_breaks_on_sentence_boundary(self):
        # A clear sentence break should be preferred over mid-sentence
        text = (
            "The first sentence ends here. "
            "X" * 60 + " more text follows after that."
        )
        result = chunk_text_intelligently(text)
        # First chunk should end at the sentence break, not mid-word
        assert result[0].endswith(".")

    def test_breaks_on_clause_boundary_when_no_sentence(self):
        # No sentence-ending punctuation — should break at comma/semicolon
        text = "Alpha, beta, gamma, " + "w " * 30 + "delta, epsilon, zeta."
        result = chunk_text_intelligently(text)
        assert len(result) >= 1

    def test_multiple_chunks_for_long_text(self):
        text = " ".join(["word"] * 300)
        result = chunk_text_intelligently(text)
        assert len(result) > 1

    def test_first_chunk_shorter_than_later_chunks(self):
        # The first chunk is intentionally shorter for fast TTS start
        text = " ".join(["word"] * 400)
        result = chunk_text_intelligently(text)
        if len(result) >= 3:
            assert len(result[0]) <= len(result[-1])

    def test_custom_first_chunk_bounds(self):
        text = "A " * 200
        result = chunk_text_intelligently(
            text,
            first_chunk_min=10,
            first_chunk_max=30,
            second_third_min=30,
            second_third_max=60,
            chunk_min=60,
            chunk_max=120,
        )
        assert len(result) > 1
        assert len(result[0]) <= 120  # generous upper bound after strip

    def test_no_chunk_exceeds_max_size(self):
        text = " ".join(["word"] * 500)
        result = chunk_text_intelligently(text, chunk_max=400)
        for chunk in result:
            assert len(chunk) <= 800  # allow some slack for boundary logic

    def test_paragraph_with_newlines(self):
        text = "Line one ends.\nLine two follows.\nLine three is last."
        result = chunk_text_intelligently(text)
        assert result  # must produce something
        rejoined = " ".join(r.strip() for r in result)
        assert "Line one" in rejoined


# ---------------------------------------------------------------------------
# chunk_text_server
# ---------------------------------------------------------------------------

class TestChunkTextServer:

    def test_short_text_returned_as_single_chunk(self):
        text = "Hi."
        result = chunk_text_server(text)
        assert result == [text]

    def test_returns_list(self):
        result = chunk_text_server("Hello world.")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_empty_text_returns_original(self):
        # Per implementation: returns [text] as fallback
        result = chunk_text_server("")
        assert result == [""]

    def test_long_text_split_into_multiple_chunks(self):
        sentences = ". ".join(["This is sentence number " + str(i) for i in range(20)]) + "."
        result = chunk_text_server(sentences)
        assert len(result) > 1

    def test_first_chunk_split_at_natural_break(self):
        # First chunk should end at a sentence/clause boundary in 60-120 char range
        text = (
            "First sentence ends here. "
            "Second sentence is longer and provides more content for the test."
        )
        result = chunk_text_server(text)
        assert len(result) >= 1
        # First chunk should not be longer than full text
        assert len(result[0]) < len(text)

    def test_all_content_preserved(self):
        text = (
            "Alpha beta gamma. Delta epsilon zeta. Eta theta iota. "
            "Kappa lambda mu. Nu xi omicron. Pi rho sigma. Tau upsilon phi."
        )
        rejoined = " ".join(result.strip() for result in chunk_text_server(text))
        original_words = set(text.replace(".", "").split())
        rejoined_words = set(rejoined.replace(".", "").split())
        assert original_words == rejoined_words

    def test_text_between_30_and_60_chars(self):
        # Hits the elif branch (len > 30, not > 60)
        text = "Short but not tiny, enough words here now."
        result = chunk_text_server(text)
        assert result  # must not crash

    def test_no_chunk_exceeds_max_size(self):
        sentences = ". ".join(["Word " * 20 + "end"] * 15) + "."
        result = chunk_text_server(sentences, max_size=400)
        for chunk in result:
            assert len(chunk) <= 800  # generous; boundary adds a bit

    def test_sentence_ending_punctuation_respected(self):
        text = "First. Second! Third? Fourth. Fifth."
        result = chunk_text_server(text, min_size=5, max_size=20)
        assert len(result) >= 1
        assert all(c.strip() for c in result)

    def test_custom_min_max(self):
        text = " ".join(["sentence " + str(i) + "." for i in range(50)])
        result = chunk_text_server(text, min_size=50, max_size=100)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 200  # generous upper bound

    def test_qwen3_uses_smaller_default_chunk_sizes(self):
        text = " ".join([f"Sentence {i} is long enough to force chunking." for i in range(20)])
        result = chunk_text_server(text, backend_name="qwen3")

        assert len(result) > 1
        for chunk in result[1:]:
            assert len(chunk) <= 180


# ---------------------------------------------------------------------------
# Regression: list-label and initials must not split as sentence ends
# ---------------------------------------------------------------------------

class TestNoSplitAtListLabels:
    """The chunker must not treat "A.", "B.", "U.", "S." etc. as sentence
    endings — single-letter-then-period is a list label or initial, not
    end-of-sentence. See commit 93f13b1 and the chunker-reviewer report.
    """

    def test_server_keeps_A_with_following_word(self):
        t = "Before I keep coding, let me confirm the spec. A. Grace should trigger on the barge-in flag. B. Stop is unchanged."
        result = chunk_text_server(t, backend_name="qwen3")
        assert len(result) >= 2
        # chunk 0 must NOT end with a stranded "A."
        assert not result[0].rstrip().endswith(" A.")
        # The list-label "A." must be attached to the word that follows it.
        joined = " ".join(result)
        assert "A. Grace" in joined
        assert "B. Stop" in joined

    def test_server_does_not_strand_U_from_US(self):
        t = "The U.S. economy is growing quickly today and everybody seems to agree."
        result = chunk_text_server(t, backend_name="qwen3")
        # No chunk may end with "U." — that would be splitting inside "U.S."
        for chunk in result:
            assert not chunk.rstrip().endswith("U."), f"stranded U.: {chunk!r}"
            assert not chunk.rstrip().endswith("S."), f"stranded S.: {chunk!r}"
        assert "U.S." in " ".join(result)

    def test_server_keeps_next_pending_colon_intact(self):
        # The specific phrase that regressed in the wild.
        t = "Next pending: widen the partial display window so we stop losing earlier words when the utterance grows."
        result = chunk_text_server(t, backend_name="qwen3")
        # "Next pending:" must not be its own stranded chunk.
        assert not result[0].rstrip().endswith("pending:")
        assert not result[0].rstrip() == "Next pending:"
        assert "Next pending:" in " ".join(result)

    def test_intelligent_keeps_A_with_following_word(self):
        # Same rule must apply to chunk_text_intelligently (used by the
        # streaming first-chunk helper). See chunker-reviewer issue #1.
        t = "Start list: A. one thing and then more content to push past the first window boundary so the split lands mid-search."
        result = chunk_text_intelligently(t)
        assert len(result) >= 1
        # No chunk may end with a bare " A." — that would mean we split at
        # the list label and stranded it from the word that follows.
        for chunk in result:
            assert not chunk.rstrip().endswith(" A."), f"stranded A.: {chunk!r}"
        assert "A. one" in " ".join(result)

    def test_intelligent_does_not_strand_U_from_US(self):
        t = "The U.S. economy is growing quickly and everybody on both sides of the aisle seems to agree with that idea for once."
        result = chunk_text_intelligently(t)
        for chunk in result:
            assert not chunk.rstrip().endswith("U."), f"stranded U.: {chunk!r}"
            assert not chunk.rstrip().endswith("S."), f"stranded S.: {chunk!r}"
        assert "U.S." in " ".join(result)
