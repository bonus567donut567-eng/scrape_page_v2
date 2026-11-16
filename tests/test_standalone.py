import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import collector
import facebook_post_scraper as post_scraper
import google_submit
import main
import output_cleanup
import page_state
from json_store import load_json, save_json


class CollectorTests(unittest.TestCase):
    def test_build_photo_urls_accepts_username(self):
        self.assertEqual(
            collector.build_photo_urls("facebook.com/example"),
            [
                "https://www.facebook.com/example/photos",
                "https://www.facebook.com/example?sk=photos",
            ],
        )

    def test_build_photo_urls_rejects_post(self):
        with self.assertRaises(ValueError):
            collector.build_photo_urls("https://www.facebook.com/example/posts/123")

    def test_extract_post_info_keeps_album_set(self):
        post_id, url = collector.extract_post_info(
            "https://www.facebook.com/photo.php?fbid=12345&set=a.999"
        )
        self.assertEqual(post_id, "12345")
        self.assertEqual(url, "https://www.facebook.com/photo/?fbid=12345&set=a.999")

    def test_extract_items_ignores_profile_picture(self):
        html = '''
        <a href="/photo/?fbid=111&set=pb.55"><img alt="Profile picture"></a>
        <a href="/photo/?fbid=222&set=a.77"><img alt="ข่าววันนี้"></a>
        '''
        items = collector.extract_photo_items(
            html, "https://www.facebook.com/example/photos",
            "https://www.facebook.com/55",
        )
        self.assertEqual([item["post_id"] for item in items], ["222"])

    def test_stops_page_after_five_consecutive_seen_posts(self):
        class FakePage:
            url = "https://www.facebook.com/example/photos"

            def goto(self, *_args, **_kwargs):
                return None

            def wait_for_timeout(self, _milliseconds):
                return None

            def content(self):
                return ""

        items = [
            {"post_id": value, "url": f"https://facebook.com/photo/?fbid={value}"}
            for value in ("new-1", "old-1", "old-2", "new-2",
                          "old-3", "old-4", "old-5", "old-6", "old-7", "new-3")
        ]
        seen = {f"old-{index}" for index in range(1, 8)}
        with (
            patch.object(collector, "close_login_dialog"),
            patch.object(collector, "extract_photo_items", return_value=items),
        ):
            results = collector.collect_single_page(
                FakePage(),
                "https://facebook.com/example",
                20,
                seen,
                max_scrolls=10,
                debug_dir=Path("debug"),
                page_number=1,
                stop_after_seen=5,
            )
        self.assertEqual([item["post_id"] for item in results], ["new-1", "new-2"])

    def test_extract_items_keeps_newest_to_oldest_dom_order(self):
        html = '''
        <a href="/photo/?fbid=300&set=a.77"><img alt="ล่าสุด"></a>
        <a href="/photo/?fbid=200&set=a.77"><img alt="ก่อนหน้า"></a>
        <a href="/photo/?fbid=100&set=a.77"><img alt="เก่าสุด"></a>
        '''
        items = collector.extract_photo_items(
            html, "https://www.facebook.com/example/photos",
            "https://www.facebook.com/77",
        )
        self.assertEqual(
            [item["post_id"] for item in items],
            ["300", "200", "100"],
        )

    def test_anchor_mode_stops_after_required_anchors_and_tail(self):
        class FakePage:
            url = "https://www.facebook.com/example/photos"

            def goto(self, *_args, **_kwargs):
                return None

            def wait_for_timeout(self, _milliseconds):
                return None

            def content(self):
                return ""

        values = ("new-1", "a-1", "new-2", "a-2", "new-3", "a-3", "tail-1", "tail-2", "ignored")
        items = [
            {"post_id": value, "url": f"https://facebook.com/photo/?fbid={value}"}
            for value in values
        ]
        metadata = {}
        with (
            patch.object(collector, "close_login_dialog"),
            patch.object(collector, "extract_photo_items", return_value=items),
        ):
            results = collector.collect_single_page(
                FakePage(),
                "https://facebook.com/example",
                200,
                {"a-1", "a-2", "a-3"},
                max_scrolls=10,
                debug_dir=Path("debug"),
                page_number=1,
                anchor_post_ids={"a-1", "a-2", "a-3"},
                anchor_hits_required=3,
                tail_after_anchor=2,
                collection_metadata=metadata,
            )
        self.assertEqual(
            [item["post_id"] for item in results],
            ["new-1", "new-2", "new-3", "tail-1", "tail-2"],
        )
        self.assertEqual(metadata["https://facebook.com/example"]["stop_reason"], "anchor_tail_complete")
        self.assertNotIn("ignored", metadata["https://facebook.com/example"]["observed_post_ids"])


