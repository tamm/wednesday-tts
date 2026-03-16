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
import subprocess
import threading
import time

import numpy as np
import sounddevice as sd  # type: ignore[import]

from .backends import REGISTRY, TTSBackend

SOCKET_PATH = "/tmp/tts-daemon.sock"
PID_PATH = "/tmp/tts-daemon.pid"
DEFAULT_SPEED = float(os.environ.get("TTS_SPEED", "1.15"))

def _check_competing_instances() -> list[str]:
    """Log warnings if other processes or launchd services could conflict.

    Checks for:
    1. Other processes already running this daemon module.
    2. Multiple launchd services that reference the same socket path or module.

    Logs warnings only — does not kill or modify anything.
    Returns the list of warnings (empty if clean).
    """
    my_pid = os.getpid()
    warnings: list[str] = []

    # --- 1. Duplicate daemon processes ---
    try:
        result = subprocess.run(
            ["pgrep", "-f", "wednesday_tts.server.daemon"],
            capture_output=True, text=True, timeout=5,
        )
        other_pids = [
            int(p) for p in result.stdout.strip().splitlines()
            if p.strip() and int(p) != my_pid
        ]
        if other_pids:
            warnings.append(
                f"Other daemon processes already running: {other_pids}. "
                "Multiple instances will fight over the socket and corrupt PortAudio."
            )
    except Exception as exc:
        warnings.append(f"Could not check for duplicate processes: {exc}")

    # --- 2. Competing launchd services ---
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
        matching = []
        for line in result.stdout.splitlines():
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            label = cols[2].strip()
            # Skip Apple's own TTS services
            if label.startswith("com.apple."):
                continue
            if "tts" in label.lower():
                matching.append(label)
        if len(matching) > 1:
            warnings.append(
                f"Multiple TTS-related launchd services loaded: {matching}. "
                "Stale plists in ~/Library/LaunchAgents/ may be spawning duplicates."
            )
    except Exception as exc:
        warnings.append(f"Could not check launchd services: {exc}")

    for w in warnings:
        print(f"[startup] WARNING: {w}", flush=True)
    return warnings

# Error chime — played when a request times out or errors.
# Set "error_chime" in ~/.claude/tts-config.json to a sound file path.
# Falls back to macOS system alert sound.
_SYSTEM_CHIME = "/System/Library/Sounds/Sosumi.aiff"


