# Changelog

## 2026-03-16
- Added: hex code normalizer (`normalize/hex_codes.py`) — speaks 0xFF as "hex F F"
- Added: IP address normalizer (`normalize/ip_addresses.py`) — speaks octets naturally
- Added: phone number normalizer (`normalize/phone.py`) — AU and international formats
- Added: numbers-to-words normalizer (`normalize/numbers_to_words.py`) — cardinal/ordinal expansion
- Changed: number normalizer — improved handling of large numbers and edge cases
- Changed: all backends (`pocket`, `chatterbox`, `kokoro`, `sam`, `soprano`) accept per-request `voice` param
- Changed: pocket backend docs added to project CLAUDE.md — read README before changing it
- Changed: unified voice switching syntax using guillemet tags — replaces `__v:` prefix
  - `««text»»` — SAM voice (backward compatible)
  - `««alba»text»»` — named pocket voice
  - `««2»text»»` — voice_pool index
  - `««/path/to/voice.safetensors»text»»` — custom voice file
- REMOVED: `__v:<voice>__` prefix parsing from daemon — use guillemet tags instead
- Changed: hooks now wrap text in `««voice»...»»` instead of prepending `__v:`
- Changed: streaming preserved for single-voice guillemet-wrapped messages

## 2026-03-15
- Added: voice-per-repo in Claude Code hooks — hooks hash the git root to pick a deterministic voice from `voice_pool` in `~/.claude/tts-config.json`
- Changed: `speak-response.py` and `pre-tool-speak.py` use guillemet voice tags for per-repo voice
- **Setup required**: add a `voice_pool` array to `~/.claude/tts-config.json` with 2+ voice names/paths. Without it, the default single voice is used for all repos.

## 2026-03-11
- Changed: table normalizer — varied speech preambles, expanded known topic words
- Changed: markdown normalizer — minor cleanup
- Changed: refactored streaming tests (`test_daemon_streaming.py`)
