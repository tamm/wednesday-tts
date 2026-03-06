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

import collections
import hashlib
import json
import os
import queue
import re
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
# Per-request voice override cache
# ---------------------------------------------------------------------------
_voice_cache: dict[str, TTSBackend] = {}
_voice_cache_lock = threading.Lock()


def _get_override_backend(name: str) -> TTSBackend | None:
    """Lazy-init and cache a secondary backend for per-request voice overrides."""
    with _voice_cache_lock:
        if name in _voice_cache:
            return _voice_cache[name]
    # Load outside lock (may be slow for neural backends, instant for SAM)
    cls = REGISTRY.get(name)
    if cls is None:
        print(f"[voice-override] Unknown backend: {name!r}", flush=True)
        return None
    try:
        # Read config for this backend if available
        cfg_path = os.path.expanduser("~/.claude/tts-config.json")
        model_cfg: dict = {}
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                model_cfg = json.load(f).get("models", {}).get(name, {})
        # Filter out comment keys
        kwargs = {k: v for k, v in model_cfg.items() if not k.startswith("_")}
        backend = cls(**kwargs)
        print(f"[voice-override] Loading {name}...", flush=True)
        backend.load()
        print(f"[voice-override] {name} ready.", flush=True)
    except Exception as exc:
        print(f"[voice-override] Failed to load {name}: {exc}", flush=True)
        return None
    with _voice_cache_lock:
        _voice_cache[name] = backend
    return backend


def _split_voice_segments(
    text: str, pattern: "re.Pattern[str]"
) -> list[tuple[str | None, str]]:
    """Split text into segments of (voice_name, text).

    Plain text segments have voice_name=None (use primary backend).
    Tagged segments like {voice:sam}words{/voice} have voice_name="sam".

    Returns a list of (voice, text) tuples preserving original order.
    Empty segments are skipped.
    """
    segments: list[tuple[str | None, str]] = []
    last_end = 0
    for m in pattern.finditer(text):
        # Plain text before this tag
        before = text[last_end:m.start()].strip()
        if before:
            segments.append((None, before))
        # Tagged segment
        voice_name = m.group(1)
        tagged_text = m.group(2).strip()
        if tagged_text:
            segments.append((voice_name, tagged_text))
        last_end = m.end()
    # Trailing plain text after last tag
    after = text[last_end:].strip()
    if after:
        segments.append((None, after))
    # If no tags found at all, return the whole text as one segment
    if not segments and text.strip():
        segments.append((None, text.strip()))
    return segments


def _render_segments(
    segments: list[tuple[str | None, str]],
    primary_backend: TTSBackend,
    speed: float,
    gen_snap: int,
) -> "np.ndarray | None":
    """Render a list of voice segments and concatenate into one audio array.

    Each segment is rendered with its specified backend (or the primary if None).
    All audio is resampled to the primary backend's sample rate before concatenation.
    """
    chunks: list[np.ndarray] = []
    target_rate = primary_backend.sample_rate

    for voice_name, segment_text in segments:
        if _stop_gen != gen_snap:
            break

        if voice_name and voice_name != _active_backend_name:
            render_backend = _get_override_backend(voice_name)
            if render_backend is None:
                render_backend = primary_backend
        else:
            render_backend = primary_backend

        audio = render_backend.generate(segment_text, speed=speed)
        if audio is not None:
            if render_backend.sample_rate != target_rate:
                audio = _upsample(audio, render_backend.sample_rate, target_rate)
            chunks.append(audio)

    if not chunks:
        return None
    return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]


# ---------------------------------------------------------------------------
# Dedup ring buffer — skip recently-spoken text
# ---------------------------------------------------------------------------

_DEDUP_SIZE = 20
_dedup_ring: collections.deque[tuple[str, float]] = collections.deque(maxlen=_DEDUP_SIZE)
_dedup_lock = threading.Lock()


def _dedup_check(text: str) -> bool:
    """Return True if text was recently spoken (duplicate). Adds it if not."""
    h = hashlib.md5(text.encode()).hexdigest()
    with _dedup_lock:
        for stored_hash, _ in _dedup_ring:
            if stored_hash == h:
                return True
        _dedup_ring.append((h, time.monotonic()))
        return False


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

# Timestamp of the last time in-flight count changed (request started or finished).
# Used by the hung-request watchdog to detect generate() hangs.
_last_activity_time: float = 0.0


def _stat_inc(key: str, n: float = 1) -> None:
    global _last_activity_time
    with _stats_lock:
        _stats[key] += n
        _last_activity_time = time.monotonic()


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
_active_backend_name: str = ""


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
    """Attempt sd.play() with current default device. Returns True on success.

    get_default_output_device() forces a PortAudio terminate/initialize cycle
    before querying, which recovers from err=-50 (hardware not running).
    """
    device = get_default_output_device()
    try:
        sd.play(item, samplerate=sample_rate, device=device)
    except Exception as exc:
        print(f"sd.play() failed: {exc}", flush=True)
        return False
    duration_s = len(item) / sample_rate
    deadline = duration_s + 5.0
    start = time.monotonic()
    while True:
        try:
            active = sd.get_stream().active
        except Exception:
            # PortAudio in bad state — treat as finished
            return False
        if not active:
            break
        if time.monotonic() - start > deadline:
            print(f"Playback watchdog: exceeded {deadline:.1f}s, stopping", flush=True)
            sd.stop()
            return False
        time.sleep(0.05)
    return True


