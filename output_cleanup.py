"""Bound local scraper output without touching pending submissions."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from json_store import load_json


SUBMITTED_FLAG = "submitted.flag"
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".img"}
DEFAULT_DEBUG_RETENTION_DAYS = 7
DEFAULT_RUN_HISTORY_LIMIT = 30


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _remove_path(path: Path) -> tuple[int, int, list[str]]:
    """Return removed file count, released bytes and errors."""
    try:
        if path.is_symlink() or path.is_file():
            size = _file_size(path)
            path.unlink()
            return 1, size, []
        if path.is_dir():
            files = [item for item in path.rglob("*") if item.is_file()]
            size = sum(_file_size(item) for item in files)
            shutil.rmtree(path)
            return len(files), size, []
    except OSError as exc:
        return 0, 0, [f"{path}: {exc}"]
    return 0, 0, []


def _cleanup_post_payload(post_dir: Path, *, include_caption: bool) -> dict:
    stats = {"removed_files": 0, "freed_bytes": 0, "errors": []}
    if not post_dir.is_dir() or post_dir.is_symlink():
        return stats
    targets: list[Path] = []
    if include_caption and (post_dir / "caption.txt").is_file():
        targets.append(post_dir / "caption.txt")
    comments_path = post_dir / "comments"
    if comments_path.exists() or comments_path.is_symlink():
        targets.append(comments_path)
    targets.extend(
        path for path in post_dir.iterdir()
        if path.is_file()
        and path.name.lower().startswith("image")
        and path.suffix.lower() in MEDIA_SUFFIXES
    )
    for target in dict.fromkeys(targets):
        removed, released, errors = _remove_path(target)
        stats["removed_files"] += removed
        stats["freed_bytes"] += released
        stats["errors"].extend(errors)
    return stats


def cleanup_submitted_post(post_directory: str | Path) -> dict:
    """Delete caption and media only after submitted.flag exists."""
    post_dir = Path(post_directory).resolve()
    if not (post_dir / SUBMITTED_FLAG).is_file():
        return {"removed_files": 0, "freed_bytes": 0, "errors": []}
    return _cleanup_post_payload(post_dir, include_caption=True)


def cleanup_incomplete_post(post_directory: str | Path) -> dict:
    """Delete partial payload from a scrape that did not complete successfully."""
    post_dir = Path(post_directory).resolve()
    return _cleanup_post_payload(post_dir, include_caption=True)


def cleanup_submitted_posts(output_root: str | Path) -> dict:
    """Sweep payloads for posts confirmed by submitted.flag."""
    posts_root = Path(output_root).resolve() / "posts"
    stats = {
        "submitted_posts_checked": 0,
        "posts_cleaned": 0,
        "removed_files": 0,
        "freed_bytes": 0,
        "errors": [],
    }
    if not posts_root.is_dir():
        return stats
    for post_dir in posts_root.iterdir():
        if not post_dir.is_dir() or not (post_dir / SUBMITTED_FLAG).is_file():
            continue
        stats["submitted_posts_checked"] += 1
        result = cleanup_submitted_post(post_dir)
        stats["removed_files"] += result["removed_files"]
        stats["freed_bytes"] += result["freed_bytes"]
        stats["errors"].extend(result["errors"])
        if result["removed_files"]:
            stats["posts_cleaned"] += 1
    return stats


def cleanup_incomplete_scrapes(output_root: str | Path) -> dict:
    """Sweep partial payloads belonging to failed scrape records only."""
    posts_root = Path(output_root).resolve() / "posts"
    stats = {
        "incomplete_posts_checked": 0,
        "posts_cleaned": 0,
        "removed_files": 0,
        "freed_bytes": 0,
        "errors": [],
    }
    if not posts_root.is_dir():
        return stats
    for post_dir in posts_root.iterdir():
        record = load_json(post_dir / "post.json", dict)
        if not isinstance(record, dict) or record.get("status") == "success":
            continue
        stats["incomplete_posts_checked"] += 1
        result = cleanup_incomplete_post(post_dir)
        stats["removed_files"] += result["removed_files"]
        stats["freed_bytes"] += result["freed_bytes"]
        stats["errors"].extend(result["errors"])
        if result["removed_files"]:
            stats["posts_cleaned"] += 1
    return stats


def cleanup_debug_files(
    output_root: str | Path,
    retention_days: int = DEFAULT_DEBUG_RETENTION_DAYS,
) -> dict:
    debug_root = Path(output_root).resolve() / "debug"
    stats = {"removed_files": 0, "freed_bytes": 0, "errors": []}
    if not debug_root.is_dir():
        return stats
    cutoff = time.time() - max(0, retention_days) * 86400
    for path in debug_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            is_expired = path.stat().st_mtime < cutoff
        except OSError as exc:
            stats["errors"].append(f"{path}: {exc}")
            continue
        if is_expired:
            removed, released, errors = _remove_path(path)
            stats["removed_files"] += removed
            stats["freed_bytes"] += released
            stats["errors"].extend(errors)
    for directory in sorted(
        (path for path in debug_root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return stats


def cleanup_run_history(
    output_root: str | Path,
    keep: int = DEFAULT_RUN_HISTORY_LIMIT,
) -> dict:
    runs_root = Path(output_root).resolve() / "runs"
    stats = {"removed_files": 0, "freed_bytes": 0, "errors": []}
    if not runs_root.is_dir():
        return stats
    run_files = sorted(
        (path for path in runs_root.glob("run_*.json") if path.is_file()),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    for path in run_files[max(0, keep):]:
        removed, released, errors = _remove_path(path)
        stats["removed_files"] += removed
        stats["freed_bytes"] += released
        stats["errors"].extend(errors)
    return stats


def cleanup_output(
    output_root: str | Path,
    *,
    debug_retention_days: int = DEFAULT_DEBUG_RETENTION_DAYS,
    run_history_limit: int = DEFAULT_RUN_HISTORY_LIMIT,
) -> dict:
    submitted = cleanup_submitted_posts(output_root)
    incomplete = cleanup_incomplete_scrapes(output_root)
    debug = cleanup_debug_files(output_root, debug_retention_days)
    runs = cleanup_run_history(output_root, run_history_limit)
    return {
        "submitted_posts": submitted,
        "incomplete_scrapes": incomplete,
        "debug": debug,
        "runs": runs,
        "removed_files": (
            submitted["removed_files"]
            + incomplete["removed_files"]
            + debug["removed_files"]
            + runs["removed_files"]
        ),
        "freed_bytes": (
            submitted["freed_bytes"]
            + incomplete["freed_bytes"]
            + debug["freed_bytes"]
            + runs["freed_bytes"]
        ),
        "errors": [
            *submitted["errors"],
            *incomplete["errors"],
            *debug["errors"],
            *runs["errors"],
        ],
    }
