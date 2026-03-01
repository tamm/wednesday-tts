# Rule: Hash Abbreviation

**What it does**: Abbreviates hex hashes inside backticks to "hash ending in X Y Z" (last 3 chars spelled out). Prefixed hashes like `sha256:abc123...` preserve the algorithm name.

**Pipeline position**: step 0, inside `identifier_to_speech()` — runs before other backtick processing.

## Examples

| Input                                            | Output                                |
| ------------------------------------------------ | ------------------------------------- |
| `` `f4c5c15` ``                                  | `hash ending in see one five`         |
| `` `a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0` `` | `hash ending in nine bee oh`          |
| `` `sha256:3e8a1c9f2b4d` ``                      | `sha 256 hash ending in bee four dee` |
| `` `md5:abcdef1234567890` ``                     | `md5 hash ending in eight nine oh`    |

## Detection rules

### Pure hex hashes

- Pattern: `^[0-9a-fA-F]{7,}$`
- **Must contain both letters AND digits.** All-alpha like `deadbeef` could be a word. All-digits like `1234567` could be a number. Only mixed strings are treated as hashes.

### Prefixed hashes

- Pattern: `^(sha\d+|md5|blake2[bs]?):([0-9a-fA-F]{7,})$`
- Algorithm prefix is kept in the spoken output, colon is dropped.
- The hex portion does NOT require mixed letters/digits (the prefix already confirms it's a hash).

## Why "ending in" instead of full spelling

Hashes are long and meaningless when read aloud. The last 3 characters give enough to distinguish them in context (git log, docker images, etc.) without wasting 20 seconds spelling out 40 hex chars.

## Known edge cases

- `deadbeef`, `cafebabe`, `feedface` — all-alpha hex strings are left alone (no digits).
- `1234567890` — all-digit hex strings are left alone (no letters). Will be handled by number rules instead.
- Hashes not in backticks are not caught by this rule. Bare hashes in prose pass through unchanged.
- The `sha256` prefix is further cleaned by the TTS dictionary entry `SHA256` → "sha 256".
