<div align="center">
  <img src="docs/logo.png" alt="Wednesday TTS" width="180" />
  <h1>Wednesday TTS</h1>
  <p>Text normalization and speech synthesis for Claude Code.<br>
  Raw markdown in → natural spoken audio out.</p>
</div>

---

## Quickstart

```bash
git clone https://github.com/tamm/wednesday-tts
cd wednesday-tts
bash quickstart.sh
```

The script detects Python, creates a venv, installs Pocket TTS with the bundled DWP Aussie
male voice, writes `~/.claude/tts-config.json`, and offers to wire up the Claude Code hooks
and auto-start.

Requires Python 3.10+. Works on macOS, Linux, and Windows (Git Bash / MSYS2).

---

## How it works

```
Claude response
  → thin hook         integrations/claude-code/speak-response.py
  → POST /speak       localhost:5678
  → normalize         src/wednesday_tts/normalize/pipeline.py
  → synthesize        src/wednesday_tts/server/backends/
  → audio plays
```

---

## Manual setup

<details>
<summary>If you prefer to set up by hand</summary>

**Venv + install**

```bash
# Pocket TTS — default, uses bundled DWP voice
uv venv --python 3.12
uv pip install -e ".[pocket]"

# Kokoro — built-in named voices, no voice file needed
uv pip install -e ".[kokoro]"

# Both + dev tools
uv pip install -e ".[pocket,kokoro,dev]"
```

**Config**

```bash
cp config/tts-config-template.json ~/.claude/tts-config.json
```

| Key              | Value                                                                 |
| ---------------- | --------------------------------------------------------------------- |
| `active_model`   | `pocket` (default) or `kokoro`                                        |
| `voice` (pocket) | Path to a `.safetensors` file — bundled DWP voice is at `voices/dwp/` |
| `voice` (kokoro) | Built-in name e.g. `af_bella`                                         |

**Run the server**

```bash
.venv/bin/python -m wednesday_tts.server.app    # macOS / Linux
.venv/Scripts/python -m wednesday_tts.server.app  # Windows
```

Server listens on `localhost:5678`.

**Auto-start**

- Windows: elevated PowerShell → `scripts/install-tts-service.ps1`
- macOS: `bash quickstart.sh` handles launchd, or see `config/com.anthropic.wednesday-tts.plist`

**Claude Code hooks**

```bash
bash integrations/claude-code/install.sh
```

Then add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{ "command": "python ~/.claude/hooks/speak-response.py" }],
    "PreToolUse": [{ "command": "python ~/.claude/hooks/pre-tool-speak.py" }]
  }
}
```

See [`integrations/claude-code/README.md`](integrations/claude-code/README.md) for full details.

</details>

---

## Voice setup

The DWP Aussie male voice is bundled at `voices/dwp/`. The quickstart uses it automatically.

To use a different voice, point `voice` in `~/.claude/tts-config.json` at any `.safetensors` file.

---

## Testing

```bash
.venv/Scripts/python -m pytest   # Windows
.venv/bin/python -m pytest       # macOS / Linux
```

---

## Project layout

```
src/wednesday_tts/
  normalize/      17 normalization modules + pipeline
  server/         Flask HTTP server + backends (pocket, kokoro, soprano, chatterbox)
  client/         Thin HTTP client library
  platform.py     Cross-platform helpers
integrations/
  claude-code/    Hooks + install script
data/             tts-dictionary.json, tts-filenames.json
config/           Config template + macOS plist
scripts/          Start/stop/install scripts
docs/normalization/  Rule library (15 rule docs)
tests/            406 tests
```

---

## Credits

[Kyutai Labs](https://github.com/kyutai-labs) — creators of
[Pocket TTS](https://github.com/kyutai-labs/pocket-tts), the default backend.
Brilliant lightweight TTS that runs on CPU with voice cloning support.
Several streaming and chunking patterns in this project are drawn from their work.

Rope logo by Tamm, generated with AI assistance.