class PageStateTests(unittest.TestCase):
    def test_migrates_only_legacy_success_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page_states.json"
            state = page_state.load_page_state(path, {"sent-1", "sent-2"})
            self.assertEqual(page_state.permanent_ids(state), {"sent-1", "sent-2"})
            self.assertEqual(state["pages"], {})

    def test_parser_version_change_rechecks_non_permanent_posts_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page_states.json"
            save_json(path, {
                "schema_version": page_state.SCHEMA_VERSION,
                "reaction_parser_version": 1,
                "legacy_submitted_ids": [],
                "pages": {
                    "page": {
                        "anchors": [],
                        "posts": {
                            "low": {"status": "expired", "reaction_count": 10, "url": "low"},
                            "sent": {"status": "submitted", "reaction_count": 2000, "url": "sent"},
                        },
                    },
                },
            })
            state = page_state.load_page_state(path)
            posts = state["pages"]["page"]["posts"]
            self.assertEqual(posts["low"]["status"], "watching")
            self.assertNotIn("reaction_count", posts["low"])
            self.assertEqual(posts["sent"]["status"], "submitted")
            self.assertEqual(posts["sent"]["reaction_count"], 2000)

    def test_watchlist_rechecks_then_expires_known_low_reaction(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        page_url = "https://facebook.com/example"
        item = {
            "post_id": "100",
            "url": "https://facebook.com/photo/?fbid=100",
            "source_page_url": page_url,
        }
        state = page_state.new_state()
        page_state.register_discovered(state, [item], now=now - timedelta(days=8))
        page_state.update_post_result(
            state,
            item,
            {
                "status": "filtered",
                "error": "reaction_below_threshold",
                "reaction_count": 500,
                "reaction_text": "500",
                "reaction_source": "dom_primary_icon_count",
            },
            now=now - timedelta(days=8),
        )
        self.assertEqual(
            page_state.due_watch_items(state, [page_url], now=now, watch_days=7),
            [],
        )
        self.assertEqual(state["pages"][page_url]["posts"]["100"]["status"], "expired")

    def test_unavailable_reaction_never_expires_and_submitted_is_permanent(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        page_url = "https://facebook.com/example"
        item = {
            "post_id": "100",
            "url": "https://facebook.com/photo/?fbid=100",
            "source_page_url": page_url,
        }
        state = page_state.new_state()
        page_state.register_discovered(state, [item], now=now - timedelta(days=30))
        page_state.update_post_result(
            state,
            item,
            {
                "status": "filtered",
                "error": "reaction_count_unavailable",
                "reaction_count": None,
            },
            now=now - timedelta(days=30),
        )
        self.assertEqual(len(page_state.due_watch_items(state, [page_url], now=now)), 1)
        page_state.mark_submitted(state, {"100"}, now=now)
        self.assertEqual(page_state.permanent_ids(state), {"100"})
        self.assertEqual(page_state.due_watch_items(state, [page_url], now=now), [])

    def test_anchors_update_only_after_clean_collection(self):
        state = page_state.new_state()
        page_state.update_anchors(state, {
            "good": {"completed": True, "observed_post_ids": ["3", "2", "1"], "stop_reason": "scan_limit"},
            "blocked": {"completed": False, "observed_post_ids": ["9"], "stop_reason": "login_required"},
        })
        self.assertEqual(state["pages"]["good"]["anchors"], ["3", "2", "1"])
        self.assertNotIn("blocked", state["pages"])


class ReactionCountTests(unittest.TestCase):
    def test_parse_reaction_count_formats(self):
        cases = {
            "1,234 reactions": 1234,
            "1.2K reactions": 1200,
            "12.5K reactions": 12500,
            "1.2 พัน ความรู้สึก": 1200,
            "๑,๒๓๔ ความรู้สึก": 1234,
            "1 แสน": 100000,
            "7.7 หมื่น": 77000,
            "1.3 หมื่น": 13000,
            "1 ล้าน reactions": 1000000,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(post_scraper.parse_reaction_count(value), expected)

    def test_extract_reaction_count_prefers_largest_supported_evidence(self):
        class FakeDriver:
            page_source = '{"reaction_count":{"count":1300}}'

            def execute_script(self, _script):
                return ["All reactions: 1.2K", "23 comments", "Like"]

        self.assertEqual(
            post_scraper.extract_reaction_count(FakeDriver()),
            (1200, "All reactions: 1.2K", "dom"),
        )

    def test_extract_reaction_count_uses_primary_dom_candidate_not_largest(self):
        class FakeDriver:
            page_source = ""

            def execute_script(self, _script):
                return [
                    {
                        "value": "1 แสน",
                        "top": 415,
                        "priority": 90,
                        "kind": "primary_icon_count",
                    },
                    {
                        "value": "941 reactions",
                        "top": 939,
                        "priority": 10,
                        "kind": "comment",
                    },
                ]

        self.assertEqual(
            post_scraper.extract_reaction_count(FakeDriver(), "1430813572517612"),
            (100000, "1 แสน", "dom_primary_icon_count"),
        )

    def test_extract_reaction_count_does_not_use_unscoped_embedded_json(self):
        class FakeDriver:
            page_source = '{"reaction_count":{"count":941}}'

            def execute_script(self, _script):
                return []

        self.assertEqual(
            post_scraper.extract_reaction_count(FakeDriver(), "1430813572517612"),
            (None, "", "unavailable"),
        )


class StoreAndCliTests(unittest.TestCase):
    def test_atomic_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_json(path, {"ภาษา": "ไทย", "items": [1, 2]})
            self.assertEqual(load_json(path, dict)["ภาษา"], "ไทย")

    def test_split_urls_deduplicates(self):
        self.assertEqual(main.split_urls(["a,b", " b ", "c"]), ["a", "b", "c"])

    def test_pages_file_supports_comments_names_and_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pages.txt"
            path.write_text(
                "# เพจข่าว\nIG.dara\nhttps://www.facebook.com/example\nIG.dara\n",
                encoding="utf-8",
            )
            self.assertEqual(
                main.load_pages_file(path),
                [
                    "https://www.facebook.com/IG.dara",
                    "https://www.facebook.com/example",
                ],
            )

    def test_full_run_has_fixed_target_and_seen_boundary(self):
        args = main.parse_args(["--full-run", "--max-posts", "99", "--no-comments"])
        self.assertEqual(args.pages_file, str(main.DEFAULT_PAGES_FILE))
        self.assertEqual(args.max_posts, 20)
        self.assertEqual(args.min_likes, 3000)
        self.assertEqual(args.scan_limit, 200)
        self.assertEqual(args.stop_after_seen, 5)
        self.assertFalse(args.retry_all)
        self.assertFalse(args.no_comments)
        self.assertFalse(args.no_submit)

    def test_cli_validates_max_posts(self):
        with self.assertRaises(SystemExit):
            main.parse_args(["--url", "https://facebook.com/example", "--max-posts", "0"])

    def test_filtered_post_is_not_downloaded_or_saved_as_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fake_scrape(*_args, metadata=None, **_kwargs):
                metadata.update({
                    "reaction_count": 1000,
                    "reaction_text": "1K reactions",
                    "reaction_source": "dom",
                })
                return False, "", "reaction_below_threshold"

            with patch.object(main, "scrape_fb_post_with_comments", side_effect=fake_scrape):
                record = main.write_post_result(
                    {"post_id": "123", "url": "https://facebook.com/photo/?fbid=123"},
                    root,
                    collect_comments=True,
                    min_reactions=1000,
                )
            self.assertEqual(record["status"], "filtered")
            self.assertEqual(record["reaction_count"], 1000)
            self.assertFalse((root / "posts" / "123").exists())

    def test_run_scans_in_order_until_target_count_is_reached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = main.parse_args([
                "--url", "example",
                "--max-posts", "2",
                "--min-likes", "1000",
                "--scan-limit", "5",
                "--retry-all",
                "--no-comments",
                "--no-submit",
                "--output", str(root / "out"),
                "--history", str(root / "seen.json"),
                "--scanned-history", str(root / "scanned.json"),
            ])
            items = [
                {
                    "post_id": str(index),
                    "url": f"https://facebook.com/photo/?fbid={index}",
                    "source_page_url": "https://facebook.com/example",
                    "page_sequence": "1",
                    "page_position": str(index),
                }
                for index in range(1, 6)
            ]
            outcomes = [
                {"status": "filtered", "error": "reaction_below_threshold", "reaction_count": 500},
                {"status": "success", "error": "", "reaction_count": 1100},
                {"status": "filtered", "error": "reaction_below_threshold", "reaction_count": 1000},
                {"status": "success", "error": "", "reaction_count": 2000},
            ]
            with (
                patch.object(main, "collect_page_links", return_value=items),
                patch.object(main, "write_post_result", side_effect=outcomes) as writer,
            ):
                self.assertEqual(main.run(args), 0)
            self.assertEqual(writer.call_count, 4)
            summary = load_json(root / "out" / "latest_run.json", dict)
            self.assertEqual(summary["success_count"], 2)
            self.assertEqual(summary["filtered_count"], 2)
            self.assertEqual(summary["accepted_per_page"], {"1": 2})
            self.assertEqual(
                load_json(root / "scanned.json", list),
                ["1", "2", "3", "4"],
            )

    def test_incremental_run_ignores_legacy_scanned_and_keeps_low_post_watching(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_json(root / "seen.json", ["sent-1"])
            save_json(root / "scanned.json", ["poisoned-1", "poisoned-2"])
            state_path = root / "page_states.json"
            args = main.parse_args([
                "--url", "example",
                "--max-posts", "2",
                "--min-likes", "1000",
                "--scan-limit", "5",
                "--no-comments",
                "--no-submit",
                "--output", str(root / "out"),
                "--history", str(root / "seen.json"),
                "--scanned-history", str(root / "scanned.json"),
                "--page-state", str(state_path),
            ])
            page_url = "https://www.facebook.com/example"
            item = {
                "post_id": "new-1",
                "url": "https://facebook.com/photo/?fbid=new-1",
                "source_page_url": page_url,
                "page_sequence": "1",
                "page_position": "1",
            }

            def fake_collect(_pages, **kwargs):
                self.assertEqual(kwargs["seen_posts"], {"sent-1"})
                metadata = kwargs["collection_metadata"]
                metadata[page_url] = {
                    "completed": True,
                    "stop_reason": "scan_limit",
                    "observed_post_ids": ["new-1", "sent-1"],
                    "anchor_hits": [],
                    "new_post_ids": ["new-1"],
                }
                return [item]

            outcome = {
                "post_id": "new-1",
                "status": "filtered",
                "error": "reaction_below_threshold",
                "reaction_count": 500,
                "reaction_text": "500",
                "reaction_source": "dom_primary_icon_count",
            }
            with (
                patch.object(main, "collect_page_links", side_effect=fake_collect),
                patch.object(main, "write_post_result", return_value=outcome),
            ):
                self.assertEqual(main.run(args), 0)

            state = load_json(state_path, dict)
            post = state["pages"][page_url]["posts"]["new-1"]
            self.assertEqual(post["status"], "watching")
            self.assertEqual(post["reaction_count"], 500)
            self.assertEqual(state["pages"][page_url]["anchors"], ["new-1", "sent-1"])
            self.assertEqual(load_json(root / "scanned.json", list), ["poisoned-1", "poisoned-2"])

    def test_failed_scrape_is_not_added_to_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = main.parse_args([
                "--post-url", "https://www.facebook.com/photo/?fbid=12345",
                "--output", str(root / "out"),
                "--history", str(root / "seen.json"),
                "--no-comments",
                "--no-submit",
            ])
            with patch.object(
                main,
                "scrape_fb_post_with_comments",
                return_value=(False, "", "blocked"),
            ):
                self.assertEqual(main.run(args), 2)
            self.assertEqual(load_json(root / "seen.json", list), [])

    def test_submit_only_does_not_require_page_url(self):
        args = main.parse_args(["--submit-only"])
        with patch.object(
            main,
            "process_pending_submissions",
            return_value={"total": 1, "submitted": 1, "failed": 0},
        ) as submitter, patch.object(
            main,
            "cleanup_output",
            return_value={"removed_files": 0, "freed_bytes": 0},
        ):
            self.assertEqual(main.run(args), 0)
        submitter.assert_called_once_with(main.DEFAULT_OUTPUT.resolve())


class GoogleSubmissionQueueTests(unittest.TestCase):
    def test_append_post_uploads_drive_and_appends_columns_by_header(self):
        class Request:
            def __init__(self, value):
                self.value = value

            def execute(self):
                return self.value

        class DriveFiles:
            def list(self, **_kwargs):
                return Request({"files": [{"id": "folder-1"}]})

            def create(self, **_kwargs):
                return Request({"id": "file-1"})

            def get(self, **_kwargs):
                return Request({"webViewLink": "https://drive.example/file-1"})

        class DrivePermissions:
            def create(self, **_kwargs):
                return Request({})

        class Drive:
            def files(self):
                return DriveFiles()

            def permissions(self):
                return DrivePermissions()

        appended = {}

        class SheetValues:
            def get(self, **_kwargs):
                return Request({"values": [["original_picture", "time", "original_caption"]]})

            def append(self, **kwargs):
                appended.update(kwargs)
                return Request({})

        class Spreadsheets:
            def get(self, **_kwargs):
                return Request({"sheets": [{"properties": {"sheetId": 1674818093, "title": "Data"}}]})

            def values(self):
                return SheetValues()

        class Sheets:
            def spreadsheets(self):
                return Spreadsheets()

        with tempfile.TemporaryDirectory() as directory:
            post_dir = Path(directory) / "123"
            post_dir.mkdir()
            (post_dir / "caption.txt").write_text("ทดสอบ caption", encoding="utf-8")
            (post_dir / "image.jpg").write_bytes(b"image")
            google_submit._folder_id_cache = None
            google_submit._folder_resolved = False
            with (
                patch("googleapiclient.discovery.build", side_effect=lambda name, *_a, **_k: Drive() if name == "drive" else Sheets()),
                patch("googleapiclient.http.MediaFileUpload", return_value=object()),
            ):
                self.assertTrue(google_submit.append_post(post_dir, credentials=object()))

            row = appended["body"]["values"][0]
            self.assertEqual(row[0], "https://drive.example/file-1")
            self.assertTrue(row[1])
            self.assertEqual(row[2], "ทดสอบ caption")
            self.assertTrue((post_dir / "submitted.flag").is_file())

    def test_pending_queue_skips_submitted_flag_and_retries_unsubmitted(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            pending = output / "posts" / "100"
            submitted = output / "posts" / "200"
            for post_dir in (pending, submitted):
                post_dir.mkdir(parents=True)
                (post_dir / "post.json").write_text("{}", encoding="utf-8")
                (post_dir / "caption.txt").write_text("caption", encoding="utf-8")
                (post_dir / "image.jpg").write_bytes(b"image")
            (submitted / "submitted.flag").write_text("submitted\n", encoding="utf-8")

            with patch.object(google_submit, "append_post", return_value=True) as append:
                stats = google_submit.process_pending_submissions(output)

            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["submitted_post_ids"], ["100"])
            append.assert_called_once_with(pending)

    def test_append_post_with_flag_is_idempotent_without_api(self):
        with tempfile.TemporaryDirectory() as directory:
            post_dir = Path(directory)
            (post_dir / "submitted.flag").write_text("submitted\n", encoding="utf-8")
            self.assertTrue(google_submit.append_post(post_dir))

    def test_failed_submission_retries_once_then_becomes_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            post_dir = output / "posts" / "100"
            post_dir.mkdir(parents=True)
            (post_dir / "post.json").write_text("{}", encoding="utf-8")
            (post_dir / "caption.txt").write_text("caption", encoding="utf-8")
            (post_dir / "image.jpg").write_bytes(b"image")

            with (
                patch.object(google_submit, "append_post", return_value=False) as append,
                patch.object(google_submit.time, "sleep") as sleep,
            ):
                result = google_submit.process_pending_submissions(output)
                state = load_json(
                    post_dir / google_submit.SUBMISSION_STATE, dict
                )

            self.assertEqual(result["terminal_failed_post_ids"], ["100"])
            self.assertEqual(state["attempt_count"], 2)
            self.assertEqual(state["status"], "failed")
            self.assertNotIn("delete_after", state)
            self.assertEqual(append.call_count, 2)
            sleep.assert_called_once_with(20)
            self.assertFalse((post_dir / "image.jpg").exists())
            self.assertFalse((post_dir / "caption.txt").exists())


class OutputCleanupTests(unittest.TestCase):
    def test_cleanup_removes_only_submitted_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            submitted = output / "posts" / "100"
            pending = output / "posts" / "200"
            for post_dir in (submitted, pending):
                comments = post_dir / "comments"
                comments.mkdir(parents=True)
                (post_dir / "post.json").write_text("{}", encoding="utf-8")
                (post_dir / "caption.txt").write_text("caption", encoding="utf-8")
                (post_dir / "image.jpg").write_bytes(b"image")
                (comments / "comment_001.jpg").write_bytes(b"comment")
            (submitted / "submitted.flag").write_text("submitted\n", encoding="utf-8")

            stats = output_cleanup.cleanup_submitted_posts(output)

            self.assertEqual(stats["posts_cleaned"], 1)
            self.assertTrue((submitted / "post.json").is_file())
            self.assertTrue((submitted / "submitted.flag").is_file())
            self.assertFalse((submitted / "caption.txt").exists())
            self.assertFalse((submitted / "image.jpg").exists())
            self.assertFalse((submitted / "comments").exists())
            self.assertTrue((pending / "caption.txt").is_file())
            self.assertTrue((pending / "image.jpg").is_file())
            self.assertTrue((pending / "comments").is_dir())

    def test_cleanup_keeps_latest_thirty_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            runs = output / "runs"
            runs.mkdir(parents=True)
            for index in range(35):
                path = runs / f"run_{index:02d}.json"
                path.write_text("{}", encoding="utf-8")
            stats = output_cleanup.cleanup_run_history(output, keep=30)
            remaining = sorted(runs.glob("run_*.json"))
            self.assertEqual(stats["removed_files"], 5)
            self.assertEqual(len(remaining), 30)
            self.assertEqual(remaining[0].name, "run_05.json")

    def test_cleanup_removes_expired_debug_only(self):
        import os
        import time

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            debug = output / "debug"
            debug.mkdir(parents=True)
            old = debug / "old.html"
            fresh = debug / "fresh.html"
            old.write_text("old", encoding="utf-8")
            fresh.write_text("fresh", encoding="utf-8")
            expired = time.time() - 8 * 86400
            os.utime(old, (expired, expired))

            stats = output_cleanup.cleanup_debug_files(output, retention_days=7)

            self.assertEqual(stats["removed_files"], 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

    def test_cleanup_removes_terminal_failure_media_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            failed = output / "posts" / "failed"
            retry = output / "posts" / "retry"
            for post_dir in (failed, retry):
                post_dir.mkdir(parents=True)
                (post_dir / "post.json").write_text("{}", encoding="utf-8")
                (post_dir / "caption.txt").write_text("caption", encoding="utf-8")
                (post_dir / "image.jpg").write_bytes(b"image")

            save_json(failed / "submission_state.json", {
                "status": "failed",
                "attempt_count": 2,
            })
            save_json(retry / "submission_state.json", {
                "status": "retry_pending",
                "attempt_count": 1,
            })

            stats = output_cleanup.cleanup_failed_post_media_all(output)

            self.assertEqual(stats["posts_cleaned"], 1)
            self.assertTrue(failed.is_dir())
            self.assertFalse((failed / "image.jpg").exists())
            self.assertFalse((failed / "caption.txt").exists())
            self.assertTrue(retry.is_dir())
            self.assertTrue((retry / "image.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
