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

DEFAULT_LOG = Path.home() / "Library/Logs/Wednesday/tts/daemon.log"

TS_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})\s")

# 21:44:52 [req] msg_id=1 source=permission ... → voice=fantine ... 18 chars latency=0.02s
PAT_REQ = re.compile(
    r"^(\d{2}:\d{2}:\d{2})\s+\[req\]\s+msg_id=(\d+)\s+source=(\S+).*?voice=(\S+).*?(\d+)\s+chars\s+latency=([\d.]+)s"
)
# 21:44:53 [stream-pipe] generated 1.7s audio in 0.5s, waiting for stretch
# 07:59:48 [moss] generated 3.3s audio in 4.3s (RTF 1.31, preset=Junhao)
# 21:44:53 [qwen3] generated 7.6s audio in 2.7s (RTF 0.36, ref=...)
PAT_GEN = re.compile(
    r"^(\d{2}:\d{2}:\d{2})\s+\[(stream(?:-pipe|-direct)?|moss|qwen3|pocket|kokoro|sam)\][^\n]*?generated\s+([\d.]+)s\s+audio\s+in\s+([\d.]+)s"
)
PAT_RTF = re.compile(r"RTF\s+([\d.]+)")
# 21:44:53 [spatial] [SpatialStream] ready rate=48000.0 pan=0.47 device=...
PAT_SPATIAL = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+\[spatial\]\s+\[SpatialStream\]\s+ready")
# 21:44:53 [playback] opening PortAudio stream / PortAudio stream opened
PAT_PLAYBACK_OPEN = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+\[playback\]\s+(opening|PortAudio stream opened)")


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
    try:
        return datetime.strptime(s, "%H:%M:%S").time()
    except ValueError:
        return None


def t_delta_s(a: time, b: time) -> float:
    """b - a in seconds (handles same-day only; negative if log wraps midnight)."""
    da = datetime.combine(datetime.today(), a)
    db = datetime.combine(datetime.today(), b)
    return (db - da).total_seconds()


def parse_log(lines, since: time | None = None) -> list[Utt]:
    utts: dict[int, Utt] = {}
    last_msg_id: int | None = None

    for line in lines:
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
            backend_hint = "?"
            voice = m.group(4)
            # Backend will be filled in from the [moss]/[qwen3]/[stream-pipe] line later.
            utts[msg_id] = Utt(
                msg_id=msg_id,
                req_ts=ts,
                source=m.group(3),
                voice=voice,
                chars=int(m.group(5)),
                req_latency=float(m.group(6)),
                backend=backend_hint,
            )
            last_msg_id = msg_id
            continue

        m = PAT_GEN.match(line)
        if m and last_msg_id is not None and last_msg_id in utts:
            u = utts[last_msg_id]
            backend_token = m.group(2)
            if backend_token.startswith("stream"):
                # Pocket emits [stream-pipe]/[stream-direct]; keep that distinction.
                u.backend = "pocket"
            else:
                u.backend = backend_token
            u.audio_s = float(m.group(3))
            u.synth_elapsed = float(m.group(4))
            u.synth_done_ts = ts
            rtf_m = PAT_RTF.search(line)
            if rtf_m:
                u.rtf = float(rtf_m.group(1))
            elif u.audio_s and u.synth_elapsed:
                u.rtf = u.synth_elapsed / u.audio_s
            continue

        m = PAT_SPATIAL.match(line) or PAT_PLAYBACK_OPEN.match(line)
        if m and last_msg_id is not None and last_msg_id in utts:
            u = utts[last_msg_id]
            if u.playback_ts is None:
                u.playback_ts = ts

    return [u for _, u in sorted(utts.items())]


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

    stages = [
        ("hook→daemon",      lambda u: u.req_latency),
        ("→synth_done",      _synth_dt),
        ("→playback",        _play_dt),
        ("hook→playback",    _total_to_play),
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
