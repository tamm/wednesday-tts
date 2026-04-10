#!/usr/bin/env python3
"""
Claude Code post-response hook — thin client for Wednesday TTS.

Triggered on Stop events. Reads the JSON hook payload from stdin,
extracts the assistant message, and sends a JSON speak request to the
Wednesday TTS daemon over the Unix socket at /tmp/tts-daemon.sock.

All heavy lifting (voice selection, normalization, chunking, synthesis)
happens in the daemon. This hook stays thin: compute voice_hash, pan,
timestamp, and send.

Environment variables:
    TTS_MUTE=1   Disable TTS (also honoured via /tmp/tts-mute file)
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UNIX_SOCKET_PATH = "/tmp/tts-daemon.sock"

_TEMP = tempfile.gettempdir()
MUTE_PATH = os.path.join(_TEMP, "tts-mute")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_last_assistant_message(transcript_path: str | None) -> str:
    """Fallback: extract last assistant message from the JSONL transcript.

    Claude Code writes transcripts as newline-delimited JSON, one message
    per line, with `type` of "assistant"/"user" and content nested under
    `message.content`. Parse line-by-line — `json.load` on the whole file
    fails silently and leaves TTS mute for the turn.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        messages = []
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        for msg in reversed(messages):
            if msg.get("type") != "assistant":
                continue
            content = msg.get("message", {}).get("content", "")
            if isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                text = " ".join(p for p in parts if p).strip()
                if text:
                    return text
            elif isinstance(content, str) and content.strip():
                return content.strip()
    except Exception:
        pass
    return ""


def _compute_voice_hash(cwd: str) -> str:
    """SHA-256 of git repo root (or cwd if not in a repo), first 8 hex chars."""
    try:
        repo = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2
        ).stdout.strip()
    except Exception:
        repo = ""
    key = repo or cwd
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _fire_and_forget(msg: dict) -> None:
    """Send a JSON speak message to the TTS daemon over the Unix socket.

    Waits up to 0.5s for an acknowledgement. On 10s connect timeout,
    pings the daemon and optionally restarts it via launchctl.
    """
    payload = (json.dumps(msg) + "\n").encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(UNIX_SOCKET_PATH)
    try:
        s.sendall(payload)
        s.settimeout(0.5)
        try:
            s.recv(64)
        except socket.timeout:
            # No confirmation yet — daemon may be busy, that's fine
            pass
    except socket.timeout:
        # 10s connect timeout — daemon may be dead, diagnose with ping
        try:
            ps = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            ps.settimeout(2)
            ps.connect(UNIX_SOCKET_PATH)
            try:
                ps.sendall((json.dumps({"command": "ping"}) + "\n").encode("utf-8"))
                ps.settimeout(1)
                resp = ps.recv(64).strip()
            except Exception:
                resp = b""
            finally:
                ps.close()
        except Exception:
            resp = b""
        if resp in (b"dying", b""):
            # Daemon unreachable or dying — kick it
            subprocess.run(
                ["launchctl", "kickstart", "-k",
                 f"gui/{os.getuid()}/com.tamm.wednesday-tts"],
                capture_output=True,
            )
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

    # Barge-in active — user is still speaking, hold TTS until insertion completes.
    # Max age 30s to prevent stale flags from silencing TTS permanently.
    barge_in_path = os.path.join(_TEMP, "wednesday-yarn-barge-in")
    try:
        age = time.time() - os.path.getmtime(barge_in_path)
        if age < 30:
            sys.exit(0)
        else:
            os.unlink(barge_in_path)  # stale, clean up
    except FileNotFoundError:
        pass

    # Parse hook payload from stdin
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    # Teammate/subagent sessions — only the main session speaks
    if payload.get("agent_id") or payload.get("agent_name") or payload.get("team_name"):
        sys.exit(0)

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "") or os.getcwd()

    # Prefer the message Claude inlines in the hook payload — transcript can lag
    text = payload.get("last_assistant_message") or \
           _get_last_assistant_message(payload.get("transcript_path"))

    if not text or len(text.strip()) < 5:
        sys.exit(0)

    # Compute voice_hash from repo root or cwd
    voice_hash = _compute_voice_hash(cwd)

    # Compute stereo pan from terminal window position (macOS only, silent fallback)
    pan = 0.5
    try:
        from window_position import compute_pan
        pan = compute_pan()
    except Exception:
        pass

    # Build JSON message per wire protocol spec
    msg: dict = {
        "command": "speak",
        "text": text,
        "normalization": "markdown",
        "voice_hash": voice_hash,
        "timestamp": wall_time,
    }
    if session_id:
        msg["session_id"] = session_id
    if pan != 0.5:
        msg["pan"] = pan

    # Send to daemon — silent fail if not running
    try:
        _fire_and_forget(msg)
    except (ConnectionRefusedError, OSError, TimeoutError):
        pass
    except Exception as exc:
        print(f"wednesday-tts hook: {exc}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
