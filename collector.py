"""Collect public photo-post links from Facebook Pages without a login profile."""
from __future__ import annotations

import random
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from playwright.sync_api import Page, sync_playwright

from browser_runtime import chromium_launch_options
from feed_extract import dedupe_permalinks, extract_embedded_permalinks

FACEBOOK_HOSTS = {
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "mbasic.facebook.com", "web.facebook.com",
}
DECORATION_MARKERS = (
    "profile picture", "profile photo", "cover photo", "profile_picture",
    "profile_cover", "รูปโปรไฟล์", "รูปภาพโปรไฟล์", "รูปหน้าปก", "รูปภาพหน้าปก",
)
BLOCK_MARKERS = (
    "captcha", "checkpoint", "temporarily blocked", "you're temporarily blocked",
    "you’re temporarily blocked", "misusing this feature",
)
LOGIN_MARKERS = (
    "log in to continue", "see more on facebook", "ดูเพิ่มเติมบน facebook",
    "เข้าสู่ระบบเพื่อดำเนินการต่อ",
)


def normalize_page_url(value: str) -> str:
    value = value.strip()
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        value = "https://" + value.lstrip("/")
    parts = urlsplit(value)
    if (parts.hostname or "").lower() not in FACEBOOK_HOSTS:
        raise ValueError("รองรับเฉพาะ URL บน facebook.com")
    if parts.scheme not in {"http", "https"}:
        raise ValueError("URL ต้องใช้ http หรือ https")
    return urlunsplit(("https", "www.facebook.com", parts.path or "/", parts.query, ""))


def build_photo_urls(value: str) -> list[str]:
    url = normalize_page_url(value)
    parts = urlsplit(url)
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.pop("ref", None)
    query.pop("refid", None)
    lowered = path.lower()
    if lowered in {"/photo", "/photo.php"} or "/posts/" in lowered or "/videos/" in lowered:
        raise ValueError("กรุณาใส่ URL ของเพจ ไม่ใช่ URL ของโพสต์หรือรูปเดี่ยว")

    candidates: list[str] = []
    def add(candidate_path: str, candidate_query: dict[str, str] | None = None) -> None:
        candidate = urlunsplit((
            "https", "www.facebook.com", candidate_path,
            urlencode(candidate_query or {}), "",
        ))
        if candidate not in candidates:
            candidates.append(candidate)

    if lowered == "/profile.php":
        page_id = query.get("id")
        if not page_id:
            raise ValueError("profile.php ต้องมีพารามิเตอร์ id")
        add("/profile.php", {"id": page_id, "sk": "photos"})
        add(f"/{page_id}/photos")
    elif re.match(r"^/(?:people|pages)/[^/]+/(\d+)$", path, flags=re.IGNORECASE):
        page_id = re.match(r"^/(?:people|pages)/[^/]+/(\d+)$", path, flags=re.IGNORECASE).group(1)
        add(path, {"sk": "photos"})
        add(f"/{page_id}/photos")
        add("/profile.php", {"id": page_id, "sk": "photos"})
    elif query.get("sk", "").lower() == "photos":
        add(path, {"sk": "photos"})
        numeric = re.match(r"^/(\d+)$", path)
        if numeric:
            add(f"/{numeric.group(1)}/photos")
    elif lowered.endswith(("/photos", "/photos_by", "/photos_albums")):
        add(path, query)
    else:
        add(path.rstrip("/") + "/photos")
        add(path, {"sk": "photos"})
    return candidates


def extract_post_info(url: str) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    fbid = query.get("fbid", [None])[0]
    if not fbid:
        match = re.search(r"/photos/(?:[^/]+/)?(\d+)", parts.path)
        fbid = match.group(1) if match else None
    if not fbid or not str(fbid).isdigit():
        return None, None
    clean = f"https://www.facebook.com/photo/?fbid={fbid}"
    set_value = query.get("set", [None])[0]
    if set_value:
        clean += f"&set={set_value}"
    return str(fbid), clean


