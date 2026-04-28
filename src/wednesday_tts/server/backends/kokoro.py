"""Kokoro 82M TTS backend."""

from __future__ import annotations

import os
import threading
import time

import numpy as np

from .base import DEFAULT_SPEED, TTSBackend


class KokoroBackend(TTSBackend):
    """Kokoro 82M neural TTS via the `kokoro` package.

    Config keys (from tts-config.json models.kokoro):
        voice       — Kokoro voice ID (default: af_bella)
        speed       — playback speed multiplier; applied natively by Kokoro
        samplerate  — output sample rate (default: 24000)
        lang_code   — language pipeline code (default: "a" = American English)
        repo_id     — HF repo override (default: hexgrad/Kokoro-82M)
    """

    sample_rate = 24000
    supports_streaming = False

    def __init__(
        self,
        voice: str | None = None,
        speed: float = DEFAULT_SPEED,
        samplerate: int = 24000,
        lang_code: str = "a",
        repo_id: str = "hexgrad/Kokoro-82M",
    ) -> None:
        self._pipeline = None
        self._voice = voice or os.environ.get("KOKORO_VOICE", "af_bella")
        self._speed = speed
        self._lang_code = lang_code
        self._repo_id = repo_id
        self.sample_rate = samplerate
        self._lock = threading.Lock()

    def load(self) -> None:
        from kokoro import KPipeline  # type: ignore[import]

        self._pipeline = KPipeline(lang_code=self._lang_code, repo_id=self._repo_id)

    def generate(
        self, text: str, speed: float | None = None, voice: str | None = None
    ) -> np.ndarray | None:
        if self._pipeline is None:
            raise RuntimeError("KokoroBackend not loaded — call load() first")
        if not text or not text.strip():
            return None

        use_speed = speed if speed is not None else self._speed
        use_voice = voice or self._voice

        t0 = time.time()
        chunks: list[np.ndarray] = []
        n_segments = 0
        try:
            with self._lock:
                for result in self._pipeline(text, voice=use_voice, speed=use_speed):
                    if result.audio is None:
                        continue
                    arr = result.audio
                    if hasattr(arr, "numpy"):
                        arr = arr.numpy()
                    arr = np.asarray(arr, dtype=np.float32)
                    if arr.ndim > 1:
                        arr = arr.squeeze()
                    if arr.size > 0:
                        chunks.append(arr)
                        n_segments += 1
        except Exception as exc:
            print(f"[kokoro] generate error: {exc}", flush=True)
            return None

        if not chunks:
            print(f"[kokoro] no audio for {len(text)} chars", flush=True)
            return None

        combined = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        elapsed = time.time() - t0
        duration = combined.size / self.sample_rate if self.sample_rate else 0.0
        rtf = f"{elapsed / duration:.2f}" if duration > 0 else "n/a"
        print(
            f"[kokoro] {n_segments} segments, generated {duration:.1f}s audio in {elapsed:.1f}s "
            f"(RTF {rtf}, voice={use_voice})",
            flush=True,
        )
        return combined
