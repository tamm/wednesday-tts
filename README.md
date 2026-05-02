<div align="center">
  <h1>Wednesday TTS</h1>
  <p>Text normalization and speech synthesis for Claude Code and local tooling.</p>
</div>

## What Is Here

Wednesday TTS is two things in one repo:

- a substantial text-normalization pipeline for technical / markdown-heavy text
- a multi-backend TTS runtime with Claude Code hook integrations

The current primary runtime is the macOS Unix-socket daemon in
`src/wednesday_tts/server/daemon.py`. The Windows Flask service in
`src/wednesday_tts/server/app.py` still exists and is useful, but most of the
recent TTS-system work has landed in the daemon path.

## Current Backends

Registered backends:

- `pocket`
- `vibevoice`
- `kokoro`
- `qwen3`
- `moss`
- `sam`
- `soprano`
- `chatterbox`

Practical notes:

- `pocket`
  - default local backend
  - streaming-capable
  - works with predefined names and voice prompt paths
- `vibevoice`
  - streaming-capable
  - supports the repo's newest low-latency playback work
- `sam`
  - retro formant synth
  - default inline `««text»»` robot voice unless overridden

## Runtime Split

### macOS / Claude Code path

```text
Claude hook
  -> JSON over /tmp/tts-daemon.sock
  -> daemon.py
  -> normalize / voice-resolve / render
  -> playback queue or direct-play backend
```

### Windows / HTTP path

```text
HTTP client
  -> localhost:5678
  -> app.py
  -> render + play
```

## Key Docs

- `docs/architecture.md`
  - overall runtime topology
- `docs/voice-pipeline-spec.md`
  - daemon wire protocol and request lifecycle
- `docs/voice-system-spec.md`
  - backend-specific voice behaviour
- `docs/playback-spec.md`
  - queue playback vs direct-play
- `docs/data-flow.md`
  - hook-to-daemon path
- `docs/observability.md`
  - analytics and logging guidance
- `docs/normalization/README.md`
  - normalization rule library

## Observability

Primary analytics script:

```bash
uv run python scripts/analyse_latency.py
```

This is the single supported latency-analysis tool. Extend it when backend log
shapes change.

Current analytics coverage includes the backends recognised by the parser:

- `pocket`
- `vibevoice`
- `kokoro`
- `qwen3`
- `moss`
- `sam`
- `chatterbox`
- `soprano`

## Logging Consistency Direction

The repo already has a decent analytics base now. The main remaining weakness
is inconsistency in backend log shape.

Recommended next step:

1. Standardize per-backend `start`, `first-audio`, and `done` markers.
2. Include `msg_id`, backend, `voice`, `source`, `audio_s`, `elapsed_s`, and
   `rtf` wherever possible.
3. Keep `scripts/analyse_latency.py` as the single parser instead of spawning
   new ad hoc stats tools.

## Install Notes

### Pocket

Read `CLAUDE.md` before changing the pocket backend. Predefined pocket voice
names should be passed straight to `get_state_for_audio_prompt(...)`.

### VibeVoice

VibeVoice is not on PyPI. Install it from a local clone and point it at the
upstream `.pt` voice prompts. See `CLAUDE.md` and `docs/architecture.md` for
the current assumptions.

## Testing

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest -q
uv run ruff check .
```

## Repo Layout

```text
src/wednesday_tts/
  normalize/      normalization pipeline and rules
  server/         macOS daemon, Windows HTTP app, backends
  client/         thin HTTP client
integrations/
  claude-code/    hook clients for the daemon
docs/             architecture, playback, voice, normalization docs
scripts/          analytics and control helpers
tests/            daemon, backend, hook, and normalization coverage
```
