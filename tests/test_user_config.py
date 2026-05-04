"""Tests for wednesday_tts.user_config."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _reload(monkeypatch, platform: str, xdg: str | None = None):
    monkeypatch.setattr(sys, "platform", platform)
    if xdg is not None:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    else:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    import wednesday_tts.user_config as m
    importlib.reload(m)
    return m


def test_macos_path(monkeypatch):
    m = _reload(monkeypatch, "darwin")
    assert m.USER_CONFIG_DIR == Path.home() / "Library" / "Application Support" / "Tamm Yarn"
    assert m.USER_CONFIG_PATH == m.USER_CONFIG_DIR / "config.local.yaml"
    assert m.TTS_CONFIG_PATH == m.USER_CONFIG_DIR / "tts-config.json"


def test_linux_xdg_override(monkeypatch, tmp_path):
    m = _reload(monkeypatch, "linux", xdg=str(tmp_path))
    assert m.USER_CONFIG_DIR == tmp_path / "tamm-yarn"


def test_linux_default_no_xdg(monkeypatch):
    m = _reload(monkeypatch, "linux")
    assert m.USER_CONFIG_DIR == Path.home() / ".config" / "tamm-yarn"
