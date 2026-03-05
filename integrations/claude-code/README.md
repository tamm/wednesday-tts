# Claude Code Hooks for Wednesday TTS

Two hooks wire Claude Code's response events into the Wednesday TTS server.

## Hooks

### speak-response.py

Fires on the `Stop` event — the end of each assistant turn. It reads the
assistant's final response, runs it through the normalization pipeline, and
sends the cleaned text to the TTS server to be spoken.

This hook handles the bulk of the work: markdown stripping, code block
truncation, table flattening, URL and path expansion, number pronunciation,
and CamelCase splitting.

### pre-tool-speak.py

Fires on `PreToolUse` — before every tool call within a turn. Claude often
writes a sentence before invoking a tool ("Let me check that."). Because the
`Stop` hook only fires at the end of a full turn, those mid-turn sentences
would otherwise be skipped. This hook tracks which assistant text blocks have
already been spoken (via a per-session hash file in `/tmp`) and speaks any
that haven't been read out yet.

## Prerequisites

The Wednesday TTS server must be running before any output will be spoken:

```
wednesday-tts
```

or on Windows, via Task Scheduler / a terminal:

```
python -m wednesday_tts.server.app
```

The server listens on `localhost:5678`. Both hooks POST to `/speak` on that
address. If the server is not running, hooks fail silently — Claude Code
continues normally, just without audio.

## Install

Run the install script from this directory:

```bash
bash integrations/claude-code/install.sh
```

Or create the symlinks manually:

```bash
ln -sf "$(pwd)/integrations/claude-code/speak-response.py" ~/.claude/hooks/speak-response.py
ln -sf "$(pwd)/integrations/claude-code/pre-tool-speak.py" ~/.claude/hooks/pre-tool-speak.py
```

Then register the hooks in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{ "command": "python ~/.claude/hooks/speak-response.py" }],
    "PreToolUse": [{ "command": "python ~/.claude/hooks/pre-tool-speak.py" }]
  }
}
```

## Uninstall

```bash
rm ~/.claude/hooks/speak-response.py
rm ~/.claude/hooks/pre-tool-speak.py
```

Remove the corresponding entries from `~/.claude/settings.json`.

## macOS (launchd)

On macOS the server can be managed as a launchd user agent so it starts
automatically at login.

### Setup

Copy the plist template from the repo:

```bash
cp config/com.tamm.wednesday-tts.plist ~/Library/LaunchAgents/com.tamm.wednesday-tts.plist
```

Edit the copy and replace both placeholders:

- `REPLACE_WITH_VENV_PATH` — absolute path to the `.venv` directory inside
  the repo, e.g. `/Users/yourname/dev/wednesday-tts/.venv`
- `REPLACE_WITH_REPO_PATH` — absolute path to the repo root, e.g.
  `/Users/yourname/dev/wednesday-tts`

### Start

```bash
launchctl load ~/Library/LaunchAgents/com.tamm.wednesday-tts.plist
```

### Stop

```bash
launchctl unload ~/Library/LaunchAgents/com.tamm.wednesday-tts.plist
```

### Restart

```bash
launchctl kickstart -k gui/$(id -u)/com.tamm.wednesday-tts
```

### Socket vs HTTP

On macOS the daemon uses a Unix socket at `/tmp/tts-daemon.sock`, not HTTP.
The Claude Code hooks connect via that socket automatically. The `localhost:5678`
HTTP interface is a Windows/Linux path; on macOS the socket transport is used
instead.

### Logs

```
/tmp/wednesday-tts.log   stdout
/tmp/wednesday-tts.err   stderr
```
