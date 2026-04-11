#!/usr/bin/env python3
"""
Platform abstraction for TTS hooks.

All OS-specific code lives here so speak-response.py and pre-tool-speak.py
can be shared across macOS and Windows without conditional imports.

macOS:  Unix domain socket IPC, fcntl locking, SIGUSR1 stop, afplay chime
Windows: HTTP IPC to localhost:5678, msvcrt locking, HTTP stop, beep chime
"""
import os
import signal
import sys
import tempfile

IS_WINDOWS = sys.platform == 'win32'

# ── Paths ────────────────────────────────────────────────────────────────────

if IS_WINDOWS:
    _TEMP = tempfile.gettempdir()  # e.g. ~\AppData\Local\Temp
    LOCK_PATH = os.path.join(_TEMP, "tts-daemon.lock")
    MUTE_PATH = os.path.join(_TEMP, "tts-mute")
    SUPPRESS_PATH = os.path.join(_TEMP, "dictation-suppress")
    FAILURE_PATH = os.path.join(_TEMP, "tts-daemon-failures")
    PID_PATH = None  # not used on Windows
    SOCKET_PATH = None  # not used on Windows
    SERVICE_URL = "http://localhost:5678"
else:
    LOCK_PATH = "/tmp/tts-daemon.lock"
    MUTE_PATH = "/tmp/tts-mute"
    SUPPRESS_PATH = "/tmp/dictation-suppress"
    FAILURE_PATH = "/tmp/tts-daemon-failures"
    PID_PATH = "/tmp/tts-daemon.pid"
    SOCKET_PATH = "/tmp/tts-daemon.sock"
    SERVICE_URL = None  # not used on macOS


def spoken_hashes_path(session_id: str) -> str:
    """Cross-platform path for the spoken-hashes dedup file."""
    if IS_WINDOWS:
        return os.path.join(_TEMP, f"tts-spoken-{session_id}")
    return f"/tmp/tts-spoken-{session_id}"


# ── IPC ──────────────────────────────────────────────────────────────────────
# The daemon speaks only the JSON wire protocol described in
# docs/voice-pipeline-spec.md. Every helper in this module that talks to the
# daemon sends {"command": "..."}\n frames. There is no legacy raw-bytes or
# colon-delimited SEQ: path left — do not reintroduce one.


def drain_daemon() -> None:
    """Wait for all queued audio to finish playing.

    macOS: sends JSON drain command over the Unix socket (blocks until
    the playback queue empties).
    Windows: no-op — the Flask service's queue worker handles ordering
    internally, so callers do not need a drain barrier.
    """
    if IS_WINDOWS:
        return
    import socket
    import json as _json
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(600)  # generous: covers longest realistic audio
    try:
        sock.connect(SOCKET_PATH)
        sock.sendall((_json.dumps({"command": "drain"}) + "\n").encode("utf-8"))
        sock.recv(16)
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass


def daemon_is_responsive(timeout: float = 2.0) -> bool:
    """Quick ping to check if daemon is alive."""
    if IS_WINDOWS:
        import urllib.request
        try:
            req = urllib.request.Request(f"{SERVICE_URL}/health", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
            return True
        except Exception:
            return False
    else:
        import socket as _socket
        if not os.path.exists(SOCKET_PATH):
            return False
        try:
            import json as _json
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(SOCKET_PATH)
            sock.sendall((_json.dumps({"command": "ping"}) + "\n").encode("utf-8"))
            sock.recv(16)
            sock.close()
            return True
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            return False


# ── Stop ─────────────────────────────────────────────────────────────────────

def stop_daemon_audio() -> None:
    """Stop daemon audio immediately."""
    if IS_WINDOWS:
        import urllib.request
        try:
            req = urllib.request.Request(f"{SERVICE_URL}/stop", data=b"", method="POST")
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass
    else:
        # Try SIGUSR1 first — no socket overhead
        try:
            with open(PID_PATH) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGUSR1)
            return
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            pass
        # Fallback: socket stop command (JSON wire protocol)
        try:
            if os.path.exists(SOCKET_PATH):
                import json as _json
                import socket as _socket
                sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect(SOCKET_PATH)
                sock.sendall((_json.dumps({"command": "stop"}) + "\n").encode("utf-8"))
                sock.recv(16)
                sock.close()
        except Exception:
            pass


# ── Locking ──────────────────────────────────────────────────────────────────

def acquire_lock(timeout: float = 30) -> int | None:
    """Acquire an exclusive file lock. Returns lock_fd on success, None on timeout."""
    import time
    start = time.time()
    while time.time() - start < timeout:
        lock_fd = -1
        try:
            lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR)
            if IS_WINDOWS:
                import msvcrt
                msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_fd
        except (OSError, IOError):
            try:
                if lock_fd >= 0:
                    os.close(lock_fd)
            except Exception:
                pass
            time.sleep(0.5)
    return None


