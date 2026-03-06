# Wednesday TTS

Text normalization and speech synthesis service. This is the canonical home — the code has been fully extracted from `parent-repo`.

## Scope

**Only edit files in this repo.** Do NOT touch `parent-repo` or any other repo.

## Inclusive terminology

Read `docs/inclusive-terms.md`. No exceptions. The default branch is `main`.

## Current state

Phases 1–3 complete. Phase 4 (cutover) in progress.

- 18 normalization modules under `src/wednesday_tts/normalize/` (including dates/years), 489 tests passing
- Server at `src/wednesday_tts/server/` — Flask on localhost:5678, backends extracted
- SAM backend (retro 1982 formant synth) with lowpass + reverb post-processing
- Per-request voice switching via `««...»»` guillemet tags (SAM voice) — mid-sentence backend swaps
- Thin Claude Code hooks in `integrations/claude-code/`
- Client library at `src/wednesday_tts/client/api.py`
- `.venv/` initialized with uv (Python 3.12), package installed
- Hooks installed into `~/.claude/hooks/` via `install.sh`
- Task Scheduler updated to use `.venv/Scripts/pythonw.exe -m wednesday_tts.server.app`

## What to work on

Pick up wherever `PLAN.md` says the next unchecked item is:

1. **Phase 4**: Finish cutover (symlinks, smoke tests)
2. **Phase 5**: Delegate to Claude in `parent-repo` — strip TTS files from that repo, update its CLAUDE.md to point here

## Testing

```bash
.venv/Scripts/python -m pytest
```

Initialize venv if not present:

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Fallback without venv (pythonpath set in pyproject.toml):

```bash
py -3.12 -m pytest
```

## Code style

- Python 3.10+, type hints on public APIs
- `ruff` for linting (config in pyproject.toml)
- Tests use pytest, fixtures in `conftest.py`
- Keep modules focused — one concern per file
- No `master` terminology anywhere

## Normalization rule docs

`docs/normalization/rule-*.md` — each has an Examples table. Use these as test case sources.
