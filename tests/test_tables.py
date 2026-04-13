"""Tests for pipe table and box-drawing table to speech conversion."""

import random

from wednesday_tts.normalize.tables import (
    _KNOWN_TOPICS,
    MARKDOWN_TABLE_RE,
    UNICODE_TABLE_RE,
    parse_table_rows,
    table_to_speech,
)

# --- parse_table_rows ---

def test_parse_simple_pipe_table():
    lines = [
        '| Name | Value |',
        '|------|-------|',
        '| foo  | 1     |',
        '| bar  | 2     |',
    ]
    headers, data = parse_table_rows(lines)
    assert headers == ['Name', 'Value']
    assert data == [['foo', '1'], ['bar', '2']]


def test_parse_separator_row_skipped():
    lines = [
        '| A | B |',
        '|---|---|',
        '| x | y |',
    ]
    headers, data = parse_table_rows(lines)
    assert headers == ['A', 'B']
    assert len(data) == 1
    assert data[0] == ['x', 'y']


def test_parse_colon_separator_skipped():
    lines = [
        '| Col |',
        '|:----|',
        '| val |',
    ]
    headers, data = parse_table_rows(lines)
    assert headers == ['Col']
    assert data == [['val']]


def test_parse_too_few_rows_returns_none():
    lines = ['| Only one row |']
    headers, data = parse_table_rows(lines)
    assert headers is None
    assert data is None


def test_parse_empty_lines_ignored():
    lines = [
        '| A | B |',
        '',
        '| 1 | 2 |',
        '',
        '| 3 | 4 |',
    ]
    headers, data = parse_table_rows(lines)
    assert headers == ['A', 'B']
    assert data == [['1', '2'], ['3', '4']]


def test_parse_box_drawing_table():
    lines = [
        '┌──────┬──────┐',
        '│ Name │ Val  │',
        '├──────┼──────┤',
        '│ foo  │ 1    │',
        '└──────┴──────┘',
    ]
    headers, data = parse_table_rows(lines)
    assert headers == ['Name', 'Val']
    assert data == [['foo', '1']]


def test_parse_strips_leading_trailing_pipes():
    lines = [
        '| X | Y |',
        '| a | b |',
    ]
    headers, data = parse_table_rows(lines)
    assert headers == ['X', 'Y']
    assert data[0] == ['a', 'b']


# --- table_to_speech ---

def test_speech_known_topic_has_preamble_sometimes():
    """Known topics get a preamble mentioning the topic (not every time)."""
    table = '| Name | Score |\n|------|-------|\n| Alice | 95 |'
    results = set()
    random.seed(0)
    for _ in range(50):
        results.add(table_to_speech(table).split('\n')[0])
    # At least one result should mention "names" in the preamble
    assert any('names' in r.lower() for r in results)


def test_speech_known_topic_word():
    table = '| Word | Meaning |\n|------|----------|\n| foo | bar |'
    random.seed(0)
    results = [table_to_speech(table).lower() for _ in range(30)]
    # "Word" header -> topic "words"; appears either in preamble or column header
    assert any('word' in r for r in results)


def test_speech_known_topic_command():
    table = '| Command | Description |\n|---------|-------------|\n| ls | list files |'
    random.seed(0)
    results = [table_to_speech(table).lower() for _ in range(30)]
    # "Command" header -> topic "commands"; appears either in preamble or column header
    assert any('command' in r for r in results)


def test_speech_unknown_header_skips_preamble_usually():
    """Unknown first-column headers mostly skip the preamble entirely."""
    table = '| Score | Player |\n|-------|--------|\n| 100 | Alice |'
    random.seed(0)
    results = [table_to_speech(table) for _ in range(40)]
    # Most results should start directly with row data (no "Table of" / topic)
    no_preamble = sum(1 for r in results if r.split('\n')[0].startswith('Score:') or r.split('\n')[0] == '100.')
    assert no_preamble > len(results) // 2


def test_speech_contains_cell_values():
    table = '| Name | Value |\n|------|-------|\n| foo | 42 |'
    result = table_to_speech(table)
    assert 'foo' in result
    assert '42' in result


def test_speech_row_ends_with_period():
    table = '| Item | Count |\n|------|-------|\n| apple | 3 |\n| banana | 5 |'
    result = table_to_speech(table)
    lines = result.strip().split('\n')
    # Every line (preamble or data) ends with a period
    for line in lines:
        assert line.endswith('.')


def test_speech_fallback_when_no_structure():
    # Only one parseable row -> fallback
    text = '| just one row |'
    result = table_to_speech(text)
    # Box chars removed, pipes replaced
    assert '|' not in result


def test_speech_multiple_data_rows():
    table = (
        '| Name | Age |\n'
        '|------|-----|\n'
        '| Alice | 30 |\n'
        '| Bob | 25 |\n'
        '| Carol | 28 |\n'
    )
    result = table_to_speech(table)
    assert 'Alice' in result
    assert 'Bob' in result
    assert 'Carol' in result


# --- regex patterns ---

def test_markdown_table_re_matches():
    table = '| A | B |\n|---|---|\n| 1 | 2 |\n'
    assert MARKDOWN_TABLE_RE.search(table) is not None


def test_markdown_table_re_no_match_single_row():
    single = '| A | B |\n'
    assert MARKDOWN_TABLE_RE.search(single) is None


def test_unicode_table_re_matches():
    table = '┌──┐\n│  │\n└──┘\n'
    assert UNICODE_TABLE_RE.search(table) is not None


def test_known_topic_singular_stripped():
    # Header "items" -> singular "item" -> known topic -> "items"
    table = '| Items | Value |\n|-------|-------|\n| x | 1 |'
    random.seed(42)
    result = table_to_speech(table)
    # Either gets a topic preamble with "items" or starts with data
    assert 'items' in result.lower() or result.startswith('Items:') or result.startswith('x')


def test_known_topic_param():
    table = '| Param | Default |\n|-------|----------|\n| timeout | 30 |'
    random.seed(42)
    result = table_to_speech(table)
    assert 'param' in result.lower() or result.startswith('timeout')


def test_known_topics_expanded():
    """New topic words like hotkey, shortcut, etc. are recognised."""
    for word in ('hotkey', 'shortcut', 'preference', 'hook', 'service',
                 'step', 'action', 'endpoint', 'route', 'tool', 'plugin'):
        assert word in _KNOWN_TOPICS


def test_speech_variation_across_runs():
    """Multiple calls produce varied preambles, not identical output."""
    table = '| Command | Description |\n|---------|-------------|\n| ls | list |\n| cd | change dir |'
    random.seed(0)
    results = set(table_to_speech(table) for _ in range(30))
    assert len(results) > 1
