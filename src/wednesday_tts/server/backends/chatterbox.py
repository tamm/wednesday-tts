"""Chatterbox TTS backend."""

from __future__ import annotations

import os

import numpy as np

from .base import DEFAULT_SPEED, TTSBackend, soundstretch_tempo


class ChatterboxBackend(TTSBackend):
    """Chatterbox neural TTS with optional voice cloning.

    Config keys (from tts-config.json models.chatterbox):
        device          — torch device (default: cuda)
        voice_clone     — path to voice reference WAV (optional)
        exaggeration    — voice expressiveness for fast zone (default: 0.3)
        cfg_weight      — CFG weight for fast zone (default: 0.3)

    Generation strategy: the first ~200 chars use lower settings for speed,
    subsequent chunks use normal quality. This matches the original service.
    """

    sample_rate = 22050  # updated from model.sr after load()
    supports_streaming = False

    _FAST_ZONE_CHARS = 200

    def __init__(
        self,
        device: str = "cuda",
        voice_clone: str | None = None,
        exaggeration: float = 0.3,
        cfg_weight: float = 0.3,
    ) -> None:
        self._device = device or os.environ.get("CHATTERBOX_DEVICE", "cuda")
        self._voice_clone = voice_clone or os.environ.get("CHATTERBOX_VOICE_CLONE", "")
        self._exaggeration = exaggeration
        self._cfg_weight = cfg_weight
        self._model = None

    def load(self) -> None:
        from chatterbox.tts import ChatterboxTTS  # type: ignore[import]
        self._model = ChatterboxTTS.from_pretrained(device=self._device)
        self.sample_rate = self._model.sr

    def generate(
        self,
        text: str,
        speed: float | None = None,
        chars_preceding: int = 0,
        voice: str | None = None,
    ) -> np.ndarray | None:
        """Render text to audio.

        Args:
            text: Text to synthesize.
            speed: Tempo multiplier (soundstretch applied when != 1.0).
            chars_preceding: Cumulative characters already synthesised in this
                utterance. Selects fast vs normal generation settings.
            voice: Optional voice ID or reference path.
        """
        if self._model is None:
            raise RuntimeError("ChatterboxBackend not loaded — call load() first")

        use_speed = speed if speed is not None else DEFAULT_SPEED
        use_fast = chars_preceding < self._FAST_ZONE_CHARS
        use_voice = voice or self._voice_clone

        try:
            kwargs: dict = {}
            if use_voice and os.path.exists(use_voice):
                kwargs["audio_prompt_path"] = use_voice
            if use_fast:
                kwargs["exaggeration"] = self._exaggeration
                kwargs["cfg_weight"] = self._cfg_weight

            wav = self._model.generate(text, **kwargs)
            arr: np.ndarray = wav.squeeze(0).cpu().numpy()
            if arr.size == 0:
                return None

            if abs(use_speed - 1.0) > 0.01:
                arr = soundstretch_tempo(arr, self.sample_rate, use_speed)
            return arr
        except Exception as exc:
            print(f"[chatterbox] generate error: {exc}")
            return None
