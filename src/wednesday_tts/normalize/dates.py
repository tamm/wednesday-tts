"""Year and date normalization for TTS.

Converts standalone years and slash-format dates to natural spoken form.
Years use varied pronunciation like a real person would — sometimes
"nineteen eighty two", sometimes "eighty two", and special handling
for 2000s-era years.
"""

import random
import re

TEENS = {
    10: 'ten', 11: 'eleven', 12: 'twelve', 13: 'thirteen', 14: 'fourteen',
    15: 'fifteen', 16: 'sixteen', 17: 'seventeen', 18: 'eighteen', 19: 'nineteen',
}
TENS = {
    2: 'twenty', 3: 'thirty', 4: 'forty', 5: 'fifty',
    6: 'sixty', 7: 'seventy', 8: 'eighty', 9: 'ninety',
}
ONES = {
    1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five',
    6: 'six', 7: 'seven', 8: 'eight', 9: 'nine',
}

MONTH_NAMES = {
    1: 'January', 2: 'February', 3: 'March', 4: 'April',
    5: 'May', 6: 'June', 7: 'July', 8: 'August',
    9: 'September', 10: 'October', 11: 'November', 12: 'December',
}

ORDINALS = {
    1: 'first', 2: 'second', 3: 'third', 4: 'fourth', 5: 'fifth',
    6: 'sixth', 7: 'seventh', 8: 'eighth', 9: 'ninth', 10: 'tenth',
    11: 'eleventh', 12: 'twelfth', 13: 'thirteenth', 14: 'fourteenth',
    15: 'fifteenth', 16: 'sixteenth', 17: 'seventeenth', 18: 'eighteenth',
    19: 'nineteenth', 20: 'twentieth', 21: 'twenty first', 22: 'twenty second',
    23: 'twenty third', 24: 'twenty fourth', 25: 'twenty fifth',
    26: 'twenty sixth', 27: 'twenty seventh', 28: 'twenty eighth',
    29: 'twenty ninth', 30: 'thirtieth', 31: 'thirty first',
}


def _two_digit_to_words(n: int) -> str:
    """Convert a number 0–99 to words."""
    if n == 0:
        return 'zero'
    if n < 10:
        return ONES[n]
    if n < 20:
        return TEENS[n]
    tens_digit = n // 10
    ones_digit = n % 10
    if ones_digit == 0:
        return TENS[tens_digit]
    return f'{TENS[tens_digit]} {ONES[ones_digit]}'


def _year_to_words(year: int) -> str:
    """Convert a year (1000–2999) to spoken form with natural variation.

    Uses weighted random choice to mimic how real people say years:
    - 1900–1999: usually "nineteen eighty two", sometimes just "eighty two"
    - 2000: always "two thousand"
    - 2001–2009: "two thousand one" or "two thousand and one" or "oh one"
    - 2010–2019: "twenty ten" or "two thousand and ten"
    - 2020+: usually "twenty twenty five", sometimes "two thousand and twenty five"
    - Pre-1900: always full form ("eighteen sixty one")
    """
    century = year // 100
    remainder = year % 100

    # 2000 exactly
    if year == 2000:
        return 'two thousand'

    # 2001–2009: "two thousand (and) one" or "oh one"
    if 2001 <= year <= 2009:
        forms = [
            (f'two thousand and {_two_digit_to_words(remainder)}', 0.45),
            (f'two thousand {_two_digit_to_words(remainder)}', 0.35),
            (f'oh {_two_digit_to_words(remainder)}', 0.20),
        ]
        return _weighted_choice(forms)

    # 2010–2019: "twenty ten" or "two thousand and ten"
    if 2010 <= year <= 2019:
        forms = [
            (f'twenty {_two_digit_to_words(remainder)}', 0.65),
            (f'two thousand and {_two_digit_to_words(remainder)}', 0.35),
        ]
        return _weighted_choice(forms)

    # 2020+: "twenty twenty five" or occasionally "two thousand and twenty five"
    if year >= 2020:
        century_words = _two_digit_to_words(century)
        remainder_words = _two_digit_to_words(remainder)
        forms = [
            (f'{century_words} {remainder_words}', 0.80),
            (f'two thousand and {remainder_words}', 0.20),
        ]
        return _weighted_choice(forms)

    # 1000–1999: standard split form
    century_words = _two_digit_to_words(century)
    if remainder == 0:
        return f'{century_words} hundred'

    remainder_words = _two_digit_to_words(remainder)

    # 1900–1999 sometimes drops the century ("eighty two" instead of "nineteen eighty two")
    if 1900 <= year <= 1999:
        forms = [
            (f'{century_words} {remainder_words}', 0.75),
            (remainder_words, 0.25),
        ]
        return _weighted_choice(forms)

    # 1000–1899: "oh" prefix for 01–09 remainders
    if remainder < 10:
        return f'{century_words} oh {remainder_words}'
    return f'{century_words} {remainder_words}'


