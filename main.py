"""CLI entry point for the standalone Facebook Page scraper."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

from filelock import FileLock, Timeout

from collector import collect_page_links
from facebook_post_scraper import COMMENT_MANIFEST_NAME, scrape_fb_post_with_comments
from google_submit import process_pending_submissions
from json_store import load_json, save_json
from output_cleanup import cleanup_output
from page_state import (
    anchors_by_page,
    due_watch_items,
    known_ids,
    load_page_state,
    mark_submitted,
    register_discovered,
    save_page_state,
    update_anchors,
    update_post_result,
)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "outputs"
DEFAULT_HISTORY = ROOT / "data" / "seen_posts.json"
DEFAULT_SCANNED_HISTORY = ROOT / "data" / "scanned_posts.json"
DEFAULT_PAGE_STATE = ROOT / "data" / "page_states.json"
DEFAULT_PAGES_FILE = ROOT / "pages.txt"


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_print(*values: object) -> None:
    try:
        print(*values)
    except UnicodeEncodeError:
        message = " ".join(str(value) for value in values) + "\n"
        sys.stdout.buffer.write(message.encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


def split_urls(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for item in str(value or "").split(","):
            item = item.strip()
            if item and item not in result:
                result.append(item)
    return result


def normalize_page_entry(value: str) -> str:
    """Accept a full Facebook URL, facebook.com path, or Page username/ID."""
    value = str(value or "").strip()
    if not value:
        return ""
    if "://" in value or value.lower().startswith(("facebook.com/", "www.facebook.com/")):
        return value
    return f"https://www.facebook.com/{value.lstrip('/')}"


def load_pages_file(path: str | Path) -> list[str]:
    pages_path = Path(path).expanduser().resolve()
    if not pages_path.is_file():
        raise SystemExit(f"ไม่พบไฟล์รายชื่อเพจ: {pages_path}")
    values: list[str] = []
    for raw_line in pages_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        value = normalize_page_entry(line)
        if value and value not in values:
            values.append(value)
    return values


def relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def comment_summary(directory: Path) -> dict:
    manifest_path = directory / COMMENT_MANIFEST_NAME
    manifest = load_json(manifest_path, dict)
    if not isinstance(manifest, dict):
        manifest = {}
    return {
        "manifest": str(manifest_path),
        "status": manifest.get("status", "not_requested"),
        "accepted_count": len(manifest.get("accepted", [])) if isinstance(manifest.get("accepted"), list) else 0,
        "rejected_count": len(manifest.get("rejected", [])) if isinstance(manifest.get("rejected"), list) else 0,
    }


def write_post_result(
    item: dict[str, str],
    output_root: Path,
    *,
    collect_comments: bool,
    min_reactions: int = 0,
) -> dict:
    post_id = str(item["post_id"])
    post_dir = output_root / "posts" / post_id
    image_path = post_dir / "image.jpg"
    caption_path = post_dir / "caption.txt"
    comments_dir = post_dir / "comments"
    started_at = now_text()
    scrape_metadata: dict = {}

    success, caption, error = scrape_fb_post_with_comments(
        item["url"],
        str(image_path),
        str(comments_dir) if collect_comments else None,
        source_page_id=str(item.get("source_page_id", "")),
        source_page_url=str(item.get("source_page_url", "")),
        collect_comments=collect_comments,
        min_reactions=min_reactions,
        metadata=scrape_metadata,
    )
    caption = str(caption or "").strip()
    error = str(error or "")
    filtered = error in {"reaction_below_threshold", "reaction_count_unavailable"}
    status = "success" if success else "filtered" if filtered else "failed"
    if success:
        post_dir.mkdir(parents=True, exist_ok=True)
        caption_path.write_text(caption + "\n", encoding="utf-8")

    record = {
        "post_id": post_id,
        "url": item["url"],
        "source_page_url": item.get("source_page_url", ""),
        "source_page_id": item.get("source_page_id", ""),
        "page_sequence": item.get("page_sequence", ""),
        "page_position": item.get("page_position", ""),
        "status": status,
        "error": "" if success else error or "unknown_error",
        "reaction_count": scrape_metadata.get("reaction_count"),
        "reaction_text": scrape_metadata.get("reaction_text", ""),
        "reaction_source": scrape_metadata.get("reaction_source", "unavailable"),
        "minimum_reactions_exclusive": int(min_reactions),
        "started_at": started_at,
        "finished_at": now_text(),
        "caption": caption if success else "",
        "caption_path": relative_to_root(caption_path, output_root) if success else "",
        "image_path": relative_to_root(image_path, output_root) if success and image_path.is_file() else "",
        "comments": comment_summary(comments_dir) if success and collect_comments else {
            "manifest": "",
            "status": "disabled" if not collect_comments else "not_started",
            "accepted_count": 0,
            "rejected_count": 0,
        },
    }
    if status != "filtered":
        save_json(post_dir / "post.json", record)
    return record


def load_success_ids(history_path: Path) -> set[str]:
    value = load_json(history_path, list)
    if isinstance(value, list):
        return {str(item) for item in value if str(item).strip()}
    return set()


def save_success_ids(history_path: Path, values: set[str]) -> None:
    save_json(history_path, sorted(values))


def run(args: argparse.Namespace) -> int:
    output_root = Path(args.output).expanduser().resolve()
    history_path = Path(args.history).expanduser().resolve()
    scanned_history_path = Path(args.scanned_history).expanduser().resolve()
    page_state_path = Path(args.page_state).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if args.submit_only:
        stats = process_pending_submissions(output_root)
        cleanup_stats = cleanup_output(output_root)
        safe_print(
            f"[สรุปส่งงาน] ค้าง {stats['total']} | "
            f"ส่งสำเร็จ {stats['submitted']} | ล้มเหลว {stats['failed']} | "
            f"cleanup {cleanup_stats['removed_files']} ไฟล์ "
            f"({cleanup_stats['freed_bytes'] / 1048576:.2f} MB)"
        )
        return 2 if stats["failed"] else 0
    success_ids = load_success_ids(history_path)
    scanned_ids = load_success_ids(scanned_history_path)
    previously_seen_ids = success_ids | scanned_ids
    incremental_state = load_page_state(page_state_path, success_ids)
    use_incremental_state = args.min_likes > 0 and not args.post_url and not args.retry_all
    page_urls = [normalize_page_entry(url) for url in split_urls([*args.url, args.urls])]
    if not args.post_url and args.pages_file:
        for url in load_pages_file(args.pages_file):
            if url not in page_urls:
                page_urls.append(url)

    if not page_urls and not args.post_url:
        raise SystemExit(
            "ยังไม่มีรายชื่อเพจ กรุณาเพิ่มชื่อผู้ใช้หรือ URL ลงใน pages.txt "
            "หนึ่งเพจต่อหนึ่งบรรทัด"
        )

    collection_metadata: dict[str, dict] = {}
    if args.post_url:
        post_items = []
        for url in split_urls(args.post_url):
            from collector import extract_post_info
            post_id, clean_url = extract_post_info(url)
            if not post_id or not clean_url:
                raise SystemExit(f"ไม่พบ Facebook photo post ID จาก URL: {url}")
            post_items.append({"post_id": post_id, "url": clean_url, "source_page_url": ""})
    else:
        candidate_limit = args.scan_limit if args.min_likes > 0 else args.max_posts
        if use_incremental_state:
            safe_print(
                f"[*] ใช้ incremental state v2 | watchlist {args.watch_days} วัน | "
                f"anchor {args.anchor_hits} + tail {args.anchor_tail} | "
                f"ไม่ใช้ scanned history รุ่นเก่า {len(scanned_ids):,} ID"
            )
            discovered_items = collect_page_links(
                page_urls,
                max_posts_per_page=candidate_limit,
                max_scrolls=args.max_scrolls,
                headed=args.headed,
                seen_posts=known_ids(incremental_state),
                stop_after_seen=args.stop_after_seen,
                debug_dir=output_root / "debug",
                page_anchors=anchors_by_page(incremental_state),
                anchor_hits_required=args.anchor_hits,
                tail_after_anchor=args.anchor_tail,
                collection_metadata=collection_metadata,
            )
            register_discovered(incremental_state, discovered_items)
            update_anchors(incremental_state, collection_metadata)
            post_items = due_watch_items(
                incremental_state,
                page_urls,
                watch_days=args.watch_days,
            )
            save_page_state(page_state_path, incremental_state)
        else:
            post_items = collect_page_links(
                page_urls,
                max_posts_per_page=candidate_limit,
                max_scrolls=args.max_scrolls,
                headed=args.headed,
                seen_posts=set() if args.retry_all else previously_seen_ids,
                stop_after_seen=args.stop_after_seen,
                debug_dir=output_root / "debug",
            )

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_file = output_root / "runs" / f"run_{run_id}.json"
    summary = {
        "run_id": run_id,
        "started_at": now_text(),
        "page_urls": page_urls,
        "requested_posts": len(post_items),
        "target_posts_per_page": args.max_posts,
        "minimum_reactions_exclusive": args.min_likes,
        "scan_limit_per_page": args.scan_limit,
        "stop_after_consecutive_seen": 0 if use_incremental_state else args.stop_after_seen,
        "legacy_scanned_ids_ignored": len(scanned_ids) if use_incremental_state else 0,
        "incremental_state_enabled": use_incremental_state,
        "page_state_path": str(page_state_path) if use_incremental_state else "",
        "watch_days": args.watch_days if use_incremental_state else 0,
        "anchor_hits_required": args.anchor_hits if use_incremental_state else 0,
        "anchor_tail_posts": args.anchor_tail if use_incremental_state else 0,
        "collection_metadata": collection_metadata,
        "collect_comments": not args.no_comments,
        "google_submission_enabled": not args.no_submit,
        "results": [],
    }
    save_json(run_file, summary)

    safe_print(f"\n[*] พบ {len(post_items)} โพสต์ผู้สมัครสำหรับประมวลผล")
    if args.min_likes > 0:
        safe_print(
            f"[*] เกณฑ์: Reaction มากกว่า {args.min_likes:,} | "
            f"เป้าหมาย {args.max_posts} โพสต์ต่อเพจ"
        )
    accepted_per_page: dict[str, int] = {}
    for index, item in enumerate(post_items, start=1):
        page_key = str(item.get("page_sequence") or item.get("source_page_url") or "single")
        if accepted_per_page.get(page_key, 0) >= args.max_posts:
            continue
        post_id = str(item["post_id"])
        if post_id in success_ids and not args.retry_all:
            safe_print(f"[{index}/{len(post_items)}] ข้าม {post_id}: เคยสำเร็จแล้ว")
            continue
        safe_print(f"\n[{index}/{len(post_items)}] กำลังดึง post_id={post_id}")
        record = write_post_result(
            item,
            output_root,
            collect_comments=not args.no_comments,
            min_reactions=args.min_likes,
        )
        record["run_position"] = index
        summary["results"].append(record)
        if use_incremental_state:
            update_post_result(incremental_state, item, record)
            save_page_state(page_state_path, incremental_state)
        if record["status"] == "success":
            accepted_per_page[page_key] = accepted_per_page.get(page_key, 0) + 1
            success_ids.add(post_id)
            save_success_ids(history_path, success_ids)
            safe_print(
                f"[+] รับโพสต์ {accepted_per_page[page_key]}/{args.max_posts}: "
                f"{post_id} | Reaction {record.get('reaction_count')}"
            )
        elif record["status"] == "filtered":
            reaction = record.get("reaction_count")
            detail = "อ่านยอดไม่ได้" if reaction is None else f"Reaction {reaction:,}"
            safe_print(f"[-] กรองออก: {post_id} | {detail}")
        else:
            safe_print(f"[!] ไม่สำเร็จ: {post_id} ({record['error']})")
        if not use_incremental_state and (record["status"] == "success" or (
            record["status"] == "filtered" and record.get("reaction_count") is not None
        )):
            scanned_ids.add(post_id)
            save_success_ids(scanned_history_path, scanned_ids)
        save_json(run_file, summary)
        save_json(output_root / "latest_run.json", summary)

    summary["finished_at"] = now_text()
    summary["success_count"] = sum(row["status"] == "success" for row in summary["results"])
    summary["filtered_count"] = sum(row["status"] == "filtered" for row in summary["results"])
    summary["failed_count"] = sum(row["status"] == "failed" for row in summary["results"])
    summary["accepted_per_page"] = accepted_per_page
    if args.no_submit:
        submission_stats = {
            "enabled": False,
            "total": 0,
            "submitted": 0,
            "failed": 0,
            "submitted_post_ids": [],
            "failed_post_ids": [],
        }
    else:
        safe_print("\n[*] เริ่มส่งโพสต์ที่ค้างไป Google Drive/Sheets")
        submission_stats = {"enabled": True, **process_pending_submissions(output_root)}
        submitted_ids = set(submission_stats["submitted_post_ids"])
        failed_ids = set(submission_stats["failed_post_ids"])
        for row in summary["results"]:
            post_id = str(row.get("post_id", ""))
            if post_id in submitted_ids:
                row["submission_status"] = "submitted"
            elif post_id in failed_ids:
                row["submission_status"] = "failed"
        if use_incremental_state:
            mark_submitted(incremental_state, submitted_ids)
            save_page_state(page_state_path, incremental_state)
    summary["google_submission"] = submission_stats
    cleanup_stats = cleanup_output(output_root)
    summary["cleanup"] = cleanup_stats
    save_json(run_file, summary)
    save_json(output_root / "latest_run.json", summary)
    safe_print(
        f"\n[สรุป] รับ {summary['success_count']} | "
        f"กรองออก {summary['filtered_count']} | "
        f"ดึงไม่สำเร็จ {summary['failed_count']} | "
        f"ส่ง Sheet {submission_stats['submitted']}/{submission_stats['total']} | "
        f"cleanup {cleanup_stats['removed_files']} ไฟล์ "
        f"({cleanup_stats['freed_bytes'] / 1048576:.2f} MB) | {run_file}"
    )
    return 2 if summary["failed_count"] or submission_stats["failed"] else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone Facebook Page scraper: links, main image, caption and Page-owner comment images"
    )
    parser.add_argument("--url", action="append", default=[], help="URL เพจ ใช้ซ้ำได้หลายครั้ง")
    parser.add_argument("--urls", default="", help="URL เพจหลายรายการคั่นด้วย comma")
    parser.add_argument(
        "--pages-file", default="", help="ไฟล์รายชื่อเพจ หนึ่งชื่อผู้ใช้หรือ URL ต่อหนึ่งบรรทัด"
    )
    parser.add_argument(
        "--full-run", action="store_true",
        help="รอบเต็ม: ใช้ anchor/watchlist รับโพสต์ใหม่ที่ Reaction > 3,000 สูงสุด 20 ต่อเพจ",
    )
    parser.add_argument("--post-url", action="append", default=[], help="ดึงโพสต์รูปเดี่ยวโดยไม่กวาดหน้าเพจ")
    parser.add_argument("--max-posts", type=int, default=20, help="จำนวนโพสต์ที่ผ่านเกณฑ์สูงสุดต่อเพจ")
    parser.add_argument(
        "--min-likes", type=int, default=0,
        help="รับเฉพาะโพสต์ที่มี Reaction มากกว่าค่านี้ (0 คือไม่กรอง)",
    )
    parser.add_argument(
        "--scan-limit", type=int, default=200,
        help="จำนวนโพสต์ผู้สมัครสูงสุดที่จะไล่ตรวจต่อเพจเมื่อกรอง Reaction",
    )
    parser.add_argument("--max-scrolls", type=int, default=80, help="จำนวนรอบเลื่อนสูงสุดต่อเพจ")
    parser.add_argument("--headed", action="store_true", help="แสดงหน้าต่าง browser ตอนกวาดหน้าเพจ")
    parser.add_argument("--no-comments", action="store_true", help="ไม่ดึงภาพจากคอมเมนต์ของเจ้าของเพจ")
    parser.add_argument(
        "--no-submit", action="store_true",
        help="ดึงและบันทึกไฟล์อย่างเดียว ไม่ส่ง Google Drive/Sheets",
    )
    parser.add_argument(
        "--submit-only", action="store_true",
        help="ไม่ดึง Facebook ส่งเฉพาะโพสต์ที่ค้างและยังไม่มี submitted.flag",
    )
    parser.add_argument(
        "--retry-all", action="store_true",
        help="ไม่ใช้ประวัติเคยตรวจ และลองโพสต์ที่พบใหม่ทั้งหมด",
    )
    parser.add_argument(
        "--stop-after-seen", type=int, default=5,
        help="โหมดเดิมเท่านั้น: หยุดเมื่อพบ Post ID เดิมติดกันตามจำนวนนี้",
    )
    parser.add_argument(
        "--watch-days", type=int, default=7,
        help="จำนวนวันที่ตรวจ Reaction ซ้ำสำหรับโพสต์ที่ยังไม่ผ่านเกณฑ์",
    )
    parser.add_argument(
        "--anchor-hits", type=int, default=3,
        help="จำนวน anchor เดิมที่ต้องพบก่อนเริ่มระยะตรวจเผื่อ",
    )
    parser.add_argument(
        "--anchor-tail", type=int, default=20,
        help="จำนวนโพสต์ที่ตรวจต่อหลังพบ anchor ครบ",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="โฟลเดอร์ผลลัพธ์")
    parser.add_argument("--history", default=str(DEFAULT_HISTORY), help="ไฟล์ประวัติ post ID ที่สำเร็จ")
    parser.add_argument(
        "--scanned-history", default=str(DEFAULT_SCANNED_HISTORY),
        help="ไฟล์ประวัติแบบเดิม (ใช้เฉพาะโหมดไม่กรองหรือ --retry-all)",
    )
    parser.add_argument(
        "--page-state", default=str(DEFAULT_PAGE_STATE),
        help="ไฟล์ state v2 สำหรับ anchor, watchlist และสถานะส่งสำเร็จ",
    )
    args = parser.parse_args(argv)
    if args.submit_only and args.no_submit:
        parser.error("--submit-only ใช้พร้อม --no-submit ไม่ได้")
    if args.full_run:
        args.pages_file = str(DEFAULT_PAGES_FILE)
        args.max_posts = 20
        args.min_likes = 3000
        args.scan_limit = 200
        args.stop_after_seen = 5
        args.no_comments = False
    if not 1 <= args.max_posts <= 500:
        parser.error("--max-posts ต้องอยู่ระหว่าง 1 ถึง 500")
    if args.min_likes < 0:
        parser.error("--min-likes ต้องไม่ติดลบ")
    if not args.max_posts <= args.scan_limit <= 1000:
        parser.error("--scan-limit ต้องไม่น้อยกว่า --max-posts และไม่เกิน 1000")
    if not 1 <= args.stop_after_seen <= 100:
        parser.error("--stop-after-seen ต้องอยู่ระหว่าง 1 ถึง 100")
    if not 1 <= args.watch_days <= 90:
        parser.error("--watch-days ต้องอยู่ระหว่าง 1 ถึง 90")
    if not 1 <= args.anchor_hits <= 10:
        parser.error("--anchor-hits ต้องอยู่ระหว่าง 1 ถึง 10")
    if not 0 <= args.anchor_tail <= 200:
        parser.error("--anchor-tail ต้องอยู่ระหว่าง 0 ถึง 200")
    if not 1 <= args.max_scrolls <= 100:
        parser.error("--max-scrolls ต้องอยู่ระหว่าง 1 ถึง 100")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lock_path = ROOT / "data" / "scraper.instance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(lock_path)).acquire(timeout=0):
            return run(args)
    except Timeout:
        safe_print("[!] Scraper กำลังทำงานอยู่แล้ว จึงไม่เปิดซ้ำ")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