def _decoration_fbids(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    excluded: set[str] = set()
    for anchor in soup.select('a[href*="fbid="]'):
        href = str(anchor.get("href", ""))
        match = re.search(r"(?:[?&])fbid=(\d+)", href)
        if not match:
            continue
        image = anchor.find("img")
        aria_label = str(anchor.get("aria-label", ""))
        set_value = str(parse_qs(urlsplit(href).query).get("set", [""])[0])
        context = " ".join(filter(None, (
            href, aria_label, anchor.get_text(" ", strip=True),
            str(image.get("alt", "")) if image else "",
        ))).lower()
        if (aria_label and image is None and set_value.startswith("a.")) or any(
            marker in context for marker in DECORATION_MARKERS
        ):
            excluded.add(match.group(1))
    return excluded


def _source_page_id(photo_url: str, source_page_url: str) -> str:
    set_value = str(parse_qs(urlsplit(photo_url).query).get("set", [""])[0])
    match = re.match(r"pb\.(\d+)(?:\.|$)", set_value)
    if match:
        return match.group(1)
    parts = urlsplit(source_page_url)
    profile_id = str(parse_qs(parts.query).get("id", [""])[0])
    if profile_id.isdigit():
        return profile_id
    path_parts = [part for part in parts.path.split("/") if part]
    return path_parts[0] if len(path_parts) == 1 and path_parts[0].isdigit() else ""


def extract_photo_items(html: str, base_url: str, source_page_url: str) -> list[dict[str, str]]:
    excluded = _decoration_fbids(html)
    links = extract_embedded_permalinks(html, base_url)
    items: list[dict[str, str]] = []
    for link in dedupe_permalinks(links, base_url):
        post_id, clean_url = extract_post_info(link)
        if not post_id or not clean_url or post_id in excluded:
            continue
        item = {"post_id": post_id, "url": clean_url, "source_page_url": source_page_url}
        page_id = _source_page_id(clean_url, source_page_url)
        if page_id:
            item["source_page_id"] = page_id
        items.append(item)
    return items


def close_login_dialog(page: Page) -> None:
    for selector in (
        'div[role="dialog"] [aria-label="Close"]',
        'div[role="dialog"] [aria-label="ปิด"]',
        'div[aria-label="Close"]', 'div[aria-label="ปิด"]',
    ):
        try:
            button = page.locator(selector).first
            if button.is_visible(timeout=500):
                button.click(timeout=1000)
                page.wait_for_timeout(500)
                return
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _access_reason(page: Page, status: int | None) -> str:
    if status in {403, 429}:
        return f"http_{status}"
    if "/checkpoint" in page.url.lower():
        return "checkpoint"
    try:
        body = page.locator("body").inner_text(timeout=2000).lower()
    except Exception:
        body = ""
    if any(marker in body for marker in BLOCK_MARKERS):
        return "checkpoint_or_block"
    if any(marker in body for marker in LOGIN_MARKERS):
        return "login_required"
    return ""


def _save_debug(page: Page, debug_dir: Path, page_number: int, reason: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    stem = f"page_{page_number}_{reason}"
    try:
        page.screenshot(path=str(debug_dir / f"{stem}.png"), full_page=True)
        (debug_dir / f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def collect_single_page(
    page: Page,
    page_url: str,
    max_posts: int,
    seen_posts: set[str],
    *,
    max_scrolls: int,
    debug_dir: Path,
    page_number: int,
    stop_after_seen: int = 5,
    anchor_post_ids: set[str] | None = None,
    anchor_hits_required: int = 3,
    tail_after_anchor: int = 20,
    collection_metadata: dict | None = None,
) -> list[dict[str, str]]:
    target_url = build_photo_urls(page_url)[0]
    print(f"[*] เปิดหน้า Photos: {target_url}")
    response = page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
    status = response.status if response else None
    page.wait_for_timeout(random.randint(1800, 2800))
    observed: set[str] = set()
    observed_order: list[str] = []
    results: list[dict[str, str]] = []
    stable = 0
    consecutive_seen = 0
    anchor_mode = anchor_post_ids is not None
    anchors = {str(item) for item in (anchor_post_ids or set())}
    anchor_hits: set[str] = set()
    required_anchor_hits = min(max(1, anchor_hits_required), len(anchors)) if anchors else 0
    tail_remaining: int | None = None

    def finish(reason: str, *, completed: bool) -> list[dict[str, str]]:
        if collection_metadata is not None:
            collection_metadata[page_url] = {
                "completed": completed,
                "stop_reason": reason,
                "observed_post_ids": observed_order,
                "anchor_hits": sorted(anchor_hits),
                "new_post_ids": [str(item["post_id"]) for item in results],
            }
        return results

    for attempt in range(1, max_scrolls + 1):
        close_login_dialog(page)
        added = 0
        for item in extract_photo_items(page.content(), page.url, page_url):
            post_id = item["post_id"]
            if post_id in observed:
                continue
            observed.add(post_id)
            observed_order.append(post_id)
            if anchors:
                if post_id in anchors:
                    anchor_hits.add(post_id)
                    if len(anchor_hits) >= required_anchor_hits and tail_remaining is None:
                        tail_remaining = max(0, tail_after_anchor)
                        print(
                            f"  [A] พบ anchor {len(anchor_hits)}/{required_anchor_hits} "
                            f"เริ่มตรวจเผื่ออีก {tail_remaining} โพสต์"
                        )
                elif tail_remaining is not None:
                    tail_remaining -= 1
            if post_id in seen_posts:
                if not anchor_mode:
                    consecutive_seen += 1
                    print(
                        f"  [=] โพสต์เดิม {consecutive_seen}/{stop_after_seen} "
                        f"post_id={post_id}"
                    )
                    if consecutive_seen >= stop_after_seen:
                        print(
                            f"[*] พบโพสต์เดิมติดกัน {stop_after_seen} รายการ "
                            "หยุดเพจนี้และไปเพจถัดไป"
                        )
                        return finish("legacy_consecutive_seen", completed=True)
            else:
                consecutive_seen = 0
                results.append(item)
                added += 1
                print(f"  [+] {len(results)} โพสต์ใหม่ post_id={post_id}")
            if tail_remaining is not None and tail_remaining <= 0:
                print("[*] ตรวจพ้น anchor ครบระยะเผื่อแล้ว ไปเพจถัดไป")
                return finish("anchor_tail_complete", completed=True)
            if anchor_mode and len(observed) >= max_posts:
                return finish("scan_limit", completed=True)
            if not anchor_mode and len(results) >= max_posts:
                return finish("result_limit", completed=True)
        reason = _access_reason(page, status)
        if reason:
            print(f"[!] Facebook จำกัดการเข้าถึง: {reason}")
            _save_debug(page, debug_dir, page_number, reason)
            return finish(reason, completed=False)
        stable = stable + 1 if added == 0 else 0
        if stable >= 4:
            return finish("stable_end", completed=True)
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(random.randint(2500, 4500))
    return finish("max_scrolls", completed=True)


def collect_page_links(
    page_urls: list[str],
    *,
    max_posts_per_page: int = 20,
    max_scrolls: int = 80,
    headed: bool = False,
    seen_posts: set[str] | None = None,
    stop_after_seen: int = 5,
    debug_dir: str | Path = "debug",
    page_anchors: dict[str, set[str]] | None = None,
    anchor_hits_required: int = 3,
    tail_after_anchor: int = 20,
    collection_metadata: dict | None = None,
) -> list[dict[str, str]]:
    if not page_urls:
        raise ValueError("ต้องมี URL เพจอย่างน้อยหนึ่งรายการ")
    seen_posts = set(seen_posts or ())
    results: list[dict[str, str]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not headed,
            args=["--disable-notifications", "--mute-audio", "--no-first-run"],
            **chromium_launch_options(playwright.chromium),
        )
        context = browser.new_context(viewport={"width": 1366, "height": 900}, locale="th-TH")
        page = context.new_page()
        try:
            for index, page_url in enumerate(page_urls, start=1):
                print(f"\n[*] เพจ {index}/{len(page_urls)}: {page_url}")
                try:
                    found = collect_single_page(
                        page, page_url, max_posts_per_page, seen_posts,
                        max_scrolls=max_scrolls,
                        debug_dir=Path(debug_dir),
                        page_number=index,
                        stop_after_seen=stop_after_seen,
                        anchor_post_ids=(page_anchors or {}).get(page_url, set()),
                        anchor_hits_required=anchor_hits_required,
                        tail_after_anchor=tail_after_anchor,
                        collection_metadata=collection_metadata,
                    )
                except Exception as exc:
                    print(f"[!] เก็บลิงก์ไม่สำเร็จ: {type(exc).__name__}: {exc}")
                    _save_debug(page, Path(debug_dir), index, "unexpected_error")
                    if collection_metadata is not None:
                        collection_metadata[page_url] = {
                            "completed": False,
                            "stop_reason": "unexpected_error",
                            "observed_post_ids": [],
                            "anchor_hits": [],
                            "new_post_ids": [],
                        }
                    found = []
                for position, item in enumerate(found, start=1):
                    item["page_sequence"] = str(index)
                    item["page_position"] = str(position)
                    if item["post_id"] not in {row["post_id"] for row in results}:
                        results.append(item)
                if index < len(page_urls):
                    time.sleep(random.randint(4, 8))
        finally:
            context.close()
            browser.close()
    return results
