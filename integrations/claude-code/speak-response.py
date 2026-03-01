#!/usr/bin/env python3
"""
Claude Code post-response hook — thin client for Wednesday TTS.

Triggered on Stop events. Reads the JSON hook payload from stdin,
extracts the assistant message, and POSTs the raw markdown to the
Wednesday TTS server at localhost:5678 for normalization and synthesis.

All heavy lifting (normalization, chunking, synthesis) happens in the
server. This script stays under 120 lines.

Environment variables:
    TTS_MUTE=1   Disable TTS (also honoured via /tmp/tts-mute file)
"""
import hashlib
import json
import os
import socket
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = 5678
SERVICE_URL = f"http://localhost:{SERVICE_PORT}"

# Path to mute sentinel file (shared with old hooks via tts_platform)
_TEMP = tempfile.gettempdir()
MUTE_PATH = os.path.join(_TEMP, "tts-mute")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spoken_hashes_path(session_id: str) -> str:
    """Per-session dedup file — prevents double-speak if Stop fires twice."""
    return os.path.join(_TEMP, f"tts-spoken-{session_id}")


def _already_spoken(text: str, session_id: str) -> bool:
    """Return True if this text hash has already been sent this session."""
    h = hashlib.md5(text.encode()).hexdigest()
    path = _spoken_hashes_path(session_id)
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                if h in {line.strip() for line in f}:
                    return True
        # Record now so a racing second invocation sees it
        with open(path, "a", encoding="utf-8") as f:
            f.write(h + "\n")
    except Exception:
        pass
    return False


def _get_last_assistant_message(transcript_path: str | None) -> str:
    """Fallback: extract last assistant message from the transcript JSON."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path, encoding="utf-8") as f:
            data = json.load(f)
        messages = data if isinstance(data, list) else data.get("messages", [])
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Content blocks — join text parts
                    parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    return " ".join(parts).strip()
                return str(content).strip()
    except Exception:
        pass
    return ""


def _fire_and_forget(text: str, session_id: str, wall_time: float) -> None:
    """POST text to /speak using a raw socket (fire-and-forget, no wait for response).

    Prepends __t:<wall_time>__ for server-side end-to-end timing.
    Sets content_type=markdown so the server runs the full normalization pipeline.
    """
    body_str = f"__t:{wall_time}__" + text
    body = body_str.encode("utf-8")

    request_line = "POST /speak?content_type=markdown HTTP/1.0\r\n"
    headers = (
        f"Content-Length: {len(body)}\r\n"
        f"X-Session-Id: {session_id}\r\n"
        "\r\n"
    )
    raw = (request_line + headers).encode("utf-8") + body

    s = socket.create_connection((SERVICE_HOST, SERVICE_PORT), timeout=2)
    try:
        s.sendall(raw)
        # Give the server a moment to read the request before we close
        s.settimeout(0.5)
        try:
            s.recv(256)
        except Exception:
            pass
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    wall_time = time.time()

    # Honour mute sentinel (meetings, quiet mode)
    if os.path.exists(MUTE_PATH) or os.environ.get("TTS_MUTE"):
        sys.exit(0)

    # Parse hook payload from stdin
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")

    # Prefer the message Claude inlines in the hook payload — transcript can lag
    text = payload.get("last_assistant_message") or \
           _get_last_assistant_message(payload.get("transcript_path"))

    if not text or len(text.strip()) < 5:
        sys.exit(0)

    # Dedup guard — bail if we've already sent this text this session
    if _already_spoken(text, session_id):
        sys.exit(0)

    # Send to server — silent fail if server is not running
    try:
        _fire_and_forget(text, session_id, wall_time)
    except (ConnectionRefusedError, OSError, TimeoutError):
        # Server not running — skip silently
        pass
    except Exception as exc:
        print(f"wednesday-tts hook: {exc}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
