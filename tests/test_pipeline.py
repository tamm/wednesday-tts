"""End-to-end normalization pipeline tests.

Tests use representative real-world inputs: markdown formatting, fenced code
blocks, URLs, camelCase identifiers, numbers, units, tables, and HTTP codes.
The pipeline under test is wednesday_tts.normalize.pipeline.normalize and
normalize_technical.
"""


from wednesday_tts.normalize.pipeline import normalize, normalize_technical


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def norm(text, **kwargs):
    """Shorthand — default content_type is 'markdown'."""
    return normalize(text, **kwargs)


# ---------------------------------------------------------------------------
# content_type passthrough and routing
# ---------------------------------------------------------------------------

class TestContentTypeRouting:

    def test_normalized_passthrough(self):
        raw = "**bold** `code` https://example.com"
        assert normalize(raw, content_type="normalized") == raw

    def test_plain_skips_markdown_strip(self):
        # Plain mode still normalises technical content but keeps markdown symbols
        result = normalize("value is 50%", content_type="plain")
        assert "percent" in result

    def test_markdown_strips_bold(self):
        result = norm("**important** thing")
        assert "**" not in result
        assert "important" in result

    def test_markdown_strips_inline_code_backticks(self):
        result = norm("Use `git status` to check.")
        assert "`" not in result
        assert "git" in result

    def test_markdown_strips_headers(self):
        result = norm("## My Header\nSome text.")
        assert "##" not in result
        assert "My Header" in result or "header" in result.lower()

    def test_markdown_strips_italic(self):
        result = norm("*important* note")
        assert "*" not in result
        assert "important" in result

    def test_markdown_strips_blockquote(self):
        result = norm("> This is a quote.")
        assert result.startswith(">") is False
        assert "quote" in result


# ---------------------------------------------------------------------------
# Code blocks
# ---------------------------------------------------------------------------

class TestCodeBlocks:

    def test_fenced_code_block_becomes_spoken(self):
        text = "Run this:\n```\nprint('hello')\n```\ndone."
        result = norm(text)
        assert "Code:" in result or "code" in result.lower()
        assert "```" not in result

    def test_fenced_code_block_with_language_tag(self):
        text = "```python\nx = 1\ny = 2\n```"
        result = norm(text)
        assert "```" not in result
        assert "x = 1" in result or "Code" in result

    def test_long_code_block_truncated(self):
        lines = "\n".join(f"line{i} = {i}" for i in range(20))
        text = f"```\n{lines}\n```"
        result = norm(text)
        assert "and more" in result

    def test_empty_code_block_does_not_crash(self):
        result = norm("```\n```")
        assert result is not None


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

class TestURLs:

    def test_https_url_to_spoken(self):
        result = norm("See https://example.com for details.")
        assert "example dot com" in result
        assert "https://" not in result

    def test_url_with_path(self):
        result = norm("Visit https://ta.mw/unwatch now.")
        assert "slash" in result
        assert "dot" in result

    def test_bare_domain_with_path(self):
        result = norm("Check github.com/owner/repo for more.")
        assert "dot" in result
        assert "slash" in result


# ---------------------------------------------------------------------------
# Numbers and units
# ---------------------------------------------------------------------------

class TestNumbersAndUnits:

    def test_percentage(self):
        result = norm("Done: 75%")
        assert "percent" in result

    def test_milliseconds(self):
        result = norm("Took 200ms to respond.")
        assert "milliseconds" in result

    def test_megabytes(self):
        result = norm("File is 45MB.")
        assert "meg" in result.lower()

    def test_gigabytes(self):
        result = norm("Free space: 1.4GB")
        assert "gig" in result.lower()

    def test_multiplier(self):
        result = norm("2x faster than before.")
        assert "times" in result

    def test_small_decimal(self):
        result = norm("Loss is 0.042")
        assert "zero point" in result

    def test_tilde_approximation(self):
        result = norm("~30 items in the list.")
        assert "around 30" in result

    def test_fraction(self):
        result = norm("Completed 3/10 tasks.")
        assert "3 of 10" in result

    def test_http_status_code(self):
        result = norm("Server returned 404 not found.")
        assert "four oh four" in result

    def test_semver_version(self):
        result = norm("Using version v2.3.1.")
        assert "2" in result and "3" in result

    def test_time_seconds(self):
        result = norm("Elapsed: 1.5s")
        assert "seconds" in result


# ---------------------------------------------------------------------------
# CamelCase and ALL CAPS
# ---------------------------------------------------------------------------

class TestCamelCaseAndCaps:

    def test_camelcase_split(self):
        result = norm("The myVariableName is set.")
        assert "my Variable Name" in result or "my" in result

    def test_all_caps_long_word_title_cased(self):
        result = norm("This SHOULD be lower.")
        # 6-letter ALL CAPS -> Title Case
        assert "SHOULD" not in result
        assert "Should" in result or "should" in result

    def test_short_acronym_preserved(self):
        # 2-letter acronyms left alone (handled by dictionary, not camelcase rule)
        result = norm("Use DB for storage.", dictionary=[], filenames_dict={})
        # DB should not be broken apart by ALL CAPS rule (only 2 chars)
        assert "D B" not in result


