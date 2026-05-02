# Observability and Logging

This repo's TTS observability is log-first.

## Current State

What already exists:

- daemon request envelope logs via `[req]`
- playback logs via `[playback]`
- spatial logs via `[spatial]`
- backend-specific synth logs
- hook debug logging in `/tmp/wednesday-tts-hook-debug.log`
- summary analytics via `scripts/analyse_latency.py`

This is enough to compare backends and spot latency regressions, but the log
contracts are still uneven across backends.

## Canonical Analytics Tool

Use:

```bash
uv run python scripts/analyse_latency.py
```

Do not create a parallel latency script for each backend.

If a backend emits a new log marker, update this script instead.

## Minimum Useful Log Contract

Every backend path should make it possible to recover:

- request accepted
- synth started
- first audio available
- synth / playback finished

For comparability, each line should include as many of these as practical:

- `msg_id`
- backend name
- `voice`
- `source`
- short session token or `session_id`
- `audio_s`
- `elapsed_s`
- `rtf`

## Recommended Next Cleanup

The lowest-risk improvement would be to standardize backend lifecycle lines.

Suggested event set:

- `[backend-start]`
- `[backend-first-audio]`
- `[backend-done]`

Suggested common fields:

- `msg_id`
- `backend`
- `voice`
- `source`
- `session`
- `audio_s`
- `elapsed_s`
- `rtf`

That would let `scripts/analyse_latency.py` become simpler and more accurate
without adding a new telemetry stack.

## Why Not Overbuild This Yet

A full metrics subsystem would be heavier than the problem requires.

The repo already gets useful answers from plain logs:

- backend-to-backend latency comparisons
- cold-start vs warm-start differences
- first-audio timing
- audio duration and realtime factor

The next step should be log-shape normalization, not a second observability
platform.
