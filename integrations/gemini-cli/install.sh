#!/usr/bin/env bash
# Install Wednesday TTS Gemini CLI hooks.
#
# Creates symlinks in ~/.gemini/hooks/ that point to this repo.
# Re-run this script after pulling changes to update registration.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
HOOKS_DIR="$HOME/.gemini/hooks"
SETTINGS_FILE="$HOME/.gemini/settings.json"
INTEGRATION_DIR="$REPO_DIR/integrations/gemini-cli"

mkdir -p "$HOOKS_DIR"

# Source script
SRC_SCRIPT="$INTEGRATION_DIR/gemini-speak.py"
# Target symlink
LINK_PATH="$HOOKS_DIR/gemini-speak.py"

if [ ! -f "$SRC_SCRIPT" ]; then
    echo "ERROR: source not found: $SRC_SCRIPT"
    exit 1
fi

ln -sf "$SRC_SCRIPT" "$LINK_PATH"
chmod +x "$SRC_SCRIPT"
echo "Symlinked: $LINK_PATH -> $SRC_SCRIPT"

echo ""
echo "Updating $SETTINGS_FILE..."

# Update settings.json using python to register AfterModel, AfterAgent, and Notification
python3 <<EOF
import json
import os

path = os.path.expanduser("$SETTINGS_FILE")
if not os.path.exists(path):
    data = {}
else:
    with open(path, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = {}

if "hooks" not in data:
    data["hooks"] = {}

hook_command = "python3 $LINK_PATH"
events_to_register = ["AfterModel", "AfterAgent", "Notification"]

for event in events_to_register:
    if event not in data["hooks"]:
        data["hooks"][event] = []
    
    # Check if this specific hook is already registered for this event
    already_exists = False
    for entry in data["hooks"][event]:
        for hook in entry.get("hooks", []):
            if "$LINK_PATH" in hook.get("command", ""):
                already_exists = True
                break
    
    if not already_exists:
        hook_entry = {
            "matcher": "*",
            "hooks": [
                {
                    "name": f"wednesday-tts-{event.lower()}",
                    "type": "command",
                    "command": hook_command
                }
            ]
        }
        data["hooks"][event].append(hook_entry)
        print(f"Registered {event} hook.")
    else:
        print(f"{event} hook already registered.")

with open(path, "w") as f:
    json.dump(data, f, indent=2)
EOF

echo ""
echo "Done. REPO_DIR: $REPO_DIR"
echo "Gemini CLI will now speak for: Model Output, Final Agent Response, and System Notifications."

# Warn if TTS server isn't reachable
if command -v curl &>/dev/null; then
    curl -sf --max-time 2 http://localhost:5678/health &>/dev/null \
        && echo "TTS server is running." \
        || echo "WARNING: TTS server not running on localhost:5678. Start it before using hooks."
fi
