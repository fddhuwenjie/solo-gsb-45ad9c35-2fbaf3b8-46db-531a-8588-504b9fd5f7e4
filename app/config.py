"""Runtime configuration, all values overridable via environment variables."""
import os
import pathlib


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DATA_DIR = pathlib.Path(os.environ.get("BRAILLE_DATA_DIR", "/data"))
DB_PATH = pathlib.Path(os.environ.get("BRAILLE_DB", str(DATA_DIR / "braille.db")))
HOST = os.environ.get("BRAILLE_HOST", "0.0.0.0")
PORT = _int("BRAILLE_PORT", 8080)
# Safety guard so a pathological request cannot exhaust memory.
MAX_GRAPHEMES = _int("BRAILLE_MAX_GRAPHEMES", 200_000)
