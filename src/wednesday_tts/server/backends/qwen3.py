"""Qwen3-TTS backend — Alibaba's multilingual TTS via mlx-audio on Apple Silicon."""

from __future__ import annotations

import os
import time
import threading

import numpy as np

from .base import TTSBackend, DEFAULT_SPEED

_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}


def _is_audio_file(path: str) -> bool:
    """Check if path points to an existing audio file with a supported extension."""
    return os.path.isfile(path) and os.path.splitext(path)[1].lower() in _AUDIO_EXTS


def _parse_seed_tag(voice: str) -> int | None:
    """Parse a 'seed:N' voice identifier. Returns the seed int or None."""
    if voice.startswith("seed:"):
        try:
            return int(voice[5:])
        except ValueError:
            pass
    return None


class Qwen3TTSBackend(TTSBackend):
    """Qwen3-TTS via mlx-audio (MLX-native, Apple Silicon optimised).

    Config keys (from tts-config.json models.qwen3):
        model_id    — mlx-community model ID
                      (default: mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit)
        voice       — path to reference audio WAV for voice cloning (optional)
        voice_text  — transcription of the reference audio (optional, improves quality)
        speed       — native speed multiplier (handled by the model, not soundstretch)
        seed        — fixed random seed for reproducible output (int, optional)
        instruct    — style/emotion instruction string (optional)

    Voice pool entries (in tts-config.json models.qwen3.voice_pool) can be:
        - Audio file paths: "/path/to/voice.wav" — used as ref_audio for ICL cloning
        - Seed tags: "seed:42" — deterministic seed-based voice generation
    """

    sample_rate = 24000
    supports_streaming = False  # mid-word cuts at streaming_interval boundaries cause choppy audio

    def __init__(
        self,
        model_id: str = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit",
        voice: str | None = None,
        voice_text: str | None = None,
        speed: float = DEFAULT_SPEED,
        seed: int | None = None,
        instruct: str = "",
        temperature: float = 0.75,
        repetition_penalty: float = 1.2,
        top_p: float = 0.85,
        top_k: int = 30,
    ) -> None:
        self._model_id = model_id or os.environ.get(
            "QWEN3_TTS_MODEL", "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit"
        )
        self._voice = voice  # path to default reference audio WAV
        self._voice_text = voice_text  # transcription of default reference audio
        self._speed = speed
        self._seed = seed if seed is not None else 7  # always pin voice
        self._temperature = temperature
        self._repetition_penalty = repetition_penalty
        self._top_p = top_p
        self._top_k = top_k
        self._instruct = instruct
        self._model = None
        self._lock = threading.Lock()

    def _resolve_voice(self, voice: str | None) -> tuple[str | None, str | None, int]:
        """Resolve a voice parameter into (ref_audio, ref_text, seed).

        Voice resolution order:
        1. voice is a seed tag ("seed:42") → use that seed, no ref_audio
        2. voice is a supported audio file → use as ref_audio with fixed seed
        3. voice is something unrecognised (pocket safetensors, predefined name, etc.)
           → log warning, fall back to configured default voice
        4. voice is None → use configured default voice with fixed seed

        Seed is NEVER None — always returns a valid int to prevent random voice.

        Returns:
            (ref_audio_path | None, ref_text | None, seed)
        """
        if voice is not None:
            # Seed tag: "seed:42"
            tag_seed = _parse_seed_tag(voice)
            if tag_seed is not None:
                return None, None, tag_seed

            # Supported audio file
            if _is_audio_file(voice):
                ref_text = self._voice_text if voice == self._voice else None
                return voice, ref_text, self._seed

            # Unrecognised — fall back to default
            print(
                f"[qwen3] voice {voice!r} not recognised (not audio, not seed:N), "
                f"using default",
                flush=True,
            )

        # Default: configured voice with fixed seed
        if self._voice and _is_audio_file(self._voice):
            return self._voice, self._voice_text, self._seed
        return None, None, self._seed

    def load(self) -> None:
        from mlx_audio.tts import load  # type: ignore[import]

        self._model = load(self._model_id)
        if hasattr(self._model, "sample_rate"):
            self.sample_rate = self._model.sample_rate

    def generate(
        self, text: str, speed: float | None = None, voice: str | None = None,
        instruct: str | None = None,
    ) -> np.ndarray | None:
        if self._model is None:
            raise RuntimeError("Qwen3TTSBackend not loaded — call load() first")

        use_speed = speed if speed is not None else self._speed
        ref_audio, ref_text, seed = self._resolve_voice(voice)
        print(f"[qwen3] resolve: voice={voice!r} → ref_audio={ref_audio!r}, seed={seed}", flush=True)
        use_instruct = instruct or self._instruct or None

        t0 = time.time()

        try:
            with self._lock:
                import mlx.core as mx  # type: ignore[import]
                mx.random.seed(seed)

                chunks = list(self._model.generate(
                    text=text,
                    speed=use_speed,
                    temperature=self._temperature,
                    repetition_penalty=self._repetition_penalty,
                    top_p=self._top_p,
                    top_k=self._top_k,
                    ref_audio=ref_audio,
                    ref_text=ref_text,
                    instruct=use_instruct,
                    split_pattern="",  # we handle chunking in the daemon
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
            voice_desc = f"ref={ref_audio}" if ref_audio else f"seed={seed}"
            print(
                f"[qwen3] generated {duration:.1f}s audio in {elapsed:.1f}s "
                f"(RTF {elapsed / duration:.2f}, {voice_desc})",
                flush=True,
            )
            return combined

        except Exception as exc:
            print(f"[qwen3] generate error: {exc}", flush=True)
            return None

    def generate_streaming(
        self, text: str, speed: float | None = None, voice: str | None = None,
        instruct: str | None = None, playback_queue=None, stop_check=None,
        msg_id: int = -1,
    ) -> "np.ndarray | None":
        """Generate audio with streaming — yield chunks to playback_queue as they arrive.

        If playback_queue is provided, chunks are queued directly and None is returned.
        If playback_queue is None, collects all chunks and returns concatenated array.
        """
        if self._model is None:
            raise RuntimeError("Qwen3TTSBackend not loaded — call load() first")

        ref_audio, ref_text, seed = self._resolve_voice(voice)
        use_instruct = instruct or self._instruct or None
        _FIRST_CHUNK_TIMEOUT = 15.0  # qwen3 ICL prefill can be slow

        t0 = time.time()
        total_samples = 0
        n_chunks = 0
        collected: list[np.ndarray] = []

        try:
            with self._lock:
                import mlx.core as mx  # type: ignore[import]
                mx.random.seed(seed)

                for result in self._model.generate(
                    text=text,
                    temperature=self._temperature,
                    repetition_penalty=self._repetition_penalty,
                    top_p=self._top_p,
                    top_k=self._top_k,
                    ref_audio=ref_audio,
                    ref_text=ref_text,
                    instruct=use_instruct,
                    split_pattern="",  # we handle chunking in the daemon
                    stream=True,
                    streaming_interval=1.5,
                ):
                    if stop_check and stop_check():
                        break
                    if n_chunks == 0 and time.time() - t0 > _FIRST_CHUNK_TIMEOUT:
                        print(f"[qwen3-stream] first-chunk timeout ({_FIRST_CHUNK_TIMEOUT}s)", flush=True)
                        break

                    arr = np.array(result.audio, dtype=np.float32)
                    if arr.ndim > 1:
                        arr = arr.squeeze()
                    if arr.size == 0:
                        continue

                    if result.sample_rate and result.sample_rate != self.sample_rate:
                        self.sample_rate = result.sample_rate

                    n_chunks += 1
                    total_samples += arr.size

                    if playback_queue is not None:
                        # First chunk carries subtitle text; rest are None
                        subtitle = text if n_chunks == 1 else None
                        playback_queue.put((arr, subtitle, msg_id))
                    else:
                        collected.append(arr)

        except Exception as exc:
            print(f"[qwen3-stream] error: {exc}", flush=True)
            if playback_queue is None and not collected:
                return None

        elapsed = time.time() - t0
        duration = total_samples / self.sample_rate if total_samples > 0 else 0
        voice_desc = f"ref={ref_audio}" if ref_audio else f"seed={seed}"
        rtf = f"{elapsed / duration:.2f}" if duration > 0 else "n/a"
        print(
            f"[qwen3-stream] {n_chunks} chunks, {duration:.1f}s audio in {elapsed:.1f}s "
            f"(RTF {rtf}, {voice_desc})",
            flush=True,
        )

        if playback_queue is not None:
            return None

        if not collected:
            return None
        return np.concatenate(collected) if len(collected) > 1 else collected[0]
