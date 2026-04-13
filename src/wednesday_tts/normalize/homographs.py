"""Homograph disambiguation for TTS."""

import re


def fix_read_homograph(text):
    """Disambiguate 'read' for TTS — past tense -> 'red', present -> 'reed'.

    First converts past-tense/passive 'read' to 'red' using grammatical triggers,
    then converts any remaining 'read' to 'reed'. The TTS model never sees the
    ambiguous spelling.
    """
    # Pattern A — passive voice (be-family + read)
    text = re.sub(r"(?i)\b(was|were|is|are|am|be|been|being)(\s+)read\b(?!-)", r"\1\2red", text)
    # Pattern B — perfect aspect (have-family + read)
    text = re.sub(r"(?i)\b(has|have|had)(\s+)read\b(?!-)", r"\1\2red", text)
    # Pattern C — modal passive (modal + be + read)
    text = re.sub(
        r"(?i)\b(can|could|will|would|shall|should|may|might|must)(\s+be\s+)read\b(?!-)",
        r"\1\2red",
        text,
    )
    # All remaining 'read' is present tense / imperative
    text = re.sub(r"\bread\b(?!-)", "reed", text)
    text = re.sub(r"\bRead\b(?!-)", "Reed", text)
    return text
