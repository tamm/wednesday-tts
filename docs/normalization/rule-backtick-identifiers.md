# Rule: Backtick Identifiers

**What it does**: Expands snake_case identifiers inside backticks to natural spoken words, then strips the backticks.

**Pipeline position**: step 0 — must run first, before any other rule, so that underscore-separated parts are visible before the backtick characters are removed by later cleanup.

## Examples

| Input                 | Output                                        |
| --------------------- | --------------------------------------------- |
| `` `tts_normalize` `` | `tts normalize`                               |
| `` `audio_buffer` ``  | `audio buffer`                                |
| `` `api_url` ``       | `Ae pee eye you ar el`                        |
| `` `stderr` ``        | `standard error`                              |
| `` `fn_ctx` ``        | `function context`                            |
| `` `x` ``             | `ex` (single letter via LETTER_NAMES)         |
| `` `hello world` ``   | `hello world` (no underscore, returned as-is) |

## Pattern / logic

Pattern: `` `([^`\n]+)` ``

For each backtick-delimited span:

1. If the content contains an underscore, split on `_`, drop empty parts, and call `expand_identifier_part()` on each.
2. `expand_identifier_part()` checks the token (lowercased) against `LOWERCASE_ACRONYMS` — a hardcoded dict of ~50 entries mapping common identifiers like `tts`, `api`, `stderr` to their phonetic expansions.
3. If the token is a single alphabetic character it is looked up in `LETTER_NAMES` (a→"ay", b→"bee", …, z→"zee").
4. Otherwise the part is returned unchanged.
5. Parts are joined with spaces.
6. If there is no underscore the content is returned unchanged (the backticks are still removed by the substitution).

The `LOWERCASE_ACRONYMS` table is defined directly in `tts_normalize()` (local variable, not a module-level constant). Notable entries: `ms` → `milliseconds`, `fn` → `function`, `cfg` → `config`, `kwargs` → `keyword args`, `stdout`/`stdin`/`stderr` → full English names.

## Known edge cases / limitations

- Only snake_case is expanded. camelCase inside backticks is NOT split here — that happens later at step 9 (rule-camelcase.md).
- Single-word identifiers with no underscore pass through unchanged, meaning `` `api` `` (lowercase, no underscore) is returned as `api`, not expanded. The dictionary at step 1 handles uppercase `API` but lowercase without underscores falls through.
- The `LOWERCASE_ACRONYMS` table is hardcoded in the function body. Adding new entries requires editing the source; there is no external config for this table.
- Nested backticks and multiline spans are not matched (`[^`\n]+` stops at newlines and inner backticks).

## Future improvements

- Expose LOWERCASE_ACRONYMS as a module-level constant or merge with tts-dictionary.json so it can be extended without editing Python source.
- Optionally apply camelCase splitting here for backtick content so the two identifier rules are co-located.
