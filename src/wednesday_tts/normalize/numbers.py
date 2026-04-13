"""Number, unit, storage, and HTTP code normalization for TTS."""

import re

from wednesday_tts.normalize.constants import (
    DIGIT_WORDS,
    STORAGE_MAP,
    UNIT_MAP,
    decimal_to_spoken,
    digits_to_spoken,
)
from wednesday_tts.normalize.numbers_to_words import number_to_words


def normalize_tilde_approx(text):
    """~10 -> "around 10", multiplication sign -> "times", percentages."""
    text = re.sub(r'~\s*(\d)', r'around \1', text)
    text = text.replace('\u00d7', ' times ')  # ×
    text = re.sub(r'(\d+(?:\.\d+)?)\s*%', r'\1 percent', text)
    return text


def normalize_fractions(text):
    """Progress fractions: "4/10" -> "4 of 10", "1-4/10" -> "1 to 4 of 10".

    Must run BEFORE slash-to-speech rules eat the slash.
    """
    text = re.sub(
        r'\b(\d+)\s*-\s*(\d+)\s*/\s*(\d+)\b',
        r'\1 to \2 of \3',
        text
    )
    # Skip date-like patterns (12/01/2024)
    text = re.sub(r'(?<!/)\b(\d+)\s*/\s*(\d+)\b(?!\s*/\s*\d)', r'\1 of \2', text)
    return text


_SEP_CHARS = re.compile(r"[,_']")

def _strip_seps(s: str) -> str:
    """Strip thousand separators from a number string."""
    return _SEP_CHARS.sub('', s)


def _num_spoken(raw: str) -> str:
    """Convert a number string (possibly with separators/decimal) to spoken form."""
    clean = _strip_seps(raw)
    if '.' in clean:
        return decimal_to_spoken(clean)
    n = int(clean)
    if len(clean) >= 3:
        return number_to_words(n)
    return clean


# Number pattern: digits with optional thousand separators and optional decimal
_NUM_PAT = r"\d[\d,_']*(?:\.\d+)?"


def normalize_time_units(text):
    """Time/unit abbreviations: 300ms -> "300 milliseconds", 0.5s -> "zero point five seconds".

    Also handles ranges: "2-4s" -> "2 to 4 seconds".
    Must run BEFORE small-decimal rule.
    """
    def range_unit_to_speech(m):
        lo, hi, unit = m.group(1), m.group(2), m.group(3)
        return f'{_num_spoken(lo)} to {_num_spoken(hi)} {UNIT_MAP[unit]}'

    text = re.sub(rf'\b({_NUM_PAT})-({_NUM_PAT})(ms|min|s)\b', range_unit_to_speech, text)

    def unit_to_speech(m):
        num, unit = m.group(1), m.group(2)
        return f'{_num_spoken(num)} {UNIT_MAP[unit]}'

    text = re.sub(rf'\b({_NUM_PAT})(ms|min|s)\b', unit_to_speech, text)
    return text


def normalize_storage_units(text):
    """Storage/byte units: 1.4TB -> "1 point 4 terabytes", 50,023KB -> "fifty thousand and twenty three kilobytes"."""
    def storage_unit_to_speech(m):
        num, unit = m.group(1), m.group(2)
        return f'{_num_spoken(num)} {STORAGE_MAP[unit]}'

    text = re.sub(
        rf'\b({_NUM_PAT})(TB|GB|MB|KB|PB|tb|gb|mb|kb|pb)\b',
        storage_unit_to_speech, text
    )
    return text


def normalize_multipliers(text):
    """Multipliers: 1.0x -> "1 point oh times", 2x -> "2 times"."""
    def multiplier_to_speech(m):
        num = m.group(1)
        return f'{_num_spoken(num)} times'

    text = re.sub(rf'\b({_NUM_PAT})x\b', multiplier_to_speech, text)
    return text


def normalize_small_decimals(text):
    """Small decimals: 0.022 -> "zero point zero two two" (digit-by-digit).

    Also catches leading-dot form: .5 -> "point five".
    """
    def small_decimal_to_speech(m):
        has_leading_zero = m.group(1) is not None
        digits = m.group(2)
        spoken = ' '.join(DIGIT_WORDS.get(c, c) for c in digits)
        if has_leading_zero:
            return f'zero point {spoken}'
        return f'point {spoken}'

    text = re.sub(r'\b(0)\.(\d+)\b', small_decimal_to_speech, text)
    text = re.sub(
        r'(?<!\w)(?<!\d)\.(\d+)\b',
        lambda m: 'point ' + ' '.join(DIGIT_WORDS.get(c, c) for c in m.group(1)),
        text
    )
    return text


def normalize_regular_decimals(text):
    """Regular decimals: 8.2 -> "8 point 2", 3.14 -> "3 point 14".

    Only single-dot numbers NOT preceded by 'v' (those are versions).
    """
    text = re.sub(
        r'(?<!v)(?<!\.)(\d+)\.(\d+)(?!\.\d)\b',
        r'\1 point \2',
        text
    )
    return text


def normalize_http_codes(text):
    """3-digit HTTP-like codes -> digit by digit when context indicates a code.

    "error 500" -> "error five oh oh", but "500 files" stays as-is.
    """
    CODE_BEFORE = re.compile(
        r'(?:error|status|code|returned|return|response|HTTP|H T T P|threw|raised|got|received|with)\s+(?:a\s+)?$',
        re.IGNORECASE
    )

    def code_to_speech(m):
        n = m.group(1)
        first = int(n[0])
        if first < 1 or first > 5:
            return n
        before = m.string[max(0, m.start() - 30):m.start()]
        after = m.string[m.end():m.end() + 3]
        if CODE_BEFORE.search(before) or re.match(r'\s*:', after):
            return digits_to_spoken(n)
        return n

    text = re.sub(r'\b([1-9]\d{2})\b', code_to_speech, text)
    return text


def normalize_repeated_punctuation(text):
    """Repeated punctuation to spoken form: "..." -> "dot dot dot"."""
    text = re.sub(r'\.{3,}', ' dot dot dot ', text)
    text = re.sub(r'(?<!\.)\.\.(?!\.)', ' dot dot ', text)
    text = re.sub(r'\?{2,}', ' question marks', text)
    text = re.sub(r'!{2,}', ' exclamation marks', text)
    text = re.sub(r'[?!]{2,}', ' interrobang', text)
    return text


def normalize_standalone_punctuation(text):
    """Standalone separator slash and isolated punctuation symbols."""
    text = re.sub(r' / ', ' slash ', text)
    text = re.sub(r' \. ', ' dot ', text)
    text = re.sub(r' ! ', ' bang ', text)
    text = re.sub(r' \? ', ' question mark ', text)
    return text
