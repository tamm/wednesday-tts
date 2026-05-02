"""Chatterbox TTS backend."""

from __future__ import annotations

import os
import time

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
        turbo: bool = False,
    ) -> None:
        self._device = device or os.environ.get("CHATTERBOX_DEVICE", "cuda")
        self._voice_clone = voice_clone or os.environ.get("CHATTERBOX_VOICE_CLONE", "")
        self._exaggeration = exaggeration
        self._cfg_weight = cfg_weight
        self._turbo = turbo
        self._model = None

    def load(self) -> None:
        # Apple Silicon path: chatterbox has hardcoded .cuda() / map_location
        # calls that crash on mps. The fix (per the Jimmi42 HF Space) is:
        #   1. Patch torch.load to default map_location='cpu' so weight files
        #      with cuda tensors don't try to materialise on cuda.
        #   2. Load the whole model on cpu via from_pretrained("cpu").
        #   3. Move the actual sub-modules (t3, s3gen, ve) to mps afterwards.
        # On non-mps (cuda or plain cpu), skip the patch and load directly.
        import torch

        if self._device == "mps":
            original_torch_load = torch.load

            def _patched_torch_load(f, map_location=None, **kwargs):
                if map_location is None:
                    map_location = "cpu"
                return original_torch_load(f, map_location=map_location, **kwargs)

            torch.load = _patched_torch_load
            try:
                if self._turbo:
                    from chatterbox.tts_turbo import ChatterboxTurboTTS  # type: ignore[import]
                    self._model = ChatterboxTurboTTS.from_pretrained(device="cpu")
                else:
                    from chatterbox.tts import ChatterboxTTS  # type: ignore[import]
                    self._model = ChatterboxTTS.from_pretrained(device="cpu")
            finally:
                torch.load = original_torch_load

            for attr in ("t3", "s3gen", "ve"):
                module = getattr(self._model, attr, None)
                if module is not None:
                    setattr(self._model, attr, module.to("mps"))
        else:
            if self._turbo:
                from chatterbox.tts_turbo import ChatterboxTurboTTS  # type: ignore[import]
                self._model = ChatterboxTurboTTS.from_pretrained(device=self._device)
            else:
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

        t0 = time.monotonic()
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

            elapsed = time.monotonic() - t0
            duration = arr.size / self.sample_rate if self.sample_rate else 0.0
            rtf = f"{elapsed / duration:.2f}" if duration > 0 else "n/a"
            voice_label = (
                os.path.basename(use_voice).rsplit(".", 1)[0] if use_voice else "default"
            )
            tag = "chatterbox-turbo" if self._turbo else "chatterbox"
            print(
                f"[{tag}] generated {duration:.1f}s audio in {elapsed:.1f}s "
                f"(RTF {rtf}, voice={voice_label}, fast={use_fast})",
                flush=True,
            )
            return arr
        except Exception as exc:
            print(f"[chatterbox] generate error: {exc}")
            return None
