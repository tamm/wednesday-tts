#!/usr/bin/env python3
"""Wednesday TTS — macOS Unix socket daemon.

Keeps the model loaded in memory for fast responses.
Supports overlapping chunk processing: renders chunk N+1 while N plays.

Backend selection via active_model in ~/.claude/tts-config.json (default: pocket).

Run:
    python -m wednesday_tts.server.daemon
"""
from __future__ import annotations

import collections
import fcntl
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

from wednesday_tts.platform import suppress_dictation, unsuppress_dictation

from .backends import REGISTRY, TTSBackend
from .vpio import VPIOUnit
from ..normalize.chunking import chunk_text_server

SOCKET_PATH = "/tmp/tts-daemon.sock"
PID_PATH = "/tmp/tts-daemon.pid"
DEFAULT_SPEED = float(os.environ.get("TTS_SPEED", "1.15"))

# Path to the SpatialStream binary for head-tracked playback on BT headphones
_SPATIAL_STREAM_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "integrations", "spatial-audio", "SpatialStream",
)

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
) -> list[tuple[str | None, str | None, str]]:
    """Split text into segments of (voice_name, instruct, text).

    Plain text segments have voice_name=None, instruct=None (use primary backend).
    Tagged segments use guillemet syntax:
      - ««text»»                        → voice_name="sam" (backward compatible)
      - ««alba»text»»                   → voice_name="alba" (named voice)
      - ««2»text»»                      → voice_pool index 2 (resolved from config)
      - ««tamm1»text»»                  → voice_pool name lookup
      - ««/path/v.safetensors»text»»    → custom voice file path
      - ««|calm and warm»text»»         → instruct only, default voice

    Returns a list of (voice, instruct, text) tuples preserving original order.
    Empty segments are skipped.
    """
    # Match everything between double guillemets
    pattern = re.compile(r"\u00ab\u00ab(.+?)\u00bb\u00bb", re.DOTALL)

    segments: list[tuple[str | None, str | None, str]] = []
    last_end = 0
    for m in pattern.finditer(text):
        # Plain text before this tag
        before = text[last_end:m.start()].strip()
        if before:
            segments.append((None, None, before))

        content = m.group(1)
        instruct = None
        # Check if content contains a single » that splits voice_id from text.
        # The double »» is already consumed by the outer regex, so any » inside
        # is a voice/text separator.
        if "\u00bb" in content:
            voice_id, tagged_text = content.split("\u00bb", 1)
            voice_id = voice_id.strip()
            tagged_text = tagged_text.strip()
            # Parse voice|instruct if pipe is present
            if "|" in voice_id:
                voice_part, instruct = voice_id.split("|", 1)
                voice_id = voice_part.strip()
                instruct = instruct.strip() or None
            # Empty voice (e.g. ««|calm»text»») means default voice
            if not voice_id:
                voice_id = None
            # Resolve pool reference (index or name) from config
            elif voice_id != "sam":
                voice_id = _resolve_pool_entry(voice_id)
        else:
            # No separator — SAM voice, whole content is text
            voice_id = "sam"
            tagged_text = content.strip()

        if tagged_text:
            segments.append((voice_id, instruct, tagged_text))
        last_end = m.end()

    # Trailing plain text after last tag
    after = text[last_end:].strip()
    if after:
        segments.append((None, None, after))
    # If no tags found at all, return the whole text as one segment
    if not segments and text.strip():
        segments.append((None, None, text.strip()))
    return segments


def _voice_label(voice: "str | dict | None") -> str:
    """Human-readable label for a voice value (for logging)."""
    if voice is None:
        return "default"
    if isinstance(voice, dict):
        return voice.get("name") or os.path.basename(voice.get("voice", "?"))
    return str(voice)


