#!/usr/bin/env python3
"""Parse ~/Library/Logs/Wednesday/tts/daemon.log and emit TTS latency tables.

Tracks each msg_id from request acceptance → first synth output → playback start.

Per-utterance columns (deltas in seconds, all relative to req_accept):
    req_accept       — when the daemon picked up the request (`[req] msg_id=N ... latency=Xs`)
    synth_done_dt    — `[moss]` / `[qwen3]` / `[stream*]` "generated Xs audio in Ys" emit time
    playback_dt      — first `[spatial] ready` after the req
    audio_s          — duration of synthesised audio
    rtf              — synth elapsed / audio_s (extracted from the moss/qwen3 line, else computed)
    backend          — moss | qwen3 | pocket | sam | other
    voice            — voice or preset name
    chars            — request char count
    source           — pre-tool | stop | user | etc.

Summary table: count / mean / p50 / p90 / p99 / max for each delta column,
broken down by backend.

Usage:
    uv run python scripts/analyse_latency.py [--log PATH] [--since HH:MM]
                                             [--last N] [--backend BACKEND]
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


def render_per_utt(utts: list[Utt]) -> str:
    out = []
    out.append(
        f"{'msg':>4} {'backend':<8} {'voice':<18} {'src':<10} {'chars':>5}  "
        f"{'req_lat':>7} {'synth_dt':>8} {'audio':>6} {'rtf':>5} {'play_dt':>7}"
    )
    out.append("-" * 92)
    for u in utts:
        synth_dt = (
            t_delta_s(u.req_ts, u.synth_done_ts) if u.synth_done_ts else None
        )
        play_dt = t_delta_s(u.req_ts, u.playback_ts) if u.playback_ts else None
        out.append(
            f"{u.msg_id:>4} {u.backend:<8} {voice_short(u.voice):<18} {u.source[:10]:<10} "
            f"{u.chars:>5}  {u.req_latency:>7.2f} "
            f"{fmt_delta(synth_dt)} {fmt_delta(u.audio_s):>6} {fmt_rtf(u.rtf)} "
            f"{fmt_delta(play_dt):>7}"
        )
    return "\n".join(out)


def render_summary(utts: list[Utt]) -> str:
    by_backend: dict[str, list[Utt]] = {}
    for u in utts:
        by_backend.setdefault(u.backend, []).append(u)

    lines = []
    lines.append(
        f"{'backend':<10} {'n':>4}  "
        f"{'metric':<14} {'mean':>7} {'p50':>7} {'p90':>7} {'p99':>7} {'max':>7}"
    )
    lines.append("-" * 75)
    metrics = [
        ("req_latency", lambda u: u.req_latency),
        ("synth_dt", lambda u: t_delta_s(u.req_ts, u.synth_done_ts) if u.synth_done_ts else None),
        ("synth_elapsed", lambda u: u.synth_elapsed),
        ("audio_s", lambda u: u.audio_s),
        ("rtf", lambda u: u.rtf),
        ("play_dt", lambda u: t_delta_s(u.req_ts, u.playback_ts) if u.playback_ts else None),
    ]
    for backend in sorted(by_backend):
        group = by_backend[backend]
        n = len(group)
        first_metric = True
        for label, fn in metrics:
            vals = [v for v in (fn(u) for u in group) if v is not None]
            if not vals:
                continue
            row = f"{backend if first_metric else '':<10} {n if first_metric else '':>4}  "
            row += f"{label:<14} "
            row += (
                f"{statistics.fmean(vals):>7.2f} "
                f"{percentile(vals, 0.50):>7.2f} "
                f"{percentile(vals, 0.90):>7.2f} "
                f"{percentile(vals, 0.99):>7.2f} "
                f"{max(vals):>7.2f}"
            )
            lines.append(row)
            first_metric = False
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--log", type=Path, default=DEFAULT_LOG)
    p.add_argument("--since", help="HH:MM, filter to log entries from this time of day")
    p.add_argument("--last", type=int, help="Only consider the last N utterances")
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
    if args.last:
        utts = utts[-args.last :]

    if not utts:
        print("no utterances found")
        return 0

    if not args.no_detail:
        print(render_per_utt(utts))
        print()
    print(render_summary(utts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
