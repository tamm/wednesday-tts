#!/usr/bin/env python3
"""Codex notify handler that forwards assistant turns to Wednesday TTS."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

UNIX_SOCKET_PATH = "/tmp/tts-daemon.sock"
MUTE_PATH = os.path.join(tempfile.gettempdir(), "tts-mute")


def _compute_pan() -> float:
    try:
        claude_code_dir = Path(__file__).resolve().parents[1] / "claude-code"
        sys.path.insert(0, str(claude_code_dir))
        from window_position import compute_pan

        return compute_pan()
    except Exception:
        return 0.5


def _send_to_tts(text: str, pan: float) -> bool:
    body = text.strip()
    if len(body) < 5:
        return False
    if os.path.exists(MUTE_PATH) or os.environ.get("TTS_MUTE"):
        return False

    pan_str = f"{pan:.3f}" if pan != 0.5 else ""
    cmd = f"SEQ:0:N:markdown:{time.time()}:{pan_str}:{body}\n".encode("utf-8")

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(UNIX_SOCKET_PATH)
        try:
            sock.sendall(cmd)
            sock.settimeout(0.25)
            try:
                sock.recv(64)
            except Exception:
                pass
            return True
        finally:
            sock.close()
    except Exception:
        return False


def _extract_message(payload: dict) -> str | None:
    event_type = payload.get("type")
    if event_type not in {"agent-turn-complete", "turn-complete"}:
        return None

    message = payload.get("last-assistant-message") or payload.get("last_assistant_message")
    if not isinstance(message, str) or not message.strip():
        return None
    return message.strip()


def main() -> int:
    if len(sys.argv) < 2:
        return 0

    try:
        payload = json.loads(sys.argv[1])
    except Exception:
        return 0

    if not isinstance(payload, dict):
        return 0

    message = _extract_message(payload)
    if not message:
        return 0

    _send_to_tts(message, pan=_compute_pan())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
