# Wednesday TTS

Text normalization and speech synthesis service.

## Inclusive terminology

Read `docs/inclusive-terms.md`. No exceptions. The default branch is `main`.

## Current state

- 18 normalization modules under `src/wednesday_tts/normalize/` (including dates/years)
- Server at `src/wednesday_tts/server/` — Flask on localhost:5678, backends extracted
- SAM backend (retro 1982 formant synth) with lowpass + reverb post-processing
- Per-request voice switching via `««...»»` guillemet tags (SAM voice) — mid-sentence backend swaps
- Thin Claude Code hooks in `integrations/claude-code/`
- Client library at `src/wednesday_tts/client/api.py`

## Testing

Python 3.12 (required for ML dependencies).

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## Code style

- Python 3.10+, type hints on public APIs
- `ruff` for linting (config in pyproject.toml)
- Tests use pytest, fixtures in `conftest.py`
- Keep modules focused — one concern per file
- No `master` terminology anywhere

## Normalization rule docs

`docs/normalization/rule-*.md` — each has an Examples table. Use these as test case sources.

## Pocket TTS (primary backend)

GitHub: https://github.com/kyutai-labs/pocket-tts

**Read the README before making changes to the pocket backend.** Predefined voice names (alba, marius, fantine, etc.) are passed directly to `get_state_for_audio_prompt("name")` — do NOT resolve them through `PREDEFINED_VOICES` or construct `hf://` URIs manually.
