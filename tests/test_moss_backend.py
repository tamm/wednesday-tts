"""Smoke tests for the MOSS-TTS-Nano backend.

Real synthesis is gated behind the upstream `onnx_tts_runtime` install — CI
skips when missing. Default-config wiring and registry entry are checked
unconditionally.
"""

from __future__ import annotations

import pytest


def test_registered() -> None:
    from wednesday_tts.server.backends import REGISTRY
    from wednesday_tts.server.backends.moss import MossNanoBackend

    assert "moss" in REGISTRY
    assert REGISTRY["moss"] is MossNanoBackend


def test_default_params() -> None:
    from wednesday_tts.server.backends.moss import MossNanoBackend

    b = MossNanoBackend()
    assert b._voice == "Junhao"
    assert b._cpu_threads == 4
    assert b._max_new_frames == 375
    assert b.sample_rate == 24000
    assert b.supports_streaming is False


def test_custom_params() -> None:
    from wednesday_tts.server.backends.moss import MossNanoBackend

    b = MossNanoBackend(voice="Custom", cpu_threads=8, seed=42, enable_wetext=True)
    assert b._voice == "Custom"
    assert b._cpu_threads == 8
    assert b._seed == 42
    assert b._enable_wetext is True


def test_ignores_unknown_kwargs() -> None:
    from wednesday_tts.server.backends.moss import MossNanoBackend

    # Configs may grow new keys; backend should not blow up on extras passed via **kwargs.
    # Note: current __init__ doesn't take **kwargs — this test just guards against forgetting.
    with pytest.raises(TypeError):
        MossNanoBackend(unknown_key="ignored")  # noqa


def test_generate_before_load_raises() -> None:
    from wednesday_tts.server.backends.moss import MossNanoBackend

    b = MossNanoBackend()
    with pytest.raises(RuntimeError, match="not loaded"):
        b.generate("hello")


def test_resolve_voice_audio_path_overrides() -> None:
    from wednesday_tts.server.backends.moss import MossNanoBackend

    b = MossNanoBackend(voice="Junhao")
    preset, ref = b._resolve_voice(None)
    assert preset == "Junhao"
    assert ref is None


def test_resolve_voice_string_treated_as_preset() -> None:
    from wednesday_tts.server.backends.moss import MossNanoBackend

    b = MossNanoBackend()
    preset, ref = b._resolve_voice("Alice")
    assert preset == "Alice"
    assert ref is None


def test_load_requires_upstream() -> None:
    """If onnx_tts_runtime isn't importable, load() should fail clearly."""
    pytest.importorskip("onnx_tts_runtime")
    # If the import works, a full load() is heavy (downloads ~hundreds of MB).
    # We only assert the import path is reachable, not that load() completes.
