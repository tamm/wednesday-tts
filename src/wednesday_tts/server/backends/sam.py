"""SAM (Software Automatic Mouth) retro TTS backend.

A 1982 Commodore 64 formant synthesizer — pure Python, zero native deps,
produces gloriously robotic 8-bit speech.  Useful as a novelty voice and
as a lightweight fallback on machines that can't run neural TTS.

Post-processing applies a mild lowpass filter and short reverb impulse
to warm up the raw 8-bit output.
"""

from __future__ import annotations

import numpy as np

from .base import TTSBackend, DEFAULT_SPEED

# Lowpass: single-pole IIR coefficient.  Higher = more smoothing (0–1).
_LOWPASS_ALPHA = 0.55

# Reverb: short impulse response simulating a small room / speaker cabinet.
# Delays in samples at 22050 Hz, with exponential decay.
_REVERB_DELAYS = [441, 893, 1327, 1764]  # ~20ms, 40ms, 60ms, 80ms
_REVERB_DECAY = 0.3   # gain of first tap (subsequent taps decay further)
_REVERB_FALLOFF = 0.5  # each tap is this fraction of the previous


def _lowpass(audio: np.ndarray, alpha: float = _LOWPASS_ALPHA) -> np.ndarray:
    """Single-pole IIR lowpass filter.  Cheap and effective for smoothing
    the harsh high-frequency content of 8-bit formant synthesis."""
    out = np.empty_like(audio)
    out[0] = audio[0]
    beta = 1.0 - alpha
    for i in range(1, len(audio)):
        out[i] = alpha * out[i - 1] + beta * audio[i]
    return out


def _reverb(audio: np.ndarray) -> np.ndarray:
    """Add a short comb-filter reverb to give body to the thin SAM output."""
    result = audio.copy()
    gain = _REVERB_DECAY
    for delay in _REVERB_DELAYS:
        if delay >= len(audio):
            break
        result[delay:] += gain * audio[: len(audio) - delay]
        gain *= _REVERB_FALLOFF
    # Normalise to prevent clipping
    peak = np.abs(result).max()
    if peak > 1.0:
        result /= peak
    return result


class SAMBackend(TTSBackend):
    """SAM retro formant TTS via the ``samtts`` package.

    Config keys (from tts-config.json models.sam):
        speed   — SAM speed (1–255, default 72; higher = slower)
        pitch   — fundamental frequency (1–255, default 64)
        mouth   — mouth formant freq (1–255, default 128)
        throat  — throat formant freq (1–255, default 128)
        volume  — output gain 0.0–1.0 (default 0.45, matched to pocket levels)
    """

    sample_rate = 22050
    supports_streaming = False

    def __init__(
        self,
        speed: int = 72,
        pitch: int = 64,
        mouth: int = 128,
        throat: int = 128,
        volume: float = 0.20,
        **_kwargs: object,
    ) -> None:
        self._sam = None
        self._speed = speed
        self._pitch = pitch
        self._mouth = mouth
        self._throat = throat
        self._volume = max(0.0, min(1.0, volume))

    def load(self) -> None:
        from samtts import SamTTS  # type: ignore[import-untyped]

        print("[sam] Loading SAM formant synthesizer...", flush=True)
        self._sam = SamTTS(
            speed=self._speed,
            pitch=self._pitch,
            mouth=self._mouth,
            throat=self._throat,
        )
        print("[sam] Ready.", flush=True)

    def generate(self, text: str, speed: float = DEFAULT_SPEED) -> np.ndarray | None:
        if self._sam is None:
            raise RuntimeError("SAMBackend not loaded — call load() first")

        if not text or not text.strip():
            return None

        try:
            raw = self._sam.get_audio_data(text)
            if not raw:
                return None

            # Convert 8-bit unsigned PCM (0–255, centre 128) to float32 (−1…+1)
            arr = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
            arr = (arr - 128.0) / 128.0

            if arr.size == 0:
                return None

            # Post-processing: smooth the harsh 8-bit edges, add warmth
            arr = _lowpass(arr)
            arr = _reverb(arr)

            # Anti-click: fade in/out to avoid pops from abrupt start/end.
            # SAM's formant output often starts/ends at non-zero crossings.
            FADE_SAMPLES = min(int(self.sample_rate * 0.010), len(arr) // 4)  # 10ms
            if FADE_SAMPLES > 1:
                fade_in = np.linspace(0.0, 1.0, FADE_SAMPLES, dtype=np.float32)
                arr[:FADE_SAMPLES] *= fade_in
                arr[-FADE_SAMPLES:] *= fade_in[::-1]

            # Scale to match pocket output levels (reverb normalises to peak 1.0
            # which is much louder than neural TTS output).
            arr *= self._volume

            return arr
        except Exception as exc:
            print(f"[sam] generate error: {exc}", flush=True)
            return None
