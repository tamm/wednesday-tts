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
    log_hook_event,
    log_payload_debug,
    send_speak,
)

MAX_CHARS = 2400
MIN_SENTENCE_CUT = 1200
TRANSCRIPT_POLL_ATTEMPTS = 12
TRANSCRIPT_POLL_SLEEP_S = 0.2


def _extract_text_blocks(content) -> list[str]:
    """Extract plain text blocks from common Claude transcript / payload shapes."""
    texts: list[str] = []
    if isinstance(content, str):
        raw = content.strip()
        if raw:
            texts.append(raw)
        return texts
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            raw = block.get("text", "").strip()
            if raw:
                texts.append(raw)
    return texts


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

    assistant_messages = [msg for msg in messages if msg.get("type") == "assistant"]
    if not assistant_messages:
        return []

    last_user_idx = -1
    for i, msg in enumerate(messages):
        if msg.get("type") == "user":
            last_user_idx = i
    relevant = messages[last_user_idx + 1 :] if last_user_idx >= 0 else assistant_messages

    texts = []
    for msg in relevant:
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", msg.get("content", ""))
        texts.extend(_extract_text_blocks(content))
    return texts


def _get_payload_assistant_text(payload: dict) -> str:
    """Fallback extraction from the live hook payload when transcript text lags."""
    candidate = payload.get("last_assistant_message")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()

    data_message = payload.get("data", {}).get("message", {})
    if isinstance(data_message, dict):
        extracted = _extract_text_blocks(data_message.get("content"))
        if extracted:
            return " ".join(extracted).strip()

    message = payload.get("message", {})
    if isinstance(message, dict):
        extracted = _extract_text_blocks(message.get("content"))
        if extracted:
            return " ".join(extracted).strip()

    content = payload.get("content")
    extracted = _extract_text_blocks(content)
    if extracted:
        return " ".join(extracted).strip()

    return ""


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
        log_hook_event("pre-tool-speak", "exit_muted")
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        log_hook_event("pre-tool-speak", "exit_bad_json")
        sys.exit(0)

    log_payload_debug(payload, "pre-tool-speak")

    if is_subagent(payload):
        log_hook_event("pre-tool-speak", "exit_subagent")
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd", "")
    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        log_hook_event("pre-tool-speak", "no_transcript_path", session_id=session_id)

    # Transcript may not be flushed yet when PreToolUse fires.
    # Poll briefly before giving up.
    texts: list[str] = []
    for _attempt in range(TRANSCRIPT_POLL_ATTEMPTS):
        texts = _get_unsent_assistant_texts(transcript_path)
        if texts:
            break
        time.sleep(TRANSCRIPT_POLL_SLEEP_S)

    fallback_text = ""
    if not texts:
        fallback_text = _get_payload_assistant_text(payload)
        if fallback_text:
            log_hook_event(
                "pre-tool-speak",
                "payload_fallback_used",
                session_id=session_id,
                chars=len(fallback_text),
            )
            combined = fallback_text
        else:
            log_hook_event(
                "pre-tool-speak",
                "exit_no_text",
                session_id=session_id,
                transcript_path=transcript_path,
            )
            sys.exit(0)
    else:
        log_hook_event(
            "pre-tool-speak",
            "transcript_text_found",
            session_id=session_id,
            blocks=len(texts),
            chars=sum(len(t) for t in texts),
        )
        combined = " ".join(texts).strip()

    if len(combined) < 5:
        log_hook_event(
            "pre-tool-speak",
            "exit_too_short",
            session_id=session_id,
            chars=len(combined),
        )
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
        log_hook_event(
            "pre-tool-speak",
            "sent",
            session_id=session_id,
            chars=len(combined),
            transcript_path=transcript_path,
            used_fallback=bool(fallback_text),
        )
    except Exception as exc:
        log_hook_event(
            "pre-tool-speak",
            "send_exception",
            session_id=session_id,
            error=str(exc),
        )
        print(f"wednesday-tts pre-tool-speak: {exc}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"wednesday-tts pre-tool-speak: unhandled error: {exc}", file=sys.stderr)
        sys.exit(0)
