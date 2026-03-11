"""Tests for markdown, emoji, and whitespace cleanup for TTS."""

from wednesday_tts.normalize.markdown import clean_text_for_speech


# --- em/en dash ---

def test_em_dash_becomes_comma_pause():
    result = clean_text_for_speech('before \u2014 after')
    assert ',' in result
    assert '\u2014' not in result


def test_en_dash_becomes_comma_pause():
    result = clean_text_for_speech('before \u2013 after')
    assert ',' in result
    assert '\u2013' not in result


def test_em_dash_no_surrounding_space():
    result = clean_text_for_speech('one\u2014two')
    assert '\u2014' not in result


# --- emoji and symbol replacements ---

def test_checkmark_unicode_2713():
    result = clean_text_for_speech('\u2713 done')
    assert 'check' in result


def test_checkmark_unicode_2705():
    result = clean_text_for_speech('\u2705 done')
    assert 'check' in result


def test_x_mark_274c():
    result = clean_text_for_speech('\u274c failed')
    assert 'x' in result.lower()


def test_right_arrow_becomes_to():
    result = clean_text_for_speech('A \u2192 B')
    assert 'to' in result


def test_thumbs_up():
    result = clean_text_for_speech('\U0001f44d great')
    assert 'thumbs up' in result


def test_thumbs_down():
    result = clean_text_for_speech('\U0001f44e bad')
    assert 'thumbs down' in result


def test_fire_emoji():
    result = clean_text_for_speech('\U0001f525 hot')
    assert 'fire' in result


def test_bug_emoji():
    result = clean_text_for_speech('\U0001f41b issue')
    assert 'bug' in result


def test_robot_emoji():
    result = clean_text_for_speech('\U0001f916 ai')
    assert 'robot' in result


def test_bullet_point_removed():
    result = clean_text_for_speech('\u2022 item one')
    assert '\u2022' not in result


def test_middle_dot_removed():
    result = clean_text_for_speech('a\u00b7b')
    assert '\u00b7' not in result


def test_ellipsis_char_replaced():
    # … -> '...' via SPOKEN_REPLACEMENTS, then collapsed to '.' by multi-dot rule
    result = clean_text_for_speech('wait\u2026')
    assert '\u2026' not in result
    assert '.' in result


def test_copyright_symbol():
    result = clean_text_for_speech('\u00a9 2024')
    assert 'copyright' in result


def test_trademark_symbol():
    result = clean_text_for_speech('Brand\u2122')
    assert 'trademark' in result


# --- inline code ---

def test_inline_code_backticks_stripped():
    result = clean_text_for_speech('Run `ls -la` now.')
    assert '`' not in result
    assert 'ls -la' in result


def test_inline_code_multiple():
    result = clean_text_for_speech('Use `foo` or `bar`.')
    assert '`' not in result
    assert 'foo' in result
    assert 'bar' in result


# --- markdown formatting ---

def test_bold_double_asterisk_stripped():
    result = clean_text_for_speech('**important** text')
    assert '**' not in result
    assert 'important' in result


def test_bold_double_underscore_stripped():
    result = clean_text_for_speech('__important__ text')
    assert '__' not in result
    assert 'important' in result


def test_italic_asterisk_stripped():
    result = clean_text_for_speech('*emphasis* here')
    assert '*' not in result
    assert 'emphasis' in result


def test_italic_underscore_kept_for_emphasis():
    """Single underscores are TTS emphasis markers — they must survive."""
    result = clean_text_for_speech('_emphasis_ here')
    assert '_emphasis_' in result
    assert 'emphasis' in result


def test_h1_header_stripped():
    result = clean_text_for_speech('# Title')
    assert '#' not in result
    assert 'Title' in result


def test_h3_header_stripped():
    result = clean_text_for_speech('### Section')
    assert '#' not in result
    assert 'Section' in result


def test_blockquote_stripped():
    result = clean_text_for_speech('> quoted text')
    assert '>' not in result
    assert 'quoted text' in result


