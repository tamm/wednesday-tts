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


def is_muted() -> bool:
    """True if TTS is silenced via sentinel file or TTS_MUTE env var."""
    return os.path.exists(MUTE_PATH) or bool(os.environ.get("TTS_MUTE"))


def is_subagent(payload: dict) -> bool:
    """True if the Claude Code payload indicates a sub-agent or teammate turn.

    Only the primary Claude session is allowed to speak. Sub-agents and
    teammates must be silent — Tamm hears all assistant turns via TTS and
    overlapping voices are not acceptable.

    WHAT WE DISCOVERED (from live payload capture, 2026-04-11):
    - Sub-agents: `agent_id` and `agent_type` ARE present in the Stop /
      PreToolUse payload dict. Definitive signal per official docs.
    - Agent-team teammates: `team_name` and `teammate_name` are NOT present
      in the Stop / PreToolUse payload dict. The docs only document those
      fields for TaskCreated events. Teammate sessions fire ordinary Stop
      hooks with no identifying payload fields.
    - Authoritative teammate signal: the transcript JSONL at `transcript_path`
      has `teamName` and `agentName` on every line for teammate sessions.
      The lead's transcript does NOT have those fields.

    Check order:
    1. Payload agent_id / agent_type → sub-agent, block.
    2. Payload team_name / teammate_name → belt-and-braces in case Claude Code
       ever adds them; harmless since they never fire in practice.
    3. Transcript teamName / agentName → teammate, block.
    4. Team-registry session_id fallback → last-resort teammate check.
    """
    if (
        payload.get("agent_id")
        or payload.get("agent_type")
        or payload.get("team_name")
        or payload.get("teammate_name")
    ):
        return True
    if _transcript_is_teammate(payload.get("transcript_path")):
        return True
    session_id = payload.get("session_id")
    if session_id and _session_is_non_lead_teammate(session_id):
        return True
    return False


def _transcript_is_teammate(transcript_path: str | None) -> bool:
    """Return True if the transcript JSONL identifies this as a teammate session.

    Agent-team teammate transcripts have `teamName` and `agentName` on every
    line. The lead's transcript does not. We only need to find ONE line with
    either field to be sure.

    Reads up to 20 lines to avoid blocking on a large transcript. Silent on
    all errors — a missing or unreadable transcript must never silence the
    lead. Default answer on failure is False (allow to speak).
    """
    if not transcript_path:
        return False
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 20:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("teamName") or obj.get("agentName"):
                    return True
    except Exception:
        pass
    return False


def _session_is_non_lead_teammate(session_id: str) -> bool:
    """Return True if session_id appears in any team config as something other than the lead.

    Walks ~/.claude/teams/*/config.json. If the given session_id matches
    any team's leadSessionId, this is the lead — return False (it can
    speak). Otherwise, if the session_id is listed anywhere in the team
    config (members array, inboxes, etc.) it is a teammate — return True.

    Silent on errors: the registry is optional; a missing or malformed
    file must never crash the hook. The default answer on failure is
    False (speak), because this is a secondary check layered on top of
    the payload-level filter above.
    """
    teams_dir = os.path.expanduser("~/.claude/teams")
    if not os.path.isdir(teams_dir):
        return False
    try:
        for entry in os.listdir(teams_dir):
            cfg_path = os.path.join(teams_dir, entry, "config.json")
            if not os.path.isfile(cfg_path):
                continue
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                continue
            if cfg.get("leadSessionId") == session_id:
                return False  # explicitly the lead, allowed to speak
            raw = json.dumps(cfg)
            if session_id in raw:
                return True
    except Exception:
        return False
    return False


def log_payload_debug(payload: dict, hook_name: str) -> None:
    """Append the FULL hook payload to a debug log for diagnosis.

    Dumps every key in the payload verbatim so we can see exactly what
    Claude Code sends — no guessing which field identifies a teammate.
    Truncates any single field value to 2000 chars to keep lines
    readable. Silent on any failure — debug logging must never break
    the hook.
    """
    try:
        log_path = os.path.join(_TEMP, "wednesday-tts-hook-debug.log")
        safe: dict = {}
        for k, v in payload.items():
            try:
                s = json.dumps(v, default=str)
                if len(s) > 2000:
                    s = s[:2000] + "...[truncated]"
                safe[k] = json.loads(s) if not s.endswith("...[truncated]") else s
            except Exception:
                safe[k] = f"<unserialisable {type(v).__name__}>"
        line = (
            json.dumps(
                {
                    "t": time.time(),
                    "hook": hook_name,
                    "payload": safe,
                },
                default=str,
            )
            + "\n"
        )
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def compute_voice_hash(cwd: str) -> str:
    """SHA-256 of the git repo root (or cwd if not in a repo), first 8 hex chars."""
    try:
        repo = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
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


def _log_send_error(source: str, phase: str, exc: Exception | None) -> None:
    """Append a one-line error to the hook debug log. Silent on failure."""
    try:
        log_path = os.path.join(_TEMP, "wednesday-tts-hook-debug.log")
        line = json.dumps({"t": time.time(), "hook": "send_speak", "source": source, "phase": phase, "error": str(exc)}) + "\n"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def send_speak(msg: dict, *, kick_on_timeout: bool = False) -> None:
    """Send a JSON speak message to the TTS daemon over the Unix socket.

    Waits briefly for an acknowledgement. On a connect timeout, optionally
    pings the daemon and kicks it via launchctl if it appears dead. Silent
    on any failure — the hook must never crash the turn.
    """
    payload = (json.dumps(msg) + "\n").encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10 if kick_on_timeout else 1.0)
    source = msg.get("source", "unknown")
    try:
        s.connect(UNIX_SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        _log_send_error(source, "connect", exc)
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
        except TimeoutError:
            pass
    except TimeoutError:
        _log_send_error(source, "send-timeout", None)
        if kick_on_timeout:
            _kick_daemon_if_dying()
    except Exception as exc:
        _log_send_error(source, "send", exc)
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
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.tamm.wednesday-tts"],
            capture_output=True,
        )
