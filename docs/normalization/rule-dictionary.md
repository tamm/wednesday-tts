# Rule: Dictionary

**What it does**: Applies whole-word pronunciation replacements from `tts-dictionary.json`, converting acronyms, brand names, and awkward tokens to phonetically natural spoken forms.

**Pipeline position**: step 1 — runs after operators (step 0b) and before tilde/percent/fraction rules (step 1a). Placing it here means backtick identifiers and operators have already been expanded, so the dictionary sees cleaned text. The dictionary runs before path/URL rules so that tokens like `cwd` inside a path are caught if the path rule fails to split them.

## Examples

| Input        | Output                                                                          |
| ------------ | ------------------------------------------------------------------------------- |
| `API`        | `Ae pee eye`                                                                    |
| `LLM`        | `el el em`                                                                      |
| `JSON`       | `jason`                                                                         |
| `YAML`       | `yamel`                                                                         |
| `GitHub`     | `git hub`                                                                       |
| `pytest`     | `pie test`                                                                      |
| `sudo`       | `sue do`                                                                        |
| `macOS`      | `mac O S`                                                                       |
| `2FA`        | `two factor` (case-insensitive)                                                 |
| `regex`      | `regex` (identity, case-insensitive — prevents camelCase rule from mangling it) |
| `Qwen`       | `Kwen`                                                                          |
| `irrational` | `ih-rash-onal` (case-insensitive — pronunciation correction)                    |

## Pattern / logic

The dictionary file lives at `hooks/tts-dictionary.json` (path resolved relative to the script, so symlinks work). It is loaded by `load_dictionary()` at startup and passed into `tts_normalize()`.

`apply_dictionary(text, replacements)` iterates entries in file order. Each entry has:

- `"pattern"` — the literal string to match (not a regex; it is `re.escape`d before use)
- `"replacement"` — the string to substitute
- `"case_sensitive"` (optional, default `true`) — if `false`, matching uses `re.IGNORECASE`

The actual match is: `r'\b' + re.escape(pattern) + r'\b'`

This is whole-word — `API` will not match `APIs` and vice versa. The file contains separate entries for plurals where needed (e.g., `"APIs"` → `"Ae pee eyes"`).

### Adding new entries

1. Open `hooks/tts-dictionary.json`.
2. Add a JSON object inside the `"replacements"` array:
   ```json
   { "pattern": "MYTERM", "replacement": "my term spoken form" }
   ```
3. For case-insensitive matching add `"case_sensitive": false`.
4. For plurals, add a separate entry — `\b` word boundaries mean `"API"` does not match `"APIs"`.
5. Entries are applied in array order; put more-specific entries before less-specific if there could be overlap.

### Current categories in the file

- HTTP/networking acronyms: `npm`, `npx`, `API`, `URL`, `HTTP`, `HTTPS`, `JSON`, `YAML`, `SQL`, `CLI`
- AI/ML terms: `LLM`, `TTS`, `MCP`, `GPU`, `CPU`, `RAM`, `Qwen`, `ONNX`
- Audio/DSP: `AEC`, `VAD`, `RMS`, `PCM`, `FFT`, `TTL`
- Auth: `SSH`, `2FA`
- Developer tools: `GitHub`, `PyPI`, `pytest`, `venv`, `uv`, `hf`, `RTK`
- Shell utilities: `chmod`, `chown`, `chgrp`, `sudo`, `fcntl`, `ffmpeg`, `jq`, `awk`, `sed`, `grep`, `cron`
- Python: `kwargs`, `stdin`, `stdout`, `stderr`
- Brand/product names: `SvelteKit`, `TypeScript`, `JavaScript`, `macOS`, `iOS`, `Kokoro`, `Ollama`
- Hook events: `PreToolUse`, `PostToolUse`
- Pronunciation corrections: `irrational`, `G'day`, `regex`
- Identity entries (prevent later rules from mangling): `awk`, `sed`, `grep`, `cron`, `regex`

## Known edge cases / limitations

- `\b` word boundaries are Unicode-unaware in Python's `re` module by default. Patterns adjacent to non-ASCII characters may not match as expected.
- Entries are applied in order, so if entry A's replacement contains a string that would be matched by entry B, entry B will also fire on the already-replaced text. This is usually harmless but can cause double-conversion.
- There is no mechanism to prevent dictionary entries from matching inside already-processed spans (the pipeline has no "consumed" tracking). The regex/operator steps before the dictionary help reduce this, but it is not guaranteed.
- `"case_sensitive": false` with `\b` can produce odd results for patterns containing punctuation like `G'day` — the apostrophe is not a word character, so `\b` falls at the `G` and at `y`, which works here but is fragile for similar patterns.

## Future improvements

- Add a `"comment"` field to entries for self-documenting the file.
- Support regex patterns (currently all patterns are treated as literals via `re.escape`).
- Deduplicate with `LOWERCASE_ACRONYMS` in `tts_normalize()` — the two tables cover overlapping acronyms and diverge in places.
