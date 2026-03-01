#!/usr/bin/env python3
"""Wednesday TTS — macOS Unix socket daemon.

Keeps the model loaded in memory for fast responses.
Supports overlapping chunk processing: renders chunk N+1 while N plays.

Backend selection (env var, default pocket):
    TTS_BACKEND=kokoro       Kokoro 82M (configurable via KOKORO_VOICE)
    TTS_BACKEND=pocket       Pocket TTS (configurable via POCKET_TTS_VOICE)
    TTS_BACKEND=chatterbox   Chatterbox TTS (configurable via CHATTERBOX_DEVICE)

Run:
    python -m wednesday_tts.server.daemon
    TTS_BACKEND=kokoro python -m wednesday_tts.server.daemon
"""
from __future__ import annotations

import json
import os
import queue
import signal
import socket
import struct
import threading
import time

import numpy as np
import sounddevice as sd  # type: ignore[import]

from .backends import REGISTRY, TTSBackend

SOCKET_PATH = "/tmp/tts-daemon.sock"
PID_PATH = "/tmp/tts-daemon.pid"
DEFAULT_SPEED = float(os.environ.get("TTS_SPEED", "1.25"))

# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

_stats_lock = threading.Lock()
_stats: dict = {
    "requests_total": 0,
    "requests_completed": 0,
    "requests_stopped": 0,
    "requests_errored": 0,
    "audio_seconds_total": 0.0,
    "soundstretch_calls": 0,
    "soundstretch_ms_sum": 0.0,
    "service_start_time": None,
}


def _stat_inc(key: str, n: float = 1) -> None:
    with _stats_lock:
        _stats[key] += n


# ---------------------------------------------------------------------------
# Normalization wiring
# ---------------------------------------------------------------------------

def _load_normalize_deps() -> tuple[list, dict]:
    """Load pronunciation dictionaries, searching package data dir first."""
    dictionary: list = []
    filenames_dict: dict = {}

    candidates = [
        os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "data")
        ),
        os.path.join(os.path.expanduser("~"), ".claude", "hooks"),
    ]

    for base in candidates:
        dict_path = os.path.join(base, "tts-dictionary.json")
        filenames_path = os.path.join(base, "tts-filenames.json")
        if os.path.exists(dict_path):
            try:
                with open(dict_path, encoding="utf-8") as f:
                    dictionary = json.load(f).get("replacements", [])
            except Exception:
                pass
        if os.path.exists(filenames_path):
            try:
                with open(filenames_path, encoding="utf-8") as f:
                    filenames_dict = json.load(f)
            except Exception:
                pass
        if dictionary or filenames_dict:
            break

    return dictionary, filenames_dict


_normalize_deps: tuple[list, dict] | None = None
_normalize_lock = threading.Lock()


def _get_normalize_deps() -> tuple[list, dict]:
    global _normalize_deps
    if _normalize_deps is None:
        with _normalize_lock:
            if _normalize_deps is None:
                _normalize_deps = _load_normalize_deps()
    return _normalize_deps


def run_normalize(text: str, content_type: str = "markdown") -> str:
    from wednesday_tts.normalize.pipeline import normalize  # lazy import
    dictionary, filenames_dict = _get_normalize_deps()
    return normalize(text, content_type=content_type, dictionary=dictionary, filenames_dict=filenames_dict)


# ---------------------------------------------------------------------------
# Ordered chunk delivery
# ---------------------------------------------------------------------------
# Ensures chunks enqueued for playback arrive in sequence-number order even
# when parallel renders complete out of order.

_order_lock = threading.Lock()
_order_cond = threading.Condition(_order_lock)
_next_seq = 0       # sequence number the playback queue expects next
_stop_gen = 0       # incremented on STOP; in-flight chunks compare to bail out

playback_queue: queue.Queue = queue.Queue()
_active_backend: TTSBackend | None = None


