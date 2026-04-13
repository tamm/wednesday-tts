"""Tests for fenced code block to spoken form conversion."""

import re

from wednesday_tts.normalize.code_blocks import code_block_to_speech, normalize_code_blocks


def _make_match(content):
    """Build a fake re.Match-like object with group(1) returning content."""
    m = re.search(r'```[a-zA-Z]*\n?([\s\S]*?)```', f'```\n{content}\n```')
    return m


# --- code_block_to_speech ---

def test_empty_block_returns_space():
    m = _make_match('')
    result = code_block_to_speech(m)
    assert result.strip() == ''


def test_single_line_code():
    m = _make_match('print("hello")')
    result = code_block_to_speech(m)
    assert result.startswith(' Code:')
    assert 'print' in result


def test_multiple_lines_joined_with_periods():
    m = _make_match('foo = 1\nbar = 2')
    result = code_block_to_speech(m)
    assert 'foo = 1' in result
    assert 'bar = 2' in result
    assert '. ' in result


def test_truncates_at_8_lines():
    lines = '\n'.join(f'line{i} = {i}' for i in range(12))
    m = _make_match(lines)
    result = code_block_to_speech(m)
    assert 'and more.' in result
    # Should not contain lines beyond index 7
    assert 'line8' not in result
    assert 'line9' not in result


def test_exactly_8_lines_not_truncated():
    lines = '\n'.join(f'x = {i}' for i in range(8))
    m = _make_match(lines)
    result = code_block_to_speech(m)
    assert 'and more.' not in result


def test_strips_tree_drawing_chars():
    tree = '├── src\n└── tests'
    m = _make_match(tree)
    result = code_block_to_speech(m)
    assert '├' not in result
    assert '└' not in result
    assert 'src' in result
    assert 'tests' in result


def test_blank_lines_in_code_are_skipped():
    m = _make_match('alpha\n\n\nbeta')
    result = code_block_to_speech(m)
    assert 'alpha' in result
    assert 'beta' in result


def test_block_with_only_tree_chars_returns_space():
    m = _make_match('├──\n└──\n│')
    result = code_block_to_speech(m)
    assert result.strip() == ''


def test_result_wrapped_in_code_label():
    m = _make_match('x = 1')
    result = code_block_to_speech(m)
    assert ' Code: ' in result
    assert result.endswith('. ')


# --- normalize_code_blocks ---

def test_normalize_replaces_fenced_block():
    text = 'Here is some code:\n```python\nprint("hi")\n```\nDone.'
    result = normalize_code_blocks(text)
    assert '```' not in result
    assert 'Code:' in result
    assert 'print' in result


def test_normalize_language_tag_stripped():
    text = '```bash\nls -la\n```'
    result = normalize_code_blocks(text)
    assert '```' not in result
    assert 'ls -la' in result


def test_normalize_no_language_tag():
    text = '```\nhello\n```'
    result = normalize_code_blocks(text)
    assert '```' not in result
    assert 'hello' in result


def test_normalize_multiple_blocks():
    text = '```\nfirst\n```\nsome text\n```\nsecond\n```'
    result = normalize_code_blocks(text)
    assert '```' not in result
    assert 'first' in result
    assert 'second' in result


def test_normalize_no_blocks_passthrough():
    text = 'Just plain text with no code.'
    result = normalize_code_blocks(text)
    assert result == text


def test_normalize_empty_block():
    text = 'before\n```\n```\nafter'
    result = normalize_code_blocks(text)
    assert '```' not in result
    assert 'before' in result
    assert 'after' in result
