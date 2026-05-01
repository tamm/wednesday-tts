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


def _send_json(msg: dict) -> None:
    """Connect to Unix socket, send JSON newline-terminated, wait briefly, close."""
    try:
        payload = (json.dumps(msg) + "\n").encode("utf-8")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(UNIX_SOCKET_PATH)
        try:
            sock.sendall(payload)
            sock.settimeout(0.25)
            try:
                sock.recv(64)
            except Exception:
                pass
        finally:
            sock.close()
    except Exception:
        pass


def _send_to_tts(text: str, pan: float) -> bool:
    body = text.strip()
    if len(body) < 5:
        return False
    if os.path.exists(MUTE_PATH) or os.environ.get("TTS_MUTE"):
        return False

    _send_json(
        {
            "command": "speak",
            "text": body,
            "normalization": "markdown",
            "pan": pan,
            "timestamp": time.time(),
        }
    )
    return True


def _extract_message(payload: dict) -> str | None:
    event_type = payload.get("type")
    if event_type in {"agent-turn-complete", "turn-complete"}:
        message = payload.get("last-assistant-message") or payload.get("last_assistant_message")
        if isinstance(message, str) and message.strip():
            return message.strip()

    # Newer Codex notify payloads resemble the opencode event surface:
    # `session.idle` with the final assistant message nested under data.message.
    if event_type != "session.idle":
        return None

    message = payload.get("data", {}).get("message")
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

    return None


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