def _weighted_choice(forms: list[tuple[str, float]]) -> str:
    """Pick from weighted options. forms is list of (text, weight)."""
    texts, weights = zip(*forms)
    return random.choices(texts, weights=weights, k=1)[0]


def normalize_years(text: str) -> str:
    r"""Convert standalone 4-digit years to spoken form.

    Matches years 1000–2999 that look like years in context — preceded by
    word boundaries, "in", "since", "from", "by", "around", "circa", etc.
    Avoids matching numbers that are clearly not years (e.g. "1024 bytes").
    """
    # Context words that strongly suggest a year follows
    year_context = (
        r'(?:'
        r'(?:in|since|from|by|around|circa|before|after|until|during|of|year|born|died|established|founded|built|released|published|written|recorded|made)\s+'
        r')'
    )

    # Pattern: context word + 4-digit year
    def _replace_contextual(m: re.Match) -> str:
        prefix = m.group(1)
        year = int(m.group(2))
        return f'{prefix}{_year_to_words(year)}'

    text = re.sub(
        rf'({year_context})(\d{{4}})\b',
        _replace_contextual,
        text,
        flags=re.IGNORECASE,
    )

    # Standalone years at sentence/clause start or after punctuation
    # Match 4-digit numbers in 1000–2999 range that aren't followed by unit-like suffixes
    def _replace_standalone(m: re.Match) -> str:
        year = int(m.group(1))
        if year < 1000 or year > 2999:
            return m.group(0)
        return _year_to_words(year)

    # Years preceded by start-of-string, comma, dash, or parenthesis
    text = re.sub(
        r'(?:^|(?<=, )|(?<=\()|(?<=— ))(\d{4})\b(?!\s*(?:bytes|bits|items|files|lines|pixels|px|rows|cols|nodes|steps|times|iterations|errors|warnings|tests|commits|downloads|users|requests|connections|threads|processes|samples|entries|records|characters|chars|words|pages|MB|GB|KB|TB|ms|rpm|hz|Hz))',
        _replace_standalone,
        text,
        flags=re.MULTILINE,
    )

    return text


def normalize_dates(text: str) -> str:
    """Convert slash-format dates to spoken form.

    DD/MM/YYYY or MM/DD/YYYY -> "the Nth of Month, Year" (assumes DD/MM for
    ambiguous dates, since the user is Australian).
    Also handles DD/MM and MM/YYYY partial forms.
    """
    # Full date: DD/MM/YYYY or D/M/YYYY
    def _replace_full_date(m: re.Match) -> str:
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # Interpret as DD/MM/YYYY (Australian convention)
        day, month = a, b
        # Only swap if the first number can't be a valid day, or the
        # second number can't be a valid month (i.e. b > 12 means b must be a day)
        if month > 12 and day <= 12:
            day, month = b, a
        if month < 1 or month > 12 or day < 1 or day > 31:
            return m.group(0)  # nonsensical, leave alone
        if year < 1000 or year > 2999:
            return m.group(0)

        month_name = MONTH_NAMES[month]
        day_ord = ORDINALS.get(day, str(day))
        year_words = _year_to_words(year)
        return f'the {day_ord} of {month_name}, {year_words}'

    text = re.sub(
        r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b',
        _replace_full_date,
        text,
    )

    return text
