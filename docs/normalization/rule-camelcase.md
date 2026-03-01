# Rule: CamelCase

**What it does**: Inserts spaces into camelCase identifiers so TTS reads them as separate words rather than running them together. Also handles ALL CAPS words (4+ letters) by converting them to Title Case, and normalises a small set of common exclamatory caps words.

**Pipeline position**: steps 8 and 9 — run near the end of `tts_normalize()`, after all structural rules. Must run after the dictionary (step 1) so that known acronyms like `JSON`, `API`, `TTS` are already converted to their phonetic forms before the ALL CAPS rule fires on them. Backtick identifiers (step 0) handle snake_case; this rule handles camelCase in bare prose.

## Examples

| Input               | Output                                                            |
| ------------------- | ----------------------------------------------------------------- |
| `myVariableName`    | `my Variable Name`                                                |
| `handleHttpRequest` | `handle Http Request`                                             |
| `onClick`           | `on Click`                                                        |
| `getJSON`           | `get J S O N` (all-upper sub-word — no split inside all-caps run) |
| `IMPORTANT`         | `Important` (4+ letter all-caps → Title Case)                     |
| `WARNING`           | `Warning`                                                         |
| `OMG`               | `oh my god` (special-cased exclamation)                           |
| `WOW`               | `Wow`                                                             |
| `YES`               | `Yes`                                                             |
| `API`               | already converted by dictionary — not reached here                |
| `allowercase`       | `allowercase` (no change — all lowercase, not camelCase)          |
| `URL`               | already converted by dictionary — not reached here                |

## Pattern / logic

### Step 8 — ALL CAPS handling

First, a small hardcoded dict `CAPS_EXCLAMATIONS` maps specific short caps words to natural spoken forms:

```
HI→Hi, OH→Oh, NO→No, YES→Yes, UM→Um, AH→Ah, UH→Uh, WOW→Wow,
HEY→Hey, BYE→Bye, AWW→Aww, GOD→God, OMG→oh my god
```

Each is applied with `re.sub(r'\b{WORD}\b', replacement, text)`.

Then: `\b([A-Z][A-Z']{3,})\b` → `m.group(1).capitalize()`

Matches any word of 4+ uppercase letters (apostrophes allowed, to handle `DON'T` etc.) and capitalises it — first letter uppercase, rest lowercase. This prevents TTS from spelling out long caps words like `IMPORTANT` letter by letter.

The 4-letter threshold means 2–3 letter acronyms (`DB`, `OK`, `IO`) are left alone — they are expected to be handled by the dictionary.

### Step 9 — camelCase splitting

Pattern: `\b[a-zA-Z]{4,}\b` (4+ letter words)

`split_camel()`:

1. If the word is entirely uppercase or entirely lowercase — return unchanged (not camelCase).
2. Otherwise apply: `re.sub(r'([a-z])([A-Z])', r'\1 \2', word)` — inserts a space before every uppercase letter that follows a lowercase letter.

This handles the most common case (`myVarName` → `my Var Name`) but does NOT insert a space between consecutive uppercase letters followed by lowercase (i.e., it does not split `getHTTPResponse` into `get HTTP Response` — the `HTTP` run is kept intact).

## Known edge cases / limitations

- The camelCase pattern applies to words of 4+ characters. Three-letter or shorter camelCase identifiers like `iOS` or `onChange` (the `o` is only 1 char) are not split. `onClick` is 7 characters and will be split correctly.
- `split_camel()` only inserts spaces at lowercase→uppercase transitions. An `ALL_CAPS` sequence embedded in camelCase (e.g., `getHTTPClient`) will produce `get HTTPClient` — the transition from `P` to `C` (both uppercase) is not split.
- The ALL CAPS rule (step 8) runs before camelCase (step 9). Long all-caps words are lowercased by step 8, which means step 9 will see them as all-lowercase and skip them — correct behaviour.
- If the dictionary has not yet converted `JSON`, `API`, etc. (which it should have by step 1), the ALL CAPS rule at step 8 would convert `JSON` → `Json`, producing an unintended pronunciation. Dictionary ordering prevents this in practice.
- `CAPS_EXCLAMATIONS` is hardcoded. There is no way to add entries without editing the source.

## Future improvements

- Handle multi-word all-caps runs (`HTTP_REQUEST`) which currently only have the ALL CAPS rule applied to each word separately.
- Improve ALL-CAPS-in-camelCase splitting (e.g., `getHTTPClient` → `get HTTP Client`).
- Expose `CAPS_EXCLAMATIONS` as configuration.
- Consider applying the camelCase rule to 3-character words as well for cases like `iOS` (though this risks over-splitting normal short words).
