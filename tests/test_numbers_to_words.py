"""Tests for numbers_to_words normalization module."""


from wednesday_tts.normalize.numbers_to_words import number_to_words, normalize_large_numbers


class TestNumberToWords:
    def test_zero(self):
        assert number_to_words(0) == 'zero'

    def test_100(self):
        assert number_to_words(100) == 'one hundred'

    def test_489(self):
        assert number_to_words(489) == 'four hundred and eighty nine'

    def test_1000(self):
        assert number_to_words(1000) == 'one thousand'

    def test_1001(self):
        assert number_to_words(1001) == 'one thousand and one'

    def test_1024(self):
        assert number_to_words(1024) == 'one thousand and twenty four'

    def test_63191(self):
        assert number_to_words(63191) == 'sixty three thousand one hundred and ninety one'

    def test_1000000(self):
        assert number_to_words(1000000) == 'one million'

    def test_1500000(self):
        assert number_to_words(1500000) == 'one million five hundred thousand'

    def test_1000001(self):
        assert number_to_words(1000001) == 'one million and one'

    def test_1001001(self):
        assert number_to_words(1001001) == 'one million one thousand and one'

    def test_billion(self):
        assert number_to_words(1000000000) == 'one billion'

    def test_max_supported(self):
        assert number_to_words(999999999999) == (
            'nine hundred and ninety nine billion '
            'nine hundred and ninety nine million '
            'nine hundred and ninety nine thousand '
            'nine hundred and ninety nine'
        )

    def test_over_max_returns_digits(self):
        assert number_to_words(1000000000000) == '1000000000000'

    def test_negative(self):
        assert number_to_words(-42) == 'minus forty two'

    def test_small_numbers(self):
        assert number_to_words(1) == 'one'
        assert number_to_words(19) == 'nineteen'
        assert number_to_words(20) == 'twenty'
        assert number_to_words(99) == 'ninety nine'

    def test_110(self):
        assert number_to_words(110) == 'one hundred and ten'

    def test_200(self):
        assert number_to_words(200) == 'two hundred'

    def test_10000(self):
        assert number_to_words(10000) == 'ten thousand'

    def test_and_placement_thousands(self):
        # "and" before last group only if < 100
        assert number_to_words(2050) == 'two thousand and fifty'
        assert number_to_words(2500) == 'two thousand five hundred'


class TestNormalizeLargeNumbers:
    def test_plain_3_digit(self):
        assert normalize_large_numbers('There are 489 tests') == (
            'There are four hundred and eighty nine tests'
        )

    def test_plain_5_digit(self):
        assert normalize_large_numbers('There are 63191 tests') == (
            'There are sixty three thousand one hundred and ninety one tests'
        )

    def test_plain_million(self):
        assert normalize_large_numbers('Found 1000000 results') == (
            'Found one million results'
        )

    def test_formatted_commas(self):
        assert normalize_large_numbers('Population: 63,191') == (
            'Population: sixty three thousand one hundred and ninety one'
        )

    def test_formatted_million_commas(self):
        assert normalize_large_numbers('Revenue was 1,000,000') == (
            'Revenue was one million'
        )

    def test_formatted_underscores(self):
        assert normalize_large_numbers('MAX = 1_000_000') == (
            'MAX = one million'
        )

    def test_formatted_apostrophes(self):
        assert normalize_large_numbers("Total: 63'191") == (
            'Total: sixty three thousand one hundred and ninety one'
        )

    def test_comma_list_not_matched(self):
        assert normalize_large_numbers('items 1, 2, 3') == 'items 1, 2, 3'

    def test_comma_list_larger(self):
        assert normalize_large_numbers('values 100, 200, 300') == (
            'values one hundred, two hundred, three hundred'
        )

    def test_hex_not_matched(self):
        assert normalize_large_numbers('color 0xFF00FF') == 'color 0xFF00FF'

    def test_hash_hex_not_matched(self):
        assert normalize_large_numbers('color #AABBCC') == 'color #AABBCC'

    def test_phone_not_matched(self):
        assert normalize_large_numbers('+61412345678') == '+61412345678'

    def test_ip_not_matched(self):
        assert normalize_large_numbers('server 192.168.1.1') == 'server 192.168.1.1'

    def test_decimal_not_matched(self):
        assert normalize_large_numbers('value 3.14159') == 'value 3.14159'

    def test_two_digit_untouched(self):
        assert normalize_large_numbers('only 42 items') == 'only 42 items'

    def test_single_digit_untouched(self):
        assert normalize_large_numbers('just 5') == 'just 5'

    def test_in_sentence(self):
        assert normalize_large_numbers('There are 63191 tests passing now') == (
            'There are sixty three thousand one hundred and ninety one tests passing now'
        )

    def test_multiple_numbers(self):
        result = normalize_large_numbers('from 1000 to 2000')
        assert 'one thousand' in result
        assert 'two thousand' in result

    def test_dot_thousands_two_groups(self):
        assert normalize_large_numbers('1.234.123') == (
            'one million two hundred and thirty four thousand one hundred and twenty three'
        )

    def test_dot_thousands_three_groups(self):
        assert normalize_large_numbers('1.234.567.890') == (
            'one billion two hundred and thirty four million five hundred and sixty seven thousand eight hundred and ninety'
        )

    def test_single_dot_not_matched(self):
        # Single dot = decimal, not thousand separator
        assert normalize_large_numbers('1.234') == '1.234'

    def test_very_large_fallback(self):
        result = normalize_large_numbers('id 1000000000000')
        # Should use digit-by-digit
        assert 'one' in result
        assert 'oh' in result or 'zero' in result
