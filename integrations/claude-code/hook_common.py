"""Shared helpers for Claude Code TTS hooks.

Both `speak-response.py` (Stop hook) and `pre-tool-speak.py` (PreToolUse
hook) import from this module. Keep behaviour that MUST match between the
two hooks here — especially the primary-session filter that silences
sub-agents and teammates.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time

UNIX_SOCKET_PATH = "/tmp/tts-daemon.sock"

_TEMP = tempfile.gettempdir()
MUTE_PATH = os.path.join(_TEMP, "tts-mute")
BARGE_IN_PATH = os.path.join(_TEMP, "wednesday-yarn-barge-in")
BARGE_IN_MAX_AGE_SECS = 30.0


def is_muted() -> bool:
    """True if TTS is silenced via sentinel file or TTS_MUTE env var."""
    return os.path.exists(MUTE_PATH) or bool(os.environ.get("TTS_MUTE"))


def is_barge_in_active() -> bool:
    """True if the barge-in flag is present and not stale.

    The flag is set by wednesday-yarn while the user is dictating. Stale
    flags (older than BARGE_IN_MAX_AGE_SECS) are cleaned up so a crashed
    barge-in source can never silence TTS permanently.
    """
    try:
        age = time.time() - os.path.getmtime(BARGE_IN_PATH)
    except FileNotFoundError:
        return False
    if age < BARGE_IN_MAX_AGE_SECS:
        return True
    try:
        os.unlink(BARGE_IN_PATH)
    except OSError:
        pass
    return False


def is_subagent(payload: dict) -> bool:
    """True if the Claude Code payload indicates a sub-agent or teammate turn.

    Only the primary Claude session is allowed to speak. Sub-agents and
    teammates must be silent — Tamm hears all assistant turns via TTS and
    overlapping voices are not acceptable.

    The Claude Code Stop and PreToolUse payloads include any of these
    fields for non-primary turns. ALL three must be checked. Do not narrow
    this check without updating docs/voice-pipeline-spec.md.
    """
    return bool(
        payload.get("agent_id")
        or payload.get("agent_name")
        or payload.get("team_name")
    )


def compute_voice_hash(cwd: str) -> str:
    """SHA-256 of the git repo root (or cwd if not in a repo), first 8 hex chars."""
    try:
        repo = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except Exception:
        repo = ""
    key = repo or cwd
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def compute_pan() -> float:
    """Stereo pan from terminal window position (macOS only). 0.5 centre on failure."""
    try:
        from window_position import compute_pan as _cp  # type: ignore
        return _cp()
    except Exception:
        return 0.5


def send_speak(msg: dict, *, kick_on_timeout: bool = False) -> None:
    """Send a JSON speak message to the TTS daemon over the Unix socket.

    Waits briefly for an acknowledgement. On a connect timeout, optionally
    pings the daemon and kicks it via launchctl if it appears dead. Silent
    on any failure — the hook must never crash the turn.
    """
    payload = (json.dumps(msg) + "\n").encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10 if kick_on_timeout else 1.0)
    try:
        s.connect(UNIX_SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        try:
            s.close()
        except Exception:
            pass
        return
    try:
        s.sendall(payload)
        s.settimeout(0.5)
        try:
            s.recv(64)
        except socket.timeout:
            pass
    except socket.timeout:
        if kick_on_timeout:
            _kick_daemon_if_dying()
    except Exception:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass


def _kick_daemon_if_dying() -> None:
    """Ping the daemon; if it reports dying or is unreachable, kick via launchctl."""
    resp = b""
    try:
        ps = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ps.settimeout(2)
        ps.connect(UNIX_SOCKET_PATH)
        try:
            ps.sendall((json.dumps({"command": "ping"}) + "\n").encode("utf-8"))
            ps.settimeout(1)
            resp = ps.recv(64).strip()
        finally:
            ps.close()
    except Exception:
        resp = b""
    if resp in (b"dying", b""):
        subprocess.run(
            ["launchctl", "kickstart", "-k",
             f"gui/{os.getuid()}/com.tamm.wednesday-tts"],
            capture_output=True,
        )
