#!/usr/bin/env bash
# Wednesday TTS — Quickstart
# Detects Python, creates venv, installs Kokoro, wires up Claude Code hooks.
# Works on macOS, Linux, Windows (Git Bash / MSYS2).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Convert MSYS /c/dev/... → C:/dev/... for Python path compatibility
REPO_DIR_PY="$REPO_DIR"
if [[ "$REPO_DIR" =~ ^/[a-zA-Z]/ ]]; then
    REPO_DIR_PY="$(echo "$REPO_DIR" | sed 's|^/\([a-zA-Z]\)/|\1:/|')"
fi

OS="$(uname -s)"
HOOKS_DIR="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"
CONFIG_DEST="$HOME/.claude/tts-config.json"

ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*"; exit 1; }
ask()  { printf '\033[36m?\033[0m %s ' "$*"; read -r REPLY; }
hr()   { echo "────────────────────────────────────────"; }

hr
echo "  Wednesday TTS — Quickstart"
hr
echo ""

# ── 1. Find Python ────────────────────────────────────────────────────────────

find_python() {
    for candidate in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" &>/dev/null; then
            local ver
            ver=$("$candidate" -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>/dev/null) || continue
            local major minor
            IFS='.' read -r major minor <<< "$ver"
            if (( major >= 3 && minor >= 10 )); then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

if PYTHON=$(find_python); then
    VER=$("$PYTHON" -c "import sys; print('%d.%d' % sys.version_info[:2])")
    ok "Python $VER at: $PYTHON"
else
    warn "Python 3.10+ not found on PATH."
    ask "Path to Python 3.10+ binary (or Enter to abort):"
    [[ -z "${REPLY:-}" ]] && die "Python 3.10+ required — install from https://python.org and re-run."
    PYTHON="$REPLY"
    "$PYTHON" -c "import sys; assert sys.version_info >= (3,10)" 2>/dev/null \
        || die "That Python is too old (need 3.10+)."
    ok "Using: $PYTHON"
fi

# ── 2. Create venv ────────────────────────────────────────────────────────────

VENV_DIR="$REPO_DIR/.venv"

if [[ -d "$VENV_DIR/bin" ]] || [[ -d "$VENV_DIR/Scripts" ]]; then
    ok "Venv already exists — skipping creation"
else
    if command -v uv &>/dev/null; then
        echo "Creating venv with uv..."
        uv venv --python "$PYTHON" "$VENV_DIR"
    else
        echo "Creating venv with venv module..."
        "$PYTHON" -m venv "$VENV_DIR"
    fi
    ok "Venv created at .venv"
fi

# Pick the right python/pip paths for this OS
if [[ -f "$VENV_DIR/Scripts/python.exe" ]]; then
    VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
    VENV_PYTHON_PY="${REPO_DIR_PY}/.venv/Scripts/python.exe"
elif [[ -f "$VENV_DIR/Scripts/python" ]]; then
    VENV_PYTHON="$VENV_DIR/Scripts/python"
    VENV_PYTHON_PY="${REPO_DIR_PY}/.venv/Scripts/python"
else
    VENV_PYTHON="$VENV_DIR/bin/python"
    VENV_PYTHON_PY="${REPO_DIR_PY}/.venv/bin/python"
fi

# ── 3. Install package ────────────────────────────────────────────────────────

echo "Installing wednesday-tts[kokoro]..."
if command -v uv &>/dev/null; then
    uv pip install --python "$VENV_PYTHON" -e "$REPO_DIR[kokoro]"
else
    "$VENV_PYTHON" -m pip install -q -e "$REPO_DIR[kokoro]"
fi
ok "Package installed (Kokoro backend, built-in voices)"

# ── 4. Write tts-config.json ──────────────────────────────────────────────────

if [[ -f "$CONFIG_DEST" ]]; then
    warn "~/.claude/tts-config.json already exists — not overwriting."
    warn "Make sure active_model=kokoro and venv_path points to $VENV_PYTHON_PY"
else
    mkdir -p "$(dirname "$CONFIG_DEST")"
    "$VENV_PYTHON" - <<PYEOF
import json, os
config = {
    "active_model": "kokoro",
    "max_chars": 500,
    "models": {
        "kokoro": {
            "venv_path": "$VENV_PYTHON_PY",
            "voice": "af_bella",
            "speed": 1.3,
            "samplerate": 24000
        }
    }
}
with open("$CONFIG_DEST", "w") as f:
    json.dump(config, f, indent=2)
PYEOF
    ok "Config written: ~/.claude/tts-config.json  (voice: af_bella)"
fi

echo ""
hr

# ── 5. Claude Code hooks ──────────────────────────────────────────────────────

ask "Install Claude Code hooks (speak-response + pre-tool-speak)? [Y/n]"
if [[ "${REPLY:-y}" =~ ^[Yy]?$ ]]; then
    bash "$REPO_DIR/integrations/claude-code/install.sh"
    ok "Hooks installed to ~/.claude/hooks/"

    # Patch settings.json
    if [[ -f "$SETTINGS" ]]; then
        ask "Register hooks in ~/.claude/settings.json? [Y/n]"
        if [[ "${REPLY:-y}" =~ ^[Yy]?$ ]]; then
            "$VENV_PYTHON" - <<PYEOF
import json, sys

path = "$SETTINGS"
try:
    with open(path) as f:
        cfg = json.load(f)
except Exception as e:
    print(f"  Could not read settings.json: {e}")
    sys.exit(1)

hooks = cfg.setdefault("hooks", {})
added = []

stop = hooks.setdefault("Stop", [])
cmd_stop = "python3 ~/.claude/hooks/speak-response.py"
if not any(h.get("command") == cmd_stop for h in stop):
    stop.append({"command": cmd_stop})
    added.append("Stop")

pre = hooks.setdefault("PreToolUse", [])
cmd_pre = "python3 ~/.claude/hooks/pre-tool-speak.py"
if not any(h.get("command") == cmd_pre for h in pre):
    pre.append({"command": cmd_pre})
    added.append("PreToolUse")

with open(path, "w") as f:
    json.dump(cfg, f, indent=2)

if added:
    print(f"  Registered hooks: {', '.join(added)}")
else:
    print("  Hooks already registered — nothing changed.")
PYEOF
            ok "settings.json updated"
        fi
    else
        warn "~/.claude/settings.json not found — create it with:"
        echo '  {'
        echo '    "hooks": {'
        echo '      "Stop":       [{ "command": "python3 ~/.claude/hooks/speak-response.py" }],'
        echo '      "PreToolUse": [{ "command": "python3 ~/.claude/hooks/pre-tool-speak.py" }]'
        echo '    }'
        echo '  }'
    fi
fi

echo ""
hr

# ── 6. Auto-start ────────────────────────────────────────────────────────────

ask "Set up auto-start for the TTS server? [Y/n]"
if [[ "${REPLY:-y}" =~ ^[Yy]?$ ]]; then
    if [[ "$OS" == "Darwin" ]]; then
        PLIST_SRC="$REPO_DIR/config/com.anthropic.wednesday-tts.plist"
        PLIST_DEST="$HOME/Library/LaunchAgents/com.anthropic.wednesday-tts.plist"
        sed \
            -e "s|/REPLACE_WITH_VENV_PATH/bin/python|$VENV_DIR/bin/python|g" \
            -e "s|REPLACE_WITH_VENV_PATH|$VENV_DIR|g" \
            -e "s|REPLACE_WITH_REPO_PATH|$REPO_DIR|g" \
            "$PLIST_SRC" > "$PLIST_DEST"
        launchctl bootstrap gui/$(id -u) "$PLIST_DEST"
        ok "launchd agent loaded — TTS starts at login"
        echo "  Logs: /tmp/wednesday-tts.log  /tmp/wednesday-tts.err"
    elif [[ "$OS" == MINGW* ]] || [[ "$OS" == CYGWIN* ]] || [[ "$OS" == MSYS* ]]; then
        warn "Windows Task Scheduler setup needs an elevated PowerShell."
        echo "  Open PowerShell as Administrator and run:"
        echo "  scripts/install-tts-service.ps1"
    else
        warn "Linux: add a systemd service or crontab @reboot entry."
        echo "  Command: $VENV_PYTHON -m wednesday_tts.server.app"
    fi
else
    echo ""
    echo "  Start manually:"
    if [[ "$OS" == "Darwin" ]] || [[ "$OS" == "Linux" ]]; then
        echo "  .venv/bin/python -m wednesday_tts.server.app"
    else
        echo "  .venv/Scripts/python -m wednesday_tts.server.app"
    fi
fi

echo ""
hr

# ── 7. Smoke test ─────────────────────────────────────────────────────────────

if command -v curl &>/dev/null; then
    if curl -sf --max-time 2 http://localhost:5678/health &>/dev/null; then
        ok "TTS server is running on localhost:5678"
        curl -s --max-time 5 -X POST http://localhost:5678/speak \
            -H "Content-Type: application/json" \
            -d '{"text":"Wednesday TTS is ready."}' &>/dev/null \
            && ok "Spoke test phrase via /speak" \
            || warn "/speak returned an error — check server logs"
    else
        warn "TTS server not running yet. Start it, then test:"
        echo "  curl http://localhost:5678/health"
    fi
fi

echo ""
echo "  All done. See integrations/claude-code/README.md for advanced options."
hr
