"""Wednesday TTS server — HTTP and Unix socket daemons.

Windows HTTP service:  wednesday_tts.server.app  (Flask, localhost:5678)
macOS Unix daemon:     wednesday_tts.server.daemon  (Unix socket)

Backends live in wednesday_tts.server.backends.
"""
