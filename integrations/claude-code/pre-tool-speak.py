#!/usr/bin/env python3
"""
PreToolUse hook — speaks any unread assistant text before each tool call.

Claude often writes a sentence before running a tool (e.g. "Let me check that.")
The Stop hook only fires at end of a full turn, so those mid-turn messages are
never spoken. This hook fires before every tool call, finds assistant text blocks
from the current turn that haven't been spoken yet, and POSTs them to the
wednesday-tts server for normalization and synthesis.

State tracking:
  /tmp/tts-spoken-<session_id>  — newline-separated hashes of already-spoken text

If the server is not running, the hook exits silently (no error, no crash).
"""

import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error

TTS_URL = "http://localhost:5678/speak?content_type=markdown"
HEALTH_URL = "http://localhost:5678/health"
SPOKEN_TTL = 240  # seconds — don't repeat same text within this window
CONNECT_TIMEOUT = 1.0  # seconds — bail fast if server not running


# ---------------------------------------------------------------------------
# Spoken-hash tracking (deduplication across concurrent hooks)
# ---------------------------------------------------------------------------

def _spoken_hashes_path(session_id: str) -> str:
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
    safe_id = session_id.replace("/", "_").replace("\\", "_")
    return os.path.join(tmp, f"tts-spoken-{safe_id}")


def _text_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _load_spoken_approx(session_id: str) -> set:
    """Non-atomic pre-filter — used only to skip obviously-already-spoken text."""
    path = _spoken_hashes_path(session_id)
    now = time.time()
    try:
        with open(path) as f:
            recent = set()
            for line in f:
                parts = line.strip().split(" ", 1)
                if len(parts) == 2:
                    h, ts = parts
                    try:
                        if now - float(ts) < SPOKEN_TTL:
                            recent.add(h)
                    except ValueError:
                        pass
                elif parts[0]:
                    recent.add(parts[0])
            return recent
    except FileNotFoundError:
        return set()


def _claim_unspoken(session_id: str, hashes: list) -> list:
    """Atomically claim hashes so concurrent hooks don't double-speak the same text."""
    path = _spoken_hashes_path(session_id)
    now = time.time()
    try:
        try:
            f = open(path, "r+")
        except FileNotFoundError:
            f = open(path, "w+")
        with f:
            # Read current state
            f.seek(0)
            recent = set()
            for line in f.readlines():
                parts = line.strip().split(" ", 1)
                if len(parts) == 2:
                    h, ts = parts
                    try:
                        if now - float(ts) < SPOKEN_TTL:
                            recent.add(h)
                    except ValueError:
                        pass
                elif parts[0]:
                    recent.add(parts[0])
            unclaimed = [h for h in hashes if h not in recent]
            if unclaimed:
                f.seek(0, 2)  # append
                ts_str = f"{now:.3f}"
                for h in unclaimed:
                    f.write(f"{h} {ts_str}\n")
                f.flush()
        return unclaimed
    except Exception:
        # Can't lock — fall back to all (may duplicate, but better than silence)
        return list(hashes)


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

def _get_unspoken_assistant_text(transcript_path: str | None, session_id: str) -> list:
    """Return (hash, raw_text) pairs for unseen assistant text in the current turn."""
    if not transcript_path or not os.path.exists(transcript_path):
        return []

    messages = []
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                msg = json.loads(line.strip())
                if msg.get("type") in ("assistant", "user"):
                    messages.append(msg)
            except (json.JSONDecodeError, KeyError):
                continue

    # Current turn = everything after the last user message
    last_user_idx = -1
    for i, msg in enumerate(messages):
        if msg.get("type") == "user":
            last_user_idx = i

    if last_user_idx < 0:
        return []

    spoken_approx = _load_spoken_approx(session_id)
    candidates = []

    for msg in messages[last_user_idx + 1:]:
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "text":
                continue
            raw = block.get("text", "").strip()
            if not raw:
                continue
            h = _text_hash(raw)
            if h not in spoken_approx:
                candidates.append((h, raw))

    return candidates


# ---------------------------------------------------------------------------
# Server communication
# ---------------------------------------------------------------------------

def _post_to_server(text: str, session_id: str) -> bool:
    """POST text to the wednesday-tts server. Returns True on success."""
    body = text.encode("utf-8")
    req = urllib.request.Request(
        TTS_URL,
        data=body,
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "X-Session-Id": session_id,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
            return resp.status < 400
    except urllib.error.URLError:
        return False  # server not running — silent fail
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # TTS mute — user toggle for meetings etc.
    import tempfile
    mute_path = os.path.join(tempfile.gettempdir(), "tts-mute")
    if os.path.exists(mute_path):
        sys.exit(0)

    try:
        input_data = json.load(sys.stdin)

        session_id = input_data.get("session_id", "unknown")
        transcript_path = input_data.get("transcript_path")

        candidates = _get_unspoken_assistant_text(transcript_path, session_id)
        if not candidates:
            sys.exit(0)

        # Atomically claim the hashes — concurrent hooks that race us will see
        # them already claimed and exit without speaking.
        claimed_hashes = _claim_unspoken(session_id, [h for h, _ in candidates])
        if not claimed_hashes:
            sys.exit(0)

        claimed_set = set(claimed_hashes)
        claimed_texts = [raw for h, raw in candidates if h in claimed_set]

        # Combine all claimed blocks and send as a single request
        combined = " ".join(claimed_texts).strip()
        if len(combined) < 5:
            sys.exit(0)

        # Truncate to ~2400 chars at a sentence boundary to avoid runaway speech
        if len(combined) > 2400:
            trunc = combined[:2400]
            last_sentence = max(trunc.rfind(". "), trunc.rfind("! "), trunc.rfind("? "))
            if last_sentence > 1200:
                combined = combined[:last_sentence + 1]
            else:
                last_space = trunc.rfind(" ")
                combined = combined[:last_space] if last_space > 0 else trunc

        _post_to_server(combined, session_id)

    except Exception as e:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"TTS unavailable: {e}",
            }
        }))

    sys.exit(0)


if __name__ == "__main__":
    main()
