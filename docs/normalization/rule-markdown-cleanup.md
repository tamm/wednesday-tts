# Rule: Markdown Cleanup

**What it does**: Strips all remaining markdown formatting, converts emoji/symbol characters to spoken words or spaces, converts code blocks to a brief spoken description, and normalises whitespace so the TTS engine receives clean prose.

**Pipeline position**: `clean_text_for_speech()` — runs after `tts_normalize()` completes. This is a separate function, not a step inside `tts_normalize()`. It is the third stage of the overall pipeline:

```
1. _code_block_to_speech()   — fenced code blocks → spoken (called in main() before tts_normalize)
2. tts_normalize()           — technical content → spoken form
3. clean_text_for_speech()   — markdown/symbols stripped, whitespace normalised
4. ollama_normalize()        — optional LLM polish (TTS_USE_LLM=1)
```

Note: fenced code blocks are also handled inside `clean_text_for_speech()` as a backup pass for any blocks not caught in step 1.

## Examples

| Input                              | Output                                             |
| ---------------------------------- | -------------------------------------------------- |
| `**bold text**`                    | `bold text`                                        |
| `_italic_`                         | `italic`                                           |
| `## Heading`                       | `Heading`                                          |
| `> blockquote`                     | `blockquote`                                       |
| `- list item`                      | `list item`                                        |
| `1. ordered item`                  | `ordered item`                                     |
| `` `code` ``                       | `code` (backticks stripped, content kept)          |
| `✓ done`                           | ` check  done`                                     |
| `→ next`                           | ` to  next`                                        |
| `⚠️ warning`                       | ` warning  warning`                                |
| `…`                                | `...` (then later collapsed by `\.(\s*\.)+` → `.`) |
| `—`                                | `, ` (em dash → comma-space)                       |
| `[link text](https://example.com)` | `link text`                                        |
| `\| col1 \| col2 \|`               | `col1  col2`                                       |
| `https://example.com`              | `` (bare URLs silently removed)                    |

## Pattern / logic

### Symbol/emoji replacements

Applied first as `str.replace` on a `SPOKEN_REPLACEMENTS` dict. Each replacement is wrapped with spaces (`f' {replacement} '`) or a single space (`' '`) for empty replacements.

Categories:

- Checkmarks (`✓✔✅☑`) → `check`
- X marks (`✗✘❌❎`) → `x`
- Arrows (`→←↑↓➡⬅`): `→` → `to`, others → `arrow`
- Common emojis: `👍` → `thumbs up`, `👎` → `thumbs down`, `🎉` → `celebration`, `🚀` → `rocket`, `💡` → `idea`, `⚠️` → `warning`, `🔥` → `fire`, `✨` → `sparkle`, `📁📂` → `folder`, `📄` → `file`, `🔧` → `tool`, `🐛` → `bug`, `🤖` → `robot`
- Bullets (`•·`) → space (silently removed)
- Ellipsis `…` → `...` (three ASCII dots, later collapsed)
- Dashes `—–` (em/en) → `, `
- Legal symbols `©®™` → spoken equivalents

### Code blocks (backup pass)

Pattern: ` ```[a-zA-Z]*\n?([\s\S]*?)``` `

`_code_block_to_speech()`:

1. Strips tree-drawing box characters (`├└│─┬┤┐┘┌┼╮╯╰╭`) from each line.
2. Removes blank lines.
3. Truncates to first 8 non-empty lines; appends `and more.` if truncated.
4. Joins lines with `. `.
5. Returns `Code: {spoken}. `.

If the block is empty after stripping, returns a single space.

### Inline code

Pattern: `` `([^`]+)` `` → `\1` (keeps content, removes backticks). This fires after `tts_normalize()` has already processed backtick content, so this is a fallback for any remaining raw backticks.

### Markdown formatting

In order:

- `**text**` and `__text__` (bold) → `text`
- `*text*` and `_text_` (italic) → `text`
- `^#{1,6}\s*` at line start → `` (remove heading markers)
- `^>\s*` at line start → `` (remove blockquote markers)
- `^\s*[-*+]\s+` at line start → `` (remove unordered list markers)
- `^\s*\d+\.\s+` at line start → `` (remove ordered list markers)

### URL removal (backup)

Pattern: `https?://[^\s\)]+` → ``(silently removed). This catches any URLs not consumed by`tts_normalize()`'s URL-to-speech rule.

### Markdown links

Pattern: `\[([^\]]+)\]\([^)]+\)` → `\1` (keep link text, drop URL).

### Table formatting

- `\|` → ` ` (pipe → space)
- `^[-:]+$` on its own line → `` (separator rows removed)

### Bracket/brace removal

`[{}\[\]()]` → `` (all brackets and braces stripped).

### Backslash removal

`\\` → ``.

### Multiple dashes/underscores

`[-_]{2,}` → ` ` (e.g., `---` section separators → single space).

### Whitespace normalisation

Applied in order:

1. Multiple newlines → single newline: `\n{2,}` → `\n`
2. Newlines → `. ` (sentence break for TTS pause): `\s*\n\s*` → `. `
3. Horizontal whitespace collapsed: `[ \t]+` → ` `
4. Double periods: `\.(\s*\.)+` → `.` (cleans up `end.. next` artefacts from list processing)
5. `.strip()`

## Known edge cases / limitations

- Bold/italic stripping uses simple patterns. Nested or mismatched markers (e.g., `**bold _nested_ bold**`) may leave partial markers in the output.
- The bullet-removal pattern (`^\s*[-*+]\s+`) also matches markdown horizontal rules (`---`) unless those are caught first by the dashes rule — but the dashes rule (`[-_]{2,}`) runs after bullet removal in the function. In practice, horizontal rules usually appear on their own line and are matched by the `^[-:]+$` table-separator pattern, or reduced to a space by the `[-_]{2,}` rule.
- Table pipe removal replaces `|` with a single space, which can leave awkward spacing around table content.
- The `…` (Unicode ellipsis) is first converted to `...` and the `\.(\s*\.)+` cleanup at the end collapses multiple dots to one, so `…` ultimately becomes a single `.`. This may lose the "trailing off" intent.
- Bracket removal strips `()` which can affect inline citations and parenthetical text — their content remains but the brackets disappear, which is usually fine for speech.
- Bare URL removal (backup) is silent — the URL is dropped entirely rather than spoken. This is the reverse of the behaviour in `tts_normalize()` where URLs are spoken. Any URL that reaches `clean_text_for_speech()` without being caught earlier is silently discarded.

## Future improvements

- Handle nested markdown formatting more robustly.
- Speak `…` as "dot dot dot" rather than collapsing to a single period.
- Add more emoji mappings as the model uses new symbols.
- Consider whether bare URLs should be spoken or dropped in the cleanup pass — currently the two behaviours (speak in `tts_normalize`, drop in `clean_text_for_speech`) are inconsistent.