def test_unordered_list_dash_stripped():
    result = clean_text_for_speech('- item one')
    assert result.strip().startswith('item one')


def test_unordered_list_asterisk_stripped():
    result = clean_text_for_speech('* item two')
    assert 'item two' in result
    assert result.strip()[0] != '*'


def test_unordered_list_plus_stripped():
    result = clean_text_for_speech('+ item three')
    assert 'item three' in result


def test_ordered_list_stripped():
    result = clean_text_for_speech('1. first item')
    assert 'first item' in result
    assert '1.' not in result


def test_ordered_list_large_number():
    result = clean_text_for_speech('10. tenth item')
    assert 'tenth item' in result
    assert '10.' not in result


# --- URLs and links ---

def test_bare_url_removed():
    result = clean_text_for_speech('Visit https://example.com for more.')
    assert 'https' not in result
    assert 'example.com' not in result


def test_markdown_link_text_kept_url_dropped():
    result = clean_text_for_speech('[click here](https://example.com)')
    assert 'click here' in result
    assert 'https' not in result
    assert 'example.com' not in result


def test_markdown_link_with_title():
    result = clean_text_for_speech('[docs](https://docs.example.com/path)')
    assert 'docs' in result
    assert 'example.com' not in result


# --- punctuation and brackets ---

def test_curly_braces_removed():
    result = clean_text_for_speech('{key: value}')
    assert '{' not in result
    assert '}' not in result


def test_square_brackets_removed():
    result = clean_text_for_speech('[note]')
    assert '[' not in result
    assert ']' not in result


def test_parentheses_removed():
    result = clean_text_for_speech('word (clarification)')
    assert '(' not in result
    assert ')' not in result


def test_backslash_removed():
    result = clean_text_for_speech('line one\\nline two')
    assert '\\' not in result


def test_double_dash_becomes_space():
    result = clean_text_for_speech('before--after')
    assert '--' not in result


def test_triple_dash_becomes_space():
    result = clean_text_for_speech('before---after')
    assert '---' not in result


def test_double_underscore_in_non_bold_context():
    result = clean_text_for_speech('some__thing')
    assert '__' not in result


# --- whitespace and newlines ---

def test_multiple_newlines_collapsed():
    result = clean_text_for_speech('para one\n\n\npara two')
    assert '\n\n' not in result


def test_single_newline_becomes_period_space():
    result = clean_text_for_speech('line one\nline two')
    assert 'line one' in result
    assert 'line two' in result
    assert '. ' in result


def test_multiple_spaces_collapsed():
    result = clean_text_for_speech('too   many   spaces')
    assert '  ' not in result


def test_repeated_dots_collapsed():
    result = clean_text_for_speech('end... more')
    # Multiple dots should collapse to a single dot
    assert '...' not in result or result.count('.') <= 2


def test_strips_leading_trailing_whitespace():
    result = clean_text_for_speech('   hello world   ')
    assert result == result.strip()


# --- stray pipes (fallback after table handling) ---

def test_stray_pipe_replaced_with_space():
    result = clean_text_for_speech('a | b | c')
    assert '|' not in result


# --- dash separator lines ---

def test_horizontal_rule_dashes_removed():
    result = clean_text_for_speech('---')
    assert '---' not in result


def test_colon_dash_separator_removed():
    result = clean_text_for_speech(':---:')
    assert ':---:' not in result


# --- integration: realistic response snippet ---

def test_realistic_response_snippet():
    text = (
        '## Summary\n'
        '\n'
        '- **Done**: extracted modules\n'
        '- *Next*: write tests\n'
        '\n'
        'See [the docs](https://example.com/docs) for details.\n'
    )
    result = clean_text_for_speech(text)
    assert '#' not in result
    assert '**' not in result
    assert '*' not in result
    assert 'example.com' not in result
    assert 'Done' in result
    assert 'extracted modules' in result
    assert 'write tests' in result
    assert 'the docs' in result
