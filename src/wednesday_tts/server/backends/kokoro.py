"""Kokoro 82M TTS backend."""

from __future__ import annotations

import os

import numpy as np

from .base import TTSBackend, DEFAULT_SPEED


class KokoroBackend(TTSBackend):
    """Kokoro 82M neural TTS via the `kokoro` package.

    Config keys (from tts-config.json models.kokoro):
        voice       — Kokoro voice ID (default: af_bella)
        speed       — playback speed multiplier (default: 1.3, applied natively)
        samplerate  — output sample rate (default: 24000)
    """

    sample_rate = 24000
    supports_streaming = False

    def __init__(self, voice: str | None = None, speed: float = DEFAULT_SPEED, samplerate: int = 24000) -> None:
        self._pipeline = None
        self._voice = voice or os.environ.get("KOKORO_VOICE", "af_bella")
        self._speed = speed
        self.sample_rate = samplerate

    def load(self) -> None:
        from kokoro import KPipeline  # type: ignore[import]
        self._pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")

    def generate(self, text: str, speed: float | None = None, voice: str | None = None) -> "np.ndarray | None":
        if self._pipeline is None:
            raise RuntimeError("KokoroBackend not loaded — call load() first")

        use_speed = speed if speed is not None else self._speed
        use_voice = voice or self._voice
        chunks: list[np.ndarray] = []
        for result in self._pipeline(text, voice=use_voice, speed=use_speed):
            if result.audio is not None:
                chunks.append(result.audio.numpy())
        return np.concatenate(chunks) if chunks else None
