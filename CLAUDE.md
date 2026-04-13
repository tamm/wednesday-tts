# Wednesday TTS

Text normalization and speech synthesis service.

## Inclusive terminology

Read `docs/inclusive-terms.md`. No exceptions. The default branch is `main`.

## Current state

- 18 normalization modules under `src/wednesday_tts/normalize/` (including dates/years)
- Server at `src/wednesday_tts/server/` — Flask on localhost:5678, backends extracted
- SAM backend (retro 1982 formant synth) with lowpass + reverb post-processing
- Voice pipeline: pool-based voice selection, inline guillemet switching — see `docs/voice-pipeline-spec.md`
- Thin Claude Code hooks in `integrations/claude-code/`
- Client library at `src/wednesday_tts/client/api.py`

## Testing

Python 3.12 (required for ML dependencies).

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest
```

## Pre-push quality gate

Before every `git push`, you MUST run and pass:

```bash
uv run pytest -q
uv run ruff check .
```

Do NOT push if either fails. Do NOT bypass with `--no-verify`. Do NOT stub or skip tests to make CI pass — if a test needs a dep, add the dep to `[test]` extras. Local and CI must run the same tests with the same deps.

## Code style

- Python 3.10+, type hints on public APIs
- `ruff` for linting and formatting (config in pyproject.toml)
- `bandit` for security linting (SAST)
- `gitleaks` for secret detection (pre-commit hook)
- Tests use pytest, fixtures in `conftest.py`
- Keep modules focused — one concern per file
- No `master` terminology anywhere
- No absolute local paths (`/Users/...`, `/home/...`) in committed code — use `~` or `os.path.expanduser`

## Normalization rule docs

`docs/normalization/rule-*.md` — each has an Examples table. Use these as test case sources.

## Spatial audio

`docs/spatial-audio.md` — feature plan for positional stereo panning based on terminal window location.

## TTS daemon restart

You can restart the TTS daemon freely whenever needed (after code changes, config changes, debugging) using:

```bash
launchctl kickstart -k gui/$(id -u)/com.tamm.wednesday-tts
```

No need to ask first. This command is always allowed.

## Voice pipeline

**Read `docs/voice-pipeline-spec.md` before making any changes to voice selection, the wire protocol, guillemet tags, or the speak-response hook.** That spec is the source of truth for how voices are chosen and how hooks communicate with the daemon.

## Pocket TTS (primary backend)

GitHub: https://github.com/kyutai-labs/pocket-tts

**Read the README before making changes to the pocket backend.** Predefined voice names (alba, marius, fantine, etc.) are passed directly to `get_state_for_audio_prompt("name")` — do NOT resolve them through `PREDEFINED_VOICES` or construct `hf://` URIs manually.
