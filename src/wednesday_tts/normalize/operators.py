"""Operator symbols to spoken form."""

import re


def normalize_operators(text):
    """Convert programming operators to spoken form.

    ===, !==, <=, >=, =>, +=, -=, *=, and standalone =.
    Order matters: multi-char operators before single =.
    """
    text = re.sub(r'!==?', ' not equals ', text)
    text = re.sub(r'===?', ' equals ', text)
    text = re.sub(r'<=', ' less than or equal to ', text)
    text = re.sub(r'>=', ' greater than or equal to ', text)
    text = re.sub(r'=>', ' to ', text)
    text = re.sub(r'\+=', ' plus equals ', text)
    text = re.sub(r'-=', ' minus equals ', text)
    text = re.sub(r'\*=', ' times equals ', text)
    # Standalone = (assignment) — compound operators already consumed above.
    # Use regex to avoid clobbering = inside already-normalized text or escaped sequences.
    text = re.sub(r'(?<![=!<>+\-*/])=(?![=>])', ' equals ', text)
    return text


def normalize_negative_numbers(text):
    """Convert negative number prefix to spoken form.

    "-3.5" -> "negative 3.5", "-4" -> "negative 4"
    Must run AFTER compound operators and BEFORE word-dash-number rules.
    """
    text = re.sub(r'(?<![a-zA-Z0-9-])-(\d)', r'negative \1', text)
    return text


def normalize_word_dash_number(text):
    """Separate word-dash-number: "vm-01" -> "vm 01", "node-18" -> "node 18".

    Prevents TTS from running letters into digits across a hyphen.
    """
    text = re.sub(r'\b([a-zA-Z]+)-(\d+)\b', r'\1 \2', text)
    return text
