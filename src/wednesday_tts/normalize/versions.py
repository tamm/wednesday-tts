"""Version string normalization for TTS."""

import re


def normalize_model_versions(text):
    """Model/tool version strings: "qwen2.5:0.5b" -> "qwen 2 point 5, 0 point 5 b".

    Catches things like llama3.1:8b, qwen2.5:0.5b, phi2:3.8b, etc.
    """

    def model_version_to_speech(m):
        name = m.group(1)
        ver1 = m.group(2).replace(".", " point ")
        result = f"{name} {ver1}"
        if m.group(3):
            ver2 = m.group(3).replace(".", " point ")
            ver2 = re.sub(r"(\d)\s*(point\s+\d+)\s*([a-zA-Z])", r"\1 \2 \3", ver2)
            ver2 = re.sub(r"^(\d+)\s*([a-zA-Z])$", r"\1 \2", ver2)
            result += ", " + ver2
        return result

    text = re.sub(
        r"\b([a-zA-Z][a-zA-Z0-9-]*)(\d+(?:\.\d+)*)(?::(\d+(?:\.\d+)*[a-zA-Z]?))\b",
        model_version_to_speech,
        text,
    )
    # Standalone name+version without colon: "qwen2.5" -> "qwen 2 point 5"
    text = re.sub(
        r"\b([a-zA-Z][a-zA-Z-]*)(\d+\.\d+(?:\.\d+)*)\b",
        lambda m: f"{m.group(1)} {m.group(2).replace('.', ' point ')}",
        text,
    )
    return text


def normalize_semver(text):
    """Version strings: v1.2.3 or 1.2.3 -> "v one dot two dot three"."""
    text = re.sub(r"\bv(\d+\.\d+(?:\.\d+)?)\b", lambda m: m.group(0).replace(".", " dot "), text)
    text = re.sub(r"\b(\d+\.\d+\.\d+)\b", lambda m: m.group(0).replace(".", " dot "), text)
    return text
