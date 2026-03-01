"""CamelCase splitting and ALL CAPS normalisation."""

import re

from wednesday_tts.normalize.constants import CAPS_EXCLAMATIONS


def normalize_all_caps(text):
    """ALL CAPS words (4+ letters) -> Title Case to prevent TTS spelling them out.

    Preserves real acronyms (2-3 letters like DB, OK) — those are already
    handled by the dictionary.
    """
    for caps, normal in CAPS_EXCLAMATIONS.items():
        text = re.sub(rf'\b{caps}\b', normal, text)
    # 4+ letter ALL CAPS -> Title Case
    text = re.sub(r"\b([A-Z][A-Z']{3,})\b", lambda m: m.group(1).capitalize(), text)
    return text


def normalize_camelcase(text):
    """Insert spaces in camelCase identifiers so TTS doesn't run them together.

    "myVariableName" -> "my Variable Name"
    Skips ALL_CAPS and single-cap words.
    """
    def split_camel(m):
        word = m.group(0)
        if word.upper() == word or word.lower() == word:
            return word
        return re.sub(r'([a-z])([A-Z])', r'\1 \2', word)

    text = re.sub(r'\b[a-zA-Z]{4,}\b', split_camel, text)
    return text
