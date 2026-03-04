"""Tests for year and date normalization."""

from __future__ import annotations

import random
import re

from wednesday_tts.normalize.dates import (
    _two_digit_to_words,
    _year_to_words,
    normalize_dates,
    normalize_years,
)


class TestTwoDigitToWords:
    def test_zero(self):
        assert _two_digit_to_words(0) == 'zero'

    def test_single_digits(self):
        assert _two_digit_to_words(1) == 'one'
        assert _two_digit_to_words(9) == 'nine'

    def test_teens(self):
        assert _two_digit_to_words(10) == 'ten'
        assert _two_digit_to_words(11) == 'eleven'
        assert _two_digit_to_words(19) == 'nineteen'

    def test_round_tens(self):
        assert _two_digit_to_words(20) == 'twenty'
        assert _two_digit_to_words(50) == 'fifty'
        assert _two_digit_to_words(90) == 'ninety'

    def test_compound(self):
        assert _two_digit_to_words(42) == 'forty two'
        assert _two_digit_to_words(99) == 'ninety nine'


class TestYearToWords:
    """Test that _year_to_words produces valid spoken forms.

    Because the function uses weighted randomness, we test that the output
    is one of the acceptable forms rather than testing exact strings.
    """

    def test_2000(self):
        assert _year_to_words(2000) == 'two thousand'

    def test_2001_forms(self):
        random.seed(None)
        results = {_year_to_words(2001) for _ in range(100)}
        assert results <= {
            'two thousand and one',
            'two thousand one',
            'oh one',
        }
        # Should produce at least 2 distinct forms over 100 tries
        assert len(results) >= 2

    def test_2005_forms(self):
        results = {_year_to_words(2005) for _ in range(100)}
        assert results <= {
            'two thousand and five',
            'two thousand five',
            'oh five',
        }

    def test_2010_forms(self):
        results = {_year_to_words(2010) for _ in range(100)}
        assert results <= {
            'twenty ten',
            'two thousand and ten',
        }

    def test_2019_forms(self):
        results = {_year_to_words(2019) for _ in range(100)}
        assert results <= {
            'twenty nineteen',
            'two thousand and nineteen',
        }

    def test_2025_forms(self):
        results = {_year_to_words(2025) for _ in range(100)}
        assert results <= {
            'twenty twenty five',
            'two thousand and twenty five',
        }

    def test_1982_forms(self):
        results = {_year_to_words(1982) for _ in range(100)}
        assert results <= {
            'nineteen eighty two',
            'eighty two',
        }

    def test_1900_forms(self):
        results = {_year_to_words(1900) for _ in range(50)}
        assert 'nineteen hundred' in results

    def test_1800_no_shortening(self):
        """Pre-1900 years should always use full form."""
        random.seed(42)
        for _ in range(20):
            result = _year_to_words(1861)
            assert result == 'eighteen sixty one'

    def test_1066(self):
        assert _year_to_words(1066) == 'ten sixty six'

    def test_1500(self):
        assert _year_to_words(1500) == 'fifteen hundred'

    def test_1805(self):
        assert _year_to_words(1805) == 'eighteen oh five'


class TestNormalizeYears:
    """Test year detection in context."""

    def test_in_year(self):
        result = normalize_years('in 1982')
        assert '1982' not in result
        assert re.search(r'(?:nineteen )?eighty two', result)

    def test_since_year(self):
        result = normalize_years('since 2020')
        assert '2020' not in result

    def test_from_year(self):
        result = normalize_years('from 1999')
        assert '1999' not in result

    def test_released_year(self):
        result = normalize_years('released 2001')
        assert '2001' not in result

    def test_born_year(self):
        result = normalize_years('born 1965')
        assert '1965' not in result

    def test_does_not_match_quantities(self):
        """Numbers followed by unit words should not be treated as years."""
        text = '1024 bytes'
        assert normalize_years(text) == text

    def test_does_not_match_file_counts(self):
        text = '2048 files'
        assert normalize_years(text) == text

    def test_does_not_match_line_counts(self):
        text = '1500 lines'
        assert normalize_years(text) == text

    def test_does_not_match_pixels(self):
        text = '1920 pixels'
        assert normalize_years(text) == text

    def test_preserves_surrounding_text(self):
        result = normalize_years('I was born in 1982 and grew up')
        assert result.startswith('I was born in ')
        assert result.endswith(' and grew up')
        assert '1982' not in result

    def test_multiple_years(self):
        result = normalize_years('from 1990 until 2020')
        assert '1990' not in result
        assert '2020' not in result

    def test_year_after_comma(self):
        result = normalize_years('On Tuesday, 2025 was mentioned')
        assert '2025' not in result

    def test_case_insensitive_context(self):
        result = normalize_years('Released 1999')
        assert '1999' not in result


class TestNormalizeDates:
    """Test slash-format date parsing (DD/MM/YYYY Australian convention)."""

    def test_full_date(self):
        result = normalize_dates('04/03/2026')
        assert 'March' in result
        assert 'fourth' in result
        assert '2026' not in result

    def test_day_first_australian(self):
        """25/12/2000 must be 25th of December (not month 25)."""
        result = normalize_dates('25/12/2000')
        assert 'December' in result
        assert 'twenty fifth' in result

    def test_ambiguous_date(self):
        """04/03/2026 — both valid as day or month; DD/MM wins."""
        result = normalize_dates('04/03/2026')
        assert 'March' in result
        assert 'fourth' in result

    def test_invalid_month_swaps(self):
        """15/06/2020 — 15 can't be a month, so day=15 month=6."""
        result = normalize_dates('15/06/2020')
        assert 'June' in result
        assert 'fifteenth' in result

    def test_nonsensical_date_unchanged(self):
        """99/99/2020 — nonsensical, leave alone."""
        assert normalize_dates('99/99/2020') == '99/99/2020'

    def test_year_out_of_range_unchanged(self):
        assert normalize_dates('01/01/0500') == '01/01/0500'

    def test_preserves_non_date_slashes(self):
        """4/10 should not be touched (no year component)."""
        assert normalize_dates('4/10') == '4/10'

    def test_preserves_surrounding_text(self):
        result = normalize_dates('meeting on 04/03/2026 at noon')
        assert result.startswith('meeting on ')
        assert result.endswith(' at noon')

    def test_single_digit_day_month(self):
        result = normalize_dates('1/2/2000')
        assert 'February' in result
        assert 'first' in result


class TestNormalizeYearsDoesNotBreakOtherPatterns:
    """Regression tests — years normalizer should not eat non-year numbers."""

    def test_port_numbers(self):
        assert normalize_years('localhost:8080') == 'localhost:8080'

    def test_four_digit_after_colon(self):
        assert normalize_years('port 5678') == 'port 5678'

    def test_version_like(self):
        """v2024 should not be matched (preceded by 'v')."""
        assert normalize_years('v2024') == 'v2024'

    def test_hex_like(self):
        assert normalize_years('0x1982') == '0x1982'

    def test_preceded_by_hash(self):
        assert normalize_years('#1234') == '#1234'
