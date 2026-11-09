"""GitHub review and recovery tools (stdlib only)."""
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = (Path(__file__).resolve().parents[2] / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    try:
        __version__ = version("fatchin")
    except PackageNotFoundError:
        __version__ = "0.0.0"
