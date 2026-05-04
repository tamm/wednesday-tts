#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
swiftc -O tts-hook.swift -o tts-hook
echo "built: $(file tts-hook)"
