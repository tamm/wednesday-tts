# Rule: Versions

**What it does**: Converts version strings of the form `v1.2.3` / `1.2.3` and model version strings like `qwen2.5:0.5b` / `llama3.1:8b` to spoken form, replacing `.` with " dot " and handling trailing letter suffixes.

**Pipeline position**: steps 1c and 5 — model version strings (step 1c) run after the dictionary (step 1) and before the URL/path block. Plain version strings (step 5) run after the numbers/units block (step 4c–4d) and before the 3-digit HTTP code rule (step 6b). The comment in the source says version strings must come before the 3-digit code rule to avoid `1.2` being partially misread.

## Examples

| Input          | Output                                  |
| -------------- | --------------------------------------- |
| `v1.2.3`       | `v1 dot 2 dot 3`                        |
| `1.2.3`        | `1 dot 2 dot 3`                         |
| `3.11`         | `3 dot 11`                              |
| `qwen2.5:0.5b` | `qwen 2 point 5, 0 point 5 b`           |
| `llama3.1:8b`  | `llama 3 point 1, 8 b`                  |
| `phi2:3.8b`    | `phi 2, 3 point 8 b`                    |
| `gemma2:2b`    | `gemma 2, 2 b`                          |
| `qwen2.5`      | `qwen 2 point 5` (standalone, no colon) |

## Pattern / logic

### Step 1c — Model/tool version strings

Two patterns:

**With colon** (e.g., `qwen2.5:0.5b`):

```
\b([a-zA-Z][a-zA-Z0-9-]*)(\d+(?:\.\d+)*)(?::(\d+(?:\.\d+)*[a-zA-Z]?))\b
```

Groups: (1) name, (2) first version, (3) second version (after colon).

`model_version_to_speech()`:

- Name → unchanged (e.g., `qwen`, `llama`).
- First version: `.` → `point` (e.g., `2.5` → `2 point 5`).
- Second version: `.` → `point`, then:
  - Trailing letter after `digit point digit`: `re.sub(r'(\d)\s*(point\s+\d+)\s*([a-zA-Z])', r'\1 \2 \3', ...)` adds space before the letter.
  - Simple `digit+letter` like `2b`: `re.sub(r'^(\d+)\s*([a-zA-Z])$', r'\1 \2', ...)` adds space.
- Parts joined with `, `.

**Without colon** (e.g., `qwen2.5`):

```
\b([a-zA-Z][a-zA-Z-]*)(\d+\.\d+(?:\.\d+)*)\b
```

Groups: (1) name letters only (no digits in name part), (2) version with at least one dot.

Replaces `.` with `point` in the version part. Name passes through unchanged.

Note: this pattern requires the name to contain only letters and hyphens before the digit sequence. So `python3` (single integer, no dot) is not matched here — that is intentional per the comment in the source.

### Step 5 — Plain version strings

Pattern: `\bv?(\d+\.\d+(?:\.\d+)?)\b`

Replaces `.` with `dot` in the entire match (including the optional leading `v`).

Note the replacement is `m.group(0).replace('.', ' dot ')` — the `v` prefix is preserved as-is if present.

## Known edge cases / limitations

- Step 5 uses `dot` (not `point`), while the model version step (1c) uses `point`. The distinction is intentional: "version one dot two dot three" vs. "qwen two point five".
- The plain version pattern `\bv?(\d+\.\d+(?:\.\d+)?)` allows only two or three segments. Four-segment versions like `1.2.3.4` are not matched.
- After the operator rule (step 0b) converts `=` to `equals`, a pattern like `version=1.2.3` will have `=` already spoken, but the `1.2.3` part should still be caught by step 5.
- The model version pattern requires the name to start with a letter. Pure-numeric model identifiers are not matched.
- The name-part regex for the no-colon form is `[a-zA-Z][a-zA-Z-]*` (letters and hyphens only), so a name like `gpt-4o` would not match because `4` appears in the name before the version digits.

## Future improvements

- Unify the `.` vs. `point` convention, or make it explicit which context uses which.
- Support four-segment versions (`1.2.3.4`).
- Handle `gpt-4o` style names where a digit appears in the model name itself.
- Consider reading the `v` prefix explicitly as "version" in some contexts.
