#!/usr/bin/env python3
"""
Gemini CLI hook — thin client for Wednesday TTS.

Triggered on:
- AfterModel: incremental speech (filtered to text only, no tool calls)
- AfterAgent: final response speech
- Notification: system alerts (tool permissions, etc.)

Dedup is handled server-side by the daemon's ring buffer.

Environment variables:
    TTS_MUTE=1   Disable TTS (also honoured via /tmp/tts-mute file)
"""
import json
import os
import socket
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UNIX_SOCKET_PATH = "/tmp/tts-daemon.sock"
_IS_WINDOWS = os.name == "nt"

# Path to mute sentinel file (shared with other hooks)
_TEMP = tempfile.gettempdir()
MUTE_PATH = os.path.join(_TEMP, "tts-mute")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_assistant_text(payload: dict) -> str:
    """Extract assistant text from Gemini CLI hook payload based on event."""
    event_name = payload.get("hook_event_name", "unknown")

    # Final response once turn is complete
    if event_name == "AfterAgent":
        return payload.get("prompt_response", "").strip()

    # Incremental response (fires after every model chunk)
    if event_name == "AfterModel":
        candidates = payload.get("llm_response", {}).get("candidates", [])
        if not candidates:
            return ""

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        text_parts = []
        for p in parts:
            if isinstance(p, str):
                text_parts.append(p)
            elif isinstance(p, dict):
                # Only speak 'text' parts, skip 'toolCall' etc.
                if "text" in p:
                    text_parts.append(p["text"])

        return " ".join(text_parts).strip()

    # System alerts
    if event_name == "Notification":
        return payload.get("message", "").strip()

    return ""


def _fire_and_forget(text: str, wall_time: float) -> None:
    """Send text to the TTS server via Unix socket using the daemon protocol.

    Uses colon-delimited fields: SEQ:0:N:markdown:<wall_time>:<text>
    """
    if not text or len(text.strip()) < 2:
        return

    if _IS_WINDOWS:
        return

    try:
        # We use SEQ:0 for all hooks; the daemon's dedup ring buffer (20 items)
        # prevents us from speaking the same text twice if AfterModel and
        # AfterAgent overlap.
        # Prepend __v:fantine__ to ensure Gemini always uses the 'fantine' voice.
        body = f"__v:fantine__{text}"
        cmd = f"SEQ:0:N:markdown:{wall_time}:{body}\n".encode("utf-8")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(UNIX_SOCKET_PATH)
        try:
            s.sendall(cmd)
            s.settimeout(0.5)
            try:
                s.recv(64)
            except socket.timeout:
                pass
        finally:
            s.close()
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    wall_time = time.time()

    if os.path.exists(MUTE_PATH) or os.environ.get("TTS_MUTE"):
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    text = _get_assistant_text(payload)

    if not text:
        sys.exit(0)

    try:
        _fire_and_forget(text, wall_time)
    except Exception as exc:
        print(f"wednesday-tts gemini hook: {exc}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
