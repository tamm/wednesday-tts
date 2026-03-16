"""Tests for per-request voice override in daemon and client API."""

from __future__ import annotations

import numpy as np

from wednesday_tts.client.api import voice_tag
from wednesday_tts.server.daemon import _split_voice_segments, _render_segments


class TestVoiceTag:
    def test_sam_default(self) -> None:
        assert voice_tag("Hello") == "««Hello»»"
        assert voice_tag("Hello", "sam") == "««Hello»»"

    def test_named_voice(self) -> None:
        assert voice_tag("Hi", "alba") == "««alba»Hi»»"

    def test_path_voice(self) -> None:
        assert voice_tag("Hi", "/path/to/v.safetensors") == "««/path/to/v.safetensors»Hi»»"


class TestSplitVoiceSegments:
    def test_plain_text_no_tags(self) -> None:
        segs = _split_voice_segments("Hello world")
        assert segs == [(None, "Hello world")]

    def test_sam_tagged_block(self) -> None:
        segs = _split_voice_segments("««Hello»»")
        assert segs == [("sam", "Hello")]

    def test_named_voice_tag(self) -> None:
        segs = _split_voice_segments("««alba»Hello from alba»»")
        assert segs == [("alba", "Hello from alba")]

    def test_path_voice_tag(self) -> None:
        segs = _split_voice_segments("««/path/to/voice.safetensors»Hello»»")
        assert segs == [("/path/to/voice.safetensors", "Hello")]

    def test_sam_with_surrounding_text(self) -> None:
        text = "Normal voice. ««Robot voice.»» Normal again."
        segs = _split_voice_segments(text)
        assert len(segs) == 3
        assert segs[0] == (None, "Normal voice.")
        assert segs[1] == ("sam", "Robot voice.")
        assert segs[2] == (None, "Normal again.")

    def test_named_voice_with_surrounding_text(self) -> None:
        text = "Normal. ««alba»Different voice.»» Normal."
        segs = _split_voice_segments(text)
        assert len(segs) == 3
        assert segs[0] == (None, "Normal.")
        assert segs[1] == ("alba", "Different voice.")
        assert segs[2] == (None, "Normal.")

    def test_multiple_tagged_blocks(self) -> None:
        text = "Start. ««Robot.»» Middle. ««More robot.»» End."
        segs = _split_voice_segments(text)
        assert len(segs) == 5
        assert segs[0] == (None, "Start.")
        assert segs[1] == ("sam", "Robot.")
        assert segs[2] == (None, "Middle.")
        assert segs[3] == ("sam", "More robot.")
        assert segs[4] == (None, "End.")

    def test_mixed_voice_types(self) -> None:
        text = "Normal. ««alba»Named.»» Middle. ««SAM voice.»» End."
        segs = _split_voice_segments(text)
        assert len(segs) == 5
        assert segs[0] == (None, "Normal.")
        assert segs[1] == ("alba", "Named.")
        assert segs[2] == (None, "Middle.")
        assert segs[3] == ("sam", "SAM voice.")
        assert segs[4] == (None, "End.")

    def test_adjacent_tagged_blocks(self) -> None:
        text = "««Robot.»»««alba»Neural.»»"
        segs = _split_voice_segments(text)
        assert len(segs) == 2
        assert segs[0] == ("sam", "Robot.")
        assert segs[1] == ("alba", "Neural.")

    def test_only_leading_text(self) -> None:
        text = "Hello ««Robot»»"
        segs = _split_voice_segments(text)
        assert len(segs) == 2
        assert segs[0] == (None, "Hello")
        assert segs[1] == ("sam", "Robot")

    def test_only_trailing_text(self) -> None:
        text = "««Robot»» Bye"
        segs = _split_voice_segments(text)
        assert len(segs) == 2
        assert segs[0] == ("sam", "Robot")
        assert segs[1] == (None, "Bye")

    def test_empty_tagged_block_skipped(self) -> None:
        text = "Hello ««»» world"
        segs = _split_voice_segments(text)
        # Empty guillemets produce no match (regex requires .+?)
        assert segs == [(None, "Hello ««»» world")]

    def test_empty_string(self) -> None:
        assert _split_voice_segments("") == []

    def test_whitespace_only(self) -> None:
        assert _split_voice_segments("   ") == []

    def test_whole_message_wrapped(self) -> None:
        """Simulates what hooks do — entire message wrapped in a voice tag."""
        text = "««alba»This is the whole message.»»"
        segs = _split_voice_segments(text)
        assert segs == [("alba", "This is the whole message.")]


class TestRenderSegments:
    """Test rendering with SAM as a real backend (no mocks needed — it's instant)."""

    def test_single_plain_segment(self) -> None:
        from wednesday_tts.server.backends.sam import SAMBackend
        sam = SAMBackend()
        sam.load()
        segs = [(None, "Hello world")]
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
        segs = [("sam", "I am a robot")]
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
            (None, "Part one."),
            ("sam", "Part two."),
            (None, "Part three."),
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
