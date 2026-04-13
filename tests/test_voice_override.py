"""Tests for per-request voice override in daemon and client API."""

from __future__ import annotations

import numpy as np

from wednesday_tts.client.api import voice_tag
from wednesday_tts.server.daemon import _render_segments, _split_voice_segments


class TestVoiceTag:
    def test_sam_default(self) -> None:
        assert voice_tag("Hello") == "««Hello»»"
        assert voice_tag("Hello", "sam") == "««Hello»»"

    def test_named_voice(self) -> None:
        assert voice_tag("Hi", "tamm1") == "««tamm1»Hi»»"

    def test_index_voice(self) -> None:
        assert voice_tag("Hi", "3") == "««3»Hi»»"


class TestSplitVoiceSegments:
    def test_plain_text_no_tags(self) -> None:
        segs = _split_voice_segments("Hello world")
        assert segs == [(None, None, "Hello world")]

    def test_sam_tagged_block(self) -> None:
        segs = _split_voice_segments("««Hello»»")
        assert segs == [("sam", None, "Hello")]

    def test_pool_index_tag(self) -> None:
        segs = _split_voice_segments("««4»Hello from pool»»")
        # Resolves through _resolve_pool_entry — result is a dict or string
        assert len(segs) == 1
        assert segs[0][2] == "Hello from pool"

    def test_sam_with_surrounding_text(self) -> None:
        text = "Normal voice. ««Robot voice.»» Normal again."
        segs = _split_voice_segments(text)
        assert len(segs) == 3
        assert segs[0] == (None, None, "Normal voice.")
        assert segs[1] == ("sam", None, "Robot voice.")
        assert segs[2] == (None, None, "Normal again.")

    def test_pool_name_with_surrounding_text(self) -> None:
        text = "Normal. ««0»Different voice.»» Normal."
        segs = _split_voice_segments(text)
        assert len(segs) == 3
        assert segs[0] == (None, None, "Normal.")
        assert segs[2] == (None, None, "Normal.")

    def test_multiple_tagged_blocks(self) -> None:
        text = "Start. ««Robot.»» Middle. ««More robot.»» End."
        segs = _split_voice_segments(text)
        assert len(segs) == 5
        assert segs[0] == (None, None, "Start.")
        assert segs[1] == ("sam", None, "Robot.")
        assert segs[2] == (None, None, "Middle.")
        assert segs[3] == ("sam", None, "More robot.")
        assert segs[4] == (None, None, "End.")

    def test_mixed_voice_types(self) -> None:
        text = "Normal. ««0»Pool voice.»» Middle. ««SAM voice.»» End."
        segs = _split_voice_segments(text)
        assert len(segs) == 5
        assert segs[0] == (None, None, "Normal.")
        assert segs[2] == (None, None, "Middle.")
        assert segs[3] == ("sam", None, "SAM voice.")
        assert segs[4] == (None, None, "End.")

    def test_adjacent_tagged_blocks(self) -> None:
        text = "««Robot.»»««0»Neural.»»"
        segs = _split_voice_segments(text)
        assert len(segs) == 2
        assert segs[0] == ("sam", None, "Robot.")
        assert segs[1][2] == "Neural."

    def test_only_leading_text(self) -> None:
        text = "Hello ««Robot»»"
        segs = _split_voice_segments(text)
        assert len(segs) == 2
        assert segs[0] == (None, None, "Hello")
        assert segs[1] == ("sam", None, "Robot")

    def test_only_trailing_text(self) -> None:
        text = "««Robot»» Bye"
        segs = _split_voice_segments(text)
        assert len(segs) == 2
        assert segs[0] == ("sam", None, "Robot")
        assert segs[1] == (None, None, "Bye")

    def test_empty_tagged_block_skipped(self) -> None:
        text = "Hello ««»» world"
        segs = _split_voice_segments(text)
        # Empty guillemets produce no match (regex requires .+?)
        assert segs == [(None, None, "Hello ««»» world")]

    def test_empty_string(self) -> None:
        assert _split_voice_segments("") == []

    def test_whitespace_only(self) -> None:
        assert _split_voice_segments("   ") == []

    def test_whole_message_wrapped(self) -> None:
        """Simulates what hooks do — entire message wrapped in a pool index."""
        text = "««4»This is the whole message.»»"
        segs = _split_voice_segments(text)
        assert len(segs) == 1
        assert segs[0][2] == "This is the whole message."

    # ── Instruct tag tests ──────────────────────────────────────────────

    def test_instruct_only_no_voice(self) -> None:
        text = "««|calm and warm»Gentle message»»"
        segs = _split_voice_segments(text)
        assert segs == [(None, "calm and warm", "Gentle message")]

    def test_voice_with_empty_instruct(self) -> None:
        """Pipe with no instruct after it — instruct should be None."""
        text = "««sam|»Hello»»"
        segs = _split_voice_segments(text)
        assert segs == [("sam", None, "Hello")]

    def test_mixed_instruct_and_plain(self) -> None:
        text = "Normal. ««0|rushed»Quick update.»» Back to normal."
        segs = _split_voice_segments(text)
        assert len(segs) == 3
        assert segs[0] == (None, None, "Normal.")
        assert segs[2] == (None, None, "Back to normal.")


class TestRenderSegments:
    """Test rendering with SAM as a real backend (no mocks needed — it's instant)."""

    def test_single_plain_segment(self) -> None:
        from wednesday_tts.server.backends.sam import SAMBackend

        sam = SAMBackend()
        sam.load()
        segs = [(None, None, "Hello world")]
        audio = _render_segments(segs, sam, 1.0, 0)
        assert audio is not None
        assert isinstance(audio, np.ndarray)
        assert audio.size > 0

    def test_single_voice_override(self) -> None:
        from wednesday_tts.server.backends.sam import SAMBackend
        from wednesday_tts.server.daemon import _voice_cache

        sam = SAMBackend()
        sam.load()
        # Use sam as both primary and override (same backend, just testing the path)
        _voice_cache["sam"] = sam
        segs = [("sam", None, "I am a robot")]
        audio = _render_segments(segs, sam, 1.0, 0)
        assert audio is not None
        assert audio.size > 0

    def test_mixed_segments_concatenated(self) -> None:
        from wednesday_tts.server.backends.sam import SAMBackend
        from wednesday_tts.server.daemon import _voice_cache

        sam = SAMBackend()
        sam.load()
        _voice_cache["sam"] = sam
        # Three segments all using SAM (to test concatenation without needing two backends)
        segs = [
            (None, None, "Part one."),
            ("sam", None, "Part two."),
            (None, None, "Part three."),
        ]
        audio = _render_segments(segs, sam, 1.0, 0)
        assert audio is not None
        # Should be longer than any single part
        single = sam.generate("Part one.")
        assert audio.size > single.size


class TestDaemonVoiceCache:
    """Test the lazy-init backend cache."""

    def test_get_override_backend_sam(self) -> None:
        from wednesday_tts.server.daemon import _get_override_backend

        backend = _get_override_backend("sam")
        assert backend is not None
        assert backend.sample_rate == 22050

    def test_get_override_backend_caches(self) -> None:
        from wednesday_tts.server.daemon import _get_override_backend, _voice_cache

        _voice_cache.clear()
        b1 = _get_override_backend("sam")
        b2 = _get_override_backend("sam")
        assert b1 is b2  # same instance

    def test_get_override_backend_unknown(self) -> None:
        from wednesday_tts.server.daemon import _get_override_backend

        assert _get_override_backend("nonexistent_backend_xyz") is None
