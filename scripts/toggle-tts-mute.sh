#!/usr/bin/env bash
# Toggle TTS mute on/off. When muted, the /tmp/tts-mute sentinel is set
# and the daemon (which honours the sentinel) silently drops audio
# without producing sound.
#
# Usage: ~/.claude/hooks/toggle-tts-mute.sh
#   or bind to a global hotkey (Ctrl+Option+Q).

MUTE_FILE="/tmp/tts-mute"

if [[ -f "$MUTE_FILE" ]]; then
    rm -f "$MUTE_FILE"
    # Unmute chime — Submarine (rising tone)
    afplay /System/Library/Sounds/Submarine.aiff &
    echo "TTS unmuted"
else
    touch "$MUTE_FILE"
    # Mute chime — Basso (low tone)
    afplay /System/Library/Sounds/Basso.aiff &
    echo "TTS muted"
fi
