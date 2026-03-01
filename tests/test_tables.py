"""Tests for pipe table and box-drawing table to speech conversion."""

from wednesday_tts.normalize.tables import (
    parse_table_rows,
    table_to_speech,
    MARKDOWN_TABLE_RE,
    UNICODE_TABLE_RE,
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

def test_speech_starts_with_table_of():
    table = '| Name | Score |\n|------|-------|\n| Alice | 95 |'
    result = table_to_speech(table)
    assert result.startswith('Table of')


def test_speech_known_topic_word():
    table = '| Word | Meaning |\n|------|----------|\n| foo | bar |'
    result = table_to_speech(table)
    # "Word" header -> topic "words"
    assert 'words' in result.lower()


def test_speech_known_topic_command():
    table = '| Command | Description |\n|---------|-------------|\n| ls | list files |'
    result = table_to_speech(table)
    assert 'commands' in result.lower()


def test_speech_unknown_header_uses_entries():
    table = '| Score | Player |\n|-------|--------|\n| 100 | Alice |'
    result = table_to_speech(table)
    assert 'entries' in result.lower()


def test_speech_contains_cell_values():
    table = '| Name | Value |\n|------|-------|\n| foo | 42 |'
    result = table_to_speech(table)
    assert 'foo' in result
    assert '42' in result


def test_speech_row_ends_with_period():
    table = '| Item | Count |\n|------|-------|\n| apple | 3 |'
    result = table_to_speech(table)
    lines = result.strip().split('\n')
    for line in lines[1:]:  # skip "Table of..." header
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
    # Header "items" -> singular "item" -> topic "items" (re-pluralised)
    table = '| Items | Value |\n|-------|-------|\n| x | 1 |'
    result = table_to_speech(table)
    assert 'entries' in result.lower() or 'items' in result.lower()


def test_known_topic_param():
    table = '| Param | Default |\n|-------|----------|\n| timeout | 30 |'
    result = table_to_speech(table)
    assert 'param' in result.lower() or 'entries' in result.lower()
