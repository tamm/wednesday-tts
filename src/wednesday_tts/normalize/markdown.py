"""Markdown formatting, emoji, and whitespace cleanup for TTS."""

import re

from wednesday_tts.normalize.code_blocks import code_block_to_speech
from wednesday_tts.normalize.tables import (
    MARKDOWN_TABLE_RE,
    UNICODE_TABLE_RE,
    table_to_speech,
)

SPOKEN_REPLACEMENTS = {
    # Checkmarks
    "\u2713": "check",
    "\u2714": "check",
    "\u2705": "check",
    "\u2611": "check",
    # X marks
    "\u2717": "x",
    "\u2718": "x",
    "\u274c": "x",
    "\u274e": "x",
    # Arrows
    "\u2192": "to",
    "\u2190": "arrow",
    "\u2191": "arrow",
    "\u2193": "arrow",
    "\u27a1": "arrow",
    "\u2b05": "arrow",
    # Common emojis
    "\U0001f44d": "thumbs up",
    "\U0001f44e": "thumbs down",
    "\U0001f389": "celebration",
    "\U0001f680": "rocket",
    "\U0001f4a1": "idea",
    "\u26a0\ufe0f": "warning",
    "\U0001f525": "fire",
    "\u2728": "sparkle",
    "\U0001f4c1": "folder",
    "\U0001f4c2": "folder",
    "\U0001f4c4": "file",
    "\U0001f527": "tool",
    "\U0001f41b": "bug",
    "\U0001f916": "robot",
    # Symbols
    "\u2022": "",
    "\u00b7": "",
    "\u2026": "...",
    "\u00a9": "copyright",
    "\u00ae": "registered",
    "\u2122": "trademark",
    "\u2502": "pipe",
}


def clean_text_for_speech(text):
    """Clean markdown and symbols after technical content has been normalised."""

    # Spaced em/en dashes — collapse surrounding whitespace into a clean comma pause
    text = re.sub(r"\s*\u2014\s*", ", ", text)
    text = re.sub(r"\s*\u2013\s*", ", ", text)

    for emoji, replacement in SPOKEN_REPLACEMENTS.items():
        text = text.replace(emoji, f" {replacement} " if replacement else " ")

    # Code blocks -> spoken (backup for any missed by main pipeline)
    text = re.sub(r"```[a-zA-Z]*\n?([\s\S]*?)```", code_block_to_speech, text)

    # Inline code: keep content, strip backticks
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # Markdown formatting — promote bold/italic to _underscore_ emphasis,
    # which the TTS engine treats as spoken stress. Bold is stronger than
    # italic; without a second stress level we map both to the same marker.
    text = re.sub(r"\*\*([^*]+)\*\*", r"_\1_", text)  # bold **
    text = re.sub(r"__([^_]+)__", r"_\1_", text)  # bold __
    text = re.sub(r"\*([^*]+)\*", r"_\1_", text)  # italic *
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)  # headers
    text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)  # blockquotes
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)  # unordered lists
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)  # ordered lists

    # Any remaining bare URLs
    text = re.sub(r"https?://[^\s\)]+", "", text)

    # Markdown links: keep text, drop URL
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Table formatting — detect structured tables and speak them semantically
    text = MARKDOWN_TABLE_RE.sub(
        lambda m: table_to_speech(m.group(0)),
        text,
    )
    text = UNICODE_TABLE_RE.sub(
        lambda m: table_to_speech(m.group(0)),
        text,
    )
    # Fallback: any remaining stray pipes
    text = re.sub(r"\|", " ", text)
    text = re.sub(r"^[-:]+$", "", text, flags=re.MULTILINE)

    # Remove remaining brackets/braces
    text = re.sub(r"[{}\[\]()]", "", text)

    # Remove backslashes
    text = re.sub(r"\\", "", text)

    # Multiple dashes/underscores -> space
    text = re.sub(r"[-_]{2,}", " ", text)

    # Newlines -> sentence breaks
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"\s*\n\s*", ". ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\.(\s*\.)+", ".", text)
    text = text.strip()

    return text
