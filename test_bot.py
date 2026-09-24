#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import bot
import config_cli


class ReliabilityTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Windows file readers prevent atomic replacement")
    def test_exports_remain_complete_during_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acl.json"
            store = bot.ACLStore(path, 100)
            store.add(200)
            def writer():
                for quota in range(1, 40):
                    store.set_quota(200, quota)
            thread = bot.threading.Thread(target=writer)
            thread.start()
            try:
                for _ in range(80):
                    snapshot = bot.ACLStore.read_access_snapshot(path)
                    self.assertEqual(len(snapshot["users"]), 2)
                    self.assertIn(snapshot["users"][1]["quota"], [50, *range(1, 40)])
            finally:
                thread.join()
            self.assertEqual(bot.ACLStore.read_access_snapshot(path)["users"][1]["quota"], 39)

    def test_disk_failure_does_not_acknowledge_update(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            api = MagicMock()
            api.call.return_value = [{"update_id": 42}]
            service = bot.Bot(api, store)
            service.workers = []
            service.handle_update = MagicMock(side_effect=OSError("disk full"))
            with patch.object(bot, "load_update_offset", return_value=0), patch.object(bot, "save_update_offset") as save:
                with self.assertRaises(OSError):
                    service.start()
                save.assert_not_called()
            service.stop()

    def test_export_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acl.json"
            store = bot.ACLStore(path, 100)
            store.add(200)
            before = (path.read_bytes(), path.stat().st_mtime_ns)
            with patch.object(bot.ACLStore, "_save", side_effect=AssertionError("write")):
                snapshot = bot.ACLStore.read_access_snapshot(path)
            self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
            store.ban(200)
            newer = bot.ACLStore.read_access_snapshot(path)
            self.assertEqual(snapshot["users"][1]["quota"], 50)
            self.assertEqual(newer["users"][1]["quota"], -1)

    def test_pending_job_survives_restart_without_second_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acl.json"
            store = bot.ACLStore(path, 100)
            store.add(200)
            job = (200, 42, 200, "https://x.com/test/status/123")
            self.assertTrue(store.consume(200, job=job)[0])
            restored = bot.ACLStore(path, 100)
            service = bot.Bot(MagicMock(), restored)
            self.assertEqual(service.jobs.get_nowait(), job)
            self.assertFalse(restored.consume(200, job=job)[0])
            self.assertEqual(restored.data["users"]["200"]["usage_count"], 1)
            restored.finish_job(200, 42)
            self.assertFalse(bot.ACLStore(path, 100).data["pending_jobs"])
            service.stop()

    def test_full_inline_slots_do_not_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            store.add(200)
            service = bot.Bot(MagicMock(), store)
            for _ in range(bot.INLINE_MAX_PENDING):
                self.assertTrue(service.inline_slots.acquire(blocking=False))
            service.handle_inline_query({"id": "test", "from": {"id": 200},
                "query": "https://x.com/test/status/123"})
            self.assertEqual(store.data["users"]["200"]["usage_count"], 0)
            service.stop()

    def test_revocation_blocks_fetch_and_delivery(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, "TMP_DIR", Path(directory)):
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            store.add(200)
            api = MagicMock()
            service = bot.Bot(api, store)
            store.ban(200)
            with patch.object(bot, "fetch_fxtwitter") as fetch:
                service.process_url(200, 1, 200, "https://x.com/test/status/123")
                fetch.assert_not_called()
            store.add(200)
            def revoke(*args, **kwargs):
                store.ban(200)
                return [], "", "", False
            with patch.object(bot, "fetch_fxtwitter", return_value=None), patch.object(
                bot, "fetch_tweet_text", return_value=("text", "author", "https://x.com/test")
            ), patch.object(bot, "download_media", side_effect=revoke):
                service.process_url(200, 1, 200, "https://x.com/test/status/123")
            api.send_message.assert_not_called()
            api.send_previews.assert_not_called()
            api.send_documents.assert_not_called()
            service.stop()


class URLTests(unittest.TestCase):
    def test_normalizes_x_url(self):
        self.assertEqual(
            bot.normalize_status_url("see https://x.com/user_1/status/12345?s=20"),
            "https://x.com/user_1/status/12345",
        )

    def test_rejects_non_status_url(self):
        self.assertIsNone(bot.normalize_status_url("https://x.com/home"))

    def test_rejects_lookalike_host(self):
        self.assertIsNone(
            bot.normalize_status_url("https://x.com.example.org/user/status/12345")
        )

    def test_oembed_parser_keeps_only_tweet_body(self):
        parser = bot.TextExtractor()
        parser.feed(
            '<blockquote><p>第一行<br>第二行 <a>pic.twitter.com/abc123</a></p>'
            '— 作者 (@name) <a>August 28, 2026</a></blockquote>'
        )
        text = " ".join(parser.parts).replace(" \n ", "\n")
        text = bot.re.sub(r"(?:https?://)?pic\.twitter\.com/\S+", "", text).strip()
        self.assertEqual(text, "第一行\n第二行")


class ConfigurationCLITests(unittest.TestCase):
    def test_owner_assignment_cannot_replace_claimed_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "acl.json"
            state.write_text(json.dumps({"owner_id": 100}), encoding="utf-8")

            config_cli.validate_owner_assignment(state, 100)
            with self.assertRaisesRegex(SystemExit, "already claimed"):
                config_cli.validate_owner_assignment(state, 200)
            with self.assertRaisesRegex(SystemExit, "out of range"):
                config_cli.validate_owner_assignment(state, 0)

    def test_oembed_returns_canonical_author_profile_url(self):
        response = MagicMock()
        response.json.return_value = {
            "html": "<blockquote><p>Tweet body</p></blockquote>",
            "author_name": "NASA",
            "author_url": "https://twitter.com/NASA",
        }
        session = MagicMock()
        session.get.return_value = response

        with patch.object(bot, "http_session", return_value=session):
            text, author, author_url = bot.fetch_tweet_text(
                "https://x.com/NASA/status/123"
            )

        self.assertEqual((text, author), ("Tweet body", "NASA"))
        self.assertEqual(author_url, "https://x.com/NASA")

    def test_author_profile_url_rejects_untrusted_or_invalid_values(self):
        self.assertEqual(bot.normalize_author_url("https://example.com/NASA"), "")
        self.assertEqual(bot.author_profile_url("invalid/name"), "")

    def test_caption_places_author_profile_url_on_first_line(self):
        text, author, author_url = bot.fxtwitter_text_author({
            "text": "Tweet body",
            "author": {"name": "NASA", "screen_name": "NASA"},
        })
        caption = bot.media_caption(
            author,
            author_url,
            text,
            "https://x.com/NASA/status/123",
        )

        self.assertEqual(
            caption.splitlines()[0],
            '<a href="https://x.com/NASA">NASA</a>:',
        )

    def test_caption_escapes_author_and_tweet_text(self):
        caption = bot.media_caption(
            "<b>Author</b>",
            "https://x.com/author",
            "one < two & three",
            "https://x.com/author/status/123",
        )

        self.assertIn(
            '<a href="https://x.com/author">&lt;b&gt;Author&lt;/b&gt;</a>:',
            caption,
        )
        self.assertIn("one &lt; two &amp; three", caption)
        self.assertNotIn("<b>Author</b>", caption)


class ACLTests(unittest.TestCase):
    def test_acl_recovers_from_last_valid_backup_and_keeps_corrupt_primary(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            store.set_quota(200, 50)
            store.set_language(200, "ja")
            self.assertTrue(path.with_name("acl.json.bak").exists())

            path.write_text("{broken", encoding="utf-8")
            recovered = bot.ACLStore(path, 100)

            self.assertEqual(recovered.quota(200), 50)
            self.assertTrue(list(Path(temporary).glob("acl.json.corrupt-*")))
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_acl_refuses_to_overwrite_when_primary_and_backup_are_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            backup = path.with_name("acl.json.bak")
            path.write_text("broken-main", encoding="utf-8")
            backup.write_text("broken-backup", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
                bot.ACLStore(path, 100)

            self.assertEqual(path.read_text(encoding="utf-8"), "broken-main")
            self.assertEqual(backup.read_text(encoding="utf-8"), "broken-backup")

    def test_access_snapshot_uses_newest_quota_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = bot.ACLStore(Path(temporary) / "first.json", 100)
            second = bot.ACLStore(Path(temporary) / "second.json", 100)
            first.set_quota(200, 50)
            older = first.export_access()
            second.import_access(older)
            second.set_quota(200, 200)
            newer = second.export_access()
            older_timestamp = next(
                item["updated_at"] for item in older["users"] if item["user_id"] == 200
            )
            next(
                item for item in newer["users"] if item["user_id"] == 200
            )["updated_at"] = older_timestamp + 1

            self.assertEqual(first.import_access(newer), 1)
            self.assertEqual(first.quota(200), 200)
            self.assertEqual(second.import_access(older), 0)
            self.assertEqual(second.quota(200), 200)

    def test_access_snapshot_only_changes_id_and_quota(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = bot.ACLStore(Path(temporary) / "source.json", 100)
            target = bot.ACLStore(Path(temporary) / "target.json", 100)
            target.observe({"id": 200, "username": "local-name"}, now=100_000)
            source.observe({"id": 200, "username": "source-name"}, now=100_000)
            source.set_quota(200, 75)

            target.import_access(source.export_access())

            self.assertEqual(target.quota(200), 75)
            self.assertEqual(target.data["users"]["200"]["username"], "local-name")
            self.assertNotIn("usage_count", source.export_access()["users"][1])

    def test_access_snapshot_rejects_invalid_records_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            snapshot = {
                "version": 1,
                "users": [
                    {"user_id": 200, "quota": 50, "updated_at": 1},
                    {"user_id": 300, "quota": 10001, "updated_at": 2},
                ],
            }
            with self.assertRaises(ValueError):
                store.import_access(snapshot)
            self.assertFalse(store.has_user(200))

    def test_owner_and_allowlist(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            self.assertTrue(store.is_allowed(100))
            self.assertFalse(store.is_allowed(200))
            store.add(200)
            self.assertTrue(store.is_allowed(200))
            store.remove(200)
            self.assertFalse(store.is_allowed(200))
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["owner_id"], 100)
            self.assertNotIn("allowed_user_ids", persisted)
            self.assertNotIn("banned_user_ids", persisted)

    def test_legacy_access_lists_migrate_to_unified_quota(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            path.write_text(json.dumps({
                "owner_id": 100,
                "allowed_user_ids": [200, 201],
                "banned_user_ids": [300],
                "users": {
                    "200": {"user_id": 200, "daily_limit": 50},
                    "201": {"user_id": 201, "daily_limit": 0},
                    "300": {"user_id": 300, "daily_limit": 50},
                    "400": {"user_id": 400, "daily_limit": 50},
                },
            }), encoding="utf-8")

            store = bot.ACLStore(path, 100)

            self.assertEqual(store.quota(200), 50)
            self.assertIsNone(store.quota(201))
            self.assertEqual(store.quota(300), -1)
            self.assertEqual(store.quota(400), 0)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("allowed_user_ids", persisted)
            self.assertNotIn("banned_user_ids", persisted)
            self.assertNotIn("daily_limit", persisted["users"]["200"])

    def test_claim_requires_matching_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            with patch.object(bot, "BOOTSTRAP_CODE", "correct-code"):
                store = bot.ACLStore(path)
                self.assertFalse(store.claim(100, "wrong-code"))
                self.assertTrue(store.claim(100, "correct-code"))
                self.assertFalse(store.claim(200, "correct-code"))

    def test_application_metadata_approval_and_daily_report_schedule(self):
        report_time = datetime(1970, 1, 3, bot.DAILY_REPORT_HOUR, tzinfo=bot.BOT_TIMEZONE).timestamp()
        next_report_time = datetime(1970, 1, 4, bot.DAILY_REPORT_HOUR, tzinfo=bot.BOT_TIMEZONE).timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.observe({
                "id": 200,
                "username": "tester",
                "first_name": "Test",
                "last_name": "User",
            })
            self.assertEqual(store.request_access(200, now=100_000), "created")
            self.assertEqual(store.request_access(200, now=100_001), "pending")
            self.assertFalse(store.daily_report_due(now=report_time - 1))
            self.assertTrue(store.daily_report_due(now=report_time))
            self.assertEqual(store.pending()[0]["username"], "tester")
            store.mark_daily_report(now=report_time)
            self.assertFalse(store.daily_report_due(now=next_report_time - 1))
            self.assertTrue(store.daily_report_due(now=next_report_time))
            self.assertTrue(store.approve(200))
            self.assertTrue(store.is_allowed(200))

    def test_denied_user_can_apply_again_without_a_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.observe({"id": 200, "first_name": "Applicant"})
            self.assertEqual(store.request_access(200, now=100_000), "created")
            self.assertTrue(store.deny(200))
            self.assertEqual(store.request_access(200, now=100_001), "created")

    def test_auto_approve_defaults_off_persists_and_approves_applicants(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            self.assertFalse(store.auto_approve_enabled)
            self.assertTrue(store.toggle_auto_approve(100))
            self.assertTrue(bot.ACLStore(path, 100).auto_approve_enabled)
            self.assertEqual(store.request_access(200, now=100_000), "auto_approved")
            self.assertEqual(store.quota(200), bot.DEFAULT_DAILY_LIMIT)
            self.assertEqual(store.pending(), [])
            store.ban(300)
            self.assertEqual(store.request_access(300, now=100_001), "banned")
            self.assertEqual(store.quota(300), -1)
            self.assertFalse(store.toggle_auto_approve(100))

    def test_auto_approve_clears_an_existing_pending_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            self.assertEqual(store.request_access(200, now=100_000), "created")
            store.toggle_auto_approve(100)
            self.assertEqual(store.request_access(200, now=100_001), "auto_approved")
            self.assertTrue(store.is_allowed(200))
            self.assertEqual(store.pending(), [])

    def test_profile_is_refreshed_only_on_first_interaction_each_day(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            self.assertTrue(store.observe(
                {"id": 200, "username": "old", "first_name": "Old"},
                now=100_000,
            ))
            self.assertFalse(store.observe(
                {"id": 200, "username": "new", "first_name": "New"},
                now=100_001,
            ))
            record = next(item for item in store.records() if item["user_id"] == 200)
            self.assertEqual(record["username"], "old")
            self.assertTrue(store.observe(
                {"id": 200, "username": "new", "first_name": "New"},
                now=186_400,
            ))
            record = next(item for item in store.records() if item["user_id"] == 200)
            self.assertEqual(record["username"], "new")

    def test_daily_limit_and_permanent_ban(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.observe({"id": 200, "first_name": "Test"})
            store.add(200)
            store.set_quota(200, 2)
            self.assertEqual(store.consume(200, now=100_000), (True, 1, 2))
            self.assertEqual(store.consume(200, now=100_001), (True, 2, 2))
            self.assertEqual(store.consume(200, now=100_002), (False, 2, 2))
            store.ban(200)
            self.assertTrue(store.is_banned(200))
            self.assertFalse(store.is_allowed(200))
            self.assertEqual(store.request_access(200, now=200_000), "banned")
            store.unban(200)
            self.assertFalse(store.is_banned(200))

    def test_daily_usage_resets_at_configured_midnight(self):
        midnight = datetime(1970, 1, 3, tzinfo=bot.BOT_TIMEZONE).timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 2)
            self.assertEqual(store.consume(200, now=midnight - 1), (True, 1, 2))
            self.assertEqual(store.consume(200, now=midnight), (True, 1, 2))
            self.assertEqual(store.usage_summary(bot.bot_date(midnight)), (1, 1))

    def test_configured_reset_hour_and_report_only_once_per_calendar_day(self):
        before = datetime(1970, 1, 3, 5, 59, tzinfo=bot.BOT_TIMEZONE).timestamp()
        reset = datetime(1970, 1, 3, 6, tzinfo=bot.BOT_TIMEZONE).timestamp()
        with patch.object(bot, "DAILY_RESET_HOUR", 6), patch.object(bot, "DAILY_REPORT_HOUR", 1), tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 2)
            self.assertEqual(store.consume(200, now=before), (True, 1, 2))
            self.assertEqual(store.consume(200, now=reset), (True, 1, 2))
            self.assertTrue(store.daily_report_due(now=before))
            store.mark_daily_report(now=before)
            self.assertFalse(store.daily_report_due(now=reset))
            self.assertEqual(bot.bot_date(before), "1970-01-02")
            self.assertEqual(bot.bot_date(reset), "1970-01-03")

    def test_daily_report_is_sent_without_pending_requests(self):
        report_time = datetime(1970, 1, 3, bot.DAILY_REPORT_HOUR, tzinfo=bot.BOT_TIMEZONE).timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.toggle_auto_approve(100)
            store.add(200)
            store.consume(200, now=report_time)
            api = MagicMock()
            service = bot.Bot(api, store)
            with patch.object(bot.time, "time", return_value=report_time):
                service.maybe_send_daily_report()
            text = api.send_message.call_args.args[1]
            self.assertIn(f"每日使用簡報（{bot.bot_date(report_time)}，{bot.BOT_TIMEZONE_NAME}）", text)
            self.assertIn("活躍使用者：1", text)
            self.assertIn("處理次數：1", text)
            self.assertIn("待審批：0", text)
            self.assertIsNone(api.send_message.call_args.kwargs["reply_markup"])
            service.stop()

    def test_owner_usage_is_counted_without_a_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            self.assertEqual(store.consume(100), (True, 1, 0))
            self.assertEqual(store.consume(100), (True, 2, 0))
            record = next(item for item in store.records() if item["user_id"] == 100)
            self.assertEqual(record["usage_count"], 2)

    def test_debug_mode_is_persistent_and_defaults_off(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            self.assertFalse(store.debug_mode(100))
            self.assertTrue(store.toggle_debug_mode(100))
            self.assertTrue(bot.ACLStore(path, 100).debug_mode(100))
            self.assertFalse(store.toggle_debug_mode(100))

    def test_administrator_cannot_enable_implementation_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            store.set_quota(200, None)
            self.assertTrue(store.is_admin(200))
            self.assertTrue(store.is_allowed(200))
            with self.assertRaises(ValueError):
                store.toggle_debug_mode(200)
            store.data["users"]["200"]["debug_mode"] = True
            self.assertFalse(store.debug_mode(200))
            self.assertFalse(store.debug_mode(100))

    def test_management_mode_is_per_admin_persistent_and_defaults_on(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            store.set_quota(200, None)
            self.assertTrue(store.management_mode(100))
            self.assertTrue(store.management_mode(200))
            self.assertFalse(store.toggle_management_mode(200))
            self.assertFalse(bot.ACLStore(path, 100).management_mode(200))
            self.assertTrue(store.management_mode(100))
            with self.assertRaises(ValueError):
                store.toggle_management_mode(300)

    def test_external_access_switch_is_persistent_and_defaults_on(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            self.assertTrue(store.external_access_enabled)
            self.assertFalse(store.toggle_external_access(100))
            self.assertFalse(bot.ACLStore(path, 100).external_access_enabled)
            self.assertTrue(store.toggle_external_access(100))

    def test_ordinary_user_cookie_switch_is_persistent_and_defaults_on(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            self.assertTrue(store.ordinary_user_cookies_enabled)
            self.assertFalse(store.toggle_ordinary_user_cookies(100))
            self.assertFalse(
                bot.ACLStore(path, 100).ordinary_user_cookies_enabled
            )
            self.assertTrue(store.toggle_ordinary_user_cookies(100))

    def test_global_switches_reject_non_owner_callers(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            for toggle in (
                store.toggle_external_access,
                store.toggle_ordinary_user_cookies,
                store.toggle_auto_approve,
            ):
                with self.assertRaises(ValueError):
                    toggle(200)

    def test_records_sort_ordinary_users_by_today_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 50)
            store.set_quota(201, 200)
            store.set_quota(202, None)
            store.set_quota(203, 100)
            store.set_quota(204, 0)
            store.set_quota(205, -1)
            store.consume(200)
            store.consume(200)
            store.consume(203)
            store.consume(201)

            self.assertEqual(
                [record["user_id"] for record in store.records()],
                [100, 202, 200, 201, 203, 204, 205],
            )
            with patch.object(bot, "bot_date", return_value="tomorrow"):
                self.assertEqual(
                    [record["user_id"] for record in store.records()],
                    [100, 202, 201, 203, 200, 204, 205],
                )

    def test_language_is_persistent_and_defaults_to_traditional_chinese(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            self.assertEqual(store.language(200), "zh")
            store.set_language(200, "ja")
            self.assertEqual(bot.ACLStore(path, 100).language(200), "ja")
            with self.assertRaises(ValueError):
                store.set_language(200, "unsupported")

    def test_application_callback_queues_without_immediate_owner_notice(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_callback({
                "id": "callback-1",
                "data": "apply",
                "from": {"id": 200, "first_name": "Applicant"},
                "message": {"chat": {"id": 200}},
            })
            self.assertEqual(len(store.pending()), 1)
            api.answer_callback.assert_called_once()
            api.send_message.assert_not_called()

    def test_application_callback_auto_approves_with_selected_language(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_language(200, "ja")
            store.toggle_auto_approve(100)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_callback({
                "id": "callback-auto",
                "data": "apply",
                "from": {"id": 200, "first_name": "Applicant"},
                "message": {"message_id": 10, "chat": {"id": 200}},
            })
            self.assertEqual(store.quota(200), bot.DEFAULT_DAILY_LIMIT)
            api.answer_callback.assert_called_once_with(
                "callback-auto",
                bot.public_text("ja", "apply_auto_approved"),
                alert=True,
            )
            api.edit_message.assert_called_once_with(
                200,
                10,
                bot.public_text("ja", "start_allowed"),
                bot.start_keyboard("ja", False, True),
            )
            api.send_message.assert_not_called()


class CookieTests(unittest.TestCase):
    def test_accepts_netscape_x_cookie(self):
        content = (
            "# Netscape HTTP Cookie File\n"
            ".x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\tvalue\n"
        ).encode()
        parsed = bot.validate_cookie_file(content)
        self.assertIn("auth_token", parsed)

    def test_accepts_httponly_cookie(self):
        content = (
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.twitter.com\tTRUE\t/\tTRUE\t2147483647\tct0\tvalue\n"
        ).encode()
        self.assertIn("twitter.com", bot.validate_cookie_file(content))

    def test_rejects_unrelated_cookie_file(self):
        content = (
            "# Netscape HTTP Cookie File\n"
            ".example.com\tTRUE\t/\tTRUE\t2147483647\tsession\tvalue\n"
        ).encode()
        with self.assertRaises(ValueError):
            bot.validate_cookie_file(content)

    def test_detects_expired_cookie_output(self):
        self.assertTrue(bot.cookies_look_invalid("ERROR: 401 Unauthorized"))
        self.assertTrue(bot.cookies_look_invalid("Please sign in to confirm"))
        self.assertFalse(bot.cookies_look_invalid("HTTP Error 404: Not Found"))

    def test_cookie_alert_is_limited_to_24_hours(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cookie-alert.json"
            self.assertTrue(bot.cookie_alert_due(path, now=100_000))
            bot.record_cookie_alert(path, now=100_000)
            self.assertFalse(bot.cookie_alert_due(path, now=186_399))
            self.assertTrue(bot.cookie_alert_due(path, now=186_400))


class MenuTests(unittest.TestCase):
    def test_start_keyboard_localizes_access_and_language_selection(self):
        keyboard = bot.start_keyboard("ja", False, False)["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["text"], "利用を申請")
        self.assertEqual(
            [button["callback_data"] for button in keyboard[1]],
            ["public:language", "public:help"],
        )
        self.assertEqual(keyboard[1][0]["text"], "🌐 Language")
        self.assertEqual(
            bot.public_text("zh", "start_allowed"),
            "請傳送有效的 X/Twitter 單篇貼文網址。",
        )
        self.assertIn("使い方", bot.public_text("ja", "help_allowed"))
        self.assertIn("/id", bot.public_text("ja", "help_allowed"))
        self.assertIn("不會切換到被引用內容", bot.public_text("zh", "help_allowed"))
        self.assertIn("quoted content is not followed", bot.public_text("en", "help_allowed"))
        self.assertIn("引用先ではなく", bot.public_text("ja", "help_allowed"))
        expected_menus = {
            "zh": ["🌐 Language", "ℹ️ 使用說明"],
            "en": ["🌐 Language", "ℹ️ How to use"],
            "ja": ["🌐 Language", "ℹ️ 使い方"],
        }
        for language in ("zh", "en", "ja"):
            menu = bot.start_keyboard(language, False, True)["inline_keyboard"][0]
            self.assertEqual(
                [button["text"] for button in menu], expected_menus[language]
            )
            self.assertTrue(bot.public_help_text(language).startswith(bot.public_text(language, "help_allowed")))
            public_copy = " ".join((
                bot.public_text(language, "start_allowed"),
                bot.public_text(language, "help_allowed"),
                bot.public_text(language, "approved", limit=50),
                bot.public_text(language, "quota", used=50, limit=50),
            ))
            self.assertNotIn("50 次", public_copy)
            self.assertNotIn("daily limit", public_copy.lower())
            self.assertNotIn("1日上限", public_copy)
            self.assertNotIn("00:00", public_copy)

    def test_regular_user_document_feedback_is_localized(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 50)
            api = MagicMock()
            service = bot.Bot(api, store)
            for language in ("zh", "en", "ja"):
                store.set_language(200, language)
                api.reset_mock()
                service.handle_update({"message": {
                    "message_id": 10,
                    "from": {"id": 200, "first_name": "User"},
                    "chat": {"id": 200},
                    "document": {"file_id": "not-a-cookie"},
                }})
                self.assertEqual(
                    api.send_message.call_args.args[1],
                    bot.public_text(language, "url_only"),
                )

    def test_authorized_regular_user_hidden_command_shows_localized_guide(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 50)
            store.set_language(200, "ja")
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_update({"message": {
                "message_id": 10,
                "from": {"id": 200, "first_name": "User"},
                "chat": {"id": 200},
                "text": "/help",
            }})
            self.assertEqual(
                api.send_message.call_args.args[1],
                bot.public_help_text("ja"),
            )
            self.assertEqual(api.send_message.call_args.kwargs["parse_mode"], "HTML")

    def test_public_help_and_language_navigation_are_shared_by_all_users(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            api = MagicMock()
            service = bot.Bot(api, store)
            callback = {
                "id": "public-help",
                "data": "public:help",
                "from": {"id": 200, "first_name": "Applicant"},
                "message": {"message_id": 10, "chat": {"id": 200}},
            }

            service.handle_callback(callback)
            help_call = api.edit_message.call_args
            self.assertEqual(help_call.args[2], bot.public_help_text("zh"))
            self.assertEqual(help_call.kwargs["parse_mode"], "HTML")

            api.reset_mock()
            service.handle_callback({
                **callback,
                "id": "public-language",
                "data": "public:language",
            })
            language_keyboard = api.edit_message.call_args.args[3]["inline_keyboard"]
            self.assertEqual(
                [button["callback_data"] for button in language_keyboard[0]],
                ["lang:zh", "lang:en", "lang:ja"],
            )
            self.assertEqual(
                language_keyboard[-1][0]["callback_data"], "public:main"
            )

    def test_administrator_start_keyboard_has_no_language_buttons(self):
        keyboard = bot.start_keyboard("zh", True, True)["inline_keyboard"]
        callbacks = {
            button["callback_data"]
            for row in keyboard
            for button in row
        }
        self.assertFalse(any(value.startswith("lang:") for value in callbacks))
        self.assertEqual(keyboard, bot.owner_keyboard()["inline_keyboard"])

    def test_disabled_management_mode_shows_public_debug_view_and_restore(self):
        keyboard = bot.start_keyboard(
            "en", True, True, False, management_mode=False
        )["inline_keyboard"]
        callbacks = [
            button["callback_data"] for row in keyboard for button in row
        ]
        self.assertIn("managementtoggle:0", callbacks)
        self.assertNotIn("public:language", callbacks)
        self.assertIn("public:help", callbacks)
        self.assertNotIn("nav:users", callbacks)

    def test_language_callback_persists_and_edits_start_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_callback({
                "id": "callback-language",
                "data": "lang:en",
                "from": {"id": 200, "first_name": "Applicant"},
                "message": {"message_id": 77, "chat": {"id": 200}},
            })

            self.assertEqual(store.language(200), "en")
            edited = api.edit_message.call_args.args
            self.assertEqual(edited[:2], (200, 77))
            self.assertIn("does not have access", edited[2])
            api.answer_callback.assert_called_once_with(
                "callback-language", "Language changed to English."
            )

    def test_owner_menu_uses_two_levels(self):
        main = bot.owner_keyboard()["inline_keyboard"]
        users = bot.user_menu_keyboard()["inline_keyboard"]
        cookies = bot.cookie_menu_keyboard()["inline_keyboard"]

        self.assertEqual(
            [[button["callback_data"] for button in row] for row in main],
            [
                ["nav:users", "managementtoggle:0"],
                ["nav:status", "nav:help"],
            ],
        )
        self.assertIn("nav:userlist", str(users))
        self.assertIn("nav:requests", str(users))
        self.assertIn("nav:finduser", str(users))
        self.assertIn("用戶權限修改", str(users))
        self.assertIn("nav:cookieupload", str(cookies))
        self.assertIn("nav:cookiehelp", str(cookies))
        self.assertIn("ordinarycookiestoggle:0", str(cookies))
        self.assertEqual(users[-1][0]["callback_data"], "nav:main")
        self.assertEqual(cookies[-1][0]["callback_data"], "nav:advanced")

    def test_user_and_request_lists_paginate_by_twenty(self):
        records = [
            {"user_id": 1000 + index, "first_name": f"User {index}"}
            for index in range(45)
        ]
        pending = bot.pending_keyboard(records, 1)["inline_keyboard"]
        action_rows = pending[:-3]
        batch_action = pending[-3]
        navigation = pending[-2]

        self.assertEqual(len(action_rows), 20)
        self.assertTrue(all("ban:" not in str(row) for row in action_rows))
        self.assertEqual(batch_action[0]["callback_data"], "approvepage:1")
        self.assertEqual(
            [button["callback_data"] for button in navigation],
            ["requestspage:0", "noop:0", "requestspage:2"],
        )
        user_navigation = bot.users_page_keyboard(1, 45)["inline_keyboard"][0]
        self.assertEqual(
            [button["callback_data"] for button in user_navigation],
            ["userspage:0", "noop:0", "userspage:2"],
        )

    def test_user_list_uses_linked_id_and_places_plain_name_last(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.observe({
                "id": 200,
                "first_name": "Example",
                "username": "example_user",
            })
            store.set_quota(200, 50)
            service = bot.Bot(MagicMock(), store)
            text, keyboard, _ = service.users_page(store.records(), 0)
            user_line = next(line for line in text.splitlines() if ">200</a>" in line)
            self.assertEqual(
                user_line,
                '<a href="https://t.me/example_user">200</a>｜普通｜50｜0｜'
                '<code>Example</code>',
            )
            self.assertNotIn("@example_user", user_line)
            self.assertEqual(len(keyboard["inline_keyboard"]), 2)

    def test_user_list_copies_id_when_username_is_missing(self):
        records = [{
            "user_id": 9876543210123456,
            "first_name": "No Username",
            "quota": 50,
        }]
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            service = bot.Bot(MagicMock(), store)
            text, _, _ = service.users_page(records, 0)
        self.assertIn(
            "<code>9876543210123456</code>｜普通｜50｜0｜"
            "<code>No Username</code>",
            text,
        )

    def test_user_list_uses_profile_refreshed_on_next_day(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.observe({
                "id": 200,
                "first_name": "Old Name",
                "username": "old_user",
            }, now=100_000)
            store.set_quota(200, 50)
            store.observe({
                "id": 200,
                "first_name": "New Name",
                "username": "new_user",
            }, now=186_400)
            service = bot.Bot(MagicMock(), store)
            text, _, _ = service.users_page(store.records(), 0)
            self.assertIn(
                '<a href="https://t.me/new_user">200</a>｜普通｜50｜0｜'
                '<code>New Name</code>',
                text,
            )
            self.assertNotIn("@new_user", text)

    def test_user_label_without_name_does_not_repeat_user_id(self):
        record = {"user_id": 987654321, "quota": 0}
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            service = bot.Bot(MagicMock(), store)
            text, _, _ = service.users_page([record], 0)
        user_line = text.splitlines()[-1]
        self.assertIn("（未提供名稱）", user_line)
        self.assertEqual(user_line.count("987654321"), 1)

    def test_user_list_escapes_names_and_rejects_invalid_username_links(self):
        record = {
            "user_id": 200,
            "first_name": "<b>Not markup</b>",
            "username": "invalid/name",
            "quota": 50,
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            service = bot.Bot(MagicMock(), store)
            text, _, _ = service.users_page([record], 0)
        self.assertIn("<code>200</code>", text)
        self.assertIn("&lt;b&gt;Not markup", text)
        self.assertNotIn("https://t.me/", text)

    def test_user_list_normalizes_and_truncates_long_names_to_one_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.observe({
                "id": 200,
                "first_name": "♣ 闇猫\n・ヴィクトリカ・ド・ブロワ ♣",
                "username": "an_extremely_long_username_for_testing",
            })
            store.set_quota(200, 50)
            service = bot.Bot(MagicMock(), store)
            text, _, _ = service.users_page(store.records(), 0)
            user_line = next(
                line for line in text.splitlines() if "<code>200</code>" in line
            )
            name_field = user_line.rsplit("｜", 1)[1]
            self.assertTrue(name_field.startswith("<code>"))
            self.assertTrue(name_field.endswith("</code>"))
            name = name_field.removeprefix("<code>").removesuffix("</code>")
            self.assertIn("…", name)
            self.assertNotIn("\n", name)
            self.assertLessEqual(bot.display_width(name), bot.USER_LIST_NAME_WIDTH)

    def test_batch_approval_only_approves_the_selected_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            for user_id in range(200, 225):
                store.observe({"id": user_id, "first_name": f"User {user_id}"})
                store.request_access(user_id, now=100_000 + user_id)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_callback({
                "id": "callback-batch-approve",
                "data": "approvepage:0",
                "from": {"id": 100, "first_name": "Owner"},
                "message": {"message_id": 77, "chat": {"id": 100}},
            })

            self.assertEqual(len(store.pending()), 5)
            self.assertTrue(all(store.is_allowed(user_id) for user_id in range(200, 220)))
            self.assertFalse(any(store.is_allowed(user_id) for user_id in range(220, 225)))
            api.answer_callback.assert_called_with(
                "callback-batch-approve", "已通過 20 筆申請。"
            )

    def test_denied_application_does_not_notify_the_applicant(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.observe({"id": 200, "first_name": "Applicant"})
            store.request_access(200)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_callback({
                "id": "callback-deny",
                "data": "deny:200:0",
                "from": {"id": 100, "first_name": "Owner"},
                "message": {"message_id": 77, "chat": {"id": 100}},
            })

            self.assertFalse(store.pending())
            self.assertFalse(any(
                call.args and call.args[0] == 200
                for call in api.send_message.call_args_list
            ))

    def test_inline_navigation_edits_existing_message_without_sending_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_callback({
                "id": "callback-menu",
                "data": "nav:users",
                "from": {"id": 100, "first_name": "Owner"},
                "message": {"message_id": 77, "chat": {"id": 100}},
            })

            api.edit_message.assert_called_once_with(
                100, 77, "使用者管理", bot.user_menu_keyboard()
            )
            api.send_message.assert_not_called()

            api.reset_mock()
            service.handle_callback({
                "id": "callback-advanced",
                "data": "nav:advanced",
                "from": {"id": 100, "first_name": "Owner"},
                "message": {"message_id": 77, "chat": {"id": 100}},
            })
            advanced_keyboard = str(api.edit_message.call_args.args[3])
            self.assertIn("nav:cookies", advanced_keyboard)
            self.assertNotIn("ordinarycookiestoggle:0", advanced_keyboard)
            api.send_message.assert_not_called()

    def test_quota_and_permission_are_one_search_result_action(self):
        allowed = bot.searched_user_keyboard(
            {"user_id": 200, "allowed": True}, 100, allow_admin=True
        )["inline_keyboard"]
        banned = bot.searched_user_keyboard(
            {"user_id": 200, "banned": True}, 100
        )["inline_keyboard"]

        self.assertIn("quota:200:50", str(allowed))
        self.assertIn("quota:200:unlimited", str(allowed))
        self.assertIn("quota:200:50", str(banned))
        self.assertNotIn("quotamenu:200", str(allowed))
        self.assertNotIn("ban:200", str(allowed))
        self.assertEqual(allowed[-1][0]["callback_data"], "nav:users")

    def test_unified_quota_menu_only_contains_role_presets(self):
        keyboard = bot.quota_choices_keyboard(200)["inline_keyboard"]
        serialized = str(keyboard)
        self.assertEqual(
            [button["callback_data"] for button in keyboard[0]],
            ["quota:200:50", "quota:200:blocked", "quota:200:0"],
        )
        self.assertEqual(
            [button["callback_data"] for button in keyboard[1]],
            ["quota:200:unlimited"],
        )
        for callback in (
            "quota:200:blocked",
            "quota:200:0",
            "quota:200:50",
            "quota:200:unlimited",
        ):
            self.assertIn(callback, serialized)
        self.assertNotIn("quota:200:100", serialized)
        self.assertNotIn("quota:200:200", serialized)

    def test_system_status_summarizes_large_user_metrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.observe({"id": 200, "first_name": "Allowed"})
            store.add(200)
            store.set_quota(200, 1)
            store.consume(200)
            store.observe({"id": 300, "first_name": "Pending"})
            store.request_access(300)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.worker.is_alive = MagicMock(return_value=True)

            status = service.system_status_text(100)

            self.assertIn("服務：正常", status)
            self.assertIn("普通：1", status)
            self.assertIn("待審批：1", status)
            self.assertIn("今日互動：1", status)
            self.assertIn("已達額度：1", status)
            self.assertEqual(
                bot.status_keyboard()["inline_keyboard"][0][0]["callback_data"],
                "statusrefresh:0",
            )
            self.assertEqual(
                bot.status_keyboard()["inline_keyboard"][1][0]["text"],
                "⚙️ 高級選項",
            )
            self.assertNotIn("nav:advanced", str(bot.status_keyboard(False)))
            advanced = bot.advanced_status_keyboard(False)["inline_keyboard"]
            self.assertEqual(
                advanced[0][0]["text"],
                "🍪 Cookies 管理",
            )
            self.assertEqual(
                advanced[1][0]["text"],
                "🌐 使用開關：開放",
            )
            self.assertEqual(
                advanced[2][0]["text"],
                "✅ 自動通過：關閉",
            )
            self.assertEqual(
                advanced[3][0]["text"],
                "🐞 實現方式：關閉",
            )
            cookies = bot.cookie_menu_keyboard(True)["inline_keyboard"]
            self.assertEqual(
                cookies[1][0]["text"], "Cookies 使用：開啟"
            )
            self.assertIn("Cookies 開關：開啟", status)
            self.assertIn("自動通過：關閉", status)
            self.assertIn("管理模式：開啟", status)

            admin_advanced = bot.advanced_status_keyboard(
                False, can_configure=False
            )["inline_keyboard"]
            self.assertEqual(len(admin_advanced), 1)
            self.assertEqual(
                admin_advanced[0][0]["callback_data"], "nav:status"
            )

            service.handle_callback({
                "id": "owner-auto-approve-toggle",
                "data": "autoapprovetoggle:0",
                "from": {"id": 100, "first_name": "Owner"},
                "message": {"message_id": 11, "chat": {"id": 100}},
            })
            self.assertTrue(store.auto_approve_enabled)
            self.assertIn(
                "✅ 自動通過：開啟",
                str(api.edit_message.call_args.args[3]),
            )
            api.answer_callback.assert_called_with(
                "owner-auto-approve-toggle", "自動通過已開啟。"
            )

            service.handle_callback({
                "id": "owner-cookie-toggle",
                "data": "ordinarycookiestoggle:0",
                "from": {"id": 100, "first_name": "Owner"},
                "message": {"message_id": 12, "chat": {"id": 100}},
            })
            self.assertFalse(store.ordinary_user_cookies_enabled)
            self.assertIn(
                "Cookies 使用：關閉",
                str(api.edit_message.call_args.args[3]),
            )
            api.answer_callback.assert_called_with(
                "owner-cookie-toggle", "Cookies 開關已關閉。"
            )
            self.assertEqual(
                api.edit_message.call_args.args[2], "Cookies 管理"
            )

    def test_administrator_cannot_open_advanced_settings_or_toggle_implementation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            api = MagicMock()
            service = bot.Bot(api, store)
            for language, implementation in (("zh", "實現方式"), ("ja", "実装方法"), ("en", "Implementation details")):
                with patch.object(bot, "OWNER_LANGUAGE", language):
                    for data in ("nav:advanced", "debugtoggle:0"):
                        with self.subTest(language=language, data=data):
                            api.reset_mock()
                            service.handle_callback({
                                "id": data, "data": data,
                                "from": {"id": 200},
                                "message": {"message_id": 1, "chat": {"id": 200}},
                            })
                            api.answer_callback.assert_called_once_with(
                                data, bot.admin_text("advanced_owner_only"), alert=True
                            )
                            api.edit_message.assert_not_called()
                    self.assertNotIn(implementation, service.system_status_text(200))
            self.assertFalse(store.debug_mode(200))
            service.handle_owner_command(200, 2, 200, "/status", "")
            self.assertNotIn("nav:advanced", str(api.send_message.call_args.args[3]))
            service.stop()

    def test_external_access_pause_blocks_users_but_not_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.add(200)
            self.assertFalse(store.toggle_external_access(100))
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 200, "first_name": "User"},
                    "chat": {"id": 200},
                    "text": "https://x.com/example/status/123",
                }
            })
            api.send_message.assert_called_once_with(
                200,
                bot.public_text("zh", "service_paused"),
                10,
            )
            self.assertTrue(service.jobs.empty())

            api.reset_mock()
            service.handle_update({
                "message": {
                    "message_id": 11,
                    "from": {"id": 100, "first_name": "Owner"},
                    "chat": {"id": 100},
                    "text": "https://x.com/example/status/124",
                }
            })
            api.send_message.assert_not_called()
            self.assertEqual(service.jobs.qsize(), 1)

    def test_disabled_management_mode_uses_regular_user_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            store.toggle_management_mode(200)
            store.toggle_external_access(100)
            api = MagicMock()
            service = bot.Bot(api, store)

            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 200, "first_name": "Admin"},
                    "chat": {"id": 200},
                    "text": "https://x.com/example/status/123",
                }
            })

            api.send_message.assert_called_once_with(
                200, bot.public_text("zh", "service_paused"), 10
            )
            self.assertTrue(service.jobs.empty())

    def test_management_mode_toggle_hides_and_restores_admin_interface(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            api = MagicMock()
            service = bot.Bot(api, store)
            callback = {
                "id": "management-off",
                "data": "managementtoggle:0",
                "from": {"id": 200, "first_name": "Admin"},
                "message": {"message_id": 12, "chat": {"id": 200}},
            }

            service.handle_callback(callback)
            self.assertFalse(store.management_mode(200))
            self.assertEqual(
                api.edit_message.call_args.args[2],
                bot.public_text("zh", "start_allowed"),
            )
            self.assertIn(
                "managementtoggle:0", str(api.edit_message.call_args.args[3])
            )

            api.reset_mock()
            service.handle_callback({
                **callback,
                "id": "old-admin-button",
                "data": "nav:users",
            })
            api.answer_callback.assert_called_once_with(
                "old-admin-button", "管理模式已關閉，請先重新開啟。", alert=True
            )

            api.reset_mock()
            service.handle_callback({
                **callback,
                "id": "management-on",
            })
            self.assertTrue(store.management_mode(200))
            self.assertEqual(api.edit_message.call_args.args[2], "管理選單")

    def test_external_pause_is_only_reported_for_valid_user_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.add(200)
            store.toggle_external_access(100)
            api = MagicMock()
            service = bot.Bot(api, store)

            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 200, "first_name": "User"},
                    "chat": {"id": 200},
                    "text": "/start",
                }
            })
            self.assertEqual(
                api.send_message.call_args.args[1],
                bot.public_text("zh", "start_allowed"),
            )

            api.reset_mock()
            service.handle_update({
                "message": {
                    "message_id": 11,
                    "from": {"id": 200, "first_name": "User"},
                    "chat": {"id": 200},
                    "text": "hello",
                }
            })
            self.assertEqual(
                api.send_message.call_args.args[1],
                bot.public_text("zh", "invalid_url"),
            )

            api.reset_mock()
            service.handle_callback({
                "id": "language-paused",
                "data": "lang:en",
                "from": {"id": 200, "first_name": "User"},
                "message": {"message_id": 12, "chat": {"id": 200}},
            })
            self.assertEqual(
                api.edit_message.call_args.args[2],
                bot.public_text("en", "start_allowed"),
            )

    def test_owner_numeric_lookup_requires_confirmation_for_unknown_user(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 100, "first_name": "Owner"},
                    "chat": {"id": 100},
                    "text": "987654321",
                }
            })

            self.assertFalse(store.has_user(987654321))
            self.assertIn("確認", api.send_message.call_args.args[1])
            self.assertIn("createuser:987654321:50", str(api.send_message.call_args.args[3]))

            service.handle_callback({
                "id": "confirm-create",
                "data": "createuser:987654321:50",
                "from": {"id": 100, "first_name": "Owner"},
                "message": {"message_id": 11, "chat": {"id": 100}},
            })

            self.assertEqual(store.quota(987654321), 50)
            self.assertFalse(store.is_admin(987654321))
            self.assertIn("普通使用者", api.edit_message.call_args.args[2])

    def test_owner_can_cancel_unknown_user_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 100, "first_name": "Owner"},
                    "chat": {"id": 100},
                    "text": "987654321",
                }
            })
            service.handle_callback({
                "id": "cancel-create",
                "data": "cancelcreate:987654321",
                "from": {"id": 100, "first_name": "Owner"},
                "message": {"message_id": 11, "chat": {"id": 100}},
            })

            self.assertFalse(store.has_user(987654321))
            self.assertIn("已取消", api.edit_message.call_args.args[2])

    def test_owner_search_requires_confirmation_for_unknown_low_user_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.pending_user_searches.add(100)
            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 100, "first_name": "Owner"},
                    "chat": {"id": 100},
                    "text": "12345",
                }
            })

            self.assertFalse(store.has_user(12345))
            self.assertIn("createuser:12345:50", str(api.send_message.call_args.args[3]))

    def test_telegram_user_id_shortcut_range(self):
        self.assertFalse(bot.is_telegram_user_id_shortcut(99_999))
        self.assertTrue(bot.is_telegram_user_id_shortcut(100_000))
        self.assertTrue(bot.is_telegram_user_id_shortcut((1 << 52) - 1))
        self.assertFalse(bot.is_telegram_user_id_shortcut(1 << 52))

    def test_owner_small_number_is_not_treated_as_user_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 100, "first_name": "Owner"},
                    "chat": {"id": 100},
                    "text": "50",
                }
            })

            self.assertNotIn("50", store.data["users"])
            self.assertIn("有效的 X/Twitter", api.send_message.call_args.args[1])

    def test_owner_numeric_quota_change_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 100, "first_name": "Owner"},
                    "chat": {"id": 100},
                    "text": "987654321 100",
                }
            })
            self.assertFalse(store.has_user(987654321))
            service.handle_callback({
                "id": "confirm-quota-create",
                "data": "createuser:987654321:100",
                "from": {"id": 100, "first_name": "Owner"},
                "message": {"message_id": 11, "chat": {"id": 100}},
            })
            self.assertEqual(store.quota(987654321), 100)

            api.reset_mock()
            service.handle_update({
                "message": {
                    "message_id": 12,
                    "from": {"id": 100, "first_name": "Owner"},
                    "chat": {"id": 100},
                    "text": "987654321 200",
                }
            })
            self.assertEqual(store.quota(987654321), 100)
            self.assertIn(
                "confirmquota:987654321:200",
                str(api.send_message.call_args.args[3]),
            )
            service.handle_callback({
                "id": "confirm-quota-change",
                "data": "confirmquota:987654321:200",
                "from": {"id": 100, "first_name": "Owner"},
                "message": {"message_id": 12, "chat": {"id": 100}},
            })
            self.assertEqual(store.quota(987654321), 200)

            service.handle_update({
                "message": {
                    "message_id": 13,
                    "from": {"id": 100, "first_name": "Owner"},
                    "chat": {"id": 100},
                    "text": "987654321 50",
                }
            })
            service.handle_callback({
                "id": "cancel-quota-change",
                "data": "cancelquota:987654321",
                "from": {"id": 100, "first_name": "Owner"},
                "message": {"message_id": 13, "chat": {"id": 100}},
            })
            self.assertEqual(store.quota(987654321), 200)

            store.set_quota(200, None)
            api.reset_mock()
            service.handle_update({
                "message": {
                    "message_id": 11,
                    "from": {"id": 200, "first_name": "Admin"},
                    "chat": {"id": 200},
                    "text": "987654321 50",
                }
            })
            self.assertEqual(store.quota(987654321), 200)
            self.assertIn(
                "confirmquota:987654321:50",
                str(api.send_message.call_args.args[3]),
            )
            service.handle_callback({
                "id": "admin-confirm-quota-change",
                "data": "confirmquota:987654321:50",
                "from": {"id": 200, "first_name": "Admin"},
                "message": {"message_id": 11, "chat": {"id": 200}},
            })
            self.assertEqual(store.quota(987654321), 50)

    def test_administrator_help_omits_owner_only_shortcuts(self):
        for language, titles, shortcut in (
            ("zh", ("所有者管理說明", "管理員使用說明"), "User ID 額度"),
            ("ja", ("所有者向けガイド", "管理者向けガイド"), "User ID 上限値"),
            ("en", ("Owner guide", "Administrator guide"), "User ID and quota"),
        ):
            with self.subTest(language=language), patch.object(bot, "OWNER_LANGUAGE", language):
                owner_help = bot.owner_help_text(True)
                admin_help = bot.owner_help_text(False)
                self.assertTrue(owner_help.startswith(titles[0]))
                self.assertTrue(admin_help.startswith(titles[1]))
                for text in (owner_help, admin_help):
                    self.assertIn(shortcut, text)
                    self.assertIn(bot.BOT_MENTION, text)
                    self.assertLess(len(text), 750)
                    for detail in ("FxTwitter", "gallery-dl", "yt-dlp", "50 MB", "52-bit", bot.BOT_TIMEZONE_NAME):
                        self.assertNotIn(detail, text)
                self.assertNotEqual(owner_help, admin_help)
                self.assertNotIn("auto-approval", admin_help)
                self.assertNotIn("自動通過", admin_help)
                self.assertNotIn("自動承認", admin_help)

    def test_administrator_numeric_shortcut_creates_only_non_privileged_users(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            store.set_quota(987654322, None)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 200, "first_name": "Admin"},
                    "chat": {"id": 200},
                    "text": "987654321 75",
                }
            })
            self.assertIn("createuser:987654321:75", str(api.send_message.call_args.args[3]))
            service.handle_callback({
                "id": "admin-create",
                "data": "createuser:987654321:75",
                "from": {"id": 200, "first_name": "Admin"},
                "message": {"message_id": 10, "chat": {"id": 200}},
            })
            self.assertEqual(store.quota(987654321), 75)

            api.reset_mock()
            service.handle_update({
                "message": {
                    "message_id": 11,
                    "from": {"id": 200, "first_name": "Admin"},
                    "chat": {"id": 200},
                    "text": "987654322 50",
                }
            })
            self.assertEqual(store.quota(987654322), None)
            self.assertIn("不能修改其他管理員", api.send_message.call_args.args[1])

    def test_blocked_user_is_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, -1)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 200, "first_name": "Blocked"},
                    "chat": {"id": 200},
                    "text": "/start",
                }
            })
            service.handle_callback({
                "id": "blocked-callback",
                "data": "apply",
                "from": {"id": 200, "first_name": "Blocked"},
                "message": {"message_id": 11, "chat": {"id": 200}},
            })

            api.send_message.assert_not_called()
            api.answer_callback.assert_not_called()

    def test_administrator_sees_full_menu_but_restricted_actions_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 200, "first_name": "Admin"},
                    "chat": {"id": 200},
                    "text": "/start",
                }
            })
            api.remove_reply_keyboard.assert_not_called()
            self.assertEqual(
                api.send_message.call_args.args[3],
                bot.start_keyboard("zh", True, True, False),
            )

            api.reset_mock()
            service.handle_callback({
                "id": "cookie-callback",
                "data": "nav:cookies",
                "from": {"id": 200, "first_name": "Admin"},
                "message": {"message_id": 11, "chat": {"id": 200}},
            })
            api.answer_callback.assert_called_once_with(
                "cookie-callback", "Cookies 只允許所有者管理。", alert=True
            )

            api.reset_mock()
            service.handle_callback({
                "id": "external-callback",
                "data": "externaltoggle:0",
                "from": {"id": 200, "first_name": "Admin"},
                "message": {"message_id": 12, "chat": {"id": 200}},
            })
            api.answer_callback.assert_called_once_with(
                "external-callback", "使用開關只允許所有者切換。", alert=True
            )

            api.reset_mock()
            service.handle_callback({
                "id": "auto-approve-callback",
                "data": "autoapprovetoggle:0",
                "from": {"id": 200, "first_name": "Admin"},
                "message": {"message_id": 13, "chat": {"id": 200}},
            })
            api.answer_callback.assert_called_once_with(
                "auto-approve-callback",
                "自動通過只允許所有者切換。",
                alert=True,
            )

            api.reset_mock()
            service.handle_callback({
                "id": "ordinary-cookies-callback",
                "data": "ordinarycookiestoggle:0",
                "from": {"id": 200, "first_name": "Admin"},
                "message": {"message_id": 14, "chat": {"id": 200}},
            })
            api.answer_callback.assert_called_once_with(
                "ordinary-cookies-callback",
                "Cookies 開關只允許所有者切換。",
                alert=True,
            )

    def test_administrator_cannot_modify_privileged_users_or_create_admins(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            store.set_quota(300, None)
            store.set_quota(400, 50)
            api = MagicMock()
            service = bot.Bot(api, store)

            def callback(data: str, callback_id: str) -> None:
                service.handle_callback({
                    "id": callback_id,
                    "data": data,
                    "from": {"id": 200, "first_name": "Admin"},
                    "message": {"message_id": 20, "chat": {"id": 200}},
                })

            for target, expected in (
                (100, "所有者"),
                (200, "自己"),
                (300, "其他管理員"),
            ):
                api.reset_mock()
                callback(f"quota:{target}:50", f"protected-{target}")
                self.assertIn(expected, api.answer_callback.call_args.args[1])
                self.assertTrue(api.answer_callback.call_args.kwargs["alert"])

            api.reset_mock()
            callback("quotamenu:400", "ordinary-menu")
            keyboard = api.edit_message.call_args.args[3]
            self.assertNotIn("unlimited", str(keyboard))

            api.reset_mock()
            callback("quota:400:unlimited", "promote")
            self.assertEqual(store.quota(400), 50)
            self.assertIn("只有所有者", api.answer_callback.call_args.args[1])

            api.reset_mock()
            callback("quota:400:100", "ordinary")
            self.assertEqual(store.quota(400), 100)

    def test_hidden_commands_obey_administrator_hierarchy(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            store.set_quota(300, None)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 200, "first_name": "Admin"},
                    "chat": {"id": 200, "type": "private"},
                    "text": "/limit 300 50",
                }
            })

            self.assertIsNone(store.quota(300))
            self.assertIn("其他管理員", api.send_message.call_args.args[1])

    def test_management_and_cookie_import_are_private_chat_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_update({
                "message": {
                    "message_id": 10,
                    "from": {"id": 200, "first_name": "Admin"},
                    "chat": {"id": -100123, "type": "supergroup"},
                    "text": "/users",
                }
            })
            self.assertIn("Bot 私聊", api.send_message.call_args.args[1])

            api.reset_mock()
            service.handle_update({
                "message": {
                    "message_id": 11,
                    "from": {"id": 100, "first_name": "Owner"},
                    "chat": {"id": -100123, "type": "supergroup"},
                    "document": {"file_id": "cookie-file", "file_size": 100},
                }
            })
            self.assertIn("Cookies 只允許在 Bot 私聊", api.send_message.call_args.args[1])
            api.download_file.assert_not_called()

    def test_malformed_management_callbacks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            store.set_quota(400, 50)
            api = MagicMock()
            service = bot.Bot(api, store)
            for callback_id, data in (
                ("bad-quota", "quota:400:not-a-number"),
                ("bad-page", "userspage:-1"),
                ("bad-id", f"quotamenu:{1 << 52}"),
            ):
                api.reset_mock()
                service.handle_callback({
                    "id": callback_id,
                    "data": data,
                    "from": {"id": 200, "first_name": "Admin"},
                    "message": {"message_id": 20, "chat": {"id": 200}},
                })
                self.assertTrue(api.answer_callback.call_args.kwargs["alert"])
            self.assertEqual(store.quota(400), 50)


