#!/usr/bin/env python3
"""Wednesday TTS — Windows HTTP service.

Listens on localhost:5678 for text-to-speech requests.

Features:
- Queue multiple requests from different sessions
- Play chime when cross-session queuing occurs
- Smart stop: clears the queue
- /normalize endpoint for testing the normalization pipeline
- content_type parameter on /speak: markdown | plain | normalized

Run:
    python -m wednesday_tts.server.app
    wednesday-tts          (if installed via pip)
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler

from flask import Flask, Response, jsonify, request

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".claude", "tts-config.json")
LOG_PATH = os.path.join(tempfile.gettempdir(), "wednesday-tts.log")

_logger = logging.getLogger("wednesday-tts")
_logger.setLevel(logging.DEBUG)
try:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    _fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
    _logger.addHandler(_fh)
except Exception:
    _logger.addHandler(logging.NullHandler())
_logger.propagate = False


def _log(msg: str) -> None:
    _logger.info(msg)


class _StdCapture:
    """Redirect stdout/stderr to the log file (captures Flask/library print output)."""

    def write(self, msg: str) -> None:
        if msg and msg.strip():
            _log(msg.rstrip())

    def flush(self) -> None:
        pass


# Capture all stdout/stderr — pythonw.exe has no console.
sys.stdout = sys.stderr = _StdCapture()  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

config: dict | None = None
_config_lock = threading.Lock()

current_model = None
current_model_name: str | None = None
current_voice_state = None

stop_playback = False

speech_queue: queue.Queue[str] = queue.Queue()
is_speaking = False
current_session_id: str | None = None
last_session_chime_time = 0.0
SESSION_CHIME_COOLDOWN = 30  # seconds

# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

_stats_lock = threading.Lock()
_stats: dict = {
    "requests_total": 0,
    "requests_completed": 0,
    "requests_stopped": 0,
    "requests_errored": 0,
    "queue_peak": 0,
    "first_sound_count": 0,
    "first_sound_sum_ms": 0.0,
    "first_sound_min_ms": None,
    "first_sound_max_ms": None,
    "duration_count": 0,
    "duration_sum_ms": 0.0,
    "duration_min_ms": None,
    "duration_max_ms": None,
    "audio_seconds_total": 0.0,
    "soundstretch_calls": 0,
    "soundstretch_ms_sum": 0.0,
    "service_start_time": None,
}


def _stat_inc(key: str, n: float = 1) -> None:
    with _stats_lock:
        _stats[key] += n


def _stat_latency(bucket: str, value_ms: float) -> None:
    with _stats_lock:
        _stats[f"{bucket}_count"] += 1
        _stats[f"{bucket}_sum_ms"] += value_ms
        if _stats[f"{bucket}_min_ms"] is None or value_ms < _stats[f"{bucket}_min_ms"]:
            _stats[f"{bucket}_min_ms"] = value_ms
        if _stats[f"{bucket}_max_ms"] is None or value_ms > _stats[f"{bucket}_max_ms"]:
            _stats[f"{bucket}_max_ms"] = value_ms


# ---------------------------------------------------------------------------
# Normalization wiring
# ---------------------------------------------------------------------------

def _load_normalize_deps() -> tuple[list, dict]:
    """Load pronunciation dictionaries from the package data directory."""

    dictionary: list = []
    filenames_dict: dict = {}

    # Try package data directory first, fall back to config path
    data_candidates = []
    try:
        # When installed as a package, data/ is alongside the src tree
        pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_candidates.append(os.path.join(pkg_dir, "data"))
    except Exception:
        pass
    # Also look relative to the repo root (dev install)
    data_candidates.append(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "data")
    )
    # And the legacy hooks location
    data_candidates.append(
        os.path.join(os.path.expanduser("~"), ".claude", "hooks")
    )

    for base in data_candidates:
        base = os.path.normpath(base)
        dict_path = os.path.join(base, "tts-dictionary.json")
        filenames_path = os.path.join(base, "tts-filenames.json")
        if os.path.exists(dict_path):
            try:
                from wednesday_tts.normalize.dictionary import load_dictionary
                active_model = (config or {}).get("active_model", "pocket")
                dictionary = load_dictionary(dict_path, backend=active_model)
                _log(f"[normalize] Loaded dictionary from {dict_path}")
            except Exception as exc:
                _log(f"[normalize] Failed to load dictionary: {exc}")
        if os.path.exists(filenames_path):
            try:
                with open(filenames_path, encoding="utf-8") as f:
                    filenames_dict = json.load(f)
                _log(f"[normalize] Loaded filenames dict from {filenames_path}")
            except Exception as exc:
                _log(f"[normalize] Failed to load filenames dict: {exc}")
        if dictionary and filenames_dict:
            break

    return dictionary, filenames_dict


_normalize_deps: tuple[list, dict] | None = None
_normalize_deps_lock = threading.Lock()


def _get_normalize_deps() -> tuple[list, dict]:
    global _normalize_deps
    if _normalize_deps is None:
        with _normalize_deps_lock:
            if _normalize_deps is None:
                _normalize_deps = _load_normalize_deps()
    return _normalize_deps


def run_normalize(text: str, content_type: str = "markdown") -> str:
    """Run the normalization pipeline and return TTS-ready text."""
    from wednesday_tts.normalize.pipeline import normalize  # lazy import

    dictionary, filenames_dict = _get_normalize_deps()
    return normalize(text, content_type=content_type, dictionary=dictionary, filenames_dict=filenames_dict)


# ---------------------------------------------------------------------------
# Config and model loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    global config
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    return config


def _setup_venv(venv_path: str | None) -> None:
    if venv_path and os.path.exists(venv_path):
        site_packages = os.path.join(venv_path, "Lib", "site-packages")
        if site_packages not in sys.path:
            sys.path.append(site_packages)


def get_model():
    """Load or reload the TTS model based on config."""
    global current_model, current_model_name, current_voice_state

    if config is None:
        load_config()

    model_name = config.get("active_model", "kokoro")  # type: ignore[union-attr]
    model_config = config.get("models", {}).get(model_name, {})  # type: ignore[union-attr]

    if current_model is None or current_model_name != model_name:
        _log(f"Loading {model_name} model...")
        current_model_name = model_name
        current_voice_state = None

        venv_path = model_config.get("venv_path")
        _setup_venv(venv_path)

        if model_name == "kokoro":
            from .backends.kokoro import KokoroBackend
            backend = KokoroBackend(
                voice=model_config.get("voice", "af_bella"),
                speed=model_config.get("speed", 1.3),
                samplerate=model_config.get("samplerate", 24000),
            )
            backend.load()
            current_model = backend

        elif model_name == "pocket":
            from .backends.pocket import PocketTTSBackend
            backend = PocketTTSBackend(
                voice=model_config.get("voice", "fantine"),
                fallback_voice=model_config.get("fallback_voice", "fantine"),
                speed=model_config.get("speed", 1.0),
                lsd_decode_steps=model_config.get("lsd_decode_steps", 1),
                noise_clamp=model_config.get("noise_clamp"),
                eos_threshold=model_config.get("eos_threshold", -4.0),
                frames_after_eos=model_config.get("frames_after_eos"),
            )
            backend.load()
            current_model = backend

        elif model_name == "soprano":
            from .backends.soprano import SopranoBackend
            backend = SopranoBackend(
                backend=model_config.get("backend", "transformers"),
                device=model_config.get("device", "cuda"),
                temperature=model_config.get("temperature", 0.3),
                top_p=model_config.get("top_p", 0.95),
                repetition_penalty=model_config.get("repetition_penalty", 1.2),
                samplerate=model_config.get("samplerate", 32000),
                venv_path=venv_path,
            )
            backend.load()
            current_model = backend

        elif model_name == "chatterbox":
            from .backends.chatterbox import ChatterboxBackend
            backend = ChatterboxBackend(
                device=model_config.get("device", "cuda"),
                voice_clone=model_config.get("voice_clone"),
            )
            backend.load()
            current_model = backend

        elif model_name == "sam":
            from .backends.sam import SAMBackend
            backend = SAMBackend(
                speed=model_config.get("speed", 72),
                pitch=model_config.get("pitch", 64),
                mouth=model_config.get("mouth", 128),
                throat=model_config.get("throat", 128),
            )
            backend.load()
            current_model = backend

        elif model_name == "qwen3":
            from .backends.qwen3 import Qwen3TTSBackend
            backend = Qwen3TTSBackend(
                model_id=model_config.get(
                    "model_id",
                    "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit",
                ),
                voice=model_config.get("voice"),
                voice_text=model_config.get("voice_text"),
                speed=model_config.get("speed", 1.0),
                instruct=model_config.get("instruct", ""),
            )
            backend.load()
            current_model = backend

        else:
            raise ValueError(f"Unknown model: {model_name!r}")

        _log(f"{model_name} model loaded.")

    return current_model, model_name, model_config


# ---------------------------------------------------------------------------
# Chime
# ---------------------------------------------------------------------------

def play_chime(sentiment: str = "neutral") -> None:
    """Play a brief notification chime in a daemon thread.

    Randomly picks from ~/Music/chirps (neutral) or ~/Music/errors (negative).
    Falls back to a mathematical E5->A5 beep if no sound files are found.
    """
    import glob
    import random

    import numpy as np
    import soundfile as sf  # type: ignore[import]

    CHIME_VOLUME = 0.7
    default_base = os.path.join(os.path.expanduser("~"), "Music")
    sound_dir = (
        os.environ.get("TTS_ERROR_DIR", os.path.join(default_base, "errors"))
        if sentiment == "negative"
        else os.environ.get("TTS_CHIME_DIR", os.path.join(default_base, "chirps"))
    )

    audio = None
    samplerate = 24000

    if os.path.exists(sound_dir):
        sound_files: list[str] = []
        for ext in ["*.wav", "*.mp3", "*.ogg"]:
            sound_files.extend(glob.glob(os.path.join(sound_dir, ext)))
        if sound_files:
            try:
                audio, samplerate = sf.read(random.choice(sound_files), dtype="float32")
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
            except Exception:
                audio = None

    if audio is None:
        duration = 0.15
        t = np.linspace(0, duration, int(samplerate * duration))
        tone1 = 0.3 * np.sin(2 * np.pi * 659 * t[: len(t) // 2])
        tone2 = 0.3 * np.sin(2 * np.pi * 880 * t[len(t) // 2 :])
        audio = np.concatenate([tone1, tone2])
        fade = np.linspace(1.0, 0.0, int(samplerate * 0.05))
        audio[-len(fade) :] *= fade

    audio_scaled = (audio * CHIME_VOLUME).astype(np.float32).reshape(-1, 1)
    sr = samplerate

    def _play() -> None:
        try:
            import sounddevice as sd  # type: ignore[import]
            with sd.OutputStream(samplerate=sr, channels=1) as stream:
                stream.write(audio_scaled)
        except Exception:
            pass

    threading.Thread(target=_play, daemon=True).start()


# ---------------------------------------------------------------------------
# Speech processing
# ---------------------------------------------------------------------------

def process_speech(text: str) -> None:
    """Generate and play speech for the given text.

    The text may be pre-normalized (content_type=normalized) or raw
    (content_type=markdown/plain). Normalization happens here if a
    __ct:<type>__ prefix is present; otherwise the text is passed
    through as-is (backward-compatible with old hook that pre-normalizes).
    """
    global stop_playback, is_speaking, _active_out_stream
    import re as _re

    import sounddevice as sd  # type: ignore[import]

    is_speaking = True
    stop_playback = False
    _proc_start = time.time()
    _first_sound_recorded = False

    # Extract content_type prefix: __ct:<type>__ (server prepends this, so strip first)
    content_type = "normalized"  # default: backward compat — assume already normalized
    _ct = _re.match(r"^__ct:(\w+)__", text)
    if _ct:
        content_type = _ct.group(1)
        text = text[_ct.end():]

    try:
        # Normalize if requested
        if content_type != "normalized":
            text = run_normalize(text, content_type=content_type)

        backend, model_name, model_config = get_model()

        from wednesday_tts.normalize.chunking import chunk_text_server as chunk_text
        text_chunks = chunk_text(text)

        try:
            dev = sd.query_devices(kind="output")
            dev_name = dev.get("name", "?") if isinstance(dev, dict) else str(dev)
        except Exception:
            dev_name = "?"
        _log(f"[TTS] Processing {len(text_chunks)} chunk(s) via '{dev_name}'")

        # ── Pocket (streaming) ──────────────────────────────────────────────
        if model_name == "pocket":
            full_text = " ".join(text_chunks)
            if backend.supports_streaming:
                # play_streaming handles the OutputStream internally
                if not _first_sound_recorded:
                    _stat_latency("first_sound", (time.time() - _proc_start) * 1000)
                    _first_sound_recorded = True
                backend.play_streaming(full_text, speed=model_config.get("speed", 1.0))
                if not stop_playback:
                    _stat_inc("audio_seconds_total", len(full_text) / 20)  # rough estimate
            else:
                audio = backend.generate(full_text)
                if audio is not None:
                    if not _first_sound_recorded:
                        _stat_latency("first_sound", (time.time() - _proc_start) * 1000)
                        _first_sound_recorded = True
                    _stat_inc("audio_seconds_total", len(audio) / backend.sample_rate)
                    sd.play(audio, samplerate=backend.sample_rate)
                    sd.wait()

        # ── Kokoro / Soprano (chunk pipeline) ──────────────────────────────
        elif model_name in ("kokoro", "soprano", "qwen3"):
            next_audio = None
            for i, chunk in enumerate(text_chunks):
                if stop_playback:
                    break

                audio = next_audio if next_audio is not None else backend.generate(chunk)
                next_audio = None

                if audio is None or stop_playback:
                    break

                if not _first_sound_recorded:
                    _stat_latency("first_sound", (time.time() - _proc_start) * 1000)
                    _first_sound_recorded = True
                _stat_inc("audio_seconds_total", len(audio) / backend.sample_rate)
                sd.play(audio, samplerate=backend.sample_rate, blocking=False)

                if i + 1 < len(text_chunks) and not stop_playback:
                    next_audio = backend.generate(text_chunks[i + 1])

                sd.wait()

        # ── Chatterbox (async generation queue) ────────────────────────────
        elif model_name == "chatterbox":
            import queue as _q
            audio_queue: _q.Queue = _q.Queue()
            gen_complete = threading.Event()

            def _generate_all() -> None:
                chars_so_far = 0
                try:
                    for chunk in text_chunks:
                        if stop_playback:
                            break
                        audio = backend.generate(chunk, chars_preceding=chars_so_far)
                        chars_so_far += len(chunk)
                        if audio is not None:
                            audio_queue.put(audio)
                finally:
                    gen_complete.set()

            gen_thread = threading.Thread(target=_generate_all, daemon=True)
            gen_thread.start()

            played = 0
            while played < len(text_chunks) and not stop_playback:
                try:
                    audio = audio_queue.get(timeout=30)
                    played += 1
                    if not _first_sound_recorded:
                        _stat_latency("first_sound", (time.time() - _proc_start) * 1000)
                        _first_sound_recorded = True
                    _stat_inc("audio_seconds_total", len(audio) / backend.sample_rate)
                    sd.play(audio, samplerate=backend.sample_rate, blocking=False)
                    sd.wait()
                except _q.Empty:
                    break

            gen_complete.wait(timeout=5)

    except Exception as exc:
        import traceback
        _log(f"Error in speech processing: {exc}\n{traceback.format_exc()}")
        _stat_inc("requests_errored")
    else:
        if stop_playback:
            _stat_inc("requests_stopped")
        else:
            _stat_inc("requests_completed")
            _stat_latency("duration", (time.time() - _proc_start) * 1000)
    finally:
        is_speaking = False


def queue_worker() -> None:
    """Background worker that processes the speech queue."""
    while True:
        try:
            text = speech_queue.get()
            if text is None:
                break
            process_speech(text)
            speech_queue.task_done()
        except Exception as exc:
            import traceback
            _log(f"Queue worker error: {exc}\n{traceback.format_exc()}")
            speech_queue.task_done()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return "ok"


@app.route("/normalize", methods=["POST"])
def normalize_endpoint():
    """Normalize text without synthesizing audio.

    Query params:
        content_type  — markdown (default) | plain | normalized

    Body: raw text (UTF-8)

    Returns: normalized text as text/plain
    """
    text = request.get_data(as_text=True)
    if not text:
        return Response("No text provided", status=400)

    content_type = request.args.get("content_type", "markdown")
    if content_type not in ("markdown", "plain", "normalized"):
        return Response(
            f"Invalid content_type {content_type!r}. Use: markdown, plain, normalized",
            status=400,
        )

    try:
        result = run_normalize(text, content_type=content_type)
        return Response(result, mimetype="text/plain")
    except Exception as exc:
        import traceback
        _log(f"[normalize] error: {exc}\n{traceback.format_exc()}")
        return Response(f"Normalization error: {exc}", status=500)


@app.route("/speak", methods=["POST"])
def speak():
    """Receive text and add to speech queue.

    Query params:
        content_type  — markdown | plain | normalized (default: normalized
                        for backward compatibility with old hooks)

    Body: raw text. The new thin hook sends raw markdown and sets content_type=markdown.
    Old hooks send pre-normalized text; they omit content_type (passthrough).
    """
    global is_speaking, current_session_id, last_session_chime_time

    text = request.get_data(as_text=True).strip()
    if not text:
        return Response("No text provided", status=400)

    _stat_inc("requests_total")
    session_id = request.headers.get("X-Session-Id", "")
    content_type = request.args.get("content_type", "normalized")

    _log(
        f"[SPEAK] {len(text)} chars ct={content_type} speaking={is_speaking} "
        f"queue={speech_queue.qsize()} session={session_id[:8] if session_id else '?'}"
    )

    # Strip timing stamp sent by hook — never pass to synthesis
    import re as _re_speak
    _t = _re_speak.match(r"^__t:[\d.]+__", text)
    if _t:
        text = text[_t.end():]

    # Inject content_type for process_speech
    if content_type != "normalized":
        text = f"__ct:{content_type}__{text}"

    # Chime for cross-session overlap
    if (
        (is_speaking or not speech_queue.empty())
        and session_id
        and current_session_id
        and session_id != current_session_id
    ):
        if time.time() - last_session_chime_time >= SESSION_CHIME_COOLDOWN:
            last_session_chime_time = time.time()
            try:
                play_chime()
            except Exception:
                pass

    if not is_speaking and speech_queue.empty():
        current_session_id = session_id

    speech_queue.put(text)
    queue_size = speech_queue.qsize()
    with _stats_lock:
        if queue_size > _stats["queue_peak"]:
            _stats["queue_peak"] = queue_size

    if queue_size > 1:
        return f"queued (position: {queue_size})"
    return "ok"


@app.route("/stop", methods=["POST"])
def stop():
    """Stop current playback and clear the queue."""
    global stop_playback
    import sounddevice as sd  # type: ignore[import]

    stop_playback = True
    sd.stop()

    # Abort pocket streaming if active
    if current_model is not None and hasattr(current_model, "abort_stream"):
        try:
            current_model.abort_stream()
        except Exception:
            pass

    cleared = 0
    while not speech_queue.empty():
        try:
            speech_queue.get_nowait()
            speech_queue.task_done()
            cleared += 1
        except queue.Empty:
            break

    if cleared:
        return f"stopped and cleared {cleared} queued item(s)"
    return "stopped"


@app.route("/drain", methods=["POST"])
def drain():
    """Block until all queued speech has been played."""
    try:
        speech_queue.join()
    except Exception:
        pass
    return "ok"


@app.route("/reload", methods=["POST"])
def reload_config():
    """Force reload the config and model."""
    global current_model, current_model_name
    current_model = None
    current_model_name = None
    load_config()
    return "Config reloaded"


@app.route("/stats", methods=["GET"])
def stats():
    """Return service telemetry as JSON or plain text (?fmt=text)."""
    with _stats_lock:
        s = dict(_stats)
    uptime = time.time() - s["service_start_time"] if s["service_start_time"] else 0
    fmt = request.args.get("fmt", "json")

    result = {
        "uptime_s": round(uptime),
        "requests": {
            "total": s["requests_total"],
            "completed": s["requests_completed"],
            "stopped": s["requests_stopped"],
            "errored": s["requests_errored"],
            "queue_peak": s["queue_peak"],
        },
        "latency_ms": {},
        "audio_seconds_total": round(s["audio_seconds_total"], 1),
        "soundstretch": {
            "calls": s["soundstretch_calls"],
            "avg_ms": round(s["soundstretch_ms_sum"] / s["soundstretch_calls"], 1)
            if s["soundstretch_calls"]
            else 0,
        },
        "backend": config.get("active_model", "unknown") if config else "unknown",
        "is_speaking": is_speaking,
        "queue_depth": speech_queue.qsize(),
    }

    for bucket in ("first_sound", "duration"):
        if s[f"{bucket}_count"] > 0:
            result["latency_ms"][bucket] = {
                "avg": round(s[f"{bucket}_sum_ms"] / s[f"{bucket}_count"], 1),
                "min": round(s[f"{bucket}_min_ms"], 1),
                "max": round(s[f"{bucket}_max_ms"], 1),
                "count": s[f"{bucket}_count"],
            }

    if fmt == "text":
        lines = [
            f"uptime={uptime // 60:.0f}m requests={s['requests_total']} "
            f"completed={s['requests_completed']} stopped={s['requests_stopped']} "
            f"errors={s['requests_errored']}",
        ]
        if s["first_sound_count"]:
            lines.append(
                f"first_sound avg={s['first_sound_sum_ms'] / s['first_sound_count']:.0f}ms "
                f"min={s['first_sound_min_ms']:.0f}ms max={s['first_sound_max_ms']:.0f}ms"
            )
        bk = config.get("active_model", "?") if config else "?"
        lines.append(
            f"audio_total={s['audio_seconds_total']:.1f}s backend={bk} "
            f"speaking={is_speaking} queue={speech_queue.qsize()}"
        )
        return "\n".join(lines), 200, {"Content-Type": "text/plain"}

    return jsonify(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _stats["service_start_time"] = time.time()
    _log("Wednesday TTS starting...")

    # Check port availability
    import socket as _sock
    probe = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 5678))
        probe.close()
    except OSError:
        probe.close()
        _log("ERROR: Port 5678 already in use. Exiting.")
        raise SystemExit(1)

    load_config()
    _log(f"Config loaded. Active model: {config.get('active_model')}")  # type: ignore[union-attr]

    worker_thread = threading.Thread(target=queue_worker, daemon=True)
    worker_thread.start()
    _log("Queue worker started")

    _log("Listening on http://localhost:5678")
    _log("Endpoints: POST /speak  POST /stop  POST /normalize  GET /health  GET /stats  POST /reload")

    try:
        get_model()
        _log("Model ready.")
    except Exception as exc:
        import traceback
        _log(f"ERROR: Could not pre-load model: {exc}\n{traceback.format_exc()}")

    app.run(host="127.0.0.1", port=5678, threaded=True)


if __name__ == "__main__":
    main()
