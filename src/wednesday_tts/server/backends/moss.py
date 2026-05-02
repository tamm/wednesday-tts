"""MOSS-TTS-Nano backend — OpenMOSS 100M ONNX runtime, CPU-only."""

from __future__ import annotations

import os
import tempfile
import threading
import time

import numpy as np

from .base import DEFAULT_SPEED, TTSBackend, soundstretch_tempo

_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}


def _is_audio_file(path: str) -> bool:
    return os.path.isfile(path) and os.path.splitext(path)[1].lower() in _AUDIO_EXTS


class MossNanoBackend(TTSBackend):
    """MOSS-TTS-Nano via the upstream `onnx_tts_runtime` (CPU, auto-downloaded ONNX assets).

    Config keys (from tts-config.json models.moss):
        voice                       — built-in preset name (default: "Junhao") OR
                                      a path to a reference WAV for cloning
        prompt_audio_path           — explicit reference WAV (overrides voice if a path)
        model_dir                   — override ONNX asset dir; None = auto-download
        cpu_threads                 — onnxruntime intra-op threads (default 4)
        max_new_frames              — generation cap (default 375)
        voice_clone_max_text_tokens — sentence chunk budget (default 75)
        speed                       — tempo multiplier; applied via soundstretch
        seed                        — sampling seed
        enable_wetext               — WeTextProcessing normalization (default False)
        text_temperature, text_top_p, text_top_k,
        audio_temperature, audio_top_p, audio_top_k,
        audio_repetition_penalty    — sampling knobs
    """

    sample_rate = 24000  # overwritten from runtime result after first synth
    supports_streaming = False  # runtime returns final waveform; revisit later

    def __init__(
        self,
        voice: str = "Junhao",
        prompt_audio_path: str | None = None,
        model_dir: str | None = None,
        cpu_threads: int = 4,
        max_new_frames: int = 375,
        voice_clone_max_text_tokens: int = 75,
        speed: float = DEFAULT_SPEED,
        seed: int | None = 7,
        enable_wetext: bool = False,
        text_temperature: float = 1.0,
        text_top_p: float = 1.0,
        text_top_k: int = 50,
        audio_temperature: float = 0.8,
        audio_top_p: float = 0.95,
        audio_top_k: int = 25,
        audio_repetition_penalty: float = 1.2,
    ) -> None:
        self._voice = voice or "Junhao"
        self._prompt_audio_path = prompt_audio_path
        self._model_dir = model_dir
        self._cpu_threads = cpu_threads
        self._max_new_frames = max_new_frames
        self._voice_clone_max_text_tokens = voice_clone_max_text_tokens
        self._speed = speed
        self._seed = seed
        self._enable_wetext = enable_wetext
        self._text_temperature = text_temperature
        self._text_top_p = text_top_p
        self._text_top_k = text_top_k
        self._audio_temperature = audio_temperature
        self._audio_top_p = audio_top_p
        self._audio_top_k = audio_top_k
        self._audio_repetition_penalty = audio_repetition_penalty
        self._runtime = None
        self._lock = threading.Lock()

    def load(self) -> None:
        # Upstream sometimes lives as a sibling install (`pip install -e ~/dev/MOSS-TTS-Nano`).
        # If the import fails because we only cloned the repo, add it to sys.path as a fallback.
        try:
            from onnx_tts_runtime import OnnxTtsRuntime  # type: ignore[import]
        except ImportError:
            import sys

            for candidate in (
                os.path.expanduser("~/dev/MOSS-TTS-Nano"),
                os.environ.get("MOSS_TTS_NANO_REPO", ""),
            ):
                if candidate and os.path.isdir(candidate) and candidate not in sys.path:
                    sys.path.insert(0, candidate)
            from onnx_tts_runtime import OnnxTtsRuntime  # type: ignore[import]

        self._runtime = OnnxTtsRuntime(
            model_dir=self._model_dir,
            thread_count=self._cpu_threads,
            max_new_frames=self._max_new_frames,
            do_sample=True,
            sample_mode="fixed",
        )
        gd = self._runtime.manifest["generation_defaults"]
        gd["text_temperature"] = float(self._text_temperature)
        gd["text_top_p"] = float(self._text_top_p)
        gd["text_top_k"] = int(self._text_top_k)
        gd["audio_temperature"] = float(self._audio_temperature)
        gd["audio_top_p"] = float(self._audio_top_p)
        gd["audio_top_k"] = int(self._audio_top_k)
        gd["audio_repetition_penalty"] = float(self._audio_repetition_penalty)

        try:
            self.sample_rate = int(self._runtime.codec_meta["codec_config"]["sample_rate"])
        except (KeyError, TypeError, ValueError):
            pass

    def _resolve_voice(self, voice: str | None) -> tuple[str, str | None]:
        """Return (preset_name, prompt_audio_path).

        - If `voice` is an audio file path → ("", that_path).
        - If `voice` is a non-empty string → (voice, configured prompt_audio_path).
        - If `voice` is None → (default preset, configured prompt_audio_path).
        """
        if isinstance(voice, str) and _is_audio_file(voice):
            return "", voice
        if isinstance(voice, str) and voice:
            return voice, self._prompt_audio_path
        # default
        if self._prompt_audio_path and _is_audio_file(self._prompt_audio_path):
            return "", self._prompt_audio_path
        return self._voice, None

    def generate(
        self, text: str, speed: float | None = None, voice: str | None = None
    ) -> np.ndarray | None:
        if self._runtime is None:
            raise RuntimeError("MossNanoBackend not loaded — call load() first")
        if not text or not text.strip():
            return None

        use_speed = speed if speed is not None else self._speed
        preset, prompt_audio = self._resolve_voice(voice)

        t0 = time.time()
        out_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                out_path = tf.name

            with self._lock:
                result = self._runtime.synthesize(
                    text=text,
                    voice=preset,
                    prompt_audio_path=prompt_audio,
                    output_audio_path=out_path,
                    sample_mode="fixed",
                    do_sample=True,
                    streaming=True,
                    max_new_frames=self._max_new_frames,
                    voice_clone_max_text_tokens=self._voice_clone_max_text_tokens,
                    enable_wetext=self._enable_wetext,
                    enable_normalize_tts_text=True,
                    seed=self._seed,
                )

            audio_path = result.get("audio_path", out_path)
            sr = int(result.get("sample_rate") or self.sample_rate)
            self.sample_rate = sr

            import soundfile as sf  # lazy

            arr, file_sr = sf.read(audio_path, dtype="float32")
            if file_sr and file_sr != sr:
                self.sample_rate = file_sr
                sr = file_sr
            if arr.ndim > 1:
                arr = arr.mean(axis=1).astype(np.float32)
            if arr.size == 0:
                return None

            if abs(use_speed - 1.0) > 0.01:
                arr = soundstretch_tempo(arr, sr, use_speed)

            elapsed = time.time() - t0
            duration = arr.size / sr if sr else 0.0
            rtf = f"{elapsed / duration:.2f}" if duration > 0 else "n/a"
            voice_desc = f"ref={prompt_audio}" if prompt_audio else f"preset={preset}"
            print(
                f"[moss] generated {duration:.1f}s audio in {elapsed:.1f}s "
                f"(RTF {rtf}, {voice_desc})",
                flush=True,
            )
            return arr

        except Exception as exc:
            print(f"[moss] generate error: {exc}", flush=True)
            return None
        finally:
            if out_path:
                try:
                    os.unlink(out_path)
                except Exception:
                    pass
