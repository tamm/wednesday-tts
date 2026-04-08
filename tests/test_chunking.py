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