def _get_error_chime_path() -> str | None:
    """Resolve error chime path from config, falling back to system sound."""
    cfg_path = os.path.expanduser("~/.claude/tts-config.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                chime = json.load(f).get("error_chime")
            if chime:
                expanded = os.path.expanduser(chime)
                if os.path.isfile(expanded):
                    return expanded
        except Exception:
            pass
    if os.path.isfile(_SYSTEM_CHIME):
        return _SYSTEM_CHIME
    return None


def _play_error_chime() -> None:
    """Play an error chime in a background process."""
    path = _get_error_chime_path()
    if path:
        try:
            subprocess.Popen(
                ["afplay", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


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
    text: str,
) -> list[tuple[str | None, str]]:
    """Split text into segments of (voice_name, text).

    Plain text segments have voice_name=None (use primary backend).
    Tagged segments use guillemet syntax:
      - ««text»»           → voice_name="sam" (backward compatible)
      - ««alba»text»»      → voice_name="alba" (named voice)
      - ««2»text»»         → voice_pool index 2 (resolved from config)
      - ««/path/v.safetensors»text»» → custom voice file path

    Returns a list of (voice, text) tuples preserving original order.
    Empty segments are skipped.
    """
    # Match everything between double guillemets
    pattern = re.compile(r"\u00ab\u00ab(.+?)\u00bb\u00bb", re.DOTALL)

    segments: list[tuple[str | None, str]] = []
    last_end = 0
    for m in pattern.finditer(text):
        # Plain text before this tag
        before = text[last_end:m.start()].strip()
        if before:
            segments.append((None, before))

        content = m.group(1)
        # Check if content contains a single » that splits voice_id from text.
        # The double »» is already consumed by the outer regex, so any » inside
        # is a voice/text separator.
        if "\u00bb" in content:
            voice_id, tagged_text = content.split("\u00bb", 1)
            voice_id = voice_id.strip()
            tagged_text = tagged_text.strip()
            # Resolve pool index (pure digits) to voice name from config
            if voice_id.isdigit():
                voice_id = _resolve_pool_index(int(voice_id))
        else:
            # No separator — SAM voice, whole content is text
            voice_id = "sam"
            tagged_text = content.strip()

        if tagged_text:
            segments.append((voice_id, tagged_text))
        last_end = m.end()

    # Trailing plain text after last tag
    after = text[last_end:].strip()
    if after:
        segments.append((None, after))
    # If no tags found at all, return the whole text as one segment
    if not segments and text.strip():
        segments.append((None, text.strip()))
    return segments


def _resolve_pool_index(index: int) -> str:
    """Resolve a voice_pool index to a voice name from tts-config.json.

    Falls back to "sam" if the config is missing or the index is out of range.
    """
    cfg_path = os.path.expanduser("~/.claude/tts-config.json")
    try:
        with open(cfg_path) as f:
            pool = json.load(f).get("voice_pool", [])
        if 0 <= index < len(pool):
            return pool[index]
    except Exception:
        pass
    print(f"[voice-tag] Pool index {index} out of range or config missing, falling back to sam", flush=True)
    return "sam"


def _render_segments(
    segments: list[tuple[str | None, str]],
    primary_backend: TTSBackend,
    speed: float,
    gen_snap: int,
    default_voice: str | None = None,
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

        if voice_name == "sam":
            # SAM = switch to the SAM backend entirely
            render_backend = _get_override_backend("sam")
            if render_backend is None:
                render_backend = primary_backend
            render_voice = None
        elif voice_name:
            # Named voice / path — use primary backend with this voice
            render_backend = primary_backend
            render_voice = voice_name
        else:
            render_backend = primary_backend
            render_voice = default_voice

        # Use backend-specific generate if it supports voice, or base generate
        try:
            audio = render_backend.generate(segment_text, speed=speed, voice=render_voice)
        except TypeError:
            audio = render_backend.generate(segment_text, speed=speed)

        if audio is not None:
            if render_backend.sample_rate != target_rate:
                audio = _upsample(audio, render_backend.sample_rate, target_rate)
            chunks.append(audio)

    if not chunks:
        return None
    if len(chunks) == 1:
        return chunks[0]

    # Cross-fade segment boundaries to prevent clicks at voice transitions.
    XFADE = int(target_rate * 0.008)  # 8ms overlap
    merged = chunks[0]
    for chunk in chunks[1:]:
        overlap = min(XFADE, len(merged), len(chunk))
        if overlap > 1:
            fade = np.linspace(0.0, 1.0, overlap, dtype=np.float32)
            merged[-overlap:] *= fade[::-1]  # fade out tail
            chunk = chunk.copy()
            chunk[:overlap] *= fade           # fade in head
        merged = np.concatenate([merged, chunk])
    return merged


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

    from wednesday_tts.normalize.dictionary import load_dictionary, load_filenames_dict

    for base in candidates:
        dict_path = os.path.join(base, "tts-dictionary.json")
        filenames_path = os.path.join(base, "tts-filenames.json")
        if os.path.exists(dict_path):
            dictionary = load_dictionary(dict_path, backend="pocket")
        if os.path.exists(filenames_path):
            filenames_dict = load_filenames_dict(filenames_path)
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
_device_changed = threading.Event()  # set by health worker when default output device changes
_portaudio_lock = threading.Lock()   # guards sd._terminate()/_initialize() vs active stream writes
_active_backend: TTSBackend | None = None
_active_backend_name: str = ""

# Playback liveness tracking — lets watchdogs detect a wedged out_stream.write()
_playback_heartbeat: float = 0.0     # monotonic time of last successful stream write
_playback_stream_ref: sd.OutputStream | None = None  # current stream; watchdog can abort this
_playback_stream_lock = threading.Lock()  # protects _playback_stream_ref


def _stop_playback() -> None:
    """Stop current audio and drain the queue. Safe to call from any thread.

    Drains the playback queue so no more items play. The persistent
    OutputStream stays open but goes silent (nothing to write).
    Increments _stop_gen so in-flight generation threads bail out.
    """
    global _next_seq, _stop_gen
    # Drain the queue — discard all pending items
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
    """Query the current default output device.

    Does NOT cycle PortAudio terminate/initialize — that would kill any
    active OutputStream. Device switches are handled by reopening the
    stream on write failure.
    """
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

    NOTE: Only used as fallback if the persistent OutputStream fails.
    Normal playback goes through the persistent stream in playback_worker.
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
                print(
                    f"[WATCHDOG] {in_flight} request(s) in-flight for {age:.0f}s — "
                    "generate() appears hung, exiting for restart",
                    flush=True,
                )
                _play_error_chime()
                time.sleep(1)  # let chime start playing before exit
                os._exit(1)


# ---------------------------------------------------------------------------
# Audio health watchdog
# ---------------------------------------------------------------------------

def _query_default_device_subprocess() -> tuple[int, str] | None:
    """Query the default output device in a subprocess.

    PortAudio caches the device list at init time. The only way to see
    Bluetooth connect/disconnect is to terminate and reinitialise PA.
    But sd._terminate() invalidates ALL stream handles process-wide,
    killing any active OutputStream.

    Solution: spawn a short-lived subprocess that initialises its own
    PA context, queries devices, and exits. The parent's PA state and
    stream handles are untouched.
    """
    try:
        result = subprocess.run(
            [
                os.sys.executable, "-c",
                "import sounddevice as sd, json; "
                "info = sd.query_devices(kind='output'); "
                "print(json.dumps({'index': info['index'], 'name': info['name']}))",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            import json as _json
            data = _json.loads(result.stdout.strip())
            return (data["index"], data["name"])
    except Exception:
        pass
    return None


def _audio_health_worker() -> None:
    """Background thread: monitor audio health and detect device switches.

    Checks three things every cycle:
    1. Has the default output device changed? (via subprocess query)
    2. Is the audio subsystem reachable? (exits after consecutive failures)
    3. Is the playback worker making progress? (aborts wedged stream, exits if stuck)

    Device detection uses a subprocess so PortAudio terminate/initialize
    doesn't destroy the parent's active stream handles.
    """
    GRACE = 60
    INTERVAL = 5   # check every 5s for responsive device switching
    MAX_FAILS = 5
    STALL_ABORT = 15   # seconds before aborting a wedged stream
    STALL_EXIT = 45    # seconds before giving up and exiting for restart

    time.sleep(GRACE)
    probe_fails = 0
    last_device: int | None = get_default_output_device()
    print(f"[HEALTH] initial output device: {last_device}", flush=True)

    while True:
        time.sleep(INTERVAL)

        # --- Device change detection (subprocess, no PA disruption) ---
        result = _query_default_device_subprocess()
        if result is not None:
            probe_fails = 0
            current_device, dev_name = result
            if current_device != last_device:
                print(f"[HEALTH] output device changed: {last_device} → {current_device} ({dev_name})", flush=True)
                last_device = current_device
                _device_changed.set()
        else:
            probe_fails += 1
            print(f"[HEALTH] device query failed ({probe_fails}/{MAX_FAILS})", flush=True)
            if probe_fails >= MAX_FAILS:
                print("[HEALTH] audio subsystem wedged — exiting for restart", flush=True)
                _play_error_chime()
                time.sleep(1)
                os._exit(1)

        # --- Playback stall detection ---
        # If items are queued but the playback worker hasn't written
        # anything in STALL_ABORT seconds, the stream is wedged.
        if not playback_queue.empty() and _playback_heartbeat > 0:
            stall_age = time.monotonic() - _playback_heartbeat
            if stall_age > STALL_EXIT:
                print(
                    f"[HEALTH] playback stalled for {stall_age:.0f}s — "
                    "exiting for restart",
                    flush=True,
                )
                _play_error_chime()
                time.sleep(1)
                os._exit(1)
            elif stall_age > STALL_ABORT:
                # Force-abort the wedged stream so the playback worker
                # gets an exception and reopens on a fresh device.
                with _playback_stream_lock:
                    ref = _playback_stream_ref
                if ref is not None:
                    print(
                        f"[HEALTH] playback stalled for {stall_age:.0f}s — "
                        "aborting stream to unwedge",
                        flush=True,
                    )
                    try:
                        ref.abort()
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# Playback worker
# ---------------------------------------------------------------------------

def playback_worker(backend: TTSBackend) -> None:
    """Dedicated thread: plays audio from the queue through a persistent OutputStream.

    This is the ONLY code that touches the audio device. Queue items are
    np.ndarray chunks (any size). They're written to one long-lived
    OutputStream — no gaps between clips, no start/stop overhead.

    If the OutputStream dies (device switch, PortAudio error), it's
    reopened on the next item.
    """
    global _playback_heartbeat
    out_stream: sd.OutputStream | None = None
    device_rate = _get_device_samplerate(backend.sample_rate)

    def _open_stream() -> sd.OutputStream | None:
        nonlocal device_rate
        global _playback_stream_ref
        for _attempt in range(3):
            try:
                # Cycle PortAudio under the lock so the health worker
                # doesn't also cycle it at the same time (double-terminate
                # can corrupt PA state).
                with _portaudio_lock:
                    sd._terminate()
                    sd._initialize()
                    device = get_default_output_device()
                    device_rate = _get_device_samplerate(backend.sample_rate)
                    s = sd.OutputStream(
                        samplerate=device_rate,
                        device=device,
                        channels=1,
                        dtype="float32",
                    )
                    s.start()
                with _playback_stream_lock:
                    _playback_stream_ref = s
                return s
            except Exception as exc:
                print(f"[playback] OutputStream open failed (attempt {_attempt + 1}/3): {exc}", flush=True)
                if _attempt < 2:
                    time.sleep(1.0)
        with _playback_stream_lock:
            _playback_stream_ref = None
        return None

    while True:
        item = playback_queue.get()
        if item is None:
            break
        _playback_heartbeat = time.monotonic()
        try:
            # Upsample to device rate
            audio = _upsample(item.astype(np.float32), backend.sample_rate, device_rate)

            # Anti-click: trim tail artefacts, fade edges, pad with silence.
            # Soundstretch can leave junk in the last few ms — chop it,
            # then apply a clean fade so the signal reaches zero smoothly.
            TRIM_START = int(device_rate * 0.005)  # chop first 5ms
            TRIM_END = int(device_rate * 0.005)    # chop last 5ms
            PAD_START = int(device_rate * 0.050)
            PAD_END = int(device_rate * 0.050)
            FADE_IN = int(device_rate * 0.015)
            FADE_OUT = int(device_rate * 0.015)
            if len(audio) > TRIM_START + TRIM_END + FADE_IN + FADE_OUT:
                audio = audio[TRIM_START:]           # trim start artefacts
                audio = audio[:-TRIM_END].copy()     # trim end artefacts (copy to ensure contiguous)
                audio[:FADE_IN] *= np.linspace(0.0, 1.0, FADE_IN, dtype=np.float32)
                audio[-FADE_OUT:] *= np.linspace(1.0, 0.0, FADE_OUT, dtype=np.float32)
                audio = np.concatenate([
                    np.zeros(PAD_START, dtype=np.float32),
                    audio,
                    np.zeros(PAD_END, dtype=np.float32),
                ])
            else:
                # Chunk too small for full treatment — just fade the whole thing
                n = len(audio)
                fade = np.linspace(0.0, 1.0, n, dtype=np.float32)
                audio = audio.copy() * fade * fade[::-1]  # fade in AND out

            # Reopen stream if device changed, stream died, or not yet opened
            need_reopen = (
                out_stream is None
                or not out_stream.active
                or _device_changed.is_set()
            )
            if need_reopen:
                if _device_changed.is_set():
                    print("[playback] device change detected, reopening stream", flush=True)
                    _device_changed.clear()
                if out_stream is not None:
                    try:
                        out_stream.close()
                    except Exception:
                        pass
                    with _playback_stream_lock:
                        _playback_stream_ref = None
                out_stream = _open_stream()
                if out_stream is None:
                    print("[playback] no stream, falling back to sd.play()", flush=True)
                    _try_play(item, backend.sample_rate)
                    continue

            # Write in small chunks so STOP can interrupt mid-playback.
            # Each chunk is ~100ms — short enough for responsive stop,
            # long enough to avoid write() call overhead.
            WRITE_CHUNK = int(device_rate * 0.1)
            write_gen = _stop_gen
            flat = audio.reshape(-1)
            offset = 0
            is_last = playback_queue.empty()
            try:
                while offset < len(flat) and _stop_gen == write_gen:
                    end = min(offset + WRITE_CHUNK, len(flat))
                    out_stream.write(flat[offset:end].reshape(-1, 1))
                    _playback_heartbeat = time.monotonic()
                    offset = end
                # If nothing else queued, write 100ms of silence through the
                # stream so the DAC settles to zero before we stop writing.
                if is_last and _stop_gen == write_gen:
                    silence = np.zeros((int(device_rate * 0.1), 1), dtype=np.float32)
                    out_stream.write(silence)
            except Exception as exc:
                print(f"[playback] write failed: {exc}, reopening stream", flush=True)
                try:
                    out_stream.close()
                except Exception:
                    pass
                with _playback_stream_lock:
                    _playback_stream_ref = None
                out_stream = _open_stream()
        except Exception as exc:
            print(f"[playback] error: {exc}", flush=True)
        finally:
            playback_queue.task_done()

    # Shutdown
    if out_stream is not None:
        try:
            out_stream.stop()
            out_stream.close()
        except Exception:
            pass


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
        voice: str | None = None

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
                _ct = re.match(r"^__ct:([a-zA-Z0-9_-]+)__", text)
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

        # ── Parse voice segments BEFORE normalisation ──────────────────
        # Voice IDs may contain paths that normalisation would mangle.
        segments = _split_voice_segments(text)

        # ── Normalize text segments (not voice IDs) ──────────────────────
        if content_type != "normalized":
            segments = [
                (v, run_normalize(t, content_type=content_type))
                for v, t in segments
            ]

        # Reassemble text for dedup check
        text = " ".join(t for _, t in segments)

        # ── Dedup: skip if this text was recently spoken ─────────────────
        if _dedup_check(text):
            print(f"[req] dedup skip, seq={seq}, {len(text)} chars", flush=True)
            _stat_inc("requests_completed")
            conn.send(b"ok")
            return

        # Determine if we need backend switching (SAM segments mixed with pocket).
        # A single pocket voice for the whole message is NOT mixed — we can stream.
        needs_backend_switch = any(
            v == "sam" for v, _ in segments
        ) and any(v != "sam" for v, _ in segments)

        # Extract voice for single-segment or uniform-voice messages
        if len(segments) == 1 and segments[0][0] and segments[0][0] != "sam":
            voice = segments[0][0]
            text = segments[0][1]
        elif not any(v is not None for v, _ in segments):
            voice = None  # no tags at all

        # ── Render ────────────────────────────────────────────────────────
        gen_snap = _stop_gen

        # Streaming: single voice on primary backend, no backend switching needed
        use_streaming = (
            not needs_backend_switch
            and not any(v == "sam" for v, _ in segments)
            and hasattr(backend, "generate_streaming")
            and _stop_gen == gen_snap
        )
        if use_streaming:
            print(f"[req] STREAM-RENDER seq={seq}, {len(text)} chars, speed={speed}, voice={voice}", flush=True)
            _gs = gen_snap  # capture for closure
            gs_kwargs = {
                "speed": speed,
                "playback_queue": playback_queue,
                "stop_check": lambda: _stop_gen != _gs,
            }
            try:
                audio = backend.generate_streaming(text, voice=voice, **gs_kwargs)
            except TypeError:
                audio = backend.generate_streaming(text, **gs_kwargs)

            # If audio is None, generate_streaming already queued chunks directly
            if audio is None:
                with _order_cond:
                    _next_seq = 0
                    _order_cond.notify_all()
                _stat_inc("requests_completed")
                conn.send(b"ok")
                return
        else:
            print(f"[req] BATCH seq={seq}, {len(text)} chars, speed={speed}, voice={voice}", flush=True)
            audio = _render_segments(segments, backend, speed, gen_snap, default_voice=voice)

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

    except BrokenPipeError:
        # Client closed socket before we replied — not a real error.
        # The audio was (or will be) played fine; the hook just didn't wait.
        pass
    except Exception as exc:
        _stat_inc("requests_errored")
        print(f"Error handling client: {exc}", flush=True)
        _play_error_chime()
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

    startup_warnings = _check_competing_instances()
    if startup_warnings:
        try:
            audio = backend.generate(
                "Warning. Competing TTS services detected at startup. "
                "Check the daemon log for details."
            )
            playback_queue.put(audio)
        except Exception:
            pass  # logged already, don't block startup

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
            conn.settimeout(300)  # 5min — generation can take a while, STOP handles cancellation
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