# ---------------------------------------------------------------------------
# Hung-request watchdog
# ---------------------------------------------------------------------------

def _hung_request_watchdog() -> None:
    """Background thread: exit if a request has been in-flight too long.

    generate() has no internal timeout — if the TTS model or audio device
    wedges mid-inference the handler thread hangs forever. This watchdog
    detects when total > completed+stopped+errored for longer than the
    threshold and forces a clean exit so launchd restarts the daemon.
    """
    HUNG_THRESHOLD = 120  # seconds before declaring a request hung
    POLL = 10             # check every N seconds

    time.sleep(30)  # grace period — let first request finish loading model
    while True:
        time.sleep(POLL)
        with _stats_lock:
            in_flight = (
                _stats["requests_total"]
                - _stats["requests_completed"]
                - _stats["requests_stopped"]
                - _stats["requests_errored"]
            )
            last = _last_activity_time

        if in_flight > 0 and last > 0:
            age = time.monotonic() - last
            if age > HUNG_THRESHOLD:
                # Don't kill the process while audio is queued or playing
                if not playback_queue.empty():
                    continue
                print(
                    f"[WATCHDOG] {in_flight} request(s) in-flight for {age:.0f}s — "
                    "generate() appears hung, exiting for restart",
                    flush=True,
                )
                os._exit(1)


# ---------------------------------------------------------------------------
# Audio health watchdog
# ---------------------------------------------------------------------------

def _audio_health_worker() -> None:
    """Background thread: periodically probe PortAudio.

    If MAX_FAILS consecutive idle probes fail, the audio subsystem is
    wedged — exit for launchd restart.

    Skips the probe while audio is actively playing or queued — opening a
    second OutputStream while one is live causes PortAudio to return -50 on
    macOS, which would be a false positive.
    """
    GRACE = 60        # seconds before first check — let daemon stabilise
    INTERVAL = 30     # seconds between checks
    MAX_FAILS = 3     # consecutive *idle* failures before exit

    time.sleep(GRACE)
    probe_fails = 0
    while True:
        time.sleep(INTERVAL)

        # Skip probe while audio is queued or playing — never kill mid-speech.
        if not playback_queue.empty():
            continue

        # ── Probe PortAudio ──────────────────────────────────────────────
        try:
            device = get_default_output_device()
            sd.query_devices(kind="output")
            stream = sd.OutputStream(
                samplerate=_get_device_samplerate(24000),
                device=device,
                channels=1,
                dtype="float32",
            )
            try:
                stream.start()
                stream.stop()
            finally:
                stream.close()
            probe_fails = 0
        except Exception as exc:
            probe_fails += 1
            print(f"[HEALTH] audio probe failed ({probe_fails}/{MAX_FAILS}): {exc}", flush=True)
            if probe_fails >= MAX_FAILS:
                if not playback_queue.empty():
                    print("[HEALTH] audio probe failing but playback queue not empty — deferring", flush=True)
                    continue
                print(
                    "[HEALTH] audio subsystem wedged — exiting for restart",
                    flush=True,
                )
                os._exit(1)


# ---------------------------------------------------------------------------
# Playback worker
# ---------------------------------------------------------------------------

def playback_worker(backend: TTSBackend) -> None:
    """Dedicated thread: plays audio arrays from the queue in FIFO order.

    This is the ONLY code that plays audio. All paths (streaming and batch)
    enqueue np.ndarray chunks here; nothing else touches the audio device.
    """
    while True:
        item = playback_queue.get()
        if item is None:
            break
        try:
            if not _try_play(item, backend.sample_rate):
                time.sleep(1.0)
                print("Retrying playback after failure...", flush=True)
                _try_play(item, backend.sample_rate)
        except Exception as exc:
            print(f"Playback error: {exc}", flush=True)
        finally:
            playback_queue.task_done()


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------

