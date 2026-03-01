# TTS Normalization Rule Library

Rules applied in `integrations/claude-code/speak-response.py` — normalization now runs server-side via `tts_normalize()` and `clean_text_for_speech()` in `src/wednesday_tts/normalize/pipeline.py` — to convert Claude's raw markdown output into natural spoken text before sending to the TTS daemon.

## Pipeline order

Rules run in a strict sequence — order matters. See each file for dependency notes.

| Step   | File                            | What it handles                                                  |
| ------ | ------------------------------- | ---------------------------------------------------------------- |
| 0      | `rule-backtick-identifiers.md`  | `code` → spoken identifiers, snake_case, acronyms                |
| 0      | `rule-hashes.md`                | `f4c5c15` → "hash ending in see one five"                        |
| 0b-pre | `rule-regex-to-speech.md`       | Regex patterns → spoken description before operators mangle them |
| 0b     | `rule-operators.md`             | `===`, `!==`, `<=`, `>=`, `=>`, `+=`, `=` etc.                   |
| 1      | `rule-dictionary.md`            | `tts-dictionary.json` whole-word replacements (API, npm, 2FA…)   |
| 1a–1b  | `rule-symbols-tilde-percent.md` | `~N`, `×`, `%`, fractions, progress ranges                       |
| 1c + 5 | `rule-versions.md`              | Model versions (`qwen2.5:0.5b`), plain versions (`v1.2.3`)       |
| 2–3    | `rule-urls.md`                  | `https://` URLs and bare `domain.tld/path` patterns              |
| 4a–4b2 | `rule-paths.md`                 | Tilde paths, slash paths, `filename.ext`, bare `.py` `.sh`       |
| 4c–4d  | `rule-numbers-units.md`         | `0.5s`, `300ms`, `0.022` → spoken digit-by-digit                 |
| 6b     | `rule-http-codes.md`            | `404`, `503` → "four oh four" in error context only              |
| 8–9    | `rule-camelcase.md`             | ALL CAPS normalisation, camelCase word splitting                 |
| post   | `rule-markdown-cleanup.md`      | Emoji, markdown formatting, code blocks, whitespace              |

## Proposed / not yet implemented

| File                 | Status                                                           |
| -------------------- | ---------------------------------------------------------------- |
| `rule-homographs.md` | Proposed — "read" (past/present) substitution, analysis complete |

## Operational docs

Server config lives in `~/.claude/tts-config.json`. Backend implementations are in `src/wednesday_tts/server/backends/`.
