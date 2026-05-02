# Playback Spec

This document describes the current playback behaviour in `daemon.py`.

## Current Model

Playback is no longer a single universal path.

There are two supported modes:

1. Queue-driven playback
2. Direct-play backend streaming

## Queue-Driven Playback

This is the default daemon path for batch output and queue-streaming backends.

Key properties:

- `playback_worker()` consumes `playback_queue`
- queued items are tagged with `msg_id`
- playback order is message-preserving
- session flush can remove queued work for one Claude session
- `skip` can remove queued work for one message

The worker maintains a long-lived output stream where possible and reopens it
when device changes or stream failures demand it.

### Spatial Audio

When the default output is a supported Bluetooth target, queue-driven playback
may route through the `SpatialStream` helper binary instead of plain PortAudio.

When spatial mode is not available, the daemon falls back to standard stereo
playback with the request `pan` value.

## Direct-Play Streaming

Some streaming backends can opt into direct-play for the lowest latency.

Current practical case:

- `vibevoice`

Properties:

- backend owns the low-latency output stream
- daemon still owns request lifecycle, stop checks, logging, and device-change
  signalling
- this path bypasses `playback_queue`

This is an explicit exception to the older queue-only playback architecture.

## Ordering Rules

The ordering unit is the message, identified by `msg_id`.

- queued chunks from the same message stay together
- `skip` removes the current message's remaining chunks
- `flush_session` removes queued and in-flight future work for one
  `session_id`
- `stop` forgets everything

For queue-streaming backends, chunks may arrive incrementally, but they are
still associated with one `msg_id` and controlled at the message level.

## Stop Semantics

### `stop`

Meaning:

- silence now
- drain queue
- clear held barge-in requests
- clear session tracking

### `skip`

Meaning:

- stop reading this message
- preserve later messages

### `flush_session`

Meaning:

- replace older queued audio from the same Claude session with the final
  response
- do not click-cut the already-started chunk

## Logging Expectations

Queue-driven playback should emit:

- `[playback] first-chunk msg_id=...`
- stream-open or spatial readiness logs when relevant

Direct-play backends should emit equivalent first-audio and completion markers
with the same `msg_id` if possible. That is the preferred direction for
keeping analytics consistent across backends.
