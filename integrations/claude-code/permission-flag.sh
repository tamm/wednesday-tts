#!/usr/bin/env bash
# permission-flag.sh — PermissionRequest hook
# Touches /tmp/wednesday-yarn-permission-{session_id} so the dictation pipeline
# can detect that a permission prompt is visible and hold text insertion.

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id','unknown'))" 2>/dev/null)
SESSION_ID="${SESSION_ID:-unknown}"

touch "/tmp/wednesday-yarn-permission-${SESSION_ID}"
exit 0
