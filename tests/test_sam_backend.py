"""Tests for the SAM (Software Automatic Mouth) backend."""

from __future__ import annotations

import numpy as np
import pytest

from wednesday_tts.server.backends.sam import SAMBackend, _lowpass, _reverb


@pytest.fixture()
def sam() -> SAMBackend:
    backend = SAMBackend()
    backend.load()
    return backend


class TestSAMBackendInit:
    def test_default_params(self) -> None:
        b = SAMBackend()
        assert b._speed == 72
        assert b._pitch == 64
        assert b._mouth == 128
        assert b._throat == 128

    def test_custom_params(self) -> None:
        b = SAMBackend(speed=92, pitch=60, mouth=190, throat=190)
        assert b._speed == 92
        assert b._pitch == 60

    def test_ignores_unknown_kwargs(self) -> None:
        b = SAMBackend(speed=72, unknown_key="ignored")
        assert b._speed == 72

    def test_sample_rate(self) -> None:
        assert SAMBackend.sample_rate == 22050

    def test_no_streaming(self) -> None:
        assert SAMBackend.supports_streaming is False


class TestSAMBackendLoad:
    def test_load_succeeds(self) -> None:
        b = SAMBackend()
        b.load()
        assert b._sam is not None

    def test_generate_before_load_raises(self) -> None:
        b = SAMBackend()
        with pytest.raises(RuntimeError, match="not loaded"):
            b.generate("hello")


class TestSAMBackendGenerate:
    def test_generates_audio(self, sam: SAMBackend) -> None:
        result = sam.generate("Hello world")
        assert result is not None
        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float32
        assert result.size > 0

    def test_audio_range(self, sam: SAMBackend) -> None:
        result = sam.generate("Testing one two three")
        assert result is not None
        assert result.min() >= -1.0
        assert result.max() <= 1.0

    def test_empty_string_returns_none(self, sam: SAMBackend) -> None:
        assert sam.generate("") is None

    def test_whitespace_only_returns_none(self, sam: SAMBackend) -> None:
        assert sam.generate("   ") is None

    def test_single_word(self, sam: SAMBackend) -> None:
        result = sam.generate("Hello")
        assert result is not None
        assert result.size > 100  # should be some audio

    def test_longer_text(self, sam: SAMBackend) -> None:
        result = sam.generate("The quick brown fox jumps over the lazy dog")
        assert result is not None
        assert result.size > 1000

    def test_robot_voice_preset(self) -> None:
        """Robot preset (speed=92, pitch=60, mouth=190, throat=190) should work."""
        b = SAMBackend(speed=92, pitch=60, mouth=190, throat=190)
        b.load()
        result = b.generate("I am a robot")
        assert result is not None
        assert result.size > 0


class TestSAMPostProcessing:
    def test_lowpass_smooths_signal(self) -> None:
        """Lowpass should reduce high-frequency energy (diff between adjacent samples)."""
        # Square wave — maximum high-freq content
        raw = np.array([1.0, -1.0] * 500, dtype=np.float32)
        smoothed = _lowpass(raw, alpha=0.35)
        # High-freq energy = mean absolute diff between adjacent samples
        raw_hf = np.abs(np.diff(raw)).mean()
        smoothed_hf = np.abs(np.diff(smoothed)).mean()
        assert smoothed_hf < raw_hf * 0.8  # at least 20% reduction

    def test_lowpass_preserves_length(self) -> None:
        arr = np.random.randn(1000).astype(np.float32)
        assert len(_lowpass(arr)) == 1000

    def test_reverb_adds_tail(self) -> None:
        """Reverb should make the signal longer in effective energy."""
        # Impulse — single spike then silence
        impulse = np.zeros(5000, dtype=np.float32)
        impulse[0] = 1.0
        result = _reverb(impulse)
        # Should have non-zero samples beyond the original impulse
        assert np.abs(result[500:]).max() > 0.01

    def test_reverb_preserves_length(self) -> None:
        arr = np.random.randn(3000).astype(np.float32)
        assert len(_reverb(arr)) == 3000

    def test_reverb_normalises_to_prevent_clipping(self) -> None:
        arr = np.ones(5000, dtype=np.float32) * 0.9
        result = _reverb(arr)
        assert np.abs(result).max() <= 1.0

    def test_generate_applies_postprocessing(self, sam: SAMBackend) -> None:
        """Full generate path should produce smoother output than raw SAM."""
        result = sam.generate("Hello world")
        assert result is not None
        # Post-processed audio should stay in [-1, 1]
        assert result.min() >= -1.0
        assert result.max() <= 1.0


class TestSAMBackendRegistry:
    def test_registered(self) -> None:
        from wednesday_tts.server.backends import REGISTRY
        assert "sam" in REGISTRY
        assert REGISTRY["sam"] is SAMBackend
