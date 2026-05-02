# Claude Code Hooks for Wednesday TTS

These hooks are thin clients for the macOS daemon in
`src/wednesday_tts/server/daemon.py`.

## Hooks

### `speak-response.py`

- Claude Code `Stop` hook
- speaks the final assistant response for the turn
- sets `flush_session=true` so stale pre-tool audio from the same session gets
  flushed before the final response is read

### `pre-tool-speak.py`

- Claude Code `PreToolUse` hook
- speaks assistant text emitted before a tool call
- extracts assistant text blocks from the transcript and sends them to the
  daemon

## Shared Behaviour

Both hooks import `hook_common.py` for:

- mute handling
- sub-agent / teammate suppression
- repo-based `voice_hash`
- stereo `pan`
- Unix-socket send logic

This is intentional. If hook behaviour must match, put it there instead of
copying logic into each hook.

## Transport

Primary path on macOS:

- Unix socket: `/tmp/tts-daemon.sock`
- payload: newline-terminated JSON

The hooks do not POST to `localhost:5678`. That HTTP path is for the Windows
service and the legacy Python client.

## Install

```bash
bash integrations/claude-code/install.sh
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

## Runtime Expectations

The daemon must be running:

```bash
python -m wednesday_tts.server.daemon
```

On macOS, launchd is the normal way to keep it running.

## launchd

Install the plist from `config/com.tamm.wednesday-tts.plist`, then restart with:

```bash
launchctl kickstart -k gui/$(id -u)/com.tamm.wednesday-tts
```

## Debugging

Useful files:

- daemon socket: `/tmp/tts-daemon.sock`
- daemon pid: `/tmp/tts-daemon.pid`
- hook debug log: `/tmp/wednesday-tts-hook-debug.log`

Useful commands:

```bash
bash scripts/stop-tts.sh
python - <<'PY'
import json, socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect('/tmp/tts-daemon.sock')
s.sendall((json.dumps({"command":"ping"}) + "\n").encode())
print(s.recv(64).decode())
PY
```