def _resolve_pool_entry(voice_id: str) -> str | dict:
    """Resolve a voice_pool reference (index or name) to a voice entry.

    Accepts a numeric index ("4") or a name ("tamm1") to match against
    the "name" field in pool entries. Returns the full dict entry (with
    voice + voice_text) if available, or a plain string path.
    Falls back to "sam" if not found.
    """
    cfg_path = os.path.expanduser("~/.claude/tts-config.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        active = cfg.get("active_model", "pocket")
        model_cfg = cfg.get("models", {}).get(active, {})
        pool = model_cfg.get("voice_pool") or cfg.get("voice_pool", [])

        # Try numeric index first
        if voice_id.isdigit():
            index = int(voice_id)
            if 0 <= index < len(pool):
                entry = pool[index]
                print(f"[voice] pool[{index}] → {_voice_label(entry)}", flush=True)
                return entry
        else:
            # Match by name
            for i, entry in enumerate(pool):
                if isinstance(entry, dict) and entry.get("name") == voice_id:
                    print(f"[voice] pool name {voice_id!r} → [{i}] {_voice_label(entry)}", flush=True)
                    return entry
    except Exception as exc:
        print(f"[voice] config error resolving {voice_id!r}: {exc}", flush=True)
    print(f"[voice] pool entry {voice_id!r} not found, falling back to sam", flush=True)
    return "sam"


def _render_segments(
    segments: list[tuple[str | None, str | None, str]],
    primary_backend: TTSBackend,
    speed: float,
    gen_snap: int,
    default_voice: str | None = None,
    default_instruct: str | None = None,
) -> "np.ndarray | None":
    """Render a list of voice segments and concatenate into one audio array.

    Each segment is rendered with its specified backend (or the primary if None).
    All audio is resampled to the primary backend's sample rate before concatenation.
    """
    chunks: list[np.ndarray] = []
    target_rate = primary_backend.sample_rate

    for seg_i, (voice_name, instruct, segment_text) in enumerate(segments):
        if _stop_gen != gen_snap:
            break

        if voice_name == "sam":
            # SAM = switch to the SAM backend entirely
            render_backend = _get_override_backend("sam")
            if render_backend is None:
                render_backend = primary_backend
            render_voice = None
        elif voice_name is not None:
            # Named voice / path / dict — use primary backend with this voice
            render_backend = primary_backend
            render_voice = voice_name
        else:
            render_backend = primary_backend
            render_voice = default_voice

        print(
            f"[voice] segment {seg_i}: backend={render_backend.__class__.__name__}, "
            f"voice={_voice_label(render_voice)}, {len(segment_text)} chars",
            flush=True,
        )

        # Build kwargs — add instruct if the backend supports it
        gen_kwargs: dict = {"speed": speed, "voice": render_voice}
        use_instruct = instruct or default_instruct
        if use_instruct:
            gen_kwargs["instruct"] = use_instruct

        # Use backend-specific generate if it supports voice/instruct, or base generate
        try:
            audio = render_backend.generate(segment_text, **gen_kwargs)
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


def _touch_activity() -> None:
    """Reset the watchdog timer without changing any stat counters."""
    global _last_activity_time
    with _stats_lock:
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
            dictionary = load_dictionary(dict_path, backend=_active_backend_name or "pocket")
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
_skip_gen = 0       # incremented on SKIP; playback write loop bails but queue/renders survive
_msg_id_counter = 0         # monotonic message ID; incremented per request
_playing_msg_id: int = -1   # msg_id of the chunk currently being played
_skip_msg_id: int = -1      # msg_id that was last skipped; generation threads bail if they match

# Message completion tracking: handlers signal when all chunks for a msg_id
# are enqueued, so the playback worker knows when it can move to the next message.
_msg_done: set[int] = set()          # msg_ids whose chunks are all enqueued
_msg_done_lock = threading.Lock()
_msg_done_event = threading.Event()  # poked when a msg finishes enqueuing


def _mark_msg_done(msg_id: int) -> None:
    """Signal that all chunks for msg_id have been enqueued."""
    with _msg_done_lock:
        _msg_done.add(msg_id)
    _msg_done_event.set()


def _is_msg_done(msg_id: int) -> bool:
    """Check if all chunks for msg_id have been enqueued."""
    with _msg_done_lock:
        return msg_id in _msg_done


def _clear_msg_done(msg_id: int) -> None:
    """Remove msg_id from the done set (after playback finishes it)."""
    with _msg_done_lock:
        _msg_done.discard(msg_id)


playback_queue: queue.Queue = queue.Queue()
_current_pan: float = 0.5  # stereo pan: 0.0=left, 0.5=centre, 1.0=right
_device_changed = threading.Event()  # set by health worker when default output device changes
_portaudio_lock = threading.Lock()   # guards sd._terminate()/_initialize() vs active stream writes
_active_backend: TTSBackend | None = None
_active_backend_name: str = ""

# Playback liveness tracking — lets watchdogs detect a wedged out_stream.write()
_playback_heartbeat: float = 0.0     # monotonic time of last successful stream write
_level_last_sent: float = 0.0        # monotonic time of last playback_level overlay event
_playback_stream_ref: sd.OutputStream | None = None  # current stream; watchdog can abort this
_playback_stream_lock = threading.Lock()  # protects _playback_stream_ref

# VPIO audio unit — set up at startup, used instead of PortAudio on speaker output
_vpio: VPIOUnit | None = None
_vpio_lock = threading.Lock()  # guards setup/teardown only; feed_audio is lock-free


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
    # Clear message-done tracking
    with _msg_done_lock:
        _msg_done.clear()
    _msg_done_event.set()
    with _order_cond:
        _stop_gen += 1
        _next_seq = 0
        _order_cond.notify_all()
    # Kill spatial stream so head-tracked audio stops immediately
    _kill_spatial_stream()
    if _vpio is not None:
        _vpio.clear_buffer()
    unsuppress_dictation()


def _skip_current() -> None:
    """Skip the entire current message (all remaining chunks with the same msg_id).

    Drains queued items belonging to the same message FIRST, then bails the
    current write loop. This ordering prevents the playback worker from
    grabbing the next same-message chunk before we can remove it.
    Also sets _skip_msg_id so in-flight generation threads bail.
    """
    global _skip_gen, _skip_msg_id
    # Drain items belonging to the currently playing message
    skip_id = _playing_msg_id
    _skip_msg_id = skip_id
    requeue = []
    while True:
        try:
            item = playback_queue.get_nowait()
            playback_queue.task_done()
            if item is None:
                requeue.append(item)
            elif isinstance(item, tuple) and len(item) >= 3 and item[2] == skip_id:
                continue  # discard — same message
            else:
                requeue.append(item)
        except queue.Empty:
            break
    for item in requeue:
        playback_queue.put(item)
    # Now bail the currently playing chunk
    _skip_gen += 1
    _kill_spatial_stream()
    if _vpio is not None:
        _vpio.clear_buffer()


def _sigusr1_handler(sig: int, frame) -> None:
    """SIGUSR1 = stop talking immediately. Sent by stop-tts.sh."""
    print("[cmd] SIGUSR1 received, stopping playback", flush=True)
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


def _query_bt_headphone_uid() -> str | None:
    """Check if default output is a Bluetooth device. Returns device name or None.

    Queries CoreAudio transport type directly via ctypes — no subprocess,
    no system_profiler, no timing issues. Returns the device name from
    sounddevice if the transport type is Bluetooth or Bluetooth LE.
    """
    try:
        import ctypes

        ca = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        )

        class _PropAddr(ctypes.Structure):
            _fields_ = [
                ("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32),
            ]

        def _fourcc(s: str) -> int:
            return (ord(s[0]) << 24 | ord(s[1]) << 16 | ord(s[2]) << 8 | ord(s[3]))

        scope_global = _fourcc("glob")

        # Get default output device ID
        addr = _PropAddr(_fourcc("dOut"), scope_global, 0)
        device_id = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        err = ca.AudioObjectGetPropertyData(
            1, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(device_id)
        )
        if err != 0:
            return None

        # Get transport type
        addr2 = _PropAddr(_fourcc("tran"), scope_global, 0)
        transport = ctypes.c_uint32(0)
        size2 = ctypes.c_uint32(4)
        err2 = ca.AudioObjectGetPropertyData(
            device_id.value, ctypes.byref(addr2), 0, None,
            ctypes.byref(size2), ctypes.byref(transport),
        )
        if err2 != 0:
            return None

        is_bt = transport.value in (_fourcc("blue"), _fourcc("blea"))
        if not is_bt:
            return None

        # Return device name for logging/SpatialStream args
        dev = sd.query_devices(kind="output")
        return dev["name"]
    except Exception:
        return None


# Spatial stream subprocess management
_spatial_proc: subprocess.Popen | None = None
_spatial_pan: float = 0.5
_spatial_lock = threading.Lock()

# Magic bytes for pan update command sent to SpatialStream stdin
_PAN_MAGIC = b"PAN!"


def _send_pan_update(proc: subprocess.Popen, pan: float) -> None:
    """Send a pan position update to a running SpatialStream subprocess."""
    global _spatial_pan
    try:
        proc.stdin.write(_PAN_MAGIC + struct.pack("f", pan))
        proc.stdin.flush()
        _spatial_pan = pan
    except (BrokenPipeError, OSError):
        pass


def _get_spatial_stream(sample_rate: int, pan: float, device_uid: str) -> subprocess.Popen | None:
    """Get or create a SpatialStream subprocess for head-tracked playback.

    Sends inline pan updates if the position has changed.
    """
    global _spatial_proc, _spatial_pan
    with _spatial_lock:
        if _spatial_proc is not None and _spatial_proc.poll() is None:
            if abs(pan - _spatial_pan) >= 0.01:
                _send_pan_update(_spatial_proc, pan)
            return _spatial_proc
        # Kill any stale process
        if _spatial_proc is not None:
            try:
                _spatial_proc.stdin.close()
                _spatial_proc.wait(timeout=2)
            except Exception:
                _spatial_proc.kill()
            _spatial_proc = None
        if not os.path.isfile(_SPATIAL_STREAM_BIN):
            print(f"[spatial] SpatialStream binary not found at {_SPATIAL_STREAM_BIN}", flush=True)
            return None
        try:
            proc = subprocess.Popen(
                [_SPATIAL_STREAM_BIN, str(int(sample_rate)), device_uid, str(pan)],
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _spatial_proc = proc
            _spatial_pan = pan
            # Read the ready message
            import select
            if select.select([proc.stderr], [], [], 3.0)[0]:
                line = proc.stderr.readline().decode("utf-8", errors="replace")
                print(f"[spatial] {line.strip()}", flush=True)
            return proc
        except Exception as exc:
            print(f"[spatial] failed to start SpatialStream: {exc}", flush=True)
            return None


def _kill_spatial_stream() -> None:
    """Terminate any running SpatialStream subprocess."""
    global _spatial_proc
    with _spatial_lock:
        if _spatial_proc is not None:
            try:
                _spatial_proc.stdin.close()
                _spatial_proc.wait(timeout=2)
            except Exception:
                try:
                    _spatial_proc.kill()
                except Exception:
                    pass
            _spatial_proc = None


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
    # Use subprocess query for initial device so the index space matches all
    # subsequent polls. The parent PA context may assign different indices than
    # a freshly-spawned subprocess, causing spurious "no change" results.
    _init_result = _query_default_device_subprocess()
    last_device: int | None = _init_result[0] if _init_result else None
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

def _anti_click(audio: np.ndarray, rate: int) -> np.ndarray:
    """Trim artefacts and apply fade-in/out to prevent clicks between chunks."""
    TRIM_START = int(rate * 0.005)
    TRIM_END = int(rate * 0.005)
    PAD_START = int(rate * 0.050)
    PAD_END = int(rate * 0.050)
    FADE_IN = int(rate * 0.015)
    FADE_OUT = int(rate * 0.015)
    if len(audio) > TRIM_START + TRIM_END + FADE_IN + FADE_OUT:
        audio = audio[TRIM_START:]
        audio = audio[:-TRIM_END].copy()
        audio[:FADE_IN] *= np.linspace(0.0, 1.0, FADE_IN, dtype=np.float32)
        audio[-FADE_OUT:] *= np.linspace(1.0, 0.0, FADE_OUT, dtype=np.float32)
        audio = np.concatenate([
            np.zeros(PAD_START, dtype=np.float32),
            audio,
            np.zeros(PAD_END, dtype=np.float32),
        ])
    else:
        n = len(audio)
        fade = np.linspace(0.0, 1.0, n, dtype=np.float32)
        audio = audio.copy() * fade * fade[::-1]
    return audio


_OVERLAY_SOCK = "/tmp/wednesday-yarn-overlay.sock"


def _send_overlay(*msgs: dict) -> None:
    """Fire-and-forget JSON messages to the wednesday-yarn overlay HUD.

    All messages are sent on a SINGLE socket connection so the overlay
    processes them in order on one thread.  This is critical for
    sequences like (transcription → playback_started) where ordering
    determines which history entry gets its timestamp reset.
    """
    if not msgs:
        return
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(_OVERLAY_SOCK)
        payload = "".join(json.dumps(m) + "\n" for m in msgs)
        s.sendall(payload.encode())
        s.close()
    except Exception:
        pass


def _send_subtitle(text: str, audio_dur: float = 0) -> None:
    """Send subtitle + playback_started as an atomic batch.

    audio_dur drives karaoke word-by-word highlighting.
    hold_s is set high as a safety ceiling — playback_stopped trims it
    when audio actually ends (natural finish or interrupt).
    """
    msg: dict = {"type": "transcription", "text": text, "role": "assistant", "hold_s": 120.0}
    if audio_dur > 0:
        msg["audio_dur"] = audio_dur
    _send_overlay(
        msg,
        {"type": "playback_started"},
    )


def _send_overlay_idle() -> None:
    """Tell the overlay we're done speaking."""
    _send_overlay(
        {"type": "playback_stopped"},
    )


def _limiter(audio: np.ndarray, ceiling: float = 0.85, window_ms: float = 30.0,
             rate: int = 48000) -> np.ndarray:
    """Simple lookahead peak limiter to prevent dangerously loud output.

    1. Hard-clip anything above ceiling (safety net).
    2. Compute per-window peak envelope and attenuate windows that exceed
       ceiling, with smoothed gain to avoid clicks.
    """
    audio = audio.copy()
    window = max(1, int(rate * window_ms / 1000))

    # Compute peak envelope per window
    n = len(audio)
    gain = np.ones(n, dtype=np.float32)
    for i in range(0, n, window):
        chunk = audio[i:i + window]
        peak = np.abs(chunk).max()
        if peak > ceiling:
            gain[i:i + window] = ceiling / peak

    # Smooth the gain curve to avoid clicks (simple moving average)
    smooth_len = min(window, n)
    if smooth_len > 1:
        kernel = np.ones(smooth_len, dtype=np.float32) / smooth_len
        gain = np.convolve(gain, kernel, mode="same")
        # After smoothing, ensure gain never exceeds 1.0
        gain = np.minimum(gain, 1.0)

    audio *= gain

    # Hard clamp as final safety net
    np.clip(audio, -ceiling, ceiling, out=audio)
    return audio


def playback_worker(backend: TTSBackend) -> None:
    """Dedicated thread: plays audio from the queue.

    Two playback modes:
    - PortAudio (sounddevice): speakers — stereo pan via equal-power law
    - SpatialStream subprocess: BT headphones — head-tracked spatial audio

    Mode is selected on device change. SpatialStream is only used when
    a Bluetooth headphone is the default output device.
    """
    global _playback_heartbeat, _level_last_sent
    out_stream: sd.OutputStream | None = None
    device_rate = _get_device_samplerate(backend.sample_rate)
    use_spatial = False
    bt_uid: str | None = None

    def _detect_spatial_mode(log: bool = True) -> tuple[bool, str | None]:
        """Check if we should use spatial playback. Returns (use_spatial, bt_uid)."""
        uid = _query_bt_headphone_uid()
        has_bin = os.path.isfile(_SPATIAL_STREAM_BIN)
        if uid and has_bin:
            if log:
                print(f"[playback] BT headphones detected, uid={uid} — using spatial stream", flush=True)
            return True, uid
        if uid and not has_bin:
            if log:
                print("[playback] BT detected but no SpatialStream binary — PortAudio fallback", flush=True)
        else:
            if log:
                print("[playback] non-BT output — using PortAudio stereo pan", flush=True)
        return False, None

    def _open_stream() -> sd.OutputStream | None:
        nonlocal device_rate
        global _playback_stream_ref
        for _attempt in range(3):
            try:
                with _portaudio_lock:
                    sd._terminate()
                    sd._initialize()
                    device = get_default_output_device()
                    device_rate = _get_device_samplerate(backend.sample_rate)
                    dev_name = "unknown"
                    try:
                        dev_name = sd.query_devices(device)["name"]
                    except Exception:
                        pass
                    print(f"[playback] _open_stream: device={device} ({dev_name}) rate={device_rate}", flush=True)
                    s = sd.OutputStream(
                        samplerate=device_rate,
                        device=device,
                        channels=2,
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

    # Initial mode detection
    use_spatial, bt_uid = _detect_spatial_mode()

    # Message grouping: buffer chunks from other messages so we finish one
    # message completely before starting the next. This prevents the
    # nightmarish interleaving of two speakers alternating every chunk.
    _deferred: dict[int, list[tuple]] = {}   # msg_id → list of queued items
    _current_msg: int | None = None          # msg_id we are currently playing

    def _next_item():
        """Get the next item to play, respecting message grouping.

        Returns (item, from_queue) where from_queue=True means the item
        came from playback_queue.get() and needs a task_done() call,
        and from_queue=False means it was deferred (task_done already called).

        If we have a current message and there are deferred chunks for it,
        return one of those. Otherwise pull from the queue. If the queue
        gives us a chunk for a different message, defer it and keep pulling.
        """
        nonlocal _current_msg

        def _advance_to_deferred():
            """Try to advance _current_msg to the next deferred message."""
            nonlocal _current_msg
            if _deferred:
                next_id = min(_deferred.keys())
                _current_msg = next_id
                items = _deferred[next_id]
                if items:
                    return items.pop(0), False
                else:
                    del _deferred[next_id]
            return None

        # First: drain any deferred chunks for the current message
        if _current_msg is not None and _current_msg in _deferred:
            items = _deferred[_current_msg]
            if items:
                return items.pop(0), False
            else:
                del _deferred[_current_msg]

        # If current message is done, advance to the next deferred message
        if _current_msg is not None and _is_msg_done(_current_msg):
            _clear_msg_done(_current_msg)
            _current_msg = None
            result = _advance_to_deferred()
            if result:
                return result

        # Pull from the main queue, using a short timeout so we can
        # periodically check if the current message finished enqueuing
        # (its chunks may already be deferred).
        while True:
            try:
                raw = playback_queue.get(timeout=0.2)
            except queue.Empty:
                # Current message might be done — check both the explicit
                # signal and the implicit "nothing left anywhere" case.
                if _current_msg is not None:
                    done = _is_msg_done(_current_msg)
                    # Also treat as done if queue is empty and no deferred
                    # chunks exist for current msg — the STOP handler may
                    # have cleared _msg_done before we could check it.
                    if done or (not _deferred.get(_current_msg)):
                        _clear_msg_done(_current_msg)
                        _current_msg = None
                        result = _advance_to_deferred()
                        if result:
                            return result
                continue

            if raw is None:
                return None, True  # shutdown sentinel

            # Extract msg_id from tuple
            if isinstance(raw, tuple) and len(raw) >= 3:
                chunk_msg_id = raw[2]
            else:
                chunk_msg_id = -1

            # No current message — start this one
            if _current_msg is None:
                _current_msg = chunk_msg_id
                return raw, True

            # Same message — play it
            if chunk_msg_id == _current_msg:
                return raw, True

            # Different message — defer it and mark this queue get() as done
            _deferred.setdefault(chunk_msg_id, []).append(raw)
            playback_queue.task_done()

            # Check if current message is now done (all chunks enqueued)
            # and we should just advance
            if _is_msg_done(_current_msg):
                _clear_msg_done(_current_msg)
                _current_msg = None
                result = _advance_to_deferred()
                if result:
                    return result

    while True:
        result = _next_item()
        if result is None:
            break
        item, _from_queue = result
        if item is None:
            break

        # Unpack (audio, subtitle_text, msg_id) tuple or bare array
        global _playing_msg_id
        if isinstance(item, tuple):
            if len(item) >= 3:
                item, subtitle_text, _playing_msg_id = item[0], item[1], item[2]
            else:
                item, subtitle_text = item[0], item[1]
        else:
            subtitle_text = None

        _chunk_t0 = time.monotonic()
        _playback_heartbeat = time.monotonic()
        suppress_dictation()
        try:
            # Re-detect mode every chunk — CoreAudio query is fast (no subprocess,
            # no PA reinit) so there's no reason to gate this on an event.
            # This guarantees we're always on the right device regardless of
            # whether the health worker fired or not.
            old_spatial = use_spatial
            old_bt_uid = bt_uid
            use_spatial, bt_uid = _detect_spatial_mode(log=False)
            switched = use_spatial != old_spatial or bt_uid != old_bt_uid
            if switched:
                if old_spatial and not use_spatial:
                    print("[playback] switching from spatial to PortAudio", flush=True)
                    _kill_spatial_stream()
                elif not old_spatial and use_spatial:
                    print("[playback] switching from PortAudio to spatial", flush=True)
                # Always reopen PortAudio stream on device change
                if out_stream is not None:
                    print("[playback] closing old stream for device change", flush=True)
                    try:
                        out_stream.close()
                    except Exception:
                        pass
                    with _playback_stream_lock:
                        _playback_stream_ref = None
                    out_stream = None
            _device_changed.clear()  # consume any pending event — detection already done

            audio = item.astype(np.float32)
            pan = _current_pan

            # --- VPIO path (speakers only, not BT headphones) ---
            # VPIO handles output + AEC reference in one unit. We feed audio and
            # wait real-time duration — same pattern as spatial, no PortAudio needed.
            vpio_ok = False
            if not use_spatial and _vpio is not None and _vpio._running:
                vpio_audio = _limiter(audio.copy(), ceiling=0.85, rate=backend.sample_rate)
                vpio_audio = _anti_click(vpio_audio, backend.sample_rate)
                if subtitle_text:
                    dur = len(audio) / backend.sample_rate
                    _send_subtitle(subtitle_text, audio_dur=dur)
                    subtitle_text = None
                else:
                    _send_overlay({"type": "playback_started"})
                write_gen_v = _stop_gen
                write_skip_v = _skip_gen

                def _should_bail_vpio() -> bool:
                    return _stop_gen != write_gen_v or _skip_gen != write_skip_v

                _vpio.feed_audio(vpio_audio, sample_rate=backend.sample_rate)
                _play_dur_v = len(vpio_audio) / backend.sample_rate
                _wait_end_v = time.monotonic() + _play_dur_v
                LEVEL_INTERVAL = 0.1
                _level_t = time.monotonic()
                while time.monotonic() < _wait_end_v and not _should_bail_vpio():
                    _sleep = min(LEVEL_INTERVAL, _wait_end_v - time.monotonic())
                    if _sleep > 0:
                        time.sleep(_sleep)
                    _playback_heartbeat = time.monotonic()
                    if _playback_heartbeat - _level_last_sent >= LEVEL_INTERVAL:
                        # Estimate level from remaining buffer fraction
                        _send_overlay({"type": "playback_level", "level": 0.5})
                        _level_last_sent = _playback_heartbeat
                vpio_ok = True

            if vpio_ok:
                continue

            spatial_ok = False
            if use_spatial and bt_uid:
                # --- Spatial stream path (BT headphones) ---
                proc = _get_spatial_stream(backend.sample_rate, pan, bt_uid)
                if proc is None or proc.poll() is not None:
                    print("[playback] spatial stream unavailable, falling back to PortAudio", flush=True)
                    use_spatial = False
                    bt_uid = None
                else:
                    spatial_audio = _limiter(audio.copy(), ceiling=0.85, rate=backend.sample_rate)
                    spatial_audio = _anti_click(spatial_audio, backend.sample_rate)
                    if subtitle_text:
                        dur = len(audio) / backend.sample_rate
                        _send_subtitle(subtitle_text, audio_dur=dur)
                        subtitle_text = None
                    else:
                        _send_overlay({"type": "playback_started"})
                    write_gen = _stop_gen
                    write_skip = _skip_gen
                    CHUNK = int(backend.sample_rate * 0.1)
                    offset = 0

                    def _should_bail() -> bool:
                        return _stop_gen != write_gen or _skip_gen != write_skip

                    # Use non-blocking IO to avoid indefinite stalls on BT buffer pressure
                    fd = proc.stdin.fileno()
                    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                    try:
                        _spatial_write_t0 = time.monotonic()
                        while offset < len(spatial_audio) and not _should_bail():
                            end = min(offset + CHUNK, len(spatial_audio))
                            chunk_bytes = spatial_audio[offset:end].tobytes()
                            written = 0
                            deadline = time.monotonic() + 10.0
                            while written < len(chunk_bytes):
                                if time.monotonic() > deadline:
                                    raise OSError("spatial write timed out (10s)")
                                if _should_bail():
                                    break
                                try:
                                    n = os.write(fd, chunk_bytes[written:])
                                    written += n
                                except BlockingIOError:
                                    time.sleep(0.01)
                            _playback_heartbeat = time.monotonic()
                            offset = end
                        # Wait for actual playback to finish — the pipe
                        # write completes much faster than real-time.
                        _write_elapsed = time.monotonic() - _spatial_write_t0
                        _play_dur = len(spatial_audio) / backend.sample_rate
                        _wait = _play_dur - _write_elapsed
                        if _wait > 0 and not _should_bail():
                            print(f"[playback] spatial: waiting {_wait:.1f}s for playback to finish", flush=True)
                            _wait_end = time.monotonic() + _wait
                            while time.monotonic() < _wait_end and not _should_bail():
                                time.sleep(min(0.2, _wait_end - time.monotonic()))
                                _playback_heartbeat = time.monotonic()
                        spatial_ok = True
                    except (BrokenPipeError, OSError) as exc:
                        print(f"[playback] spatial stream write failed: {exc}, falling back to PortAudio", flush=True)
                        _kill_spatial_stream()
                        use_spatial = False
                        bt_uid = None

            if spatial_ok:
                continue

            # --- PortAudio path (speakers or BT fallback) ---
            audio = _upsample(audio, backend.sample_rate, device_rate)
            audio = _limiter(audio, ceiling=0.85, rate=device_rate)
            audio = _anti_click(audio, device_rate)

            need_reopen = (
                out_stream is None
                or not out_stream.active
            )
            if need_reopen:
                if out_stream is not None:
                    try:
                        out_stream.close()
                    except Exception:
                        pass
                    with _playback_stream_lock:
                        _playback_stream_ref = None
                print("[playback] opening PortAudio stream", flush=True)
                out_stream = _open_stream()
                if out_stream is None:
                    print("[playback] no stream, falling back to sd.play()", flush=True)
                    _try_play(item, backend.sample_rate)
                    continue
                print(f"[playback] PortAudio stream opened, rate={device_rate}", flush=True)

            if subtitle_text:
                dur = len(item) / backend.sample_rate
                _send_subtitle(subtitle_text, audio_dur=dur)
                subtitle_text = None
            else:
                _send_overlay({"type": "playback_started"})
            WRITE_CHUNK = int(device_rate * 0.1)
            write_gen = _stop_gen
            write_skip = _skip_gen
            flat = audio.reshape(-1)
            offset = 0
            is_last = playback_queue.empty()

            def _should_bail_pa() -> bool:
                return _stop_gen != write_gen or _skip_gen != write_skip

            pan_angle = pan * (np.pi / 2.0)
            gain_l = np.float32(np.cos(pan_angle))
            gain_r = np.float32(np.sin(pan_angle))

            try:
                while offset < len(flat) and not _should_bail_pa():
                    end = min(offset + WRITE_CHUNK, len(flat))
                    mono = flat[offset:end]
                    stereo = np.column_stack((mono * gain_l, mono * gain_r))
                    out_stream.write(stereo)
                    _playback_heartbeat = time.monotonic()
                    # Send peak level to overlay at ~10 Hz
                    if _playback_heartbeat - _level_last_sent >= 0.1:
                        peak = float(np.max(np.abs(mono)))
                        _send_overlay({"type": "playback_level", "level": min(peak, 1.0)})
                        _level_last_sent = _playback_heartbeat
                    offset = end
                if is_last and not _should_bail_pa():
                    silence = np.zeros((int(device_rate * 0.1), 2), dtype=np.float32)
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
            # Only call task_done() for items that came from playback_queue.get()
            # (not for deferred items which already had task_done() called).
            if _from_queue:
                playback_queue.task_done()
            if playback_queue.empty() and not _deferred:
                unsuppress_dictation()
                _send_overlay_idle()

    # Shutdown
    unsuppress_dictation()
    _kill_spatial_stream()
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
        SEQ:N:speed:ct:ts:pan:text  render text with sequence N, play in order
                                    ct=content_type (markdown|normalized), ts=epoch float or empty
                                    pan=stereo position 0.0-1.0 (empty = 0.5 centre)
        SEQ:N:speed:ct:ts:text      (legacy 6-field form, pan defaults to 0.5)
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
            print("[cmd] STOP received, draining queue", flush=True)
            _stop_playback()
            conn.send(b"ok")
            return

        # ── SKIP ──────────────────────────────────────────────────────────
        if message == "SKIP":
            print(f"[cmd] SKIP received, msg_id={_playing_msg_id}", flush=True)
            _skip_current()
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
                "backend": _active_backend_name or "unknown",
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
        global _current_pan
        seq: int | None = None
        speed = DEFAULT_SPEED
        text = message
        content_type = "normalized"  # backward compat
        voice: str | None = None
        pan: float = 0.5  # default centre

        if message.startswith("SEQ:"):
            # 7-field: SEQ:N:speed:ct:ts:pan:text  (pan = float or empty)
            # 6-field: SEQ:N:speed:ct:ts:text       (legacy, no pan)
            # 4-field: SEQ:N:speed:text              (oldest, __ct:/__t: prefixes)
            #
            # Detect 7-field vs 6-field: try splitting at 6 colons first.
            # If parts[5] is empty or a valid float, treat as 7-field (pan field).
            # Otherwise fall back to 6-field parse.
            parts7 = message.split(":", 6)
            if len(parts7) >= 7 and (not parts7[5] or re.match(r"^[01]?\.\d+$", parts7[5])):
                # 7-field format with pan
                try:
                    seq = int(parts7[1])
                    speed = DEFAULT_SPEED if parts7[2] == "N" else float(parts7[2])
                    content_type = parts7[3] if parts7[3] else "markdown"
                    # parts7[4] is timestamp
                    if parts7[5]:
                        pan = max(0.0, min(1.0, float(parts7[5])))
                    text = parts7[6]
                except ValueError:
                    pass
            else:
                parts = message.split(":", 5)
                if len(parts) >= 6:
                    # 6-field format (no pan)
                    try:
                        seq = int(parts[1])
                        speed = DEFAULT_SPEED if parts[2] == "N" else float(parts[2])
                        content_type = parts[3] if parts[3] else "markdown"
                        # parts[4] is timestamp
                        text = parts[5]
                    except ValueError:
                        pass
            if seq is None:
                # Old 4-field format — backward compat
                parts4 = message.split(":", 3)
                if len(parts4) >= 4:
                    try:
                        seq = int(parts4[1])
                        speed = DEFAULT_SPEED if parts4[2] == "N" else float(parts4[2])
                        text = parts4[3]
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
                (v, i, run_normalize(t, content_type=content_type))
                for v, i, t in segments
            ]

        # Reassemble text for dedup check
        text = " ".join(t for _, _, t in segments)

        # ── Dedup: skip if this text was recently spoken ─────────────────
        if _dedup_check(text):
            print(f"[req] dedup skip, seq={seq}, {len(text)} chars", flush=True)
            _stat_inc("requests_completed")
            conn.send(b"ok")
            return

        # Determine if we need backend switching (SAM segments mixed with pocket).
        # A single pocket voice for the whole message is NOT mixed — we can stream.
        needs_backend_switch = any(
            v == "sam" for v, _, _ in segments
        ) and any(v != "sam" for v, _, _ in segments)

        # Extract voice and instruct for single-segment or uniform-voice messages
        instruct = None
        if len(segments) == 1 and segments[0][0] and segments[0][0] != "sam":
            voice = segments[0][0]
            instruct = segments[0][1]
            text = segments[0][2]
        elif not any(v is not None for v, _, _ in segments):
            voice = None  # no tags at all

        # ── Set stereo pan for this request ──────────────────────────────
        _current_pan = pan

        # ── Assign message ID for skip tracking ──────────────────────────
        global _msg_id_counter
        _msg_id_counter += 1
        msg_id = _msg_id_counter

        # ── Render ────────────────────────────────────────────────────────
        gen_snap = _stop_gen

        # Streaming: single voice on primary backend, no backend switching needed
        use_streaming = (
            not needs_backend_switch
            and not any(v == "sam" for v, _, _ in segments)
            and getattr(backend, "supports_streaming", False)
            and hasattr(backend, "generate_streaming")
            and _stop_gen == gen_snap
        )
        if use_streaming:
            print(f"[req] STREAM-RENDER seq={seq}, {len(text)} chars, speed={speed}, voice={_voice_label(voice)}, pan={pan:.2f}", flush=True)
            _gs = gen_snap  # capture for closure
            _mid = msg_id   # capture for closure
            gs_kwargs = {
                "speed": speed,
                "playback_queue": playback_queue,
                "stop_check": lambda: _stop_gen != _gs or _skip_msg_id == _mid,
                "msg_id": msg_id,
            }
            if instruct:
                gs_kwargs["instruct"] = instruct
            try:
                audio = backend.generate_streaming(text, voice=voice, **gs_kwargs)
            except TypeError:
                audio = backend.generate_streaming(text, **gs_kwargs)

            # If audio is None, generate_streaming already queued chunks directly
            if audio is None:
                _mark_msg_done(msg_id)
                with _order_cond:
                    _next_seq = 0
                    _order_cond.notify_all()
                _stat_inc("requests_completed")
                conn.send(b"ok")
                return
        else:
            # ── BATCH render ──────────────────────────────────────────────────
            if needs_backend_switch:
                # Mixed backends (e.g. SAM + qwen3) — render the parsed segments
                # directly so each segment uses its assigned voice/backend.
                print(
                    f"[req] MULTI-VOICE seq={seq}, {len(text)} chars → {len(segments)} segment(s), "
                    f"speed={speed}, pan={pan:.2f}",
                    flush=True,
                )
                chunk_audio = _render_segments(
                    segments, backend, speed, gen_snap, default_voice=voice,
                )
                if chunk_audio is not None and _stop_gen == gen_snap and _skip_msg_id != msg_id:
                    playback_queue.put((chunk_audio, text, msg_id))
                    total_audio_secs = len(chunk_audio) / backend.sample_rate
                    print(f"[req] multi-voice enqueued ({total_audio_secs:.1f}s)", flush=True)
                else:
                    total_audio_secs = 0.0
                _mark_msg_done(msg_id)
            else:
                # Single voice — chunk for lower latency
                text_chunks = chunk_text_server(
                    text, min_size=120, max_size=300,
                    backend_name=_active_backend_name,
                )
                print(
                    f"[req] BATCH seq={seq}, {len(text)} chars → {len(text_chunks)} chunk(s), "
                    f"speed={speed}, voice={_voice_label(voice)}, pan={pan:.2f}",
                    flush=True,
                )

                total_audio_secs = 0.0
                for ci, chunk_text in enumerate(text_chunks):
                    if _stop_gen != gen_snap or _skip_msg_id == msg_id:
                        break
                    chunk_segments = [(None, None, chunk_text)]
                    chunk_audio = _render_segments(
                        chunk_segments, backend, speed, gen_snap, default_voice=voice,
                    )
                    if chunk_audio is not None and _stop_gen == gen_snap and _skip_msg_id != msg_id:
                        playback_queue.put((chunk_audio, chunk_text, msg_id))
                        chunk_secs = len(chunk_audio) / backend.sample_rate
                        total_audio_secs += chunk_secs
                        # Heartbeat: tell the watchdog we're still making
                        # progress so it doesn't kill long multi-chunk renders.
                        _touch_activity()
                        print(
                            f"[req] chunk {ci + 1}/{len(text_chunks)} enqueued "
                            f"({chunk_secs:.1f}s)",
                            flush=True,
                        )
                _mark_msg_done(msg_id)

            with _order_cond:
                _next_seq = 0
                _order_cond.notify_all()

            if total_audio_secs > 0:
                _stat_inc("audio_seconds_total", total_audio_secs)
            _stat_inc("requests_completed")
            conn.send(b"ok")
            return

        # ── Enqueue in order (streaming path) ─────────────────────────────
        if seq is not None:
            with _order_cond:
                if _stop_gen != gen_snap or _skip_msg_id == msg_id:
                    conn.send(b"ok")
                    return
                deadline = time.monotonic() + 5
                while _next_seq != seq:
                    if _stop_gen != gen_snap or _skip_msg_id == msg_id:
                        conn.send(b"ok")
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        print(f"SEQ timeout: expected {_next_seq}, got {seq}. Resetting.", flush=True)
                        _next_seq = seq
                        break
                    _order_cond.wait(timeout=min(remaining, 10))
                if _stop_gen != gen_snap or _skip_msg_id == msg_id:
                    conn.send(b"ok")
                    return
                if audio is not None:
                    playback_queue.put((audio, text, msg_id))
                _mark_msg_done(msg_id)
                _next_seq = 0
                _order_cond.notify_all()
        elif audio is not None and _skip_msg_id != msg_id:
            playback_queue.put((audio, text, msg_id))
            _mark_msg_done(msg_id)

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

class _TimestampWriter:
    """Wrapper that prepends HH:MM:SS to each printed line."""
    def __init__(self, stream):
        self._stream = stream
        self._at_line_start = True
    def write(self, s):
        if not s:
            return
        from datetime import datetime
        parts = s.split("\n")
        for i, part in enumerate(parts):
            if i > 0:
                self._stream.write("\n")
                self._at_line_start = True
            if part:
                if self._at_line_start:
                    self._stream.write(datetime.now().strftime("%H:%M:%S "))
                    self._at_line_start = False
                self._stream.write(part)
    def flush(self):
        self._stream.flush()


def main() -> None:
    import sys
    sys.stdout = _TimestampWriter(sys.stdout)
    sys.stderr = _TimestampWriter(sys.stderr)

    _stats["service_start_time"] = time.time()

    # Load config file first — active_model drives backend selection.
    _config_path = os.path.join(os.path.expanduser("~"), ".claude", "tts-config.json")
    _cfg: dict = {}
    _model_config: dict = {}
    try:
        import json as _json
        with open(_config_path, encoding="utf-8") as _f:
            _cfg = _json.load(_f)
    except FileNotFoundError:
        print(f"No config file at {_config_path} — using env vars only", flush=True)
    except Exception as exc:
        print(f"Warning: could not load config {_config_path}: {exc}", flush=True)

    # Backend selection: active_model from config > "pocket"
    backend_name = (_cfg.get("active_model") or "pocket").lower()
    _model_config = _cfg.get("models", {}).get(backend_name, {})
    print(f"Loaded config from {_config_path} (model: {backend_name})", flush=True)

    backend_cls = REGISTRY.get(backend_name)
    if backend_cls is None:
        print(f"Unknown backend {backend_name!r}. Choose from: {', '.join(REGISTRY)}", flush=True)
        raise SystemExit(1)

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

    elif backend_name == "qwen3":
        for _key in ("model_id", "voice", "voice_text", "instruct"):
            if _model_config.get(_key):
                _kwargs[_key] = _model_config[_key]
        if _model_config.get("speed") is not None:
            _kwargs["speed"] = _model_config["speed"]
        if _model_config.get("seed") is not None:
            _kwargs["seed"] = _model_config["seed"]
        for _gen_key in ("temperature", "top_p", "top_k", "repetition_penalty"):
            if _model_config.get(_gen_key) is not None:
                _kwargs[_gen_key] = _model_config[_gen_key]

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

    # VPIO disabled — playback path not wired yet, and running VPIO without
    # routing audio through it causes the mic to hear speakers with no AEC.
    # TODO: enable once playback worker uses VPIO feed_audio() path.
    # global _vpio
    # try:
    #     _vpio = VPIOUnit()
    #     _vpio.setup()
    #     _vpio.start()
    # except Exception as _vpio_exc:
    #     _vpio = None

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
            playback_queue.put((audio, None, -1))
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
        if _vpio is not None:
            try:
                _vpio.stop()
            except Exception:
                pass
        server.close()
        for path in (SOCKET_PATH, PID_PATH):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
