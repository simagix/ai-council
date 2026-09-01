"""ai-council: three local Ollama models deliberate a question."""

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
try:
    __version__ = _VERSION_FILE.read_text(encoding="utf-8").strip() or "0.1.0"
except OSError:
    __version__ = "0.1.0"

__all__ = ["__version__"]