class TelegramConfigurationTests(unittest.TestCase):
    def test_message_methods_forward_html_parse_mode(self):
        api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
        api.call = MagicMock()
        api.send_message(100, "<code>100</code>", parse_mode="HTML")
        self.assertEqual(api.call.call_args.args[1]["parse_mode"], "HTML")
        api.edit_message(100, 10, "<code>100</code>", parse_mode="HTML")
        self.assertEqual(api.call.call_args.args[1]["parse_mode"], "HTML")

    def test_access_guide_does_not_expose_auto_approve_in_any_language(self):
        for language in bot.PUBLIC_TEXT:
            guide = bot.access_request_text(200, language)
            self.assertIn(bot.public_text(language, "access"), guide)
            self.assertNotIn("Owner:", guide)
            self.assertNotIn("Contact the owner", guide)

    def test_public_languages_have_matching_keys_and_placeholders(self):
        expected_keys = set(bot.PUBLIC_TEXT["zh"])
        placeholder_pattern = bot.re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
        for language, messages in bot.PUBLIC_TEXT.items():
            self.assertEqual(set(messages), expected_keys, language)
            self.assertIn(bot.BOT_MENTION, messages["help_allowed"])
        for key in expected_keys:
            placeholders = {
                language: set(placeholder_pattern.findall(messages[key]))
                for language, messages in bot.PUBLIC_TEXT.items()
            }
            self.assertEqual(
                len({frozenset(values) for values in placeholders.values()}),
                1,
                f"{key}: {placeholders}",
            )

    def test_command_menu_exposes_start_and_id_to_all_private_users(self):
        api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
        api.call = MagicMock()
        api.configure_commands(100)

        command_call = api.call.call_args_list[0].args
        commands = json.loads(command_call[1]["commands"])
        self.assertEqual(
            [command["command"] for command in commands],
            ["start", "id"],
        )
        self.assertEqual(
            json.loads(command_call[1]["scope"]),
            {"type": "all_private_chats"},
        )
        command_calls = api.call.call_args_list[:3]
        self.assertEqual(
            [call.args[1].get("language_code", "") for call in command_calls],
            ["", "en", "ja"],
        )
        self.assertEqual(
            json.loads(command_calls[1].args[1]["commands"])[0]["description"],
            "Start and show the options",
        )
        self.assertEqual(
            json.loads(command_calls[2].args[1]["commands"])[0]["description"],
            "起動してメニューを表示",
        )

    def test_profile_descriptions_are_configured_for_all_languages(self):
        api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
        api.call = MagicMock()
        api.configure_profile()

        self.assertEqual(api.call.call_count, 8)
        language_codes = set()
        descriptions = []
        for call in api.call.call_args_list:
            method, data = call.args
            if "language_code" in data:
                language_codes.add(data["language_code"])
            if method == "setMyShortDescription":
                self.assertLessEqual(len(data["short_description"]), 120)
            elif method == "setMyDescription":
                self.assertLessEqual(len(data["description"]), 512)
                descriptions.append(data["description"])
            else:
                self.fail(f"Unexpected API method: {method}")
        self.assertEqual(language_codes, {"zh", "en", "ja"})
        self.assertEqual(len(descriptions), 4)
        self.assertTrue(all(bot.BOT_MENTION in value for value in descriptions))


