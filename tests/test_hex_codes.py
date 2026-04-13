"""Tests for hex code normalization."""

from wednesday_tts.normalize.hex_codes import normalize_hex_codes


class TestOxPrefix:
    def test_0xff(self):
        assert normalize_hex_codes("0xFF") == "hex Ef Ef"

    def test_0x1a2b(self):
        assert normalize_hex_codes("0x1A2B") == "hex one Ay two Bee"

    def test_0xdeadbeef(self):
        assert normalize_hex_codes("0xDEADBEEF") == "hex Dee Ee Ay Dee Bee Ee Ee Ef"

    def test_0x_lowercase(self):
        assert normalize_hex_codes("0xff") == "hex Ef Ef"

    def test_0x_odd_length(self):
        assert normalize_hex_codes("0xFFF") == "hex Ef Ef Ef"

    def test_0x_single_char(self):
        assert normalize_hex_codes("0x0") == "hex oh"

    def test_0x_single_hex_letter(self):
        assert normalize_hex_codes("0xA") == "hex Ay"


class TestHashPrefix:
    def test_ff00aa(self):
        assert normalize_hex_codes("#FF00AA") == "hash Ef Ef oh oh Ay Ay"

    def test_fff_short(self):
        assert normalize_hex_codes("#fff") == "hash Ef Ef Ef"

    def test_333(self):
        assert normalize_hex_codes("#333") == "hash three three three"

    def test_000(self):
        assert normalize_hex_codes("#000") == "hash oh oh oh"

    def test_a3b2c1(self):
        assert normalize_hex_codes("#a3b2c1") == "hash Ay three Bee two See one"

    def test_lowercase_hex(self):
        assert normalize_hex_codes("#ff00aa") == "hash Ef Ef oh oh Ay Ay"


class TestCaseHandling:
    def test_letters_spoken_uppercase(self):
        result = normalize_hex_codes("0xff")
        assert "Ef Ef" in result

    def test_hash_letters_uppercase(self):
        result = normalize_hex_codes("#aabb00")
        assert "Ay Ay Bee Bee" in result


class TestInSentence:
    def test_mixed_sentence(self):
        result = normalize_hex_codes("Color is #FF00AA and address is 0x1A2B")
        assert result == "Color is hash Ef Ef oh oh Ay Ay and address is hex one Ay two Bee"

    def test_multiple_hex(self):
        result = normalize_hex_codes("0xFF and 0xAB")
        assert result == "hex Ef Ef and hex Ay Bee"


class TestNotHex:
    def test_plain_number(self):
        assert normalize_hex_codes("1234") == "1234"

    def test_word(self):
        assert normalize_hex_codes("hello") == "hello"

    def test_css_class_not_hex(self):
        # #main contains non-hex letters (m, i, n)
        assert normalize_hex_codes("#main") == "#main"

    def test_hash_with_non_hex_chars(self):
        assert normalize_hex_codes("#header") == "#header"

    def test_hash_wrong_length(self):
        # 4 chars is not a standard color code length
        assert normalize_hex_codes("#FFAA") == "#FFAA"

    def test_hash_5_chars(self):
        assert normalize_hex_codes("#FFAAB") == "#FFAAB"


class TestEdgeCases:
    def test_0x0(self):
        assert normalize_hex_codes("0x0") == "hex oh"

    def test_hash_000(self):
        assert normalize_hex_codes("#000") == "hash oh oh oh"

    def test_empty_string(self):
        assert normalize_hex_codes("") == ""

    def test_no_hex(self):
        assert normalize_hex_codes("just some text") == "just some text"
