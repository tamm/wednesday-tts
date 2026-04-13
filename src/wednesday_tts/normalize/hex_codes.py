"""Hex code normalization for TTS.

Converts 0x-prefixed hex and #-prefixed color codes to spoken form.
Runs early in the pipeline before number-to-words or dotted-name rules.
"""

import re

from wednesday_tts.normalize.constants import DIGIT_WORDS, LETTER_NAMES


def _speak_hex_char(c: str) -> str:
    """Speak a single hex character (uppercase letter name or digit word)."""
    low = c.lower()
    if low in "abcdef":
        return LETTER_NAMES[low].capitalize()
    return DIGIT_WORDS.get(low, c)


def _speak_hex_body(body: str) -> str:
    """Speak hex body as paired characters, solo first char if odd length."""
    chars = [_speak_hex_char(c) for c in body]
    pairs = []
    i = 0
    if len(chars) % 2 == 1:
        pairs.append(chars[0])
        i = 1
    while i < len(chars):
        pairs.append(f"{chars[i]} {chars[i + 1]}")
        i += 2
    return " ".join(pairs)


def _is_hex_string(s: str) -> bool:
    """Return True if s contains only hex characters and has at least one hex letter."""
    return bool(re.fullmatch(r"[0-9a-fA-F]+", s)) and bool(re.search(r"[a-fA-F]", s))


def _is_all_digits(s: str) -> bool:
    return bool(re.fullmatch(r"[0-9]+", s))


def normalize_hex_codes(text: str) -> str:
    """Convert hex code patterns to spoken form.

    Handles 0x-prefixed hex (0xFF, 0xDEADBEEF) and #-prefixed color codes
    (#FF00AA, #fff, #333). Digit-only # codes (#333, #000) are also matched
    since they are valid CSS color shorthand.
    """
    # 0x prefix: any hex digits after 0x
    text = re.sub(
        r"0x([0-9a-fA-F]+)\b",
        lambda m: f"hex {_speak_hex_body(m.group(1))}",
        text,
    )

    # # prefix: 3 or 6 hex chars (standard color code lengths)
    # Must contain hex chars (letters+digits mix) OR be all-digit 3/6 char codes
    def hash_replacement(m: re.Match) -> str:
        body = m.group(1)
        if len(body) not in (3, 6):
            return m.group(0)
        if not re.fullmatch(r"[0-9a-fA-F]+", body):
            return m.group(0)
        # Skip if it looks like a non-hex identifier (contains g-z)
        if re.search(r"[g-zG-Z]", body):
            return m.group(0)
        return f"hash {_speak_hex_body(body)}"

    text = re.sub(r"#([0-9a-zA-Z]+)\b", hash_replacement, text)

    return text