class MediaTests(unittest.TestCase):
    def test_fxtwitter_prefers_largest_mp4(self):
        tweet = {
            "media": {
                "all": [{
                    "type": "video",
                    "url": "https://video.twimg.com/small.mp4",
                    "formats": [
                        {"container": "mp4", "url": "https://video.twimg.com/low.mp4", "width": 320, "height": 180},
                        {"container": "mp4", "url": "https://video.twimg.com/high.mp4", "width": 1280, "height": 720},
                        {"container": "m3u8", "url": "https://video.twimg.com/list.m3u8", "width": 1920, "height": 1080},
                    ],
                }]
            }
        }
        self.assertEqual(
            bot.fxtwitter_media(tweet)[0]["url"],
            "https://video.twimg.com/high.mp4",
        )

    def test_fxtwitter_direct_download_falls_back_to_smaller_video_variant(self):
        tweet = {
            "media": {"all": [{
                "type": "video",
                "formats": [
                    {
                        "container": "mp4",
                        "url": "https://video.twimg.com/high.mp4",
                        "width": 1280,
                        "height": 720,
                    },
                    {
                        "container": "mp4",
                        "url": "https://video.twimg.com/low.mp4",
                        "width": 640,
                        "height": 360,
                    },
                ],
            }]},
        }
        oversized = MagicMock()
        oversized.__enter__.return_value = oversized
        oversized.status_code = 200
        oversized.url = "https://video.twimg.com/high.mp4"
        oversized.headers = {"Content-Length": str(bot.MAX_VIDEO_BYTES + 1)}
        smaller = MagicMock()
        smaller.__enter__.return_value = smaller
        smaller.status_code = 200
        smaller.url = "https://video.twimg.com/low.mp4"
        smaller.headers = {"Content-Length": "5"}
        smaller.iter_content.return_value = [b"video"]
        session = MagicMock()
        session.get.side_effect = [oversized, smaller]

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "http_session", return_value=session
        ):
            files, _, oversized_count = bot.download_fxtwitter_media(
                tweet, Path(temporary)
            )
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b"video")
            self.assertEqual(oversized_count, 0)
            self.assertEqual(session.get.call_count, 2)

    def test_twimg_download_rejects_cross_domain_redirect_before_following_it(self):
        redirect = MagicMock(status_code=302)
        redirect.headers = {"Location": "https://example.com/private.mp4"}
        session = MagicMock()
        session.get.return_value = redirect

        with patch.object(bot, "http_session", return_value=session):
            with self.assertRaisesRegex(ValueError, "left trusted"):
                bot.trusted_twimg_response(
                    "https://video.twimg.com/video.mp4", timeout=(1, 1)
                )

        session.get.assert_called_once()
        redirect.close.assert_called_once()

    def test_process_url_prefers_fxtwitter_direct_media_before_extractors(self):
        tweet = {
            "id": "123",
            "text": "direct",
            "author": {"name": "Author", "screen_name": "author"},
            "media": {"all": [{
                "type": "photo",
                "url": "https://pbs.twimg.com/photo.jpg",
            }]},
        }
        with tempfile.TemporaryDirectory() as temporary:
            media = Path(temporary) / "photo.jpg"
            media.write_bytes(b"photo")
            with patch.object(bot, "TMP_DIR", Path(temporary)), patch.object(
                bot, "fetch_fxtwitter", return_value=tweet
            ), patch.object(
                bot, "download_fxtwitter_media", return_value=([media], "", 0)
            ) as direct, patch.object(
                bot, "download_media"
            ) as extractor, patch.object(
                bot, "prepare_image", side_effect=lambda path: path
            ):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                api = MagicMock()
                service = bot.Bot(api, store)
                service.process_url(100, 10, 100, "https://x.com/author/status/123")

            direct.assert_called_once()
            extractor.assert_not_called()
            api.send_previews.assert_called_once()
            api.send_documents.assert_called_once_with(100, [media])

    def test_prepare_image_skips_reencoding_when_already_telegram_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            image_path = Path(temporary) / "safe.jpg"
            bot.Image.new("RGB", (100, 100), "white").save(image_path, "JPEG")
            with patch.object(bot.ImageOps, "exif_transpose") as transpose:
                prepared = bot.prepare_image(image_path)
            self.assertEqual(prepared, image_path)
            transpose.assert_not_called()

    def test_prepare_image_pads_extreme_ratio_for_telegram_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            image_path = Path(temporary) / "panorama.jpg"
            bot.Image.new("RGB", (2000, 50), "black").save(image_path, "JPEG")

            prepared = bot.prepare_image(image_path)

            self.assertNotEqual(prepared, image_path)
            with bot.Image.open(prepared) as image:
                self.assertTrue(
                    bot.telegram_photo_dimensions_valid(image.width, image.height)
                )

    def test_primary_tweet_media_does_not_switch_to_quoted_tweet(self):
        deepest = {
            "id": "333",
            "text": "final text",
            "author": {"screen_name": "final_user"},
            "media": {"all": [{
                "type": "photo",
                "url": "https://pbs.twimg.com/final.jpg",
            }]},
        }
        root = {
            "id": "111",
            "text": "outer text",
            "media": {"all": [{
                "type": "photo",
                "url": "https://pbs.twimg.com/outer.jpg",
            }]},
            "quote": {"id": "222", "text": "middle", "quote": deepest},
        }

        self.assertEqual(
            [item["url"] for item in bot.fxtwitter_media(root)],
            ["https://pbs.twimg.com/outer.jpg"],
        )

    def test_video_dimensions_honor_sar_and_rotation(self):
        payload = {
            "streams": [{
                "width": 720,
                "height": 1280,
                "sample_aspect_ratio": "4:3",
                "side_data_list": [{"rotation": 90}],
            }]
        }
        self.assertEqual(bot.parse_video_dimensions(payload), (1280, 960))

    def test_download_disables_quoted_media_accumulation(self):
        completed = MagicMock(stdout="ok")
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "run_command", return_value=completed
        ) as runner, patch.object(
            bot, "media_files", return_value=[Path(temporary) / "image.jpg"]
        ):
            files, _, method, cookie_invalid = bot.download_media(
                "https://x.com/user/status/123", Path(temporary)
            )
            command = runner.call_args.args[0]
            self.assertNotIn("--cookies", command)
            self.assertIn("extractor.twitter.quoted=false", command)
            self.assertEqual(len(files), 1)
            self.assertEqual(method, "gallery-dl（匿名）")
            self.assertFalse(cookie_invalid)

    def test_download_can_disable_cookie_retries(self):
        completed = MagicMock(stdout="not found")
        with tempfile.TemporaryDirectory() as temporary:
            cookies = Path(temporary) / "cookies.txt"
            cookies.write_text("cookie", encoding="utf-8")
            with patch.object(bot, "COOKIES_PATH", cookies), patch.object(
                bot, "run_command", return_value=completed
            ) as runner, patch.object(bot, "media_files", return_value=[]):
                bot.download_media(
                    "https://x.com/user/status/123",
                    Path(temporary),
                    allow_cookies=False,
                )
            self.assertEqual(runner.call_count, 2)
            for call in runner.call_args_list:
                self.assertNotIn("--cookies", call.args[0])

    def test_process_url_outputs_primary_tweet_instead_of_quote(self):
        deepest = {
            "id": "333",
            "text": "final text",
            "author": {"name": "Final Author", "screen_name": "final_user"},
            "media": {"all": []},
        }
        root = {
            "id": "111",
            "text": "outer text",
            "author": {"name": "Outer Author", "screen_name": "outer_user"},
            "quote": deepest,
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "TMP_DIR", Path(temporary)
        ), patch.object(
            bot, "fetch_fxtwitter", return_value=root
        ), patch.object(
            bot, "fetch_tweet_text"
        ) as text_fetch, patch.object(
            bot, "download_media", return_value=([], "", "", False)
        ) as downloader, patch.object(
            bot, "download_fxtwitter_media", return_value=([], "", 0)
        ):
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.toggle_ordinary_user_cookies(100)
            store.add(200)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.process_url(200, 10, 200, "https://x.com/outer/status/111")

            text_fetch.assert_not_called()
            downloader.assert_called_once_with(
                "https://x.com/outer_user/status/111",
                ANY,
                allow_cookies=False,
            )
            bot.download_fxtwitter_media.assert_not_called()
            output = api.send_message.call_args.args[1]
            self.assertIn(
                '<a href="https://x.com/outer_user">Outer Author</a>:\nouter text',
                output,
            )
            self.assertIn("https://x.com/outer_user/status/111", output)
            self.assertNotIn("final text", output)

    def test_webm_is_delivered_as_document_when_it_cannot_be_previewed(self):
        tweet = {
            "id": "123",
            "text": "webm",
            "author": {"name": "Author", "screen_name": "author"},
            "media": {"all": [{"type": "video", "url": "https://video.twimg.com/a.mp4"}]},
        }
        with tempfile.TemporaryDirectory() as temporary:
            media = Path(temporary) / "video.webm"
            media.write_bytes(b"webm")
            with patch.object(bot, "TMP_DIR", Path(temporary)), patch.object(
                bot, "fetch_fxtwitter", return_value=tweet
            ), patch.object(
                bot, "download_fxtwitter_media", return_value=([media], "", 0)
            ):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                api = MagicMock()
                service = bot.Bot(api, store)
                service.process_url(100, 10, 100, "https://x.com/author/status/123")

            api.send_documents.assert_called_once_with(100, [media])

    def test_original_file_failure_reports_localized_partial_success(self):
        tweet = {
            "id": "123",
            "text": "image",
            "author": {"name": "Author", "screen_name": "author"},
            "media": {"all": [{"type": "photo", "url": "https://pbs.twimg.com/a.jpg"}]},
        }
        with tempfile.TemporaryDirectory() as temporary:
            media = Path(temporary) / "image.jpg"
            media.write_bytes(b"image")
            with patch.object(bot, "TMP_DIR", Path(temporary)), patch.object(
                bot, "fetch_fxtwitter", return_value=tweet
            ), patch.object(
                bot, "download_fxtwitter_media", return_value=([media], "", 0)
            ), patch.object(bot, "prepare_image", side_effect=lambda path: path), patch.object(bot, "OWNER_LANGUAGE", "ja"):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                api = MagicMock()
                api.send_documents.side_effect = RuntimeError("upload failed")
                service = bot.Bot(api, store)
                service.process_url(100, 10, 100, "https://x.com/author/status/123")

            self.assertIn("元ファイル", api.send_message.call_args.args[1])

    def test_preview_failure_reports_localized_retry_guidance(self):
        tweet = {
            "id": "123",
            "text": "video",
            "author": {"name": "Author", "screen_name": "author"},
            "media": {
                "all": [{
                    "type": "video",
                    "url": "https://video.twimg.com/a.mp4",
                }]
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            media = Path(temporary) / "video.mp4"
            media.write_bytes(b"video")
            with patch.object(bot, "TMP_DIR", Path(temporary)), patch.object(
                bot, "fetch_fxtwitter", return_value=tweet
            ), patch.object(
                bot, "download_fxtwitter_media", return_value=([media], "", 0)
            ), patch.object(bot, "OWNER_LANGUAGE", "en"):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                api = MagicMock()
                api.send_previews.side_effect = RuntimeError("connection failed")
                service = bot.Bot(api, store)
                service.process_url(
                    100, 10, 100, "https://x.com/author/status/123"
                )

            fallback = api.send_message.call_args.args[1]
            self.assertIn("media preview", fallback.lower())
            self.assertIn("submit this post again", fallback.lower())
            api.send_documents.assert_called_once_with(100, [])

    def test_owner_keeps_cookie_access_when_management_mode_is_off(self):
        tweet = {
            "id": "123",
            "text": "owner test",
            "author": {"name": "Owner", "screen_name": "owner"},
            "media": {"all": []},
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "TMP_DIR", Path(temporary)
        ), patch.object(
            bot, "fetch_fxtwitter", return_value=tweet
        ), patch.object(
            bot, "download_media", return_value=([], "", "", False)
        ) as downloader, patch.object(
            bot, "download_fxtwitter_media", return_value=([], "", 0)
        ):
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.toggle_ordinary_user_cookies(100)
            store.toggle_management_mode(100)
            service = bot.Bot(MagicMock(), store)

            service.process_url(100, 10, 100, "https://x.com/owner/status/123")

            downloader.assert_called_once_with(
                "https://x.com/owner/status/123", ANY, allow_cookies=True
            )

    def test_media_group_caption_has_no_counter(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.jpg"
            second = Path(temporary) / "second.jpg"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
            api.call = MagicMock()
            api.send_previews(100, [first, second], "tweet text")
            method, data, _ = api.call.call_args.args
            media = json.loads(data["media"])
            self.assertEqual(method, "sendMediaGroup")
            self.assertEqual(media[0]["caption"], "tweet text")
            self.assertNotIn("/2", data["media"])

    def test_media_group_forwards_html_caption_parse_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.jpg"
            second = Path(temporary) / "second.jpg"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
            api.call = MagicMock()
            api.send_previews(
                100, [first, second], '<a href="https://x.com/a">A</a>',
                parse_mode="HTML",
            )
            _, data, _ = api.call.call_args.args
            media = json.loads(data["media"])
            self.assertEqual(media[0]["parse_mode"], "HTML")

    def test_trim_keeps_original_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / "original.png"
            original.write_bytes(b"original-media")
            accepted, rejected = bot.trim_files([original])
            self.assertEqual(accepted, [original])
            self.assertEqual(rejected, [])

    def test_video_over_50_mb_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "MAX_VIDEO_BYTES", 10
        ):
            video = Path(temporary) / "large.mp4"
            video.write_bytes(b"x" * 11)
            accepted, rejected = bot.trim_files([video])
            self.assertEqual(accepted, [])
            self.assertEqual(rejected, ["large.mp4"])

    def test_document_is_sent_as_uncompressed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / "image.jpg"
            original.write_bytes(b"original-media")
            api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
            api.call = MagicMock()
            api.send_document(100, original, "original")
            method, data, files = api.call.call_args.args
            self.assertEqual(method, "sendDocument")
            self.assertEqual(data["caption"], "original")
            self.assertIn("document", files)

    def test_multiple_original_images_are_grouped_as_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.jpg"
            second = Path(temporary) / "second.png"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
            api.call = MagicMock()
            api.send_documents(100, [first, second])
            method, data, _ = api.call.call_args.args
            media = json.loads(data["media"])
            self.assertEqual(method, "sendMediaGroup")
            self.assertEqual([item["type"] for item in media], ["document", "document"])
            self.assertNotIn("caption", media[0])

    def test_inline_results_use_trusted_media_without_documents(self):
        tweet = {
            "id": "123",
            "url": "https://x.com/example/status/123",
            "text": "tweet text",
            "author": {"name": "Example"},
            "media": {"all": [
                {
                    "type": "photo",
                    "url": "https://pbs.twimg.com/photo.jpg",
                    "width": 1200,
                    "height": 800,
                },
                {
                    "type": "video",
                    "url": "https://video.twimg.com/video.mp4",
                    "thumbnail_url": "https://pbs.twimg.com/video.jpg",
                    "width": 1280,
                    "height": 720,
                },
            ]},
        }
        with patch.object(bot, "fetch_fxtwitter", return_value=tweet), patch.object(
            bot, "remote_media_size", return_value=1024
        ):
            results = bot.build_inline_results(
                "https://x.com/example/status/123", debug=True
            )
        self.assertEqual([item["type"] for item in results], ["photo", "video"])
        self.assertIn("name=large", results[0]["photo_url"])
        self.assertIn("取得方式：FxTwitter Inline", results[0]["caption"])
        self.assertEqual(results[0]["parse_mode"], "HTML")
        self.assertNotIn("document", json.dumps(results))

    def test_inline_result_uses_primary_tweet_instead_of_quote(self):
        root = {
            "id": "111",
            "url": "https://x.com/outer/status/111",
            "text": "outer text",
            "author": {"name": "Outer"},
            "media": {"all": [{
                "type": "photo",
                "url": "https://pbs.twimg.com/outer.jpg",
            }]},
            "quote": {
                "id": "222",
                "text": "quoted text",
                "media": {"all": [{
                    "type": "photo",
                    "url": "https://pbs.twimg.com/quoted.jpg",
                }]},
            },
        }
        with patch.object(bot, "fetch_fxtwitter", return_value=root):
            results = bot.build_inline_results("https://x.com/outer/status/111")
        self.assertEqual(len(results), 1)
        self.assertIn("outer.jpg", results[0]["photo_url"])
        self.assertIn("outer text", results[0]["caption"])
        self.assertNotIn("quoted text", results[0]["caption"])

    def test_inline_video_requires_thumbnail_and_size_within_limit(self):
        tweet = {
            "id": "123",
            "text": "video",
            "media": {"all": [{
                "type": "video",
                "url": "https://video.twimg.com/video.mp4",
                "thumbnail_url": "https://pbs.twimg.com/video.jpg",
            }]},
        }
        with patch.object(bot, "fetch_fxtwitter", return_value=tweet), patch.object(
            bot, "remote_media_size", return_value=bot.MAX_VIDEO_BYTES + 1
        ):
            results = bot.build_inline_results("https://x.com/example/status/123")
        self.assertEqual(results[0]["type"], "article")

    def test_inline_rejects_non_twimg_media_urls(self):
        tweet = {
            "id": "123",
            "text": "unsafe",
            "media": {"all": [{
                "type": "photo",
                "url": "https://example.com/private.jpg",
            }]},
        }
        with patch.object(bot, "fetch_fxtwitter", return_value=tweet):
            results = bot.build_inline_results("https://x.com/example/status/123")
        self.assertEqual(results[0]["type"], "article")


class InlineQueryTests(unittest.TestCase):
    def test_inline_api_enables_short_personal_result_caching(self):
        api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
        api.call = MagicMock()
        api.answer_inline_query("query-1", [{"type": "article", "id": "one"}])
        method, data = api.call.call_args.args
        self.assertEqual(method, "answerInlineQuery")
        self.assertEqual(data["is_personal"], "true")
        self.assertEqual(data["cache_time"], str(bot.INLINE_CACHE_SECONDS))

    def test_authorized_inline_query_consumes_once_per_url_window(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "build_inline_results", return_value=[{"type": "article", "id": "one"}]
        ):
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 50)
            api = MagicMock()
            service = bot.Bot(api, store)
            update = {
                "id": "query-1",
                "from": {"id": 200, "first_name": "User"},
                "query": "https://x.com/example/status/123",
            }
            service.handle_inline_query(update)
            update["id"] = "query-2"
            service.handle_inline_query(update)
            service.wait_for_inline_idle()
            record = next(item for item in store.records() if item["user_id"] == 200)
            self.assertEqual(record["usage_count"], 1)
            self.assertEqual(api.answer_inline_query.call_count, 2)

    def test_inline_result_cache_avoids_duplicate_upstream_fetches(self):
        result = [{"type": "article", "id": "one"}]
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "build_inline_results", return_value=result
        ) as builder:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 50)
            api = MagicMock()
            service = bot.Bot(api, store)
            update = {
                "id": "query-1",
                "from": {"id": 200},
                "query": "https://x.com/example/status/123",
            }
            service.handle_inline_query(update)
            service.wait_for_inline_idle()
            update["id"] = "query-2"
            service.handle_inline_query(update)
            service.wait_for_inline_idle()

            builder.assert_called_once()
            self.assertEqual(api.answer_inline_query.call_count, 2)

    def test_unauthorized_inline_query_returns_no_results_without_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_language(200, "ja")
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_inline_query({
                "id": "query-1",
                "from": {"id": 200, "first_name": "User"},
                "query": "https://x.com/example/status/123",
            })
            self.assertEqual(api.answer_inline_query.call_args.args[1], [])
            self.assertEqual(
                api.answer_inline_query.call_args.args[2]["text"],
                bot.public_text("ja", "inline_apply"),
            )
            record = next(item for item in store.records() if item["user_id"] == 200)
            self.assertEqual(record["usage_count"], 0)

    def test_pause_blocks_ordinary_inline_use_but_not_admin(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "build_inline_results", return_value=[{"type": "article", "id": "one"}]
        ) as builder:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 50)
            store.set_quota(300, None)
            store.toggle_external_access(100)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_inline_query({
                "id": "ordinary",
                "from": {"id": 200},
                "query": "https://x.com/example/status/123",
            })
            service.handle_inline_query({
                "id": "admin",
                "from": {"id": 300},
                "query": "https://x.com/example/status/123",
            })
            service.wait_for_inline_idle()
            self.assertEqual(api.answer_inline_query.call_args_list[0].args[1], [])
            builder.assert_called_once()

    def test_management_mode_off_applies_ordinary_inline_switch_and_hides_debug(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "build_inline_results", return_value=[{"type": "article", "id": "one"}]
        ) as builder:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(300, None)
            store.data["users"]["300"]["debug_mode"] = True
            store.toggle_management_mode(300)
            store.toggle_external_access(100)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_inline_query({
                "id": "paused-admin",
                "from": {"id": 300},
                "query": "https://x.com/example/status/123",
            })
            self.assertEqual(api.answer_inline_query.call_args.args[1], [])
            builder.assert_not_called()

            store.toggle_external_access(100)
            service.handle_inline_query({
                "id": "ordinary-admin",
                "from": {"id": 300},
                "query": "https://x.com/example/status/123",
            })
            service.wait_for_inline_idle()
            builder.assert_called_once_with(
                "https://x.com/example/status/123", debug=False
            )

    def test_inline_video_answers_with_external_media_result(self):
        result = {
            "type": "video",
            "id": "video-one",
            "video_url": "https://video.twimg.com/video.mp4",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "build_inline_results", return_value=[result]
        ):
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 50)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.handle_inline_query({
                "id": "query-video",
                "from": {"id": 200},
                "query": "https://x.com/example/status/123",
            })
            service.wait_for_inline_idle()
            api.answer_inline_query.assert_called_once_with("query-video", [result])


