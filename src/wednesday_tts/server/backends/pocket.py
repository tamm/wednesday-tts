"""Pocket TTS backend — supports true streaming for lowest latency."""

from __future__ import annotations

import math
import os
import threading
import time

import numpy as np

from .base import TTSBackend, DEFAULT_SPEED, soundstretch_tempo


# ---------------------------------------------------------------------------
# Module-level streaming lock — one OutputStream at a time.
# ---------------------------------------------------------------------------

_streaming_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Device/sample-rate helpers (duplicated from daemon.py to avoid circular import)
# ---------------------------------------------------------------------------

def _get_device_samplerate(model_rate: int) -> int:
    """Return the native samplerate of the default output device.

    Falls back to model_rate if the query fails.
    """
    try:
        import sounddevice as sd  # type: ignore[import]
        info = sd.query_devices(kind="output")
        return int(info["default_samplerate"])
    except Exception:
        return model_rate


def _upsample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Resample audio from from_rate to to_rate.

    Uses scipy when available; falls back to numpy repeat for integer ratios.
    """
    if from_rate == to_rate:
        return audio
    try:
        from scipy.signal import resample_poly  # type: ignore[import]
        g = math.gcd(to_rate, from_rate)
        return resample_poly(audio, to_rate // g, from_rate // g).astype(np.float32)
    except ImportError:
        ratio = to_rate // from_rate
        return np.repeat(audio, ratio).astype(np.float32)


class PocketTTSBackend(TTSBackend):
    """Pocket TTS voice-cloned model.

    Config keys (from tts-config.json models.pocket):
        voice               — voice name or path/URL (default: fantine)
        fallback_voice      — fallback if voice load fails (default: fantine)
        speed               — tempo multiplier; 1.0 = native (soundstretch applied)
        lsd_decode_steps    — LSD decode steps, lower = faster (default: 1)
        noise_clamp         — noise clamping factor (default: None)
        eos_threshold       — end-of-speech threshold (default: -4.0)
        frames_after_eos    — extra frames after EOS token
    """

    sample_rate = 24000  # updated from model after load()
    supports_streaming = True

    # Lead-in samples played at native speed before soundstretch kicks in.
    # At 24 kHz this is ~1.0 s — enough for instant first-sound feel.
    _LEADIN_SAMPLES = 24000

    def __init__(
        self,
        voice: str = "fantine",
        fallback_voice: str = "fantine",
        speed: float = DEFAULT_SPEED,
        lsd_decode_steps: int = 1,
        noise_clamp: float | None = None,
        eos_threshold: float = -4.0,
        frames_after_eos: int | None = None,
    ) -> None:
        self._voice_name = voice or os.environ.get("POCKET_TTS_VOICE", "fantine")
        self._fallback_voice = fallback_voice
        self._speed = speed
        self._lsd_decode_steps = lsd_decode_steps
        self._noise_clamp = noise_clamp
        self._eos_threshold = eos_threshold
        self._frames_after_eos = frames_after_eos

        self._model = None
        self._voice_state = None
        self._lock = threading.Lock()  # generate_audio is not thread-safe
        self._active_stream = None
        self._stream_start_time: float = 0.0

    def load(self) -> None:
        from pocket_tts import TTSModel  # type: ignore[import]
        from pocket_tts.utils.utils import PREDEFINED_VOICES  # type: ignore[import]

        self._model = TTSModel.load_model(
            lsd_decode_steps=self._lsd_decode_steps,
            noise_clamp=self._noise_clamp,
            eos_threshold=self._eos_threshold,
        )
        self.sample_rate = self._model.sample_rate

        voice_ref = PREDEFINED_VOICES.get(self._voice_name, self._voice_name)
        try:
            self._voice_state = self._model.get_state_for_audio_prompt(voice_ref)
        except Exception as exc:
            fallback_ref = PREDEFINED_VOICES.get(self._fallback_voice, self._fallback_voice)
            print(f"[pocket] Voice '{self._voice_name}' failed ({exc}), falling back to '{self._fallback_voice}'")
            self._voice_state = self._model.get_state_for_audio_prompt(fallback_ref)

    def generate(self, text: str, speed: float | None = None) -> "np.ndarray | None":
        if self._model is None:
            raise RuntimeError("PocketTTSBackend not loaded — call load() first")

        use_speed = speed if speed is not None else self._speed
        with self._lock:
            audio = self._model.generate_audio(self._voice_state, text)

        if audio is None:
            return None
        arr = audio.numpy() if hasattr(audio, "numpy") else np.array(audio)
        if arr.size == 0:
            return None

        if abs(use_speed - 1.0) > 0.01:
            arr = soundstretch_tempo(arr, self.sample_rate, use_speed)
        return arr

    def generate_streaming(self, text: str, speed: float | None = None) -> "np.ndarray | None":
        """Generate audio using streaming inference, return one concatenated array.

        Uses generate_audio_stream() for fast chunk-by-chunk inference, collects
        all chunks, concatenates, applies soundstretch if needed. Returns the
        same thing as generate() but may be faster to first result since streaming
        inference can start yielding before the full sequence is planned.

        Hard deadline: if generation exceeds _GENERATE_DEADLINE, returns what
        we have so far.
        """
        if self._model is None:
            raise RuntimeError("PocketTTSBackend not loaded — call load() first")

        use_speed = speed if speed is not None else self._speed
        _GENERATE_DEADLINE = 8.0

        chunks: list[np.ndarray] = []
        gen_start = time.monotonic()

        with self._lock:
            for audio_chunk in self._model.generate_audio_stream(
                self._voice_state,
                text,
                frames_after_eos=self._frames_after_eos,
            ):
                if time.monotonic() - gen_start > _GENERATE_DEADLINE:
                    print(f"[stream] generation deadline ({_GENERATE_DEADLINE}s) hit, returning what we have", flush=True)
                    break
                arr = audio_chunk.numpy() if hasattr(audio_chunk, "numpy") else np.array(audio_chunk)
                if arr.ndim > 1:
                    arr = arr.flatten()
                if arr.size > 0:
                    chunks.append(arr)

        gen_elapsed = time.monotonic() - gen_start
        total_samples = sum(c.size for c in chunks)
        audio_dur = total_samples / self.sample_rate if total_samples else 0
        print(f"[stream] generated {len(chunks)} chunks, {audio_dur:.1f}s audio in {gen_elapsed:.1f}s wall", flush=True)

        if not chunks:
            return None

        arr = np.concatenate(chunks)
        if abs(use_speed - 1.0) > 0.01:
            arr = soundstretch_tempo(arr, self.sample_rate, use_speed)
        return arr