def handle_client(conn: socket.socket, backend: TTSBackend) -> None:
    """Handle one client connection.

    Protocols (wire format, newline-terminated or fixed recv):
        SEQ:N:speed:ct:ts:text  render text with sequence N, play in order
                                ct=content_type (markdown|normalized), ts=epoch float or empty
        SPEED:speed:text        legacy unsequenced render (backward compat, deprecated)
        PCM:speed:text          render and return raw PCM (4-byte LE sample_rate + float32)
        NORMALIZE:ct:text       normalize text and return it as UTF-8 (no audio)
        DRAIN                   wait for all audio, reset seq counter
        STOP                    stop current audio, drain queue
        PING                    health check
        STATS                   return telemetry JSON
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
            # join() has no built-in timeout — poll with a deadline instead
            deadline = time.monotonic() + 30
            while not playback_queue.empty():
                if time.monotonic() > deadline:
                    print("DRAIN timeout after 30s", flush=True)
                    break
                time.sleep(0.05)
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
            # New format: SEQ:N:speed:content_type:timestamp:text
            # Old format: SEQ:N:speed:text (with __ct:/__t: prefixes in text)
            parts = message.split(":", 5)
            if len(parts) >= 6:
                # New format — proper colon-delimited fields
                try:
                    seq = int(parts[1])
                    speed = DEFAULT_SPEED if parts[2] == "N" else float(parts[2])
                    content_type = parts[3] if parts[3] else "markdown"
                    # parts[4] is timestamp — logged but not used for logic
                    text = parts[5]
                except ValueError:
                    pass
            elif len(parts) >= 4:
                # Old format — backward compat
                try:
                    seq = int(parts[1])
                    speed = DEFAULT_SPEED if parts[2] == "N" else float(parts[2])
                    text = parts[3]
                except ValueError:
                    pass
                # Legacy __ct: prefix embedded in text
                _ct = re.match(r"^__ct:([a-zA-Z]+)__", text)
                if _ct:
                    content_type = _ct.group(1)
                    text = text[_ct.end():]
                # Legacy __t: timestamp prefix
                text = re.sub(r"^__t:[\d.]+__", "", text)

        elif message.startswith("SPEED:"):
            # DEPRECATED: use SEQ:0 instead. Will be removed in a future version.
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

        # ── Dedup: skip if this text was recently spoken ─────────────────
        if _dedup_check(text):
            print(f"[req] dedup skip, seq={seq}, {len(text)} chars", flush=True)
            _stat_inc("requests_completed")
            conn.send(b"ok")
            return

        # ── Parse voice segments: split on {voice:X}...{/voice} tags ─────
        _VOICE_TAG_RE = re.compile(r"\{voice:(\w+)\}(.*?)\{/voice\}", re.DOTALL)
        segments = _split_voice_segments(text, _VOICE_TAG_RE)
        has_mixed_voices = any(v is not None for v, _ in segments)

        # ── Render ────────────────────────────────────────────────────────
        gen_snap = _stop_gen

        # Render: use streaming inference if single-voice, else batch
        use_streaming = (
            not has_mixed_voices
            and hasattr(backend, "generate_streaming")
            and _stop_gen == gen_snap
        )
        if use_streaming:
            print(f"[req] STREAM-RENDER seq={seq}, {len(text)} chars, speed={speed}", flush=True)
            audio = backend.generate_streaming(text, speed=speed)
        else:
            print(f"[req] BATCH seq={seq}, {len(text)} chars, speed={speed}", flush=True)
            audio = _render_segments(segments, backend, speed, gen_snap)

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
                _next_seq = 0
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

    # Load config file — same path and shape as app.py uses on Windows.
    # Env vars override config file values.
    _config_path = os.path.join(os.path.expanduser("~"), ".claude", "tts-config.json")
    _model_config: dict = {}
    try:
        import json as _json
        with open(_config_path, encoding="utf-8") as _f:
            _cfg = _json.load(_f)
        _active_model = os.environ.get("TTS_BACKEND") or _cfg.get("active_model", backend_name)
        _model_config = _cfg.get("models", {}).get(_active_model, {})
        print(f"Loaded config from {_config_path} (model: {_active_model})", flush=True)
    except FileNotFoundError:
        print(f"No config file at {_config_path} — using env vars only", flush=True)
    except Exception as exc:
        print(f"Warning: could not load config {_config_path}: {exc}", flush=True)

    # Build kwargs from config, then override with env vars
    _kwargs: dict = {}
    if backend_name == "pocket":
        if _model_config.get("voice"):
            _kwargs["voice"] = _model_config["voice"]
        if _model_config.get("fallback_voice"):
            _kwargs["fallback_voice"] = _model_config["fallback_voice"]
        if _model_config.get("speed") is not None:
            _kwargs["speed"] = _model_config["speed"]
        if _model_config.get("lsd_decode_steps") is not None:
            _kwargs["lsd_decode_steps"] = _model_config["lsd_decode_steps"]
        if _model_config.get("noise_clamp") is not None:
            _kwargs["noise_clamp"] = _model_config["noise_clamp"]
        if _model_config.get("eos_threshold") is not None:
            _kwargs["eos_threshold"] = _model_config["eos_threshold"]
        if _model_config.get("frames_after_eos") is not None:
            _kwargs["frames_after_eos"] = _model_config["frames_after_eos"]
        # Env var overrides config
        _env_voice = os.environ.get("POCKET_TTS_VOICE")
        if _env_voice:
            _kwargs["voice"] = _env_voice

    global _active_backend, _active_backend_name
    backend = backend_cls(**_kwargs)
    _active_backend = backend
    _active_backend_name = backend_name
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

    watchdog_thread = threading.Thread(target=_hung_request_watchdog, daemon=True)
    watchdog_thread.start()

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
            conn.settimeout(30)
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
