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

    def play_streaming(self, text: str, speed: float | None = None) -> None:
        """Stream audio directly to output device — lowest latency to first sound.

        When speed ~= 1.0: pure streaming, chunks written directly to OutputStream.
        When speed != 1.0: hybrid — lead-in at native speed for instant first sound,
        then ALL post-lead-in audio is accumulated and passed through a SINGLE
        soundstretch call (run in a background thread) to eliminate boundary
        pops/clicks. Bridge silence keeps the stream alive while soundstretch runs.
        """
        import sounddevice as sd  # type: ignore[import]

        if self._model is None:
            raise RuntimeError("PocketTTSBackend not loaded — call load() first")

        use_speed = speed if speed is not None else self._speed
        needs_speed = abs(use_speed - 1.0) > 0.01

        # Fix 5: one OutputStream at a time
        with _streaming_lock:
            # Fix 2: match device native rate (e.g. 48kHz) rather than model rate (24kHz)
            device_rate = _get_device_samplerate(self.sample_rate)

            # Fix 1: retry OutputStream open/start up to 3 times (PortAudio error -50)
            out_stream = None
            for _attempt in range(3):
                try:
                    out_stream = sd.OutputStream(
                        samplerate=device_rate, channels=1, dtype="float32",
                    )
                    out_stream.start()
                    break
                except Exception as exc:
                    print(
                        f"[TTS] OutputStream open/start failed (attempt {_attempt + 1}/3): {exc}",
                        flush=True,
                    )
                    if out_stream is not None:
                        try:
                            out_stream.close()
                        except Exception:
                            pass
                        out_stream = None
                    if _attempt < 2:
                        time.sleep(0.5)
            if out_stream is None:
                print(
                    "[TTS] play_streaming: failed to open audio stream after 3 attempts, aborting.",
                    flush=True,
                )
                return

            self._active_stream = out_stream
            try:
                leadin_written = 0
                remainder_chunks: list[np.ndarray] = []
                stopped = False

                with self._lock:
                    for audio_chunk in self._model.generate_audio_stream(
                        self._voice_state,
                        text,
                        frames_after_eos=self._frames_after_eos,
                    ):
                        if self._active_stream is None:
                            stopped = True
                            break

                        arr = audio_chunk.numpy() if hasattr(audio_chunk, "numpy") else np.array(audio_chunk)
                        if arr.ndim > 1:
                            arr = arr.flatten()
                        if arr.size == 0:
                            continue

                        if not needs_speed:
                            # Fix 2: upsample to device rate before writing
                            up = _upsample(arr.astype(np.float32), self.sample_rate, device_rate)
                            out_stream.write(up.reshape(-1, 1))
                        else:
                            if leadin_written < self._LEADIN_SAMPLES:
                                remaining = self._LEADIN_SAMPLES - leadin_written
                                direct = arr[:remaining]
                                leftover = arr[remaining:]
                                # Fix 2: upsample lead-in to device rate
                                up = _upsample(direct.astype(np.float32), self.sample_rate, device_rate)
                                out_stream.write(up.reshape(-1, 1))
                                leadin_written += direct.size
                                if leftover.size > 0:
                                    remainder_chunks.append(leftover)
                            else:
                                remainder_chunks.append(arr)

                # Fix 3: run soundstretch in background thread; feed bridge silence to
                # keep stream alive while it processes (max 1.5 s of silence).
                if not stopped and needs_speed and remainder_chunks:
                    full_remainder = np.concatenate(remainder_chunks)

                    stretch_result: list[np.ndarray | None] = [None]
                    stretch_done = threading.Event()

                    def _do_stretch() -> None:
                        stretch_result[0] = soundstretch_tempo(
                            full_remainder, self.sample_rate, use_speed
                        )
                        stretch_done.set()

                    threading.Thread(target=_do_stretch, daemon=True).start()

                    BRIDGE_CHUNK = int(device_rate * 0.02)   # 20 ms per chunk
                    MAX_BRIDGE   = int(device_rate * 1.5)    # cap at 1.5 s silence
                    silence = np.zeros((BRIDGE_CHUNK, 1), dtype=np.float32)
                    bridge_written = 0
                    while not stretch_done.is_set() and not stopped:
                        if self._active_stream is None:
                            stopped = True
                            break
                        if bridge_written >= MAX_BRIDGE:
                            break
                        out_stream.write(silence)
                        bridge_written += BRIDGE_CHUNK

                    if not stopped:
                        stretch_done.wait(timeout=5.0)

                    if not stopped and stretch_result[0] is not None:
                        # Fix 2: upsample stretched audio to device rate
                        up = _upsample(
                            stretch_result[0].astype(np.float32), self.sample_rate, device_rate
                        )
                        out_stream.write(up.reshape(-1, 1))

                # Trailing silence prevents final-syllable clipping
                if not stopped:
                    pad = np.zeros((int(device_rate * 0.08), 1), dtype=np.float32)
                    out_stream.write(pad)

            except Exception:
                pass  # stream aborted by stop — expected
            finally:
                self._active_stream = None
                try:
                    out_stream.stop()
                    out_stream.close()
                except Exception:
                    pass

    def abort_stream(self) -> None:
        """Abort the active OutputStream if one is running."""
        s = self._active_stream
        if s is not None:
            self._active_stream = None
            try:
                s.abort()
            except Exception:
                pass
