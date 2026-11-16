"""Small atomic JSON helpers for the standalone scraper."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock


def load_json(path: str | Path, default: Any) -> Any:
    target = Path(path)
    lock = FileLock(str(target) + ".lock")
    with lock:
        if not target.exists():
            return default() if callable(default) else default
        try:
            return json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return default() if callable(default) else default


def save_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(target) + ".lock")
    with lock:
        handle, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass
