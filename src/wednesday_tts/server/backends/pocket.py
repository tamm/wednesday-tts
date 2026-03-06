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

# Sentinel object used to signal end-of-stream to the PortAudio callback.
# Using a distinct object (not None) lets the callback distinguish "buffer
# temporarily empty, keep playing silence" from "producer is done, stop".
_STREAM_SENTINEL = object()

# Consecutive PortAudio write failures. When this hits the threshold,
# the process exits so launchd restarts with a fresh PortAudio init.
# A successful stream resets the counter to zero.
_consecutive_stream_failures = 0
_stream_failure_lock = threading.Lock()
_STREAM_FAILURE_EXIT_THRESHOLD = 3


def _record_stream_failure() -> int:
    """Increment and return the consecutive stream failure count."""
    global _consecutive_stream_failures
    with _stream_failure_lock:
        _consecutive_stream_failures += 1
        return _consecutive_stream_failures


def _reset_stream_failures() -> None:
    """Reset consecutive stream failure count after a successful stream."""
    global _consecutive_stream_failures
    with _stream_failure_lock:
        _consecutive_stream_failures = 0


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

    def play_streaming(self, text: str, speed: float | None = None) -> None:
        """Stream audio directly to output device — lowest latency to first sound.

        Uses callback-mode OutputStream so PortAudio pulls audio from a queue
        via a Python callback. Our thread feeds the queue and never blocks at
        C level — if PortAudio stops consuming, queue.put(timeout=2.0) times
        out instead of hanging forever.

        When speed ~= 1.0: pure streaming, all chunks fed straight into the
        audio buffer.
        When speed != 1.0: hybrid — lead-in at native speed for instant first
        sound, then ALL post-lead-in audio is accumulated and passed through a
        SINGLE soundstretch call (run in a background thread) to eliminate
        boundary pops/clicks. The callback drains bridge silence while
        soundstretch runs.
        """
        import queue as _queue

        import sounddevice as sd  # type: ignore[import]

        if self._model is None:
            raise RuntimeError("PocketTTSBackend not loaded — call load() first")

        use_speed = speed if speed is not None else self._speed
        needs_speed = abs(use_speed - 1.0) > 0.01

        # One OutputStream at a time. Timeout prevents new requests piling up
        # behind a previous hung stream forever.
        _LOCK_TIMEOUT = 35.0  # slightly longer than daemon's min streaming timeout
        if not _streaming_lock.acquire(timeout=_LOCK_TIMEOUT):
            print(
                f"[TTS] _streaming_lock not acquired after {_LOCK_TIMEOUT:.0f}s — "
                "previous stream likely hung, skipping",
                flush=True,
            )
            return

        try:
            # Retry open/start up to 3 times. On each attempt force a full
            # PortAudio terminate/initialize cycle so err=-50 is recovered.
            out_stream = None
            device_rate = self.sample_rate  # fallback; updated on each attempt
            for _attempt in range(3):
                try:
                    # Reinit PortAudio only on retries — not on the first attempt.
                    # On first attempt PortAudio is in a known state (either freshly
                    # initialised by sounddevice's lazy init, or already running).
                    # Calling _terminate() while another thread may have a reference
                    # to PortAudio resources can corrupt state. Only reinit after a
                    # failure to recover from a bad state.
                    if _attempt > 0:
                        sd._terminate()
                        sd._initialize()
                    device_rate = _get_device_samplerate(self.sample_rate)

                    # Audio buffer: our thread enqueues chunks; PortAudio
                    # callback dequeues them. maxsize=8 gives ~170 ms of
                    # buffering at 48 kHz / 1024 frames per block.
                    audio_buf: _queue.Queue[np.ndarray | None] = _queue.Queue(maxsize=8)
                    callback_done = threading.Event()

                    def _callback(
                        outdata: np.ndarray,
                        frames: int,
                        time_info: object,
                        status: object,
                        _buf: _queue.Queue = audio_buf,  # type: ignore[type-arg]
                    ) -> None:
                        # Called by PortAudio in its own thread — must not block.
                        try:
                            chunk = _buf.get_nowait()
                        except _queue.Empty:
                            # Buffer temporarily empty — output silence and keep going.
                            # Model inference takes time; don't stop the stream yet.
                            outdata[:] = 0
                            return
                        if chunk is _STREAM_SENTINEL:
                            # Producer signalled end-of-stream — stop cleanly.
                            outdata[:] = 0
                            raise sd.CallbackStop()
                        n = min(len(chunk), frames)
                        outdata[:n, 0] = chunk[:n]
                        if n < frames:
                            outdata[n:] = 0
                        # Put remainder back so no audio is lost
                        if len(chunk) > frames:
                            try:
                                _buf.put_nowait(chunk[frames:])
                            except _queue.Full:
                                pass  # drop remainder on overflow

                    out_stream = sd.OutputStream(
                        samplerate=device_rate, channels=1, dtype="float32",
                        callback=_callback,
                        finished_callback=callback_done.set,
                        blocksize=1024,  # ~21 ms at 48 kHz
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
                        time.sleep(1.0)

            if out_stream is None:
                _n = _record_stream_failure()
                print(
                    f"[TTS] play_streaming: failed to open audio stream after 3 attempts "
                    f"({_n} consecutive failures), aborting.",
                    flush=True,
                )
                if _n >= _STREAM_FAILURE_EXIT_THRESHOLD:
                    print("[TTS] Too many open failures — exiting for restart", flush=True)
                    os._exit(1)
                return

            self._active_stream = out_stream
            self._stream_start_time = time.monotonic()

            # _buf_put: enqueue a pre-upsampled float32 column vector.
            # Returns False if the callback stopped consuming (PortAudio wedged).
            def _buf_put(arr: np.ndarray) -> bool:
                """Enqueue arr into audio_buf. Returns False on timeout."""
                up = _upsample(arr.astype(np.float32), self.sample_rate, device_rate)
                flat = up.reshape(-1)
                try:
                    audio_buf.put(flat, timeout=8.0)
                    return True
                except _queue.Full:
                    print(
                        "[TTS] callback not consuming audio — PortAudio may be wedged",
                        flush=True,
                    )
                    _record_stream_failure()
                    return False

            stopped = False
            try:
                leadin_written = 0
                remainder_chunks: list[np.ndarray] = []

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
                            if not _buf_put(arr):
                                _n = _record_stream_failure()
                                if _n >= _STREAM_FAILURE_EXIT_THRESHOLD:
                                    print(
                                        f"[TTS] {_n} consecutive stream failures "
                                        "— PortAudio is broken, exiting for restart",
                                        flush=True,
                                    )
                                    os._exit(1)
                                stopped = True
                                break
                        else:
                            if leadin_written < self._LEADIN_SAMPLES:
                                remaining = self._LEADIN_SAMPLES - leadin_written
                                direct = arr[:remaining]
                                leftover = arr[remaining:]
                                if not _buf_put(direct):
                                    _n = _record_stream_failure()
                                    if _n >= _STREAM_FAILURE_EXIT_THRESHOLD:
                                        print(
                                            f"[TTS] {_n} consecutive stream failures "
                                            "— PortAudio is broken, exiting for restart",
                                            flush=True,
                                        )
                                        os._exit(1)
                                    stopped = True
                                    break
                                leadin_written += direct.size
                                if leftover.size > 0:
                                    remainder_chunks.append(leftover)
                            else:
                                remainder_chunks.append(arr)

                # Soundstretch remainder: run in background, feed bridge silence
                # into the buffer to keep the callback alive while it processes.
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

                    # Feed bridge silence while soundstretch processes
                    BRIDGE_CHUNK = int(device_rate * 0.02)   # 20 ms per chunk
                    MAX_BRIDGE   = int(device_rate * 1.5)    # cap at 1.5 s silence
                    silence = np.zeros(BRIDGE_CHUNK, dtype=np.float32)
                    bridge_written = 0
                    while not stretch_done.is_set() and not stopped:
                        if self._active_stream is None:
                            stopped = True
                            break
                        if bridge_written >= MAX_BRIDGE:
                            break
                        try:
                            audio_buf.put(silence, timeout=2.0)
                        except _queue.Full:
                            stopped = True
                            break
                        bridge_written += BRIDGE_CHUNK

                    if not stopped:
                        stretch_done.wait(timeout=5.0)

                    if not stopped and stretch_result[0] is not None:
                        _buf_put(stretch_result[0])

                # Trailing silence prevents final-syllable clipping
                if not stopped:
                    pad = np.zeros(int(device_rate * 0.08), dtype=np.float32)
                    try:
                        audio_buf.put(pad, timeout=2.0)
                    except _queue.Full:
                        pass

                # Send sentinel to tell the callback to stop
                try:
                    audio_buf.put(_STREAM_SENTINEL, timeout=2.0)
                except _queue.Full:
                    pass

                # Wait for PortAudio to drain and finish
                if not callback_done.wait(timeout=10.0):
                    print("[TTS] callback did not finish in 10s — PortAudio wedged", flush=True)
                    _record_stream_failure()

            except Exception:
                pass  # stream aborted by stop — expected
            finally:
                self._active_stream = None
                try:
                    out_stream.abort()
                    out_stream.close()
                except Exception:
                    pass
                # Reset failure counter on any stream that didn't hit an error
                if not stopped:
                    _reset_stream_failures()
        finally:
            _streaming_lock.release()

    def stream_chunks(self, text: str, speed: float | None = None):
        """Yield np.ndarray audio chunks without playing them.

        Each chunk is a 1-D float32 array at self.sample_rate. Speed adjustment
        (soundstretch) is NOT applied here — the caller is responsible for that
        if needed, or can enqueue raw chunks for natural-speed playback.

        Uses the model's generate_audio_stream() for true streaming inference.
        """
        if self._model is None:
            raise RuntimeError("PocketTTSBackend not loaded — call load() first")

        use_speed = speed if speed is not None else self._speed

        with self._lock:
            for audio_chunk in self._model.generate_audio_stream(
                self._voice_state,
                text,
                frames_after_eos=self._frames_after_eos,
            ):
                arr = audio_chunk.numpy() if hasattr(audio_chunk, "numpy") else np.array(audio_chunk)
                if arr.ndim > 1:
                    arr = arr.flatten()
                if arr.size == 0:
                    continue

                # Apply soundstretch per-chunk if speed != 1.0
                if abs(use_speed - 1.0) > 0.01:
                    arr = soundstretch_tempo(arr, self.sample_rate, use_speed)

                yield arr.astype(np.float32)

    def abort_stream(self) -> None:
        """Abort the active OutputStream if one is running."""
        s = self._active_stream
        if s is not None:
            self._active_stream = None
            try:
                s.abort()
            except Exception:
                pass
