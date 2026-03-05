#!/usr/bin/env bash
# Toggle TTS mute on/off. When muted, all TTS hooks exit silently
# and the daemon is fully unloaded from launchd. On unmute, it's reloaded.
# Usage: ~/.claude/hooks/toggle-tts-mute.sh
#   or bind to a global hotkey (Ctrl+Option+Q).

MUTE_FILE="/tmp/tts-mute"
PID_FILE="/tmp/tts-daemon.pid"
SOCKET_PATH="/tmp/tts-daemon.sock"
LOCK_PATH="/tmp/tts-daemon.lock"
SUPPRESS_PATH="/tmp/dictation-suppress"
PLIST_LABEL="com.tamm.wednesday-tts"
PLIST_PATH="$HOME/Library/LaunchAgents/com.tamm.wednesday-tts.plist"
GUI_TARGET="gui/$(id -u)"

if [[ -f "$MUTE_FILE" ]]; then
    rm -f "$MUTE_FILE"
    # Reload daemon into launchd
    launchctl bootstrap "$GUI_TARGET" "$PLIST_PATH" 2>/dev/null
    # Unmute chime — Submarine (rising tone)
    afplay /System/Library/Sounds/Submarine.aiff &
    echo "TTS unmuted — daemon loading"
else
    touch "$MUTE_FILE"
    # Unload daemon from launchd (kills process and prevents restart)
    launchctl bootout "$GUI_TARGET/$PLIST_LABEL" 2>/dev/null
    # Clean up stale files
    rm -f "$SOCKET_PATH" "$PID_FILE" "$LOCK_PATH" "$SUPPRESS_PATH"
    # Mute chime — Basso (low tone)
    afplay /System/Library/Sounds/Basso.aiff &
    echo "TTS muted — daemon unloaded"
fi
