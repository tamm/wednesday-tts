"""Tests for number, unit, storage, and HTTP code normalization."""

from wednesday_tts.normalize.numbers import (
    normalize_tilde_approx, normalize_fractions, normalize_time_units,
    normalize_storage_units, normalize_multipliers, normalize_small_decimals,
    normalize_regular_decimals, normalize_http_codes,
    normalize_repeated_punctuation, normalize_standalone_punctuation,
)


def test_tilde_approx():
    result = normalize_tilde_approx("~10 items")
    assert "around 10" in result


def test_percentage():
    result = normalize_tilde_approx("20% done")
    assert "20 percent" in result


def test_multiplication_sign():
    result = normalize_tilde_approx("10\u00d7 faster")
    assert "10 times" in result


def test_fraction():
    result = normalize_fractions("4/10 complete")
    assert "4 of 10" in result


def test_fraction_range():
    result = normalize_fractions("1-4/10 tasks")
    assert "1 to 4 of 10" in result


def test_time_ms():
    result = normalize_time_units("300ms")
    assert "300 milliseconds" in result


def test_time_seconds():
    result = normalize_time_units("0.5s")
    assert "seconds" in result
    assert "zero point five" in result


def test_time_range():
    result = normalize_time_units("2-4s")
    assert "2 to 4 seconds" in result


def test_storage_mb():
    result = normalize_storage_units("313MB")
    assert "313 megs" in result


def test_storage_gb_decimal():
    result = normalize_storage_units("1.4GB")
    assert "gigs" in result
    assert "1 point four" in result


def test_multiplier():
    result = normalize_multipliers("2x")
    assert "2 times" in result


def test_multiplier_decimal():
    result = normalize_multipliers("1.3x")
    assert "times" in result
    assert "1 point three" in result


def test_small_decimal():
    # DIGIT_WORDS maps '0' to 'oh', so digits are digit-by-digit spoken as 'oh two two'
    result = normalize_small_decimals("0.022")
    assert "zero point oh two two" in result


def test_leading_dot_decimal():
    result = normalize_small_decimals(".5")
    assert "point five" in result


def test_regular_decimal():
    result = normalize_regular_decimals("8.2")
    assert "8 point 2" in result


def test_http_code_with_context():
    result = normalize_http_codes("error 404")
    assert "four oh four" in result


def test_http_code_with_colon():
    result = normalize_http_codes("500: Server Error")
    assert "five oh oh" in result


def test_number_without_code_context():
    # "500 files" should stay as "500" (quantity, not a code)
    result = normalize_http_codes("500 files")
    assert "five oh oh" not in result
    assert "500" in result


def test_ellipsis():
    result = normalize_repeated_punctuation("wait...")
    assert "dot dot dot" in result


def test_double_dot():
    result = normalize_repeated_punctuation("hmm..")
    assert "dot dot" in result


def test_question_marks():
    result = normalize_repeated_punctuation("really???")
    assert "question marks" in result


def test_standalone_slash():
    result = normalize_standalone_punctuation("yes / no")
    assert "slash" in result