def _stop_playback() -> None:
    """Stop current audio and drain the queue. Safe to call from any thread."""
    global _next_seq, _stop_gen
    sd.stop()
    if _active_backend is not None and hasattr(_active_backend, "abort_stream"):
        _active_backend.abort_stream()  # type: ignore[union-attr]
    while True:
        try:
            playback_queue.get_nowait()
            playback_queue.task_done()
        except queue.Empty:
            break
    with _order_cond:
        _stop_gen += 1
        _next_seq = 0
        _order_cond.notify_all()


def _sigusr1_handler(sig: int, frame) -> None:
    """SIGUSR1 = stop talking immediately. Sent by stop-tts.sh."""
    _stop_playback()


# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------

def get_default_output_device() -> int | None:
    """Query the current default output device, forcing a PortAudio rescan.

    PortAudio caches the device list at Pa_Initialize() time so it returns
    stale results after Bluetooth device switches. Force a rescan by cycling
    terminate/initialize before querying.
    """
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass
    try:
        return sd.query_devices(kind="output")["index"]
    except Exception:
        return None


def _get_device_samplerate(model_rate: int) -> int:
    """Return the native samplerate of the default output device.

    Falls back to model_rate if the query fails (e.g. no audio hardware).
    Used to detect 24kHz model output vs 48kHz device native rate mismatches.
    """
    try:
        info = sd.query_devices(kind="output")
        return int(info["default_samplerate"])
    except Exception:
        return model_rate