def release_lock(lock_fd: int | None) -> None:
    """Release a previously acquired file lock."""
    if lock_fd is None:
        return
    try:
        if IS_WINDOWS:
            import msvcrt
            try:
                os.lseek(lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        else:
            import fcntl
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    except Exception:
        pass


def flock_exclusive(f) -> None:
    """Acquire exclusive lock on an open file object (for claim_unspoken)."""
    if IS_WINDOWS:
        import msvcrt
        # Lock the first byte of the file
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX)


def flock_unlock(f) -> None:
    """Release lock on an open file object."""
    if IS_WINDOWS:
        import msvcrt
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
    else:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_UN)


# ── Chime ────────────────────────────────────────────────────────────────────

CHIME_SOUND_MAC = "/System/Library/Sounds/Glass.aiff"


def play_chime() -> None:
    """Play a brief notification chime."""
    if IS_WINDOWS:
        _play_chime_windows()
    else:
        _play_chime_mac()


def _play_chime_mac() -> None:
    import subprocess
    try:
        subprocess.Popen(
            ["afplay", CHIME_SOUND_MAC],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _play_chime_windows() -> None:
    """Generate a brief two-tone beep. The Star Trek chimes are played by the
    service itself when queuing — this is just for hook-level fallback."""
    try:
        import numpy as np
        import sounddevice as sd
        duration = 0.15
        sr = 24000
        t = np.linspace(0, duration, int(sr * duration))
        tone1 = 0.3 * np.sin(2 * np.pi * 659 * t[: len(t) // 2])
        tone2 = 0.3 * np.sin(2 * np.pi * 880 * t[len(t) // 2 :])
        chime = np.concatenate([tone1, tone2])
        fade = np.linspace(1.0, 0.0, int(sr * 0.05))
        chime[-len(fade) :] *= fade
        sd.play(chime, samplerate=sr, blocking=False)
    except Exception:
        pass


# ── Signals ──────────────────────────────────────────────────────────────────

def register_signals(handler) -> None:
    """Register SIGTERM + SIGINT everywhere, SIGHUP on macOS only."""
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    if not IS_WINDOWS and hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, handler)


# ── Dictation suppression ────────────────────────────────────────────────────

def suppress_dictation() -> None:
    """Signal the dictation/ASR service to pause mic processing."""
    try:
        open(SUPPRESS_PATH, "w").close()
    except Exception:
        pass


def unsuppress_dictation() -> None:
    """Signal the dictation/ASR service to resume mic processing."""
    try:
        os.unlink(SUPPRESS_PATH)
    except FileNotFoundError:
        pass
    except Exception:
        pass


# ── Daemon restart ────────────────────────────────────────────────────────────
# macOS: launchd restarts automatically; we just kill zombies after 300s of failure.
# Windows: try nssm start first, fall back to launching the process directly.

# macOS: restart after this many seconds of continuous failure
_MAC_FAILURE_RESTART_THRESHOLD = 300

# Windows: Task Scheduler task name
_WIN_SERVICE_NAME = "WednesdayTTS"


def record_failure() -> None:
    """Append current timestamp to the failure log."""
    import time
    try:
        with open(FAILURE_PATH, "a") as f:
            f.write(f"{time.time()}\n")
    except Exception:
        pass


def clear_failures() -> None:
    """Clear failure log on successful communication."""
    try:
        os.unlink(FAILURE_PATH)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def should_restart_daemon() -> bool:
    """True if the daemon should be restarted.

    Windows: yes on first failure (no launchd safety net).
    macOS: yes after 300s of continuous failure (launchd handles short outages).
    """
    try:
        if not os.path.exists(FAILURE_PATH):
            return False
        if IS_WINDOWS:
            return True  # restart immediately on Windows
        import time
        with open(FAILURE_PATH) as f:
            lines = f.readlines()
        if not lines:
            return False
        first_failure = float(lines[0].strip())
        return (time.time() - first_failure) >= _MAC_FAILURE_RESTART_THRESHOLD
    except Exception:
        return False


def restart_daemon() -> None:
    """Attempt to restart the TTS daemon.

    Windows: try 'schtasks /run WednesdayTTS', then fall back to launching
             the Python process directly in a detached subprocess.
    macOS:   kill the zombie process and let launchd restart it.
    """
    if IS_WINDOWS:
        _restart_daemon_windows()
    else:
        _restart_daemon_mac()


def _restart_daemon_windows() -> None:
    import subprocess
    print("TTS service not responding — attempting restart via Task Scheduler", file=sys.stderr)

    try:
        # CREATE_NO_WINDOW prevents a console window flash on Windows
        result = subprocess.run(
            ["schtasks", "/run", "/tn", _WIN_SERVICE_NAME],
            capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            print("TTS service started via Task Scheduler", file=sys.stderr)
            clear_failures()
        else:
            print(
                f"schtasks /run failed (rc={result.returncode}) — "
                "run services/install-services.ps1 as Administrator to register the task",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"TTS service restart failed: {e}", file=sys.stderr)


def _restart_daemon_mac() -> None:
    print("TTS daemon zombie detected — killing for launchd restart", file=sys.stderr)
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGKILL)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass
    for path in (SOCKET_PATH, PID_PATH, LOCK_PATH, SUPPRESS_PATH):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except Exception:
            pass
    clear_failures()
