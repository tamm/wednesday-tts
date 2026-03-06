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
UNIX_SOCKET_PATH = "/tmp/tts-daemon.sock"
_IS_WINDOWS = os.name == "nt"

# Path to mute sentinel file (shared with old hooks via tts_platform)
_TEMP = tempfile.gettempdir()
MUTE_PATH = os.path.join(_TEMP, "tts-mute")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    """Send text to the TTS server.

    On macOS/Linux: Unix socket using the daemon protocol (SEQ command).
    On Windows: raw HTTP POST to localhost:5678.

    Uses colon-delimited fields: SEQ:0:1.0:markdown:<wall_time>:<text>
    """
    body_str = text

    if _IS_WINDOWS:
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
    else:
        # Hook sends SEQ:0 blindly. Daemon is responsible for all fallback.
        # If no response in 10s, ping to diagnose and optionally restart.
        import subprocess

        cmd = f"SEQ:0:1.0:markdown:{wall_time}:{body_str}\n".encode("utf-8")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(UNIX_SOCKET_PATH)
        try:
            s.sendall(cmd)
            s.settimeout(0.5)
            try:
                s.recv(64)
            except socket.timeout:
                # No confirmation yet — daemon may be busy, that's fine
                pass
        except socket.timeout:
            # 10s connect timeout — daemon may be dead, diagnose with PING
            try:
                ps = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                ps.settimeout(2)
                ps.connect(UNIX_SOCKET_PATH)
                try:
                    ps.sendall(b"PING\n")
                    ps.settimeout(1)
                    resp = ps.recv(16).strip()
                except Exception:
                    resp = b""
                finally:
                    ps.close()
            except Exception:
                resp = b""
            if resp in (b"dying", b"") :
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
