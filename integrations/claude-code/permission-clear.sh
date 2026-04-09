#!/usr/bin/env bash
# permission-clear.sh — PostToolUse / UserPromptSubmit hook
# Removes ALL permission flag files so the dictation pipeline resumes insertion.
# Clears all sessions because any user action means the hold is no longer needed.

rm -f /tmp/wednesday-yarn-permission-*

# Clear the HUD permission block
OVERLAY_SOCK="/tmp/wednesday-yarn-overlay.sock"
if [ -S "$OVERLAY_SOCK" ]; then
    printf '{"type":"permission_clear"}\n' | nc -U -w1 "$OVERLAY_SOCK" 2>/dev/null &
fi

exit 0
