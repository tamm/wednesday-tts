#!/usr/bin/env bash
# permission-clear.sh — PostToolUse hook
# Removes the permission flag file so the dictation pipeline resumes insertion.

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id','unknown'))" 2>/dev/null)
SESSION_ID="${SESSION_ID:-unknown}"

rm -f "/tmp/wednesday-yarn-permission-${SESSION_ID}"
exit 0
