"""Main normalization pipeline — calls modules in order."""

from wednesday_tts.normalize.code_blocks import normalize_code_blocks
from wednesday_tts.normalize.urls import normalize_urls
from wednesday_tts.normalize.identifiers import (
    normalize_identifiers, normalize_escape_sequences, normalize_hashes,
    normalize_dotted_names,
)
from wednesday_tts.normalize.hex_codes import normalize_hex_codes
from wednesday_tts.normalize.ip_addresses import normalize_ip_addresses
from wednesday_tts.normalize.phone import normalize_phone_numbers
from wednesday_tts.normalize.regex_speech import (
    normalize_regex, normalize_html_tags, normalize_hotkeys,
)
from wednesday_tts.normalize.operators import (
    normalize_operators, normalize_negative_numbers, normalize_word_dash_number,
)
from wednesday_tts.normalize.paths import (
    normalize_file_extensions, normalize_tilde_paths, normalize_slash_paths,
)
from wednesday_tts.normalize.dictionary import apply_dictionary
from wednesday_tts.normalize.homographs import fix_read_homograph
from wednesday_tts.normalize.numbers import (
    normalize_tilde_approx, normalize_fractions, normalize_time_units,
    normalize_storage_units, normalize_multipliers, normalize_small_decimals,
    normalize_regular_decimals, normalize_http_codes, normalize_repeated_punctuation,
    normalize_standalone_punctuation,
)
from wednesday_tts.normalize.dates import normalize_years, normalize_dates
from wednesday_tts.normalize.versions import normalize_model_versions, normalize_semver
from wednesday_tts.normalize.numbers_to_words import normalize_large_numbers
from wednesday_tts.normalize.camelcase import normalize_all_caps, normalize_camelcase
from wednesday_tts.normalize.markdown import clean_text_for_speech


def normalize_technical(text, dictionary=None, filenames_dict=None):
    """Convert technical text to natural spoken form before TTS.

    This is the equivalent of the old tts_normalize() function.
    Handles: URLs, paths, numbers, abbreviations via dictionary.
    Run BEFORE markdown stripping so we can see the original content.
    """
    if dictionary is None:
        dictionary = []
    if filenames_dict is None:
        filenames_dict = {}

    # 0-pre. URLs first — consume whole before any other rule mangles their internals
    text = normalize_urls(text)

    # 0. Process inline backtick content as code identifiers
    text = normalize_identifiers(text)

    # 0a-pre. Literal escape sequences
    text = normalize_escape_sequences(text)

    # 0a-hex. Hex codes (0xFF, #FF00AA) — before hashes grab # prefixes
    text = normalize_hex_codes(text)

    # 0a. Hash-number references and standalone hashes
    text = normalize_hashes(text)

    # 0b-pre. Regex patterns -> spoken description
    text = normalize_regex(text)

    # 0a-html. HTML/XML tags -> spoken form
    text = normalize_html_tags(text)

    # 0a-hotkey. Keyboard shortcuts
    text = normalize_hotkeys(text)

    # 0b. Operators -> spoken form
    text = normalize_operators(text)

    # 0b2. Negative numbers
    text = normalize_negative_numbers(text)

    # 0c. Word-dash-number
    text = normalize_word_dash_number(text)

    # 0d. File extensions
    text = normalize_file_extensions(text, filenames_dict)

    # 1. Apply custom dictionary
    text = apply_dictionary(text, dictionary)

    # 1-homograph. Fix context-sensitive homographs
    text = fix_read_homograph(text)

    # 1-phone. Phone numbers — consume before number-to-words
    text = normalize_phone_numbers(text)

    # 1a. Tilde approximation, multiplication sign, percentages
    text = normalize_tilde_approx(text)

    # 1b. Progress fractions
    text = normalize_fractions(text)

    # 1b2. Slash-format dates (must run AFTER fractions, BEFORE slash paths)
    text = normalize_dates(text)

    # 1b3. Standalone years (must run BEFORE generic number rules eat them)
    text = normalize_years(text)

    # 1c. Model/tool version strings
    text = normalize_model_versions(text)

    # 4a. Tilde paths
    text = normalize_tilde_paths(text)

    # 4b. Remaining slash-separated content
    text = normalize_slash_paths(text)

    # 4b2. IPv4 addresses (before dotted names eat the dots)
    text = normalize_ip_addresses(text)

    # 4c-pre. Dotted names: module.attr, os.path.join, example.com
    # Runs AFTER slash_paths so path dots inside segments (some.file) are also caught.
    text = normalize_dotted_names(text)

    # 4c. Time/unit abbreviations (must run BEFORE small-decimal rule)
    text = normalize_time_units(text)

    # 4c2. Storage/byte units
    text = normalize_storage_units(text)

    # 4c3. Multipliers
    text = normalize_multipliers(text)

    # 4d. Small decimals
    text = normalize_small_decimals(text)

    # 4e. Regular decimals
    text = normalize_regular_decimals(text)

    # 5. Version strings (v-prefixed and multi-dot)
    text = normalize_semver(text)

    # 6b. 3-digit HTTP-like codes
    text = normalize_http_codes(text)

    # 6c. Large numbers (3+ digits) to spoken words
    text = normalize_large_numbers(text)

    # 7. Repeated punctuation
    text = normalize_repeated_punctuation(text)

    # 7b-c. Standalone punctuation
    text = normalize_standalone_punctuation(text)

    # 8. ALL CAPS -> Title Case
    text = normalize_all_caps(text)

    # 9. Camel-case splitting
    text = normalize_camelcase(text)

    return text


def normalize(text, content_type="markdown", dictionary=None, filenames_dict=None):
    """Full normalization pipeline: raw text -> TTS-ready spoken form.

    Args:
        text: Raw input text
        content_type: "markdown" (full pipeline), "plain" (general rules only),
                     or "normalized" (passthrough)
        dictionary: Resolved pronunciation dictionary entries
        filenames_dict: File extension pronunciation dict

    Returns:
        Normalized text ready for TTS synthesis
    """
    if content_type == "normalized":
        return text

    if content_type == "markdown":
        # 1. Convert code blocks to spoken form first
        text = normalize_code_blocks(text)

    # 2. Normalize technical content
    text = normalize_technical(text, dictionary, filenames_dict)

    if content_type == "markdown":
        # 3. Strip remaining markdown formatting
        text = clean_text_for_speech(text)

    return text