def _upsample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Resample audio from from_rate to to_rate.

    Uses scipy.signal.resample_poly when available (handles arbitrary ratios).
    Falls back to numpy repeat for integer ratios (e.g. 24k -> 48k = repeat x2).
    """
    if from_rate == to_rate:
        return audio
    try:
        from scipy.signal import resample_poly  # type: ignore[import]
        import math
        g = math.gcd(to_rate, from_rate)
        return resample_poly(audio, to_rate // g, from_rate // g).astype(np.float32)
    except ImportError:
        ratio = to_rate // from_rate
        return np.repeat(audio, ratio).astype(np.float32)


def _try_play(item: np.ndarray, sample_rate: int) -> bool:
    """Attempt sd.play() with current default device. Returns True on success."""
    device = get_default_output_device()
    sd.play(item, samplerate=sample_rate, device=device)
    duration_s = len(item) / sample_rate
    deadline = duration_s + 5.0
    start = time.monotonic()
    while sd.get_stream().active:
        if time.monotonic() - start > deadline:
            print(f"Playback watchdog: exceeded {deadline:.1f}s, stopping", flush=True)
            sd.stop()
            return False
        time.sleep(0.05)
    return True


# ---------------------------------------------------------------------------
# Audio health watchdog
# ---------------------------------------------------------------------------

def _audio_health_worker() -> None:
    """Background thread: periodically probe PortAudio.

    If the audio subsystem wedges (kAudioHardwareNotRunningError / PortAudio
    error -50), exit so launchd/Task Scheduler can restart the daemon cleanly.
    """
    GRACE = 120       # seconds before first check — let daemon stabilise
    INTERVAL = 30     # seconds between checks
    MAX_FAILS = 3     # consecutive failures before exit

    time.sleep(GRACE)
    fails = 0
    while True:
        try:
            device = get_default_output_device()
            stream = sd.OutputStream(
                samplerate=_get_device_samplerate(24000),
                device=device,
                channels=1,
                dtype="float32",
            )
            stream.start()
            stream.stop()
            stream.close()
            fails = 0  # success — reset counter
        except Exception as exc:
            fails += 1
            print(f"[HEALTH] audio probe failed ({fails}/{MAX_FAILS}): {exc}", flush=True)
            if fails >= MAX_FAILS:
                print("[HEALTH] audio subsystem wedged — exiting for restart", flush=True)
                os._exit(1)
        time.sleep(INTERVAL)


# ---------------------------------------------------------------------------
# Playback worker
# ---------------------------------------------------------------------------

def playback_worker(backend: TTSBackend) -> None:
    """Dedicated thread: plays audio arrays from the queue in FIFO order."""
    while True:
        item = playback_queue.get()
        if item is None:
            break
        try:
            if not _try_play(item, backend.sample_rate):
                time.sleep(0.5)
                print("Retrying playback after watchdog...", flush=True)
                try:
                    _try_play(item, backend.sample_rate)
                except Exception:
                    pass
        except Exception as exc:
            print(f"Playback error: {exc}", flush=True)
            try:
                sd.stop()
            except Exception:
                pass
            time.sleep(0.5)
            try:
                _try_play(item, backend.sample_rate)
            except Exception:
                pass
        finally:
            playback_queue.task_done()


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------

def handle_client(conn: socket.socket, backend: TTSBackend) -> None:
    """Handle one client connection.

    Protocols (wire format, newline-terminated or fixed recv):
        SEQ:N:speed:text    render text with sequence N, play in order
        SPEED:speed:text    legacy unsequenced render (backward compat)
        PCM:speed:text      render and return raw PCM (4-byte LE sample_rate + float32)
        NORMALIZE:ct:text   normalize text and return it as UTF-8 (no audio)
        DRAIN               wait for all audio, reset seq counter
        STOP                stop current audio, drain queue
        PING                health check
        STATS               return telemetry JSON
    """
    global _next_seq

    try:
        message = conn.recv(65536).decode("utf-8").strip()
        if not message:
            conn.send(b"ok")
            return

        # ── PING ──────────────────────────────────────────────────────────
        if message == "PING":
            conn.send(b"ok")
            return

        # ── STOP ──────────────────────────────────────────────────────────
        if message == "STOP":
            _stop_playback()
            conn.send(b"ok")
            return

        # ── STATS ─────────────────────────────────────────────────────────
        if message == "STATS":
            with _stats_lock:
                s = dict(_stats)
            uptime = time.time() - s["service_start_time"] if s["service_start_time"] else 0
            result = {
                "uptime_s": round(uptime),
                "requests": {
                    "total": s["requests_total"],
                    "completed": s["requests_completed"],
                    "stopped": s["requests_stopped"],
                    "errored": s["requests_errored"],
                },
                "audio_seconds_total": round(s["audio_seconds_total"], 1),
                "soundstretch": {
                    "calls": s["soundstretch_calls"],
                    "avg_ms": round(s["soundstretch_ms_sum"] / s["soundstretch_calls"], 1)
                    if s["soundstretch_calls"]
                    else 0,
                },
                "backend": os.environ.get("TTS_BACKEND", "pocket"),
            }
            conn.sendall(json.dumps(result).encode("utf-8"))
            return

        # ── DRAIN ─────────────────────────────────────────────────────────
        if message == "DRAIN":
            playback_queue.join()
            with _order_cond:
                _next_seq = 0
                _order_cond.notify_all()
            conn.send(b"ok")
            return

        # ── NORMALIZE — return normalized text, no audio ───────────────
        if message.startswith("NORMALIZE:"):
            # NORMALIZE:content_type:text
            parts = message.split(":", 2)
            if len(parts) == 3:
                ct = parts[1]
                norm_text = parts[2]
            else:
                ct = "markdown"
                norm_text = message[len("NORMALIZE:"):]
            result_text = run_normalize(norm_text, content_type=ct)
            conn.sendall(result_text.encode("utf-8"))
            return

        # ── PCM — render and return raw bytes, no playback ─────────────
        if message.startswith("PCM:"):
            parts = message.split(":", 2)
            pcm_speed = DEFAULT_SPEED
            pcm_text = message[4:]
            if len(parts) == 3:
                try:
                    pcm_speed = float(parts[1])
                    pcm_text = parts[2]
                except ValueError:
                    pass
            audio = backend.generate(pcm_text, speed=pcm_speed)
            if audio is not None:
                sr_bytes = struct.pack("<I", backend.sample_rate)
                pcm_bytes = audio.astype(np.float32).tobytes()
                conn.sendall(sr_bytes + pcm_bytes)
            else:
                conn.send(b"")
            return

        _stat_inc("requests_total")

        # ── Parse message ─────────────────────────────────────────────────
        seq: int | None = None
        speed = DEFAULT_SPEED
        text = message
        content_type = "normalized"  # backward compat

        if message.startswith("SEQ:"):
            # SEQ:N:speed:text  (speed=N means use default)
            parts = message.split(":", 3)
            if len(parts) >= 4:
                try:
                    seq = int(parts[1])
                    speed = DEFAULT_SPEED if parts[2] == "N" else float(parts[2])
                    text = parts[3]
                except ValueError:
                    pass
            # Optional content_type prefix embedded in text: __ct:markdown__
            import re
            _ct = re.match(r"^__ct:(\w+)__", text)
            if _ct:
                content_type = _ct.group(1)
                text = text[_ct.end():]

        elif message.startswith("SPEED:"):
            parts = message.split(":", 2)
            if len(parts) == 3:
                try:
                    speed = float(parts[1])
                    text = parts[2]
                except ValueError:
                    pass

        # ── Normalize if requested ────────────────────────────────────────
        if content_type != "normalized":
            text = run_normalize(text, content_type=content_type)

        # ── Render ────────────────────────────────────────────────────────
        gen_snap = _stop_gen

        # Streaming path: SEQ:0 with a streaming-capable backend plays audio
        # directly through an OutputStream for lowest time-to-first-sound.
        use_streaming = (
            seq == 0
            and getattr(backend, "supports_streaming", False)
            and _stop_gen == gen_snap
        )

        if use_streaming:
            backend.play_streaming(text, speed=speed)  # type: ignore[union-attr]
            with _order_cond:
                if _stop_gen == gen_snap:
                    _next_seq = 1
                    _order_cond.notify_all()
            conn.send(b"ok")
            return

        # Batch render
        audio = backend.generate(text, speed=speed)

        # ── Enqueue in order ──────────────────────────────────────────────
        if seq is not None:
            with _order_cond:
                if _stop_gen != gen_snap:
                    conn.send(b"ok")
                    return
                deadline = time.monotonic() + 5
                while _next_seq != seq:
                    if _stop_gen != gen_snap:
                        conn.send(b"ok")
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        print(f"SEQ timeout: expected {_next_seq}, got {seq}. Resetting.", flush=True)
                        _next_seq = seq
                        break
                    _order_cond.wait(timeout=min(remaining, 10))
                if _stop_gen != gen_snap:
                    conn.send(b"ok")
                    return
                if audio is not None:
                    playback_queue.put(audio)
                _next_seq += 1
                _order_cond.notify_all()
        elif audio is not None:
            playback_queue.put(audio)

        if audio is not None:
            _stat_inc("audio_seconds_total", len(audio) / backend.sample_rate)
        _stat_inc("requests_completed")
        conn.send(b"ok")

    except Exception as exc:
        _stat_inc("requests_errored")
        print(f"Error handling client: {exc}", flush=True)
        try:
            conn.send(b"error")
        except Exception:
            pass
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _stats["service_start_time"] = time.time()

    backend_name = os.environ.get("TTS_BACKEND", "pocket").lower()
    backend_cls = REGISTRY.get(backend_name)
    if backend_cls is None:
        print(f"Unknown TTS_BACKEND={backend_name!r}. Choose from: {', '.join(REGISTRY)}", flush=True)
        raise SystemExit(1)

    global _active_backend
    backend = backend_cls()
    _active_backend = backend
    print(f"Loading {backend_name} model...", flush=True)
    try:
        backend.load()
    except Exception as exc:
        print(f"FATAL: failed to load {backend_name}: {exc}", flush=True)
        raise SystemExit(2)
    print(f"Ready! [{backend_name}] Listening on {SOCKET_PATH}", flush=True)

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

    signal.signal(signal.SIGUSR1, _sigusr1_handler)

    pb_thread = threading.Thread(target=playback_worker, args=(backend,), daemon=True)
    pb_thread.start()

    health_thread = threading.Thread(target=_audio_health_worker, daemon=True)
    health_thread.start()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(16)

    try:
        while True:
            try:
                conn, _ = server.accept()
            except OSError as exc:
                print(f"accept() error: {exc}, retrying in 1s", flush=True)
                time.sleep(1)
                continue
            t = threading.Thread(target=handle_client, args=(conn, backend), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    finally:
        playback_queue.put(None)
        pb_thread.join(timeout=5)
        server.close()
        for path in (SOCKET_PATH, PID_PATH):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
