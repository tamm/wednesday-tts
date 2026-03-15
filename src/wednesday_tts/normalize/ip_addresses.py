"""Normalize IPv4 addresses to digit-by-digit spoken form."""

import re

from wednesday_tts.normalize.constants import DIGIT_WORDS

_IPV4_RE = re.compile(r'\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b')


def _octet_to_spoken(octet: str) -> str:
    return ' '.join(DIGIT_WORDS[c] for c in octet)


def _is_valid_ipv4(match: re.Match) -> bool:
    return all(0 <= int(match.group(i)) <= 255 for i in range(1, 5))


def normalize_ip_addresses(text: str) -> str:
    """Convert IPv4 addresses to spoken digit-by-digit form.

    192.168.1.1 -> "one nine two dot one six eight dot one dot one"

    Validates each octet is 0-255. Leaves port suffixes (e.g. :8080) untouched.
    Must run BEFORE dotted names and BEFORE number-to-words.
    """
    def replace(match: re.Match) -> str:
        if not _is_valid_ipv4(match):
            return match.group(0)
        octets = [_octet_to_spoken(match.group(i)) for i in range(1, 5)]
        return ' dot '.join(octets)

    return _IPV4_RE.sub(replace, text)