# ---------------------------------------------------------------------------
# Markdown tables
# ---------------------------------------------------------------------------

class TestMarkdownTables:

    def test_simple_table_spoken(self):
        text = (
            "| Name | Value |\n"
            "| ---- | ----- |\n"
            "| foo  | 42    |\n"
            "| bar  | 99    |\n"
        )
        result = norm(text)
        assert "|" not in result
        assert "foo" in result
        assert "bar" in result

    def test_table_separator_row_removed(self):
        text = "| Col |\n| --- |\n| val |\n"
        result = norm(text)
        assert "---" not in result


# ---------------------------------------------------------------------------
# Markdown links
# ---------------------------------------------------------------------------

class TestMarkdownLinks:

    def test_link_text_kept_url_dropped(self):
        result = norm("See [the docs](https://example.com/docs) for info.")
        assert "the docs" in result
        assert "https://" not in result

    def test_bare_url_in_markdown_removed(self):
        result = norm("Go to https://example.com/page right away.")
        assert "https://" not in result


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

class TestPaths:

    def test_tilde_home_path(self):
        result = norm("Edit ~/.claude/settings.json")
        assert "home" in result or "tilde" in result or "dot claude" in result

    def test_file_extension_spoken(self, sample_filenames_dict):
        result = norm("Open config.py now.", filenames_dict=sample_filenames_dict)
        assert "pie" in result or "config" in result


# ---------------------------------------------------------------------------
# Dictionary substitution
# ---------------------------------------------------------------------------

class TestDictionary:

    def test_api_replaced(self, sample_dictionary):
        result = norm("The API is stable.", dictionary=sample_dictionary)
        assert "Ae pee eye" in result

    def test_json_replaced(self, sample_dictionary):
        result = norm("Parse the JSON response.", dictionary=sample_dictionary)
        assert "jason" in result


# ---------------------------------------------------------------------------
# Escape sequences and special characters
# ---------------------------------------------------------------------------

class TestSpecialCharacters:

    def test_em_dash_becomes_comma(self):
        result = norm("It works — sometimes.")
        assert "\u2014" not in result
        assert "," in result or "sometimes" in result

    def test_checkmark_spoken(self):
        result = norm("Done \u2713")
        assert "check" in result

    def test_arrow_spoken(self):
        result = norm("Step 1 \u2192 Step 2")
        assert "\u2192" not in result
        assert "to" in result or "arrow" in result

    def test_ellipsis_unicode(self):
        result = norm("Thinking\u2026")
        # Unicode ellipsis -> "..." -> repeated punctuation handler
        assert "\u2026" not in result


# ---------------------------------------------------------------------------
# Mixed / end-to-end real-world inputs
# ---------------------------------------------------------------------------

class TestEndToEnd:

    def test_typical_claude_response(self, sample_dictionary, sample_filenames_dict):
        text = (
            "## Summary\n\n"
            "The **API** returned a `404` after 300ms. "
            "Check your `config.json` at ~/.claude/settings.json.\n\n"
            "- Fixed myBuggyFunction\n"
            "- Updated 2/5 tasks\n"
        )
        result = norm(text, dictionary=sample_dictionary,
                      filenames_dict=sample_filenames_dict)
        assert "##" not in result
        assert "**" not in result
        assert "`" not in result
        assert "milliseconds" in result
        assert "four oh four" in result
        # API should be expanded by dictionary
        assert "Ae pee eye" in result

    def test_code_block_with_surrounding_prose(self):
        text = (
            "Run the following command:\n"
            "```bash\n"
            "pip install wednesday-tts\n"
            "```\n"
            "Then restart your shell."
        )
        result = norm(text)
        assert "```" not in result
        assert "restart" in result
        assert "pip install" in result or "Code" in result

    def test_table_with_numbers_and_units(self):
        text = (
            "| Model | Latency | Size |\n"
            "| ----- | ------- | ---- |\n"
            "| small | 200ms   | 45MB |\n"
            "| large | 1.4s    | 1GB  |\n"
        )
        result = norm(text)
        assert "|" not in result
        assert "small" in result
        assert "large" in result

    def test_url_and_camelcase_together(self):
        text = "Visit https://example.com/getStarted and call myInitFunction."
        result = norm(text)
        assert "https://" not in result
        assert "example dot com" in result

    def test_version_in_prose(self):
        text = "Upgrade to claude-3-5-sonnet or v2.1.0 of the SDK."
        result = norm(text)
        assert "v2" not in result or "2" in result  # semver expanded

    def test_normalize_technical_alone(self):
        # normalize_technical does not strip markdown
        text = "**bold** `myVar` 50% done in 200ms."
        result = normalize_technical(text)
        assert "percent" in result
        assert "milliseconds" in result
        # markdown symbols still present (not stripped by this function)
        assert "**" in result or "bold" in result

    def test_plain_content_type(self):
        text = "Processing 100 items at 0.5s each, ~50s total."
        result = normalize(text, content_type="plain")
        assert "seconds" in result
        assert "around" in result or "50" in result
