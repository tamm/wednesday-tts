#!/usr/bin/env python3
"""Claude Code Stop hook — thin client for Wednesday TTS.

Reads the JSON hook payload from stdin, extracts the last assistant
message, and sends a JSON speak request to the Wednesday TTS daemon.
All heavy lifting (voice selection, normalisation, chunking, synthesis)
happens in the daemon.

Shared behaviour (mute, barge-in, sub-agent filter, voice hash, pan,
socket send) lives in hook_common.py — both speech hooks import from
there so they can never drift out of sync again.

Environment variables:
    TTS_MUTE=1   Disable TTS (also honoured via /tmp/tts-mute file)
"""

import json
import os
import sys
import time

from hook_common import (
    compute_pan,
    compute_voice_hash,
    is_muted,
    is_subagent,
    log_payload_debug,
    send_speak,
)


def _get_last_assistant_message(transcript_path: str | None) -> str:
    """Fallback: extract the last assistant message from the JSONL transcript.

    Claude Code writes transcripts as newline-delimited JSON, one message
    per line. Parse line-by-line — `json.load` on the whole file fails
    silently and leaves TTS mute for the turn.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        messages = []
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        for msg in reversed(messages):
            if msg.get("type") != "assistant":
                continue
            content = msg.get("message", {}).get("content", "")
            if isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                text = " ".join(p for p in parts if p).strip()
                if text:
                    return text
            elif isinstance(content, str) and content.strip():
                return content.strip()
    except Exception:
        pass
    return ""


def main() -> None:
    wall_time = time.time()

    if is_muted():
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    log_payload_debug(payload, "speak-response")

    if is_subagent(payload):
        sys.exit(0)

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "") or os.getcwd()

    # Prefer the inline payload field — the transcript file can lag.
    text = payload.get("last_assistant_message") or _get_last_assistant_message(
        payload.get("transcript_path")
    )
    if not text or len(text.strip()) < 5:
        sys.exit(0)

    msg: dict = {
        "command": "speak",
        "text": text,
        "normalization": "markdown",
        "voice_hash": compute_voice_hash(cwd),
        "timestamp": wall_time,
    }
    if session_id:
        msg["session_id"] = session_id
    pan = compute_pan()
    if pan != 0.5:
        msg["pan"] = pan

    try:
        send_speak(msg, kick_on_timeout=True)
    except Exception as exc:
        print(f"wednesday-tts hook: {exc}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
