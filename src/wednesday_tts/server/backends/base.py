"""Abstract base class for TTS backends."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time

import numpy as np


DEFAULT_SPEED = float(os.environ.get("TTS_SPEED", "1.15"))


def soundstretch_tempo(audio_arr: np.ndarray, samplerate: int, speed: float) -> np.ndarray:
    """Pitch-preserving tempo change via soundstretch binary.

    Writes audio to a temp WAV, runs soundstretch -tempo=N% -speech,
    reads back the result. Falls back to original if soundstretch is missing
    or the array is too small to process (< 400 samples).
    """
    import soundfile as sf  # lazy import — not always installed

    if audio_arr is None or audio_arr.size < 400:
        return audio_arr

    ss = shutil.which("soundstretch")
    if not ss:
        for candidate in [
            os.path.expanduser("~/bin/soundstretch.exe"),
            r"~\bin\soundstretch.exe",
        ]:
            if os.path.isfile(candidate):
                ss = candidate
                break
    if not ss:
        return audio_arr

    tempo_pct = (speed - 1.0) * 100  # e.g. 1.3 → +30%
    in_path = out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as inf:
            in_path = inf.name
        out_path = in_path.replace(".wav", "_fast.wav")

        if audio_arr.ndim > 1:
            audio_arr = (
                audio_arr[0]
                if audio_arr.shape[0] < audio_arr.shape[1]
                else audio_arr[:, 0]
            )
        sf.write(in_path, audio_arr, samplerate)

        kwargs: dict = {}
        import sys
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        t0 = time.time()
        subprocess.run(
            [ss, in_path, out_path, f"-tempo={tempo_pct:+.0f}", "-speech"],
            capture_output=True,
            timeout=10,
            **kwargs,
        )
        elapsed_ms = (time.time() - t0) * 1000
        with _soundstretch_lock:
            _soundstretch_stats["calls"] += 1
            _soundstretch_stats["ms_sum"] += elapsed_ms

        if os.path.isfile(out_path):
            result, _ = sf.read(out_path, dtype="float32")
            return result
        return audio_arr
    except Exception:
        return audio_arr
    finally:
        for p in (in_path, out_path):
            if p is not None:
                try:
                    os.unlink(p)
                except Exception:
                    pass


# Simple module-level counters for soundstretch telemetry.
_soundstretch_stats: dict[str, float] = {"calls": 0, "ms_sum": 0.0}
_soundstretch_lock = threading.Lock()


class TTSBackend:
    """Interface that every TTS engine adapter must implement."""

    sample_rate: int = 24000
    supports_streaming: bool = False

    def load(self) -> None:
        """Load the model into memory. Called once at startup."""
        raise NotImplementedError

    def generate(self, text: str, speed: float = DEFAULT_SPEED, voice: str | None = None) -> "np.ndarray | None":
        """Render text to a float32 audio array. Return None on failure."""
        raise NotImplementedError

    # Streaming extension — only required when supports_streaming = True.

    def play_streaming(self, text: str, speed: float = DEFAULT_SPEED, voice: str | None = None) -> None:
        """Stream audio directly to the output device (lowest latency)."""
        raise NotImplementedError

    def abort_stream(self) -> None:
        """Abort an in-progress streaming playback."""
        pass
