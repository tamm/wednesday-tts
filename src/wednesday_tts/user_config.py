"""User-config location helper — cross-platform.

Resolves the directory where Tamm Yarn looks for user overrides:
- macOS: ~/Library/Application Support/Tamm Yarn/
- Linux/other: $XDG_CONFIG_HOME/tamm-yarn or ~/.config/tamm-yarn
"""

from __future__ import annotations
import os
import sys
from pathlib import Path


def _user_config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Tamm Yarn"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) / "tamm-yarn" if xdg else Path.home() / ".config" / "tamm-yarn"


USER_CONFIG_DIR = _user_config_dir()
USER_CONFIG_PATH = USER_CONFIG_DIR / "config.local.yaml"
TTS_CONFIG_PATH = USER_CONFIG_DIR / "tts-config.json"
