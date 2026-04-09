#!/usr/bin/env bash
# permission-clear.sh — PostToolUse hook
# Removes the permission flag file so the dictation pipeline resumes insertion.
# Also notifies the HUD overlay to clear the permission block.

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id','unknown'))" 2>/dev/null)
SESSION_ID="${SESSION_ID:-unknown}"

rm -f "/tmp/wednesday-yarn-permission-${SESSION_ID}"

# Clear the HUD permission block
OVERLAY_SOCK="/tmp/wednesday-yarn-overlay.sock"
if [ -S "$OVERLAY_SOCK" ]; then
    printf '{"type":"permission_clear"}\n' | nc -U -w1 "$OVERLAY_SOCK" 2>/dev/null &
fi

exit 0
