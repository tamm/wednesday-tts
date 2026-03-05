#!/usr/bin/env bash
# Stop TTS audio immediately. Run from anywhere.
# Usage: ~/.claude/hooks/stop-tts.sh
#   or:  alias stts=~/.claude/hooks/stop-tts.sh

# Windows: HTTP service on localhost:5678
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "mingw"* || "$OSTYPE" == "cygwin" ]]; then
    curl -s -X POST http://localhost:5678/stop >/dev/null 2>&1
    exit 0
fi

if [[ "$1" == "skip" ]]; then
    python3 -c "
import socket
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    s.connect('/tmp/tts-daemon.sock')
    s.send(b'SKIP')
    s.recv(16)
except: pass
" 2>/dev/null
    exit 0
fi

# Stop audio via SIGUSR1; fallback to socket STOP if no PID file
PID_FILE="/tmp/tts-daemon.pid"
if [[ -f "$PID_FILE" ]]; then
    kill -USR1 "$(cat "$PID_FILE")" 2>/dev/null
fi

# Ping to check health — also serves as socket STOP fallback if SIGUSR1 unavailable
python3 -c "
import socket, json, os
pid_ok = os.path.exists('/tmp/tts-daemon.pid')
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    s.connect('/tmp/tts-daemon.sock')
    # If SIGUSR1 couldn't fire, send STOP via socket
    s.send(b'PING' if pid_ok else b'STOP')
    ok = s.recv(16)
    s.close()
    if ok not in (b'ok',):
        raise Exception()
except Exception:
    print(json.dumps({'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':'TTS unavailable'}}))
" 2>/dev/null
