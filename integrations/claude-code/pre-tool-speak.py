#!/usr/bin/env python3
"""Claude Code PreToolUse hook — speaks mid-turn assistant text.

Claude often writes a sentence before running a tool (e.g. "Let me check
that."). The Stop hook only fires at the end of a full turn, so those
mid-turn messages would otherwise be dropped. This hook fires before
every tool call, extracts assistant text blocks from the current turn,
and sends them to the daemon. Dedup happens server-side via the daemon's
ring buffer.

Shared behaviour (mute, barge-in, sub-agent filter, voice hash, pan,
socket send) lives in hook_common.py — both speech hooks import from
there so they can never drift out of sync again.
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

MAX_CHARS = 2400
MIN_SENTENCE_CUT = 1200


def _get_unsent_assistant_texts(transcript_path: str | None) -> list[str]:
    """Return raw text blocks for assistant messages since the last user turn.

    Dedup is handled server-side by the daemon's ring buffer — this hook
    just extracts all assistant text blocks after the last user message.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return []

    messages = []
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                msg = json.loads(line.strip())
            except (json.JSONDecodeError, KeyError):
                continue
            if msg.get("type") in ("assistant", "user"):
                messages.append(msg)

    last_user_idx = -1
    for i, msg in enumerate(messages):
        if msg.get("type") == "user":
            last_user_idx = i
    if last_user_idx < 0:
        return []

    texts = []
    for msg in messages[last_user_idx + 1 :]:
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", "")
        if isinstance(content, list):
            for block in content:
                if block.get("type") != "text":
                    continue
                raw = block.get("text", "").strip()
                if raw:
                    texts.append(raw)
        elif isinstance(content, str) and content.strip():
            texts.append(content.strip())
    return texts


def _truncate_at_sentence(text: str) -> str:
    """Cap combined text at MAX_CHARS, preferring a sentence boundary."""
    if len(text) <= MAX_CHARS:
        return text
    trunc = text[:MAX_CHARS]
    last_sentence = max(trunc.rfind(". "), trunc.rfind("! "), trunc.rfind("? "))
    if last_sentence > MIN_SENTENCE_CUT:
        return text[: last_sentence + 1]
    last_space = trunc.rfind(" ")
    return text[:last_space] if last_space > 0 else trunc


def main() -> None:
    if is_muted():
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    log_payload_debug(payload, "pre-tool-speak")

    if is_subagent(payload):
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd", "")
    transcript_path = payload.get("transcript_path")

    # Transcript may not be flushed yet when PreToolUse fires.
    # Poll briefly before giving up.
    texts: list[str] = []
    for _attempt in range(6):
        texts = _get_unsent_assistant_texts(transcript_path)
        if texts:
            break
        time.sleep(0.15)
    if not texts:
        sys.exit(0)
    combined = " ".join(texts).strip()
    if len(combined) < 5:
        sys.exit(0)
    combined = _truncate_at_sentence(combined)

    msg: dict = {
        "command": "speak",
        "text": combined,
        "normalization": "markdown",
        "session_id": session_id,
        "timestamp": time.time(),
        "source": "pre-tool",
    }
    if cwd:
        msg["voice_hash"] = compute_voice_hash(cwd)
    pan = compute_pan()
    if pan != 0.5:
        msg["pan"] = pan

    try:
        send_speak(msg)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": f"TTS unavailable: {exc}",
                    }
                }
            )
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
