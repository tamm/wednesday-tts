"""Phone number normalization — digit-by-digit spoken form with natural grouping."""

import re

from wednesday_tts.normalize.constants import DIGIT_WORDS


def _digits_to_words(digits: str) -> str:
    return " ".join(DIGIT_WORDS.get(c, c) for c in digits)


def _grouped_digits(digits: str, grouping: list[int]) -> str:
    parts = []
    pos = 0
    for size in grouping:
        parts.append(_digits_to_words(digits[pos : pos + size]))
        pos += size
    if pos < len(digits):
        parts.append(_digits_to_words(digits[pos:]))
    return ", ".join(parts)


def _speak_by_source_groups(groups: list[str]) -> str:
    return ", ".join(_digits_to_words(g) for g in groups)


def _replace_phone(match: re.Match) -> str:
    full = match.group(0)
    raw_digits = re.sub(r"[^\d]", "", full)

    # Emergency: 000
    if raw_digits == "000":
        return _digits_to_words("000")

    # 13 xx xx (6-digit 13-prefix service numbers)
    if len(raw_digits) == 6 and raw_digits.startswith("13"):
        return f"thirteen, {_grouped_digits(raw_digits[2:], [2, 2])}"

    # 1300 xxx xxx
    if len(raw_digits) == 10 and raw_digits.startswith("1300"):
        return f"thirteen hundred, {_grouped_digits(raw_digits[4:], [3, 3])}"

    # 1800 xxx xxx
    if len(raw_digits) == 10 and raw_digits.startswith("1800"):
        return f"one eight hundred, {_grouped_digits(raw_digits[4:], [3, 3])}"

    # International +prefixed numbers
    if full.startswith("+"):
        groups = re.split(r"[\s\-]+", full.lstrip("+"))
        groups = [g for g in groups if g]
        if len(groups) == 1:
            # No grouping in source — split country code heuristically
            d = raw_digits
            if d.startswith("61"):
                # Australian international: +61 X XXXX XXXX
                area = d[2:3]
                rest = d[3:]
                if len(rest) == 8:
                    return f"plus {_digits_to_words('61')}, {_digits_to_words(area)}, {_grouped_digits(rest, [4, 4])}"
                return f"plus {_digits_to_words('61')}, {_digits_to_words(d[2:])}"
            return f"plus {_digits_to_words(d)}"
        return "plus " + _speak_by_source_groups(groups)

    # Aussie mobile 04xx: split out source groups or apply default 4-3-3
    if raw_digits.startswith("04") and len(raw_digits) == 10:
        stripped = re.sub(r"[^\d\s\-]", "", full)
        groups = re.split(r"[\s\-]+", stripped)
        groups = [g for g in groups if g]
        if len(groups) > 1:
            return _speak_by_source_groups(groups)
        return _grouped_digits(raw_digits, [4, 3, 3])

    # Aussie landline 0X XXXX XXXX
    if raw_digits.startswith("0") and len(raw_digits) == 10:
        stripped = re.sub(r"[()]+", "", full)
        groups = re.split(r"[\s\-]+", stripped.strip())
        groups = [g for g in groups if g]
        if len(groups) > 1:
            return _speak_by_source_groups(groups)
        return _grouped_digits(raw_digits, [2, 4, 4])

    return full


# Patterns ordered most-specific first
_PHONE_PATTERNS = [
    # Emergency triple-zero
    r"\b000\b",
    # International: + then digits with optional spaces/dashes
    r"\+\d[\d\s\-]{4,}",
    # 1300/1800 with separators
    r"\b1[38]00[\s\-]?\d{3}[\s\-]?\d{3}\b",
    # 13 xx xx (6-digit)
    r"\b13[\s\-]?\d{2}[\s\-]?\d{2}\b",
    # Aussie with parens: (0X) XXXX XXXX
    r"\(0\d\)\s?\d{4}\s?\d{4}",
    # Aussie mobile/landline with spaces/dashes: 0XXX XXX XXX or 0X XXXX XXXX
    r"\b0\d(?:[\s\-]\d{4}){2}\b",
    r"\b04\d{2}[\s\-]\d{3}[\s\-]\d{3}\b",
    # Compact 10-digit starting with 0
    r"\b0[2-478]\d{8}\b",
    r"\b04\d{8}\b",
]

_COMBINED_PATTERN = re.compile("|".join(f"(?:{p})" for p in _PHONE_PATTERNS))


def normalize_phone_numbers(text: str) -> str:
    """Detect phone numbers and convert to digit-by-digit spoken form."""
    return _COMBINED_PATTERN.sub(_replace_phone, text)
