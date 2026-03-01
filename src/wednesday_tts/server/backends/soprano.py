"""Soprano TTS backend."""

from __future__ import annotations

import os

import numpy as np

from .base import TTSBackend


class SopranoBackend(TTSBackend):
    """Soprano neural TTS.

    Config keys (from tts-config.json models.soprano):
        backend             — soprano backend type (default: transformers)
        device              — torch device (default: cuda)
        temperature         — sampling temperature (default: 0.3)
        top_p               — nucleus sampling p (default: 0.95)
        repetition_penalty  — repetition penalty (default: 1.2)
        samplerate          — output sample rate (default: 32000)
        venv_path           — path to soprano venv (for dtype patch)
    """

    sample_rate = 32000
    supports_streaming = False

    def __init__(
        self,
        backend: str = "transformers",
        device: str = "cuda",
        temperature: float = 0.3,
        top_p: float = 0.95,
        repetition_penalty: float = 1.2,
        samplerate: int = 32000,
        venv_path: str | None = None,
    ) -> None:
        self._backend_type = backend or os.environ.get("SOPRANO_BACKEND", "transformers")
        self._device = device or os.environ.get("SOPRANO_DEVICE", "cuda")
        self._temperature = temperature
        self._top_p = top_p
        self._repetition_penalty = repetition_penalty
        self.sample_rate = samplerate
        self._venv_path = venv_path
        self._model = None

    def load(self) -> None:
        # Apply dtype kwarg fix by patching the imported module in memory,
        # avoiding any writes to site-packages.
        self._patch_soprano_dtype()
        from soprano import SopranoTTS  # type: ignore[import]
        self._model = SopranoTTS(backend=self._backend_type, device=self._device)

    @staticmethod
    def _patch_soprano_dtype() -> None:
        """Monkey-patch soprano.backends.transformers to use dtype= not torch_dtype=.

        soprano passes torch_dtype= to a model loader that expects dtype=.
        We fix this by rewriting the affected function's source in memory after import,
        rather than modifying the installed package file.
        """
        import inspect

        mod_name = "soprano.backends.transformers"
        try:
            import importlib
            mod = importlib.import_module(mod_name)
        except ImportError:
            return  # soprano not installed — nothing to do

        if getattr(mod, "_dtype_kwarg_patched", False):
            return  # already patched this session

        src = inspect.getsource(mod)
        if "torch_dtype=" not in src:
            mod._dtype_kwarg_patched = True  # type: ignore[attr-defined]
            return  # already correct in this version

        patched_src = src.replace("torch_dtype=", "dtype=")
        try:
            code = compile(patched_src, inspect.getfile(mod), "exec")
            exec(code, mod.__dict__)  # noqa: S102
            mod._dtype_kwarg_patched = True  # type: ignore[attr-defined]
        except Exception:
            pass  # best-effort — if it fails, soprano may still work

    def generate(self, text: str, speed: float | None = None) -> "np.ndarray | None":
        if self._model is None:
            raise RuntimeError("SopranoBackend not loaded — call load() first")

        try:
            wav = self._model.infer(
                text,
                temperature=self._temperature,
                top_p=self._top_p,
                repetition_penalty=self._repetition_penalty,
            )
            audio: np.ndarray = wav.cpu().numpy()
            return audio if audio.size > 0 else None
        except Exception as exc:
            print(f"[soprano] generate error: {exc}")
            return None
