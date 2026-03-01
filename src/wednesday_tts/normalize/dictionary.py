"""Pronunciation dictionary loading and application."""

import json
import os
import re


def load_dictionary(dict_path, backend="pocket"):
    """Load custom TTS pronunciation dictionary from JSON file.

    Each entry can have backend-specific replacements (pocket, kokoro, etc.)
    plus a universal 'replacement' fallback. Returns a flat list of entries
    with 'replacement' resolved to the active backend's value.
    """
    try:
        if not os.path.exists(dict_path):
            return []
        with open(dict_path, encoding='utf-8') as f:
            entries = json.load(f).get("replacements", [])
    except Exception:
        return []

    resolved = []
    for entry in entries:
        replacement = entry.get(backend) or entry.get("replacement", "")
        if not replacement:
            continue
        resolved.append({
            "pattern": entry.get("pattern", ""),
            "replacement": replacement,
            "case_sensitive": entry.get("case_sensitive", True),
            "literal": entry.get("literal", False),
        })
    return resolved


def load_filenames_dict(filenames_path):
    """Load file extension pronunciation dict from tts-filenames.json."""
    try:
        if os.path.exists(filenames_path):
            with open(filenames_path, encoding='utf-8') as f:
                return json.load(f).get("extensions", {})
    except Exception:
        pass
    return {}


def apply_dictionary(text, replacements):
    """Apply whole-word replacements from the pronunciation dictionary.

    Entries with "literal": true skip word-boundary wrapping — use for
    patterns containing non-word chars (e.g. "C#") where \\b won't match.
    """
    for entry in replacements:
        pattern = entry.get("pattern", "")
        replacement = entry.get("replacement", "")
        if not pattern:
            continue
        flags = 0 if entry.get("case_sensitive", True) else re.IGNORECASE
        escaped = re.escape(pattern)
        if entry.get("literal", False):
            text = re.sub(escaped, replacement, text, flags=flags)
        else:
            text = re.sub(r'\b' + escaped + r'\b', replacement, text, flags=flags)
    return text
