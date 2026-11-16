"""Versioned incremental state for Facebook Page discovery and rechecks."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from json_store import load_json, save_json

SCHEMA_VERSION = 2
REACTION_PARSER_VERSION = 2
DEFAULT_WATCH_DAYS = 7
DEFAULT_ANCHOR_COUNT = 10
DEFAULT_WATCH_LIMIT_PER_PAGE = 50
PERMANENT_STATUSES = {"submitted", "legacy_submitted", "reaction_unavailable"}
RECHECK_STATUSES = {"watching", "retry"}


def _now() -> datetime:
    return datetime.now().astimezone()


def _time_text(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
        return parsed if parsed.tzinfo else parsed.astimezone()
    except (TypeError, ValueError):
        return None


def new_state(legacy_success_ids: set[str] | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "reaction_parser_version": REACTION_PARSER_VERSION,
        "created_at": _time_text(),
        "updated_at": _time_text(),
        "legacy_submitted_ids": sorted(str(value) for value in (legacy_success_ids or set())),
        "pages": {},
    }


def load_page_state(path: str | Path, legacy_success_ids: set[str] | None = None) -> dict:
    value = load_json(Path(path), dict)
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return new_state(legacy_success_ids)
    value.setdefault("pages", {})
    value.setdefault("legacy_submitted_ids", [])
    previous_parser_version = value.get("reaction_parser_version")
    if previous_parser_version != REACTION_PARSER_VERSION:
        for page in value["pages"].values():
            for record in page.get("posts", {}).values():
                if record.get("status") in PERMANENT_STATUSES:
                    continue
                record["status"] = "watching"
                record.pop("reaction_count", None)
                record.pop("reaction_text", None)
                record.pop("reaction_source", None)
                record.pop("expired_at", None)
                record["parser_invalidated_at"] = _time_text()
    for page in value["pages"].values():
        for record in page.get("posts", {}).values():
            if (
                record.get("status") == "retry"
                and record.get("reaction_count") is None
                and record.get("last_error") == "reaction_count_unavailable"
            ):
                record["status"] = "reaction_unavailable"
                record["skipped_at"] = _time_text()
    merged = {
        str(item) for item in value.get("legacy_submitted_ids", []) if str(item).strip()
    }
    merged.update(str(item) for item in (legacy_success_ids or set()) if str(item).strip())
    value["legacy_submitted_ids"] = sorted(merged)
    value["reaction_parser_version"] = REACTION_PARSER_VERSION
    return value


def save_page_state(path: str | Path, state: dict) -> None:
    state["schema_version"] = SCHEMA_VERSION
    state["reaction_parser_version"] = REACTION_PARSER_VERSION
    state["updated_at"] = _time_text()
    save_json(Path(path), state)


def page_record(state: dict, page_url: str) -> dict:
    pages = state.setdefault("pages", {})
    page = pages.setdefault(str(page_url), {})
    page.setdefault("anchors", [])
    page.setdefault("posts", {})
    return page


def permanent_ids(state: dict) -> set[str]:
    result = {
        str(item) for item in state.get("legacy_submitted_ids", []) if str(item).strip()
    }
    for page in state.get("pages", {}).values():
        for post_id, record in page.get("posts", {}).items():
            if record.get("status") in PERMANENT_STATUSES:
                result.add(str(post_id))
    return result


def known_ids(state: dict) -> set[str]:
    result = permanent_ids(state)
    for page in state.get("pages", {}).values():
        result.update(str(post_id) for post_id in page.get("posts", {}))
    return result


def anchors_by_page(state: dict) -> dict[str, set[str]]:
    return {
        str(page_url): {str(item) for item in page.get("anchors", []) if str(item).strip()}
        for page_url, page in state.get("pages", {}).items()
    }


def register_discovered(state: dict, items: list[dict], *, now: datetime | None = None) -> None:
    timestamp = _time_text(now)
    for item in items:
        page_url = str(item.get("source_page_url", ""))
        post_id = str(item.get("post_id", ""))
        if not page_url or not post_id:
            continue
        page = page_record(state, page_url)
        record = page["posts"].setdefault(post_id, {})
        record.setdefault("first_seen_at", timestamp)
        record.setdefault("status", "watching")
        record["url"] = str(item.get("url", record.get("url", "")))
        record["source_page_id"] = str(item.get("source_page_id", record.get("source_page_id", "")))
        record["last_discovered_at"] = timestamp


def due_watch_items(
    state: dict,
    page_urls: list[str],
    *,
    now: datetime | None = None,
    watch_days: int = DEFAULT_WATCH_DAYS,
    limit_per_page: int = DEFAULT_WATCH_LIMIT_PER_PAGE
) -> list[dict[str, str]]:
    current = now or _now()
    cutoff = current - timedelta(days=max(1, watch_days))
    items: list[dict[str, str]] = []
    for sequence, page_url in enumerate(page_urls, start=1):
        page = state.get("pages", {}).get(page_url, {})
        page_candidates: list[tuple[str, dict]] = []
        for post_id, record in page.get("posts", {}).items():
            if record.get("status") not in RECHECK_STATUSES:
                continue
            first_seen = _parse_time(record.get("first_seen_at"))
            if (
                record.get("status") == "watching"
                and first_seen is not None
                and first_seen < cutoff
                and record.get("reaction_count") is not None
            ):
                record["status"] = "expired"
                record["expired_at"] = _time_text(current)
                continue
            url = str(record.get("url", ""))
            if not url:
                continue
            page_candidates.append((str(post_id), record))
        page_candidates.sort(
            key=lambda pair: str(
                pair[1].get("last_discovered_at")
                or pair[1].get("first_seen_at")
                or ""
            ),
            reverse=True,
        )
        for post_id, record in page_candidates[:max(1, limit_per_page)]:
            url = str(record.get("url", ""))
            items.append({
                "post_id": post_id,
                "url": url,
                "source_page_url": page_url,
                "source_page_id": str(record.get("source_page_id", "")),
                "page_sequence": str(sequence),
                "candidate_source": "watchlist",
            })
    return items


def update_anchors(
    state: dict,
    collection_metadata: dict[str, dict],
    *,
    anchor_count: int = DEFAULT_ANCHOR_COUNT,
) -> None:
    for page_url, metadata in collection_metadata.items():
        if not metadata.get("completed"):
            continue
        observed = [str(item) for item in metadata.get("observed_post_ids", []) if str(item)]
        if not observed:
            continue
        page = page_record(state, page_url)
        page["anchors"] = observed[:max(3, anchor_count)]
        page["last_discovery_at"] = _time_text()
        page["last_discovery_reason"] = str(metadata.get("stop_reason", ""))


def update_post_result(
    state: dict,
    item: dict,
    result: dict,
    *,
    now: datetime | None = None,
) -> None:
    page_url = str(item.get("source_page_url", ""))
    post_id = str(item.get("post_id", ""))
    if not page_url or not post_id:
        return
    current = now or _now()
    page = page_record(state, page_url)
    record = page["posts"].setdefault(post_id, {})
    record.setdefault("first_seen_at", _time_text(current))
    record["url"] = str(item.get("url", record.get("url", "")))
    record["source_page_id"] = str(item.get("source_page_id", record.get("source_page_id", "")))
    record["last_checked_at"] = _time_text(current)
    record["check_attempts"] = int(record.get("check_attempts", 0) or 0) + 1
    record["reaction_count"] = result.get("reaction_count")
    record["reaction_text"] = str(result.get("reaction_text", ""))
    record["reaction_source"] = str(result.get("reaction_source", "unavailable"))
    record["last_error"] = str(result.get("error", ""))
    if result.get("status") == "success":
        record["status"] = "scraped"
        record["scraped_at"] = _time_text(current)
    elif result.get("error") == "reaction_count_unavailable":
        record["status"] = "reaction_unavailable"
        record["skipped_at"] = _time_text(current)
    elif result.get("reaction_count") is None:
        record["status"] = "retry"
    else:
        record["status"] = "watching"


def mark_submitted(state: dict, post_ids: set[str], *, now: datetime | None = None) -> None:
    timestamp = _time_text(now)
    wanted = {str(item) for item in post_ids}
    for page in state.get("pages", {}).values():
        for post_id, record in page.get("posts", {}).items():
            if str(post_id) in wanted:
                record["status"] = "submitted"
                record["submitted_at"] = timestamp
