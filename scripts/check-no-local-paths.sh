#!/usr/bin/env bash
# Blocks commits containing absolute local paths like /Users/<name>/ or /home/<name>/
# ~/... paths are fine. /Users/Shared or similar non-personal paths are still flagged.

set -euo pipefail

# Get the staged diff (new content only, no context)
DIFF=$(git diff --cached --diff-filter=ACMR -- . ':(exclude).git' | grep '^+' || true)
DIFF=$(echo "$DIFF" | grep -v '^+++' || true)

# Match /Users/<word>/ or /home/<word>/ with a real username (not angle-bracket placeholders)
PATTERN='/(Users|home)/[a-zA-Z0-9_][^/ ]*/'
if echo "$DIFF" | grep -qE "$PATTERN"; then
    echo "ERROR: Staged diff contains absolute local paths."
    echo ""
    echo "Offending lines:"
    echo "$DIFF" | grep -En "$PATTERN" | head -20
    echo ""
    echo "Replace with relative paths or ~/ equivalents."
    exit 1
fi

exit 0
