#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
swiftc -O -target arm64-apple-macosx13.0 tts-hook.swift -o tts-hook
echo "built: $(file tts-hook)"
