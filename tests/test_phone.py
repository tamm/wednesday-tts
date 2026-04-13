"""Tests for phone number normalization."""

from wednesday_tts.normalize.phone import normalize_phone_numbers

# --- Aussie mobile ---


def test_mobile_with_spaces():
    assert (
        normalize_phone_numbers("0412 345 678")
        == "oh four one two, three four five, six seven eight"
    )


def test_mobile_compact():
    assert (
        normalize_phone_numbers("0412345678") == "oh four one two, three four five, six seven eight"
    )


def test_mobile_alt_grouping():
    result = normalize_phone_numbers("04 1234 5678")
    assert result == "oh four, one two three four, five six seven eight"


# --- Aussie landline ---


def test_landline_with_space():
    assert (
        normalize_phone_numbers("02 9876 5432")
        == "oh two, nine eight seven six, five four three two"
    )


def test_landline_with_parens():
    assert (
        normalize_phone_numbers("(02) 9876 5432")
        == "oh two, nine eight seven six, five four three two"
    )


def test_landline_compact():
    assert (
        normalize_phone_numbers("0298765432") == "oh two, nine eight seven six, five four three two"
    )


# --- International ---


def test_international_au():
    result = normalize_phone_numbers("+61 2 9876 5432")
    assert result == "plus six one, two, nine eight seven six, five four three two"


def test_international_au_mobile():
    result = normalize_phone_numbers("+61412345678")
    assert result == "plus six one, four, one two three four, five six seven eight"


def test_international_us():
    result = normalize_phone_numbers("+1 555 1234")
    assert result == "plus one, five five five, one two three four"


# --- Service numbers ---


def test_1300():
    assert normalize_phone_numbers("1300 655 506") == "thirteen hundred, six five five, five oh six"


def test_1800():
    assert (
        normalize_phone_numbers("1800 123 456") == "one eight hundred, one two three, four five six"
    )


def test_13_service():
    assert normalize_phone_numbers("13 22 33") == "thirteen, two two, three three"


def test_000():
    assert normalize_phone_numbers("000") == "oh oh oh"


# --- NOT phone numbers ---


def test_plain_number_not_phone():
    assert normalize_phone_numbers("there are 63191 items") == "there are 63191 items"


def test_port_not_phone():
    assert normalize_phone_numbers("port 5678") == "port 5678"


def test_plain_thousand_not_phone():
    assert normalize_phone_numbers("about 1000 people") == "about 1000 people"


# --- In sentence context ---


def test_phone_in_sentence():
    result = normalize_phone_numbers("Call 0412 345 678 for info")
    assert result == "Call oh four one two, three four five, six seven eight for info"


def test_landline_in_sentence():
    result = normalize_phone_numbers("Ring (02) 9876 5432 today")
    assert result == "Ring oh two, nine eight seven six, five four three two today"


def test_multiple_phones():
    result = normalize_phone_numbers("Mobile: 0412 345 678 or landline: 02 9876 5432")
    assert "oh four one two" in result
    assert "oh two" in result
