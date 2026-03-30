"""TTS backend implementations.

Each backend implements the TTSBackend interface:

    class MyBackend(TTSBackend):
        sample_rate: int = 24000

        def load(self) -> None:  ...
        def generate(self, text: str, speed: float = 1.0) -> np.ndarray | None:  ...

Backends that support streaming also implement:

        supports_streaming: bool = True
        def play_streaming(self, text: str, speed: float = 1.0) -> None:  ...
        def abort_stream(self) -> None:  ...
"""

from .base import TTSBackend
from .kokoro import KokoroBackend
from .pocket import PocketTTSBackend
from .sam import SAMBackend
from .soprano import SopranoBackend
from .chatterbox import ChatterboxBackend
from .qwen3 import Qwen3TTSBackend

REGISTRY: dict[str, type[TTSBackend]] = {
    "kokoro": KokoroBackend,
    "pocket": PocketTTSBackend,
    "sam": SAMBackend,
    "soprano": SopranoBackend,
    "chatterbox": ChatterboxBackend,
    "qwen3": Qwen3TTSBackend,
}

__all__ = [
    "TTSBackend",
    "KokoroBackend",
    "PocketTTSBackend",
    "SAMBackend",
    "SopranoBackend",
    "ChatterboxBackend",
    "Qwen3TTSBackend",
    "REGISTRY",
]
