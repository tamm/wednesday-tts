#!/usr/bin/env python3
"""Parse ~/Library/Logs/Wednesday/tts/daemon.log and emit TTS latency tables.

Tracks each msg_id from hook fire → daemon accept → synth done → playback
start, then renders a markdown table per backend covering the full pipeline.

Stages tracked per utterance (all in seconds):
    hook_to_daemon   — hook timestamp → daemon picked up request
                       (the daemon's `latency=Xs` field, which is wall(req) − wall(hook))
    synth_dt         — req accept → "generated Xs audio in Ys" line
    synth_elapsed    — backend's own reported synth time
    play_dt          — req accept → first `[spatial] ready` / portaudio open
    total_to_play    — hook_to_daemon + play_dt (HEADLINE: hook fire → audio out)
    audio_s          — duration of generated audio
    rtf              — synth_elapsed / audio_s (lower = faster than realtime)

Summary tables (markdown) per backend: mean / p50 / p90 / p99 / max for
every metric, plus a "last N per backend" detail table.

Usage:
    uv run python scripts/analyse_latency.py [--log PATH] [--since HH:MM]
                                             [--last N] [--backend BACKEND]
                                             [--no-detail]
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

DEFAULT_LOG = Path("/tmp/wednesday-tts.log")

TS_RE = re.compile(r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s")

# 21:44:52 [req] msg_id=1 source=permission ... → voice=fantine ... 18 chars latency=0.02s
PAT_REQ = re.compile(
    r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+\[req\]\s+msg_id=(\d+)\s+source=(\S+).*?voice=(\S+).*?(\d+)\s+chars\s+latency=([\d.]+)s"
)
# 21:44:53 [pocket-stream-pipe] 12 chunks, generated 1.7s audio in 0.5s (RTF 0.29, voice=fantine, ...)
# 07:59:48 [moss] generated 3.3s audio in 4.3s (RTF 1.31, preset=Junhao)
# 21:44:53 [qwen3] generated 7.6s audio in 2.7s (RTF 0.36, ref=...)
# 12:47:57 [kokoro] 1 segments, generated 2.9s audio in 0.7s (RTF 0.25, voice=af_sarah)
# 15:34:21 [vibevoice] 4.0s in 4.5s (RTF 1.12, voice=en-Frank_man.pt)  ← non-streaming
# Legacy (pre-rename) pocket lines kept for backwards-compat: [stream], [stream-direct], [stream-pipe]
PAT_GEN = re.compile(
    r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+"
    r"\[(pocket(?:-stream(?:-pipe|-direct)?)?|stream(?:-pipe|-direct)?|"
    r"moss|qwen3(?:-light|-pro)?(?:-stream)?|qwen3-stream|"
    r"kokoro|sam|chatterbox(?:-turbo)?|soprano|vibevoice)\]"
    r"[^\n]*?generated\s+([\d.]+)s\s+audio\s+in\s+([\d.]+)s"
)
PAT_RTF = re.compile(r"RTF\s+([\d.]+)")
PAT_VOICE = re.compile(r"voice=([\w\-./]+)")
# Vibevoice synth-done equivalent (streaming, no "generated Xs audio in Ys" line):
#   21:24:06 [vibevoice-play] done elapsed=74.3s audio=114.2s rtf=0.65 ... voice=en-Emma_woman.pt ok=True
PAT_VIBE_DONE = re.compile(
    r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+\[vibevoice-play\]\s+done\s+"
    r"elapsed=([\d.]+)s\s+audio=([\d.]+)s\s+"
    r"(?:synth_elapsed=([\d.]+)s\s+)?rtf=([\d.]+)"
    r".*?voice=([\w\-./]+)\s+ok=(True|False)"
)
# Vibevoice TTFS marker — fires once per utterance, just before audio plays:
#   13:05:07 [vibevoice-play] prebuffer=0.13s saw_pause=True first-audio=4328ms
PAT_VIBE_TTFS = re.compile(
    r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+\[vibevoice-play\]\s+prebuffer=[\d.]+s\s+"
    r"saw_pause=(?:True|False)\s+first-audio=([\d.]+)ms"
)
# Qwen3 streaming DIRECT-PLAY done summary (mirrors PAT_VIBE_DONE):
#   21:24:06 [qwen3-light-play] done elapsed=4.3s audio=7.6s rtf=0.57 ... voice=... ok=True
#   21:24:06 [qwen3-pro-play] done elapsed=4.3s audio=7.6s rtf=0.57 ... voice=... ok=True
PAT_QWEN3_DONE = re.compile(
    r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+\[(qwen3-(?:light|pro|[a-z]+)-play)\]\s+done\s+"
    r"elapsed=([\d.]+)s\s+audio=([\d.]+)s\s+"
    r"(?:synth_elapsed=([\d.]+)s\s+)?rtf=([\d.]+)"
    r".*?voice=(\S+)\s+ok=(True|False)"
)
# Qwen3 TTFS marker — fires just before audio starts playing:
#   13:05:07 [qwen3-light-play] prebuffer=0.13s first-audio=4328ms
#   13:05:07 [qwen3-pro-play] prebuffer=0.13s first-audio=4328ms
PAT_QWEN3_TTFS = re.compile(
    r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+\[(qwen3-(?:light|pro|[a-z]+)-play)\]\s+"
    r"prebuffer=[\d.]+s\s+first-audio=([\d.]+)ms"
)
# Kokoro streaming DIRECT-PLAY done summary (mirrors vibevoice):
#   21:24:06 [kokoro-play] done elapsed=4.3s audio=7.6s rtf=0.57 ... voice=af_heart ok=True
PAT_KOKORO_DONE = re.compile(
    r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+\[kokoro-play\]\s+done\s+"
    r"elapsed=([\d.]+)s\s+audio=([\d.]+)s\s+"
    r"(?:synth_elapsed=([\d.]+)s\s+)?rtf=([\d.]+)"
    r".*?voice=(\S+)\s+ok=(True|False)"
)
# Kokoro TTFS marker — fires just before audio starts playing:
#   13:05:07 [kokoro-play] prebuffer=0.13s first-audio=187ms
PAT_KOKORO_TTFS = re.compile(
    r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+\[kokoro-play\]\s+"
    r"prebuffer=[\d.]+s\s+first-audio=([\d.]+)ms"
)
# Generic per-message first-chunk marker emitted by the playback worker.
# Works for every backend: pocket, kokoro, sam, chatterbox, ...
#   22:05:01 [playback] first-chunk msg_id=7
PAT_FIRST_CHUNK = re.compile(
    r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+\[playback\]\s+first-chunk\s+msg_id=(-?\d+)"
)
# 21:44:53 [spatial] [SpatialStream] ready rate=48000.0 pan=0.47 device=...
PAT_SPATIAL = re.compile(r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+\[spatial\]\s+\[SpatialStream\]\s+ready")
# 21:44:53 [playback] opening PortAudio stream / PortAudio stream opened
PAT_PLAYBACK_OPEN = re.compile(r"^(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s+\[playback\]\s+(opening|PortAudio stream opened)")
# 23:14:38.743 Ready! [qwen3] Listening on /tmp/tts-daemon.sock
PAT_DAEMON_READY = re.compile(r"^\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?\s+Ready!\s+\[(\w+)\]")
# 23:07:38 Loaded speech tokenizer from .../models--mlx-community--Qwen3-TTS-12Hz-0.6B-Base-4bit/...
PAT_QWEN3_VARIANT = re.compile(r"models--mlx-community--Qwen3-TTS-12Hz-([\w.\-]+?)/snapshots")


@dataclass
class Utt:
    msg_id: int
    req_ts: time
    backend: str = "?"
    voice: str = "?"
    source: str = "?"
    chars: int = 0
    req_latency: float = 0.0
    synth_done_ts: time | None = None
    synth_elapsed: float | None = None
    audio_s: float | None = None
    rtf: float | None = None
    playback_ts: time | None = None
    extras: dict = field(default_factory=dict)


def parse_ts(s: str) -> time | None:
    fmt = "%H:%M:%S.%f" if "." in s else "%H:%M:%S"
    try:
        return datetime.strptime(s, fmt).time()
    except ValueError:
        return None


def t_delta_s(a: time, b: time) -> float:
    """b - a in seconds (handles same-day only; negative if log wraps midnight)."""
    da = datetime.combine(datetime.today(), a)
    db = datetime.combine(datetime.today(), b)
    return (db - da).total_seconds()


def parse_log(lines, since: time | None = None) -> list[Utt]:
    # List, not dict — daemon msg_ids reset to 1 across restarts, so a
    # dict keyed by msg_id collides utterances across sessions and we lose
    # everything except the last session for each id.
    utts: list[Utt] = []
    pending_cold_for: str | None = None  # backend whose next utt is cold-start
    qwen3_variant: str | None = None     # current qwen3 model variant (e.g. "0.6B-Base-4bit")

    def _last() -> Utt | None:
        return utts[-1] if utts else None

    for line in lines:
        # Daemon-ready: next utterance for this backend is its cold start.
        ready_m = PAT_DAEMON_READY.match(line)
        if ready_m:
            pending_cold_for = ready_m.group(1)
            continue
        var_m = PAT_QWEN3_VARIANT.search(line)
        if var_m:
            qwen3_variant = var_m.group(1)
            continue

        ts_m = TS_RE.match(line)
        if not ts_m:
            continue
        ts = parse_ts(ts_m.group(1))
        if ts is None:
            continue
        if since and ts < since:
            continue

        m = PAT_REQ.match(line)
        if m:
            msg_id = int(m.group(2))
            voice = m.group(4)
            utts.append(Utt(
                msg_id=msg_id,
                req_ts=ts,
                source=m.group(3),
                voice=voice,
                chars=int(m.group(5)),
                req_latency=float(m.group(6)),
                backend="?",
            ))
            continue

        m = PAT_GEN.match(line)
        u = _last()
        if m and u is not None:
            backend_token = m.group(2)
            # Both legacy [stream*] and current [pocket*]/[pocket-stream*] map to pocket.
            if backend_token.startswith("stream") or backend_token.startswith("pocket"):
                u.backend = "pocket"
            else:
                u.backend = backend_token
            # Normalise qwen3 streaming variants: qwen3-pro-stream → qwen3-pro
            if u.backend.endswith("-stream") and u.backend.startswith("qwen3-"):
                u.backend = u.backend.removesuffix("-stream")
            elif u.backend == "qwen3-stream":
                u.backend = "qwen3"
            # Backwards-compat: pre-tag logs only said [qwen3]; recover the
            # variant from the sibling tokenizer line if available.
            if u.backend == "qwen3" and qwen3_variant:
                u.backend = "qwen3-light" if qwen3_variant.startswith("0.6B") else "qwen3-pro"
            if pending_cold_for == u.backend or (
                pending_cold_for == "qwen3" and u.backend.startswith("qwen3-")
            ):
                u.extras["cold"] = True
                pending_cold_for = None
            u.audio_s = float(m.group(3))
            u.synth_elapsed = float(m.group(4))
            u.synth_done_ts = ts
            rtf_m = PAT_RTF.search(line)
            if rtf_m:
                u.rtf = float(rtf_m.group(1))
            elif u.audio_s and u.synth_elapsed:
                u.rtf = u.synth_elapsed / u.audio_s
            # Prefer the voice tag from the gen line if present; falls back to
            # the request-line voice that was set when the [req] was parsed.
            voice_m = PAT_VOICE.search(line)
            if voice_m:
                v = voice_m.group(1)
                # Strip filename trimmings: en-Frank_man.pt → Frank
                v = re.sub(r"^en-", "", v)
                v = re.sub(r"_(woman|man)$", "", v.rsplit(".", 1)[0])
                u.voice = v
            continue

        # Vibevoice streaming: no "generated Xs audio in Ys" line, but the
        # play-done summary carries equivalent fields.
        m = PAT_VIBE_DONE.match(line)
        if m and u is not None:
            u.backend = "vibevoice"
            if pending_cold_for == "vibevoice":
                u.extras["cold"] = True
                pending_cold_for = None
            # group 2 = wall elapsed (incl. playback), group 4 = synth-only
            # (when emitted by newer daemons). Prefer the synth-only value.
            wall_elapsed = float(m.group(2))
            synth_only = float(m.group(4)) if m.group(4) else None
            u.synth_elapsed = synth_only if synth_only is not None else wall_elapsed
            u.audio_s = float(m.group(3))
            u.rtf = float(m.group(5))
            u.synth_done_ts = ts
            v = m.group(6)
            v = re.sub(r"^en-", "", v.rsplit(".", 1)[0])
            v = re.sub(r"_(woman|man)$", "", v)
            u.voice = v
            u.extras["ok"] = m.group(7) == "True"
            continue

        # Vibevoice TTFS marker: prebuffer line carries first-audio=Xms,
        # which is the time from request start to first sample heard.
        m = PAT_VIBE_TTFS.match(line)
        if m and u is not None:
            ttfs_ms = float(m.group(2))
            u.playback_ts = ts
            u.extras["ttfs_ms"] = ttfs_ms
            continue

        # Qwen3 streaming DIRECT-PLAY done summary (mirrors vibevoice handling).
        m = PAT_QWEN3_DONE.match(line)
        if m and u is not None:
            raw_tag = m.group(2).removesuffix("-play")  # "qwen3-light" / "qwen3-pro"
            u.backend = raw_tag
            if pending_cold_for in (raw_tag, "qwen3"):
                u.extras["cold"] = True
                pending_cold_for = None
            wall_elapsed = float(m.group(3))
            synth_only = float(m.group(5)) if m.group(5) else None
            u.synth_elapsed = synth_only if synth_only is not None else wall_elapsed
            u.audio_s = float(m.group(4))
            u.rtf = float(m.group(6))
            u.synth_done_ts = ts
            u.voice = m.group(7)
            u.extras["ok"] = m.group(8) == "True"
            continue

        # Qwen3 TTFS marker: prebuffer line carries first-audio=Xms.
        m = PAT_QWEN3_TTFS.match(line)
        if m and u is not None:
            ttfs_ms = float(m.group(3))
            u.playback_ts = ts
            u.extras["ttfs_ms"] = ttfs_ms
            continue

        # Kokoro streaming DIRECT-PLAY done summary (mirrors vibevoice handling).
        m = PAT_KOKORO_DONE.match(line)
        if m and u is not None:
            u.backend = "kokoro"
            if pending_cold_for == "kokoro":
                u.extras["cold"] = True
                pending_cold_for = None
            wall_elapsed = float(m.group(2))
            synth_only = float(m.group(4)) if m.group(4) else None
            u.synth_elapsed = synth_only if synth_only is not None else wall_elapsed
            u.audio_s = float(m.group(3))
            u.rtf = float(m.group(5))
            u.synth_done_ts = ts
            u.voice = m.group(6)
            u.extras["ok"] = m.group(7) == "True"
            continue

        # Kokoro TTFS marker: prebuffer line carries first-audio=Xms.
        m = PAT_KOKORO_TTFS.match(line)
        if m and u is not None:
            ttfs_ms = float(m.group(2))
            u.playback_ts = ts
            u.extras["ttfs_ms"] = ttfs_ms
            continue

        # Generic first-chunk marker (all backends). Match the most recent
        # utterance with this msg_id that hasn't yet been marked as playing.
        m = PAT_FIRST_CHUNK.match(line)
        if m:
            target_id = int(m.group(2))
            for cand in reversed(utts):
                if cand.msg_id == target_id and cand.playback_ts is None:
                    cand.playback_ts = ts
                    break
            continue

        m = PAT_SPATIAL.match(line) or PAT_PLAYBACK_OPEN.match(line)
        if m and u is not None:
            if u.playback_ts is None:
                u.playback_ts = ts

    return utts


def fmt_delta(seconds: float | None) -> str:
    if seconds is None:
        return "    -"
    return f"{seconds:6.2f}"


def fmt_rtf(rtf: float | None) -> str:
    if rtf is None:
        return "  -  "
    return f"{rtf:5.2f}"


def voice_short(v: str) -> str:
    if "/" in v:
        return Path(v).stem[:18]
    return v[:18]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _synth_dt(u: Utt) -> float | None:
    return t_delta_s(u.req_ts, u.synth_done_ts) if u.synth_done_ts else None


def _play_dt(u: Utt) -> float | None:
    return t_delta_s(u.req_ts, u.playback_ts) if u.playback_ts else None


def _total_to_play(u: Utt) -> float | None:
    p = _play_dt(u)
    return None if p is None else u.req_latency + p


METRICS: list[tuple[str, callable]] = [
    ("hook_to_daemon", lambda u: u.req_latency),
    ("synth_dt",       _synth_dt),
    ("synth_elapsed",  lambda u: u.synth_elapsed),
    ("play_dt",        _play_dt),
    ("total_to_play",  _total_to_play),
    ("audio_s",        lambda u: u.audio_s),
    ("rtf",            lambda u: u.rtf),
]


def _md_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def render_per_utt(utts: list[Utt], per_backend_last: int | None) -> str:
    """Markdown detail table; if per_backend_last is set, show last N per backend."""
    if per_backend_last:
        by_backend: dict[str, list[Utt]] = {}
        for u in utts:
            by_backend.setdefault(u.backend, []).append(u)
        rows: list[Utt] = []
        for backend in sorted(by_backend):
            rows.extend(by_backend[backend][-per_backend_last:])
        utts = rows

    headers = [
        "msg", "backend", "voice", "src", "chars",
        "hook→daemon", "synth_dt", "play_dt", "total→play",
        "audio_s", "rtf",
    ]
    out = [_md_row(headers), _md_row(["---"] * len(headers))]
    for u in utts:
        out.append(_md_row([
            str(u.msg_id),
            u.backend,
            voice_short(u.voice),
            u.source[:12],
            str(u.chars),
            f"{u.req_latency:.2f}s",
            fmt_delta(_synth_dt(u)).strip() + "s" if _synth_dt(u) is not None else "—",
            fmt_delta(_play_dt(u)).strip() + "s" if _play_dt(u) is not None else "—",
            fmt_delta(_total_to_play(u)).strip() + "s" if _total_to_play(u) is not None else "—",
            f"{u.audio_s:.2f}s" if u.audio_s is not None else "—",
            f"{u.rtf:.2f}" if u.rtf is not None else "—",
        ]))
    return "\n".join(out)


def render_summary(utts: list[Utt]) -> str:
    """Sequential pipeline table: backend on the left, stages flowing
    left-to-right (hook fire → daemon accept → synth done → playback start).
    One row per backend × stat (mean/p50/p90/p99/max).
    """
    by_backend: dict[str, list[Utt]] = {}
    for u in utts:
        by_backend.setdefault(u.backend, []).append(u)

    def _ttfs(u: Utt) -> float | None:
        # Prefer backend-reported ms-precision (vibevoice).
        ms = u.extras.get("ttfs_ms")
        if ms is not None:
            return ms / 1000.0
        # Fall back to log-timestamp delta. Honest TTFS for every other
        # backend now that log timestamps have ms precision; for older
        # second-precision rows this is rounded to whole seconds.
        return _play_dt(u)

    stages = [
        ("hook→daemon",      lambda u: u.req_latency),
        ("→synth_done",      _synth_dt),
        ("→playback",        _play_dt),
        ("hook→playback",    _total_to_play),
        ("TTFS",             _ttfs),
    ]
    extras = [
        ("synth_elapsed",    lambda u: u.synth_elapsed),
        ("audio_s",          lambda u: u.audio_s),
        ("rtf",              lambda u: u.rtf),
    ]
    stat_labels = ["mean", "p50", "p90", "p99", "max"]

    def _stats(vals: list[float]) -> list[float | None]:
        if not vals:
            return [None] * 5
        return [
            statistics.fmean(vals),
            percentile(vals, 0.50),
            percentile(vals, 0.90),
            percentile(vals, 0.99),
            max(vals),
        ]

    def _fmt(v: float | None, unit: str) -> str:
        return "—" if v is None else f"{v:.2f}{unit}"

    headers = ["stat", "backend", "n"] + [s[0] for s in stages] + [e[0] for e in extras]
    out: list[str] = [_md_row(headers), _md_row(["---"] * len(headers))]

    backends = sorted(by_backend)
    # Pre-compute every metric's stats per backend
    per_backend_stats: dict[str, dict[str, list[float | None]]] = {}
    for backend in backends:
        group = by_backend[backend]
        d: dict[str, list[float | None]] = {}
        for label, fn in stages + extras:
            vs = [v for v in (fn(u) for u in group) if v is not None]
            d[label] = _stats(vs)
        per_backend_stats[backend] = d

    for i, stat in enumerate(stat_labels):
        for j, backend in enumerate(backends):
            n = len(by_backend[backend])
            row = [stat if j == 0 else "", backend, str(n)]
            for label, _ in stages:
                row.append(_fmt(per_backend_stats[backend][label][i], "s"))
            for label, _ in extras:
                unit = "" if label == "rtf" else "s"
                row.append(_fmt(per_backend_stats[backend][label][i], unit))
            out.append(_md_row(row))

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--log", type=Path, default=DEFAULT_LOG)
    p.add_argument("--since", help="HH:MM, filter to log entries from this time of day")
    p.add_argument("--last", type=int, default=5, help="Show last N utterances per backend in the detail table (default 5)")
    p.add_argument("--backend", help="Filter to a specific backend (moss, qwen3, pocket, ...)")
    p.add_argument("--no-detail", action="store_true", help="Skip per-utterance table")
    p.add_argument(
        "--include-incomplete",
        action="store_true",
        help="Include utterances that never reached synth-done (backend=?)",
    )
    p.add_argument(
        "--exclude-cold",
        action="store_true",
        help="Drop the first utterance per backend after each daemon-ready (cold-start outlier)",
    )
    args = p.parse_args(argv)

    if not args.log.is_file():
        print(f"log not found: {args.log}", file=sys.stderr)
        return 1

    since = None
    if args.since:
        since = parse_ts(args.since + ":00") if args.since.count(":") == 1 else parse_ts(args.since)

    with args.log.open(encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    utts = parse_log(lines, since=since)
    if args.backend:
        utts = [u for u in utts if u.backend == args.backend]
    if not args.include_incomplete:
        utts = [u for u in utts if u.backend != "?"]
    if args.exclude_cold:
        utts = [u for u in utts if not u.extras.get("cold")]

    if not utts:
        print("no utterances found")
        return 0

    if not args.no_detail:
        print(f"### Last {args.last} per backend\n")
        print(render_per_utt(utts, per_backend_last=args.last))
        print()
    print("### Summary")
    print(render_summary(utts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