class PerformanceSafetyTests(unittest.TestCase):
    def deploy_script(self) -> Path:
        candidate = Path(__file__).with_name("deploy.sh")
        if candidate.is_file():
            return candidate
        self.skipTest("deployment script is not installed on this host")

    def test_bot_creates_configured_bounded_workers(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            service = bot.Bot(MagicMock(), store)
            self.assertEqual(len(service.workers), bot.WORKER_COUNT)
            self.assertEqual(
                service.inline_executor._max_workers, bot.INLINE_WORKER_COUNT
            )

    def test_environment_integer_limits_are_bounded_and_invalid_values_default(self):
        with patch.dict(os.environ, {"TEST_LIMIT": "0"}):
            self.assertEqual(bot.env_int("TEST_LIMIT", 12, 1, 100), 1)
        with patch.dict(os.environ, {"TEST_LIMIT": "999"}):
            self.assertEqual(bot.env_int("TEST_LIMIT", 12, 1, 100), 100)
        with patch.dict(os.environ, {"TEST_LIMIT": "invalid"}):
            self.assertEqual(bot.env_int("TEST_LIMIT", 12, 1, 100), 12)

    def test_update_offset_round_trip_is_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "update-offset.json"
            self.assertEqual(bot.load_update_offset(path), 0)
            bot.save_update_offset(123, path)
            self.assertEqual(bot.load_update_offset(path), 123)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"offset": 123})

    def test_invalid_update_offset_shape_falls_back_to_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "update-offset.json"
            for invalid in ([], {"offset": True}, {"offset": -1}, {"offset": "12"}):
                path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertLogs(bot.LOG, level="ERROR"):
                    self.assertEqual(bot.load_update_offset(path), 0)

    def test_http_session_is_reused_per_thread(self):
        with patch.object(bot, "HTTP_LOCAL", bot.threading.local()):
            first = bot.http_session()
            second = bot.http_session()
        self.assertIs(first, second)
        self.assertEqual(first.headers["User-Agent"], f"{bot.APP_NAME}/{bot.APP_VERSION}")

    def test_telegram_request_error_does_not_expose_token(self):
        token = "12345678:test-token-value-that-must-never-be-logged"
        api = bot.TelegramAPI(token)
        session = MagicMock()
        session.post.side_effect = bot.requests.ConnectionError(
            f"failed URL {api.base}/getUpdates"
        )
        api.local.session = session

        with self.assertRaises(RuntimeError) as raised:
            api.call("getUpdates")

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("ConnectionError", str(raised.exception))

    def test_telegram_429_retries_once_using_retry_after(self):
        api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
        limited = MagicMock(status_code=429)
        limited.json.return_value = {
            "ok": False,
            "parameters": {"retry_after": 2},
        }
        successful = MagicMock(status_code=200)
        successful.json.return_value = {
            "ok": True,
            "result": {"message_id": 10},
        }
        session = MagicMock()
        session.post.side_effect = [limited, successful]
        api.local.session = session

        with patch.object(bot.time, "sleep") as sleep:
            result = api.call("sendMessage", {"chat_id": 100, "text": "ok"})

        self.assertEqual(result, {"message_id": 10})
        self.assertEqual(session.post.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_edit_message_ignores_only_message_not_modified(self):
        api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
        api.call = MagicMock(
            side_effect=bot.TelegramAPIError(
                "editMessageText", 400, "Bad Request: message is not modified"
            )
        )
        self.assertFalse(api.edit_message(100, 10, "unchanged"))

        api.call.side_effect = bot.TelegramAPIError(
            "editMessageText", 400, "Bad Request: message to edit not found"
        )
        with self.assertRaises(bot.TelegramAPIError):
            api.edit_message(100, 10, "missing")

    def test_telegram_error_description_is_diagnostic_and_token_safe(self):
        token = "12345678:test-token-value-that-must-never-be-logged"
        error = bot.TelegramAPIError(
            "editMessageText",
            400,
            f"Bad Request at https://api.telegram.org/bot{token}/editMessageText",
        )
        self.assertIn("Bad Request", str(error))
        self.assertNotIn(token, str(error))

    def test_telegram_429_does_not_wait_beyond_bounded_limit(self):
        api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
        limited = MagicMock(status_code=429)
        limited.json.return_value = {
            "ok": False,
            "parameters": {
                "retry_after": bot.TELEGRAM_RETRY_AFTER_MAX_SECONDS + 1,
            },
        }
        session = MagicMock()
        session.post.return_value = limited
        api.local.session = session

        with patch.object(bot.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                api.call("sendMessage", {"chat_id": 100, "text": "limited"})

        session.post.assert_called_once()
        sleep.assert_not_called()

    def test_telegram_429_does_not_reuse_uploaded_file_stream(self):
        api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
        limited = MagicMock(status_code=429)
        limited.json.return_value = {
            "ok": False,
            "parameters": {"retry_after": 1},
        }
        session = MagicMock()
        session.post.return_value = limited
        api.local.session = session

        with patch.object(bot.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                api.call(
                    "sendDocument",
                    {"chat_id": 100},
                    {"document": ("file.jpg", MagicMock())},
                )

        session.post.assert_called_once()
        sleep.assert_not_called()

    def test_chat_action_failure_does_not_abort_processing(self):
        api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
        api.call = MagicMock(side_effect=RuntimeError("temporary failure"))

        self.assertIsNone(api.send_action(100, "upload_document"))
        api.call.assert_called_once_with(
            "sendChatAction", {"chat_id": 100, "action": "upload_document"}
        )

    def test_deploy_runs_tests_before_replacing_installed_bot(self):
        script = self.deploy_script().read_text(encoding="utf-8")
        self.assertLess(
            script.index('"$INSTALL_DIR/venv/bin/python" -m unittest -q test_bot.py'),
            script.index('install -o root -g root -m 0755 "$SOURCE_DIR/bot.py"'),
        )
        self.assertNotIn('SYNC_ROLE', script)
        self.assertNotIn('access_sync_endpoint.sh', script)
        self.assertIn('rollback_dir=$(mktemp -d "$INSTALL_DIR/.deploy-rollback.', script)
        self.assertIn("rollback_deploy()", script)
        self.assertIn("trap rollback_deploy ERR", script)
        self.assertIn('systemctl is-active --quiet "$SERVICE"', script)

        bash = shutil.which("bash")
        if bash:
            result = subprocess.run(
                [bash, "-n", str(self.deploy_script())],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_one_bad_update_does_not_block_following_updates(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            api = MagicMock()
            api.call.return_value = [{"update_id": 1}, {"update_id": 2}]
            service = bot.Bot(api, store)

            def handle(update):
                if update["update_id"] == 1:
                    raise RuntimeError("bad callback")
                service.stop()

            service.handle_update = MagicMock(side_effect=handle)
            service.maybe_send_daily_report = MagicMock()
            with patch.object(bot, "load_update_offset", return_value=0), patch.object(
                bot, "save_update_offset"
            ) as save_offset:
                service.start()

            self.assertEqual(service.handle_update.call_count, 2)
            self.assertEqual(
                [call.args[0] for call in save_offset.call_args_list], [2, 3]
            )


class PublicReleaseLanguageTests(unittest.TestCase):
    def test_administrator_language_is_fixed_and_users_keep_their_choice(self):
        for language, status, report in (
            ("zh", "系統狀態", "每日使用簡報"),
            ("en", "System status", "Daily usage report"),
            ("ja", "システム状態", "日次利用レポート"),
        ):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary, patch.object(bot, "OWNER_LANGUAGE", language):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_quota(300, None)
                store.set_language(200, "ja")
                self.assertEqual(store.language(100), language)
                self.assertEqual(store.language(300), language)
                self.assertEqual(store.language(200), "ja")
                with self.assertRaises(ValueError):
                    store.set_language(100, "en")
                api = MagicMock()
                service = bot.Bot(api, store)
                service.handle_update({
                    "message": {"message_id": 1, "chat": {"id": 100}, "from": {"id": 100}, "text": "/start"}
                })
                self.assertIn(bot.admin_text("users"), str(api.send_message.call_args.args[3]))
                self.assertEqual(api.send_message.call_args.args[1], bot.public_text(language, "start_owner"))
                api.reset_mock()
                service.handle_owner_command(100, 2, 100, "/status", "")
                self.assertIn(status, api.send_message.call_args.args[1])
                api.reset_mock()
                service.handle_owner_command(100, 3, 100, "/users", "")
                self.assertIn(bot.admin_text("user_list"), api.send_message.call_args.args[1])
                api.reset_mock()
                service.handle_callback({
                    "id": "admin-language", "data": "lang:en", "from": {"id": 100},
                    "message": {"message_id": 4, "chat": {"id": 100}},
                })
                self.assertEqual(store.language(100), language)
                self.assertEqual(api.answer_callback.call_args.args[1], bot.admin_text("language_locked"))
                api.reset_mock()
                service.maybe_send_daily_report(force=True)
                self.assertIn(report, api.send_message.call_args.args[1])
                service.stop()

    def test_optional_contact_is_not_hardcoded(self):
        with patch.object(bot, "OWNER_CONTACT_URL", ""):
            for language in bot.PUBLIC_TEXT:
                self.assertEqual(bot.public_help_text(language), bot.public_text(language, "help_allowed"))
        with patch.object(bot, "OWNER_CONTACT_URL", 'https://example.com/contact?a=1&b=2'):
            for language in bot.PUBLIC_TEXT:
                self.assertIn('href="https://example.com/contact?a=1&amp;b=2"', bot.public_help_text(language))
        with patch.object(bot, "OWNER_CONTACT_URL", "javascript:alert(1)"):
            self.assertEqual(bot.public_help_text("en"), bot.public_text("en", "help_allowed"))

    def test_contact_label_override_is_escaped(self):
        with patch.object(bot, "OWNER_CONTACT_URL", "https://example.com/contact"), \
             patch.object(bot, "OWNER_CONTACT_LABEL", 'Contact <owner>'):
            for language in bot.PUBLIC_TEXT:
                guide = bot.public_help_text(language)
                self.assertIn('>Contact &lt;owner&gt;</a>', guide)
                self.assertNotIn('>Contact <owner></a>', guide)

    def test_version_and_translation_catalog_are_complete(self):
        self.assertEqual((Path(__file__).parent / "VERSION").read_text().strip(), bot.APP_VERSION)
        self.assertTrue(all(len(values) == 3 for values in bot.ADMIN_TEXT.values()))


if __name__ == "__main__":
    unittest.main()
