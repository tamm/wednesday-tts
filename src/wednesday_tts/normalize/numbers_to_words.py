"""Convert standalone integers (3+ digits) into natural spoken English words."""

import re

from wednesday_tts.normalize.constants import digits_to_spoken

ONES = [
    '', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
    'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
    'seventeen', 'eighteen', 'nineteen',
]

TENS = [
    '', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety',
]

SCALE = ['', 'thousand', 'million', 'billion']

MAX_SUPPORTED = 999_999_999_999


def _two_digits(n: int) -> str:
    if n < 20:
        return ONES[n]
    t, o = divmod(n, 10)
    return f'{TENS[t]} {ONES[o]}'.strip() if o else TENS[t]


def _three_digits(n: int, use_and: bool = True) -> str:
    h, remainder = divmod(n, 100)
    if h and remainder:
        joiner = ' and ' if use_and else ' '
        return f'{ONES[h]} hundred{joiner}{_two_digits(remainder)}'
    if h:
        return f'{ONES[h]} hundred'
    return _two_digits(remainder)


def number_to_words(n: int) -> str:
    """Convert an integer to English words (British/Australian style with 'and')."""
    if n < 0:
        return f'minus {number_to_words(-n)}'
    if n == 0:
        return 'zero'
    if n > MAX_SUPPORTED:
        return str(n)

    groups: list[tuple[int, str]] = []
    scale_idx = 0
    remaining = n
    while remaining:
        remaining, chunk = divmod(remaining, 1000)
        if chunk:
            groups.append((chunk, SCALE[scale_idx]))
        scale_idx += 1

    groups.reverse()

    parts: list[str] = []
    for chunk, scale in groups:
        word = _three_digits(chunk, use_and=True)
        if scale:
            parts.append(f'{word} {scale}')
        else:
            parts.append(word)

    result = ' '.join(parts)

    if len(groups) > 1 and groups[-1][0] < 100:
        last = parts[-1]
        rest = ' '.join(parts[:-1])
        result = f'{rest} and {last}'

    return result


_FORMATTED_RE = re.compile(r"\b\d{1,3}([,_']\d{3})+\b")
# Dot as thousand separator — only match 2+ groups (1.234.567) to avoid decimals (1.234)
_DOT_THOUSANDS_RE = re.compile(r'\b(\d{1,3}(?:\.\d{3}){2,})\b')

_PLAIN_RE = re.compile(r'\b\d{3,}\b')


def normalize_large_numbers(text: str) -> str:
    """Replace standalone large integers with spoken English words."""

    def _replace_formatted(m: re.Match) -> str:
        raw = m.group()
        digits = re.sub(r"[,_']", '', raw)
        n = int(digits)
        if n > MAX_SUPPORTED:
            return digits_to_spoken(digits)
        return number_to_words(n)

    def _check_formatted(m: re.Match) -> str:
        start = m.start()
        sep = m.group()[len(m.group().split(m.group(1)[0])[0])]
        after_sep_pos = text.find(sep, start)
        if after_sep_pos >= 0 and after_sep_pos + 1 < len(text) and text[after_sep_pos + 1] == ' ':
            return m.group()
        return _replace_formatted(m)

    text = _FORMATTED_RE.sub(_replace_formatted, text)

    def _replace_dot_thousands(m: re.Match) -> str:
        digits = m.group(1).replace('.', '')
        n = int(digits)
        if n > MAX_SUPPORTED:
            return digits_to_spoken(digits)
        return number_to_words(n)

    text = _DOT_THOUSANDS_RE.sub(_replace_dot_thousands, text)

    def _replace_plain(m: re.Match) -> str:
        start = m.start()
        end = m.end()

        if start > 0 and text[start - 1] in '.+':
            return m.group()
        if start > 1 and text[start - 2:start] == '0x':
            return m.group()
        if start > 0 and text[start - 1] == '#':
            return m.group()
        if end < len(text) and text[end] == '.':
            return m.group()

        n = int(m.group())
        if n > MAX_SUPPORTED:
            return digits_to_spoken(m.group())
        return number_to_words(n)

    text = _PLAIN_RE.sub(_replace_plain, text)

    return text
