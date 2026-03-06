#!/usr/bin/env python3
"""
PreToolUse hook — speaks any unread assistant text before each tool call.

Claude often writes a sentence before running a tool (e.g. "Let me check that.")
The Stop hook only fires at end of a full turn, so those mid-turn messages are
never spoken. This hook fires before every tool call, finds assistant text blocks
from the current turn and sends them to the wednesday-tts daemon for synthesis.
Dedup is handled server-side by the daemon's ring buffer.

If the server is not running, the hook exits silently (no error, no crash).
"""

import json
import os
import socket
import sys
import urllib.request
import urllib.error

TTS_URL = "http://localhost:5678/speak?content_type=markdown"
CONNECT_TIMEOUT = 1.0  # seconds — bail fast if server not running
UNIX_SOCKET_PATH = "/tmp/tts-daemon.sock"
_IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

def _get_unsent_assistant_texts(transcript_path: str | None) -> list[str]:
    """Return raw text blocks for assistant messages in the current turn.

    Dedup is handled server-side by the daemon's ring buffer — this hook
    just extracts all assistant text blocks after the last user message.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return []

    messages = []
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                msg = json.loads(line.strip())
                if msg.get("type") in ("assistant", "user"):
                    messages.append(msg)
            except (json.JSONDecodeError, KeyError):
                continue

    # Current turn = everything after the last user message
    last_user_idx = -1
    for i, msg in enumerate(messages):
        if msg.get("type") == "user":
            last_user_idx = i

    if last_user_idx < 0:
        return []

    texts = []

    for msg in messages[last_user_idx + 1:]:
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "text":
                continue
            raw = block.get("text", "").strip()
            if raw:
                texts.append(raw)

    return texts


# ---------------------------------------------------------------------------
# Server communication
# ---------------------------------------------------------------------------

def _post_to_server(text: str, session_id: str) -> bool:
    """Send text to the wednesday-tts server. Returns True on success.

    On macOS/Linux: Unix socket using the daemon protocol (SEQ command).
    On Windows: HTTP POST to localhost:5678.
    """
    if _IS_WINDOWS:
        body = text.encode("utf-8")
        req = urllib.request.Request(
            TTS_URL,
            data=body,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "X-Session-Id": session_id,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
                return resp.status < 400
        except urllib.error.URLError:
            return False
        except Exception:
            return False
    else:
        # Unix socket — daemon protocol: SEQ:0:speed:ct:ts:text
        try:
            cmd = f"SEQ:0:N:markdown::{text}\n".encode("utf-8")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(CONNECT_TIMEOUT)
            s.connect(UNIX_SOCKET_PATH)
            try:
                s.sendall(cmd)
                s.settimeout(0.5)
                try:
                    s.recv(64)
                except Exception:
                    pass
                return True
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            return False
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # TTS mute — user toggle for meetings etc.
    import tempfile
    mute_path = os.path.join(tempfile.gettempdir(), "tts-mute")
    if os.path.exists(mute_path):
        sys.exit(0)

    try:
        input_data = json.load(sys.stdin)

        session_id = input_data.get("session_id", "unknown")
        transcript_path = input_data.get("transcript_path")

        texts = _get_unsent_assistant_texts(transcript_path)
        if not texts:
            sys.exit(0)

        # Combine all text blocks and send as a single request.
        # The daemon deduplicates — if it already spoke this text it will
        # return "ok" without rendering or playing it again.
        combined = " ".join(texts).strip()
        if len(combined) < 5:
            sys.exit(0)

        # Truncate to ~2400 chars at a sentence boundary to avoid runaway speech
        if len(combined) > 2400:
            trunc = combined[:2400]
            last_sentence = max(trunc.rfind(". "), trunc.rfind("! "), trunc.rfind("? "))
            if last_sentence > 1200:
                combined = combined[:last_sentence + 1]
            else:
                last_space = trunc.rfind(" ")
                combined = combined[:last_space] if last_space > 0 else trunc

        _post_to_server(combined, session_id)

    except Exception as e:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"TTS unavailable: {e}",
            }
        }))

    sys.exit(0)


if __name__ == "__main__":
    main()
