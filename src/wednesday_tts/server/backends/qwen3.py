"""Qwen3-TTS backend — Alibaba's multilingual TTS via mlx-audio on Apple Silicon."""

from __future__ import annotations

import os
import time
import threading

import numpy as np

from .base import TTSBackend, DEFAULT_SPEED


class Qwen3TTSBackend(TTSBackend):
    """Qwen3-TTS via mlx-audio (MLX-native, Apple Silicon optimised).

    Config keys (from tts-config.json models.qwen3):
        model_id    — mlx-community model ID
                      (default: mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit)
        voice       — path to reference audio WAV for voice cloning (optional)
        voice_text  — transcription of the reference audio (optional, improves quality)
        speed       — native speed multiplier (handled by the model, not soundstretch)
        seed        — fixed random seed for consistent voice (int, optional)
        seed_pool   — list of seeds to rotate through as a voice pool (optional)
        instruct    — style/emotion instruction string (optional)
    """

    sample_rate = 24000
    supports_streaming = True

    def __init__(
        self,
        model_id: str = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit",
        voice: str | None = None,
        voice_text: str | None = None,
        speed: float = DEFAULT_SPEED,
        seed: int | None = None,
        seed_pool: list[int] | None = None,
        instruct: str = "",
    ) -> None:
        self._model_id = model_id or os.environ.get(
            "QWEN3_TTS_MODEL", "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit"
        )
        self._voice = voice  # path to reference audio WAV
        self._voice_text = voice_text  # transcription of reference audio
        self._speed = speed
        self._seed = seed
        self._seed_pool = seed_pool or []
        self._seed_index = 0
        self._instruct = instruct
        self._model = None
        self._lock = threading.Lock()

    def _pick_seed(self) -> int | None:
        """Pick the next seed from the pool, or use the fixed seed."""
        if self._seed_pool:
            seed = self._seed_pool[self._seed_index % len(self._seed_pool)]
            self._seed_index += 1
            return seed
        return self._seed

    def load(self) -> None:
        from mlx_audio.tts import load  # type: ignore[import]

        self._model = load(self._model_id)
        if hasattr(self._model, "sample_rate"):
            self.sample_rate = self._model.sample_rate

    def generate(
        self, text: str, speed: float | None = None, voice: str | None = None
    ) -> np.ndarray | None:
        if self._model is None:
            raise RuntimeError("Qwen3TTSBackend not loaded — call load() first")

        use_speed = speed if speed is not None else self._speed

        # ref_audio must be a file path — ignore pocket-style voice names,
        # fall back to configured default voice
        use_voice = voice if (voice and os.path.isfile(voice)) else self._voice

        seed = self._pick_seed()
        t0 = time.time()

        try:
            with self._lock:
                if seed is not None:
                    import mlx.core as mx  # type: ignore[import]
                    mx.random.seed(seed)

                chunks = list(self._model.generate(
                    text=text,
                    speed=use_speed,
                    ref_audio=use_voice,
                    ref_text=self._voice_text if use_voice == self._voice else None,
                    instruct=self._instruct or None,
                ))

            if not chunks:
                print(f"[qwen3] generate returned no chunks for {len(text)} chars", flush=True)
                return None

            # Collect audio from all GenerationResult segments
            arrays = []
            for result in chunks:
                audio = result.audio
                arr = np.array(audio, dtype=np.float32)
                if arr.ndim > 1:
                    arr = arr.squeeze()
                arrays.append(arr)

                if result.sample_rate and result.sample_rate != self.sample_rate:
                    self.sample_rate = result.sample_rate

            combined = np.concatenate(arrays) if len(arrays) > 1 else arrays[0]
            elapsed = time.time() - t0
            duration = len(combined) / self.sample_rate
            print(
                f"[qwen3] generated {duration:.1f}s audio in {elapsed:.1f}s "
                f"(RTF {elapsed / duration:.2f}, seed={seed})",
                flush=True,
            )
            return combined

        except Exception as exc:
            print(f"[qwen3] generate error: {exc}", flush=True)
            return None
