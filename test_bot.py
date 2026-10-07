#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import bot
import config_cli


class ReliabilityTests(unittest.TestCase):
    def test_telegram_malformed_responses_fail_safely_without_leaking_payloads(self):
        cases = [
            (502, None), (502, []), (200, "YOUR_API_TOKEN"),
            (429, {"ok": False, "parameters": ["invalid"]}),
            (429, {"ok": False, "parameters": {"retry_after": float("inf")}}),
            (200, {"ok": False, "error_code": [400]}),
            (200, {"ok": False, "error_code": "bad"}),
        ]
        api = bot.TelegramAPI("YOUR_API_TOKEN")
        for status, payload in cases:
            with self.subTest(status=status, payload=payload):
                response = MagicMock(status_code=status)
                response.json.return_value = payload
                session = MagicMock(post=MagicMock(return_value=response))
                with patch.object(api, "session", return_value=session), patch.object(bot.time, "sleep") as sleep:
                    with self.assertRaises(RuntimeError) as error:
                        api.call("getUpdates")
                    self.assertNotIn("YOUR_API_TOKEN", str(error.exception))
                    sleep.assert_not_called()
                    session.post.assert_called_once()
        response = MagicMock(status_code=200)
        response.json.side_effect = ValueError("YOUR_API_TOKEN")
        with patch.object(api, "session", return_value=MagicMock(post=MagicMock(return_value=response))):
            with self.assertRaisesRegex(RuntimeError, "invalid JSON") as error:
                api.call("getUpdates")
            self.assertTrue(error.exception.__suppress_context__)
            self.assertNotIn("YOUR_API_TOKEN", str(error.exception))

    def test_telegram_validates_entire_update_batch_before_acknowledgement(self):
        api = bot.TelegramAPI("YOUR_API_TOKEN")
        invalid = [None, {}, 1, [None], [{}], [{"update_id": "1"}],
                   [{"update_id": True}], [{"update_id": -1}],
                   [{"update_id": 1}, {"update_id": []}]]
        response = MagicMock(status_code=200)
        with patch.object(api, "session", return_value=MagicMock(post=MagicMock(return_value=response))):
            for result in invalid:
                with self.subTest(result=result), self.assertRaisesRegex(RuntimeError, "invalid updates"):
                    response.json.return_value = {"ok": True, "result": result}
                    api.call("getUpdates")
            for result in ([], [{"update_id": 0}], [{"update_id": 123}]):
                response.json.return_value = {"ok": True, "result": result}
                self.assertEqual(api.call("getUpdates"), result)

    def test_polling_retries_malformed_json_without_acknowledging_or_exiting(self):
        for payload in (None, [], {"ok": True, "result": [None]}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                store = bot.ACLStore(Path(directory) / "acl.json", 100)
                api = bot.TelegramAPI("YOUR_API_TOKEN")
                response = MagicMock(status_code=200)
                response.json.return_value = payload
                api.local.session = MagicMock(post=MagicMock(return_value=response))
                service = bot.Bot(api, store)
                service.workers = []
                with patch.object(api, "configure_commands"), patch.object(api, "configure_profile"), \
                     patch.object(bot, "cleanup_stale_media"), patch.object(bot, "load_update_offset", return_value=0), \
                     patch.object(bot, "save_update_offset") as save, \
                     patch.object(bot.time, "sleep", side_effect=lambda _: service.stop()) as sleep, \
                     self.assertLogs(bot.LOG, level="ERROR"):
                    service.start()
                    sleep.assert_called_once_with(5)
                    save.assert_not_called()
                service.inline_executor.shutdown(wait=True)

    def test_malformed_media_collections_preserve_inline_text_fallback(self):
        for media in ({"all": 1}, {"all": {}}, {"all": [None, {"type": ["photo"]}]},
                      {"all": [{"type": "photo", "url": ["invalid"]}]}):
            with self.subTest(media=media), patch.object(bot, "fetch_fxtwitter", return_value={
                "id": "123", "text": "example", "media": media,
            }):
                results = bot.build_inline_results("https://x.com/example/status/123")
                self.assertEqual(results[0]["type"], "article")
                self.assertIn("example", results[0]["input_message_content"]["message_text"])

    def test_invalid_optional_media_metadata_does_not_abort_inline_or_formats(self):
        item = {"type": "video", "url": "https://video.twimg.com/example.mp4",
                "thumbnail_url": "https://pbs.twimg.com/example.jpg",
                "width": "bad", "height": [], "duration": float("inf"),
                "formats": [{"container": "mp4", "url": "https://video.twimg.com/example.mp4",
                             "width": "bad", "height": {}, "bitrate": float("nan")} ]}
        tweet = {"id": "123", "text": "example", "media": {"all": [item]}}
        self.assertEqual(len(bot.sorted_mp4_formats(item)), 1)
        for formats in (1, {}, "invalid"):
            self.assertEqual(bot.sorted_mp4_formats({"formats": formats}), [])
        with patch.object(bot, "fetch_fxtwitter", return_value=tweet), \
             patch.object(bot, "remote_media_size", return_value=1):
            result = bot.build_inline_results("https://x.com/example/status/123")[0]
            self.assertEqual(result["type"], "video")
            for key in ("video_width", "video_height", "video_duration"):
                self.assertNotIn(key, result)
        self.assertEqual(item["width"], "bad")
        self.assertIsNone(bot.trusted_twimg_url("https://[invalid"))
        self.assertIsNone(bot.trusted_twimg_url(["https://pbs.twimg.com/example.jpg"]))

    def test_malformed_nested_media_still_reaches_normal_downloader(self):
        cases = [{"all": 1}, {"all": [{"type": ["video"]}]}, {"all": [{
            "type": "video", "url": "https://video.twimg.com/example.mp4",
            "formats": [{"container": "mp4", "url": "https://video.twimg.com/example.mp4", "width": "bad"}],
        }]}]
        for media in cases:
            with self.subTest(media=media), tempfile.TemporaryDirectory() as directory:
                store = bot.ACLStore(Path(directory) / "acl.json", 100)
                store.add(200)
                service = bot.Bot(MagicMock(), store)
                with patch.object(bot, "fetch_fxtwitter", return_value={"id": "123", "text": "example", "media": media}), \
                     patch.object(bot, "TMP_DIR", Path(directory)), \
                     patch.object(bot, "media_disk_available", return_value=True), \
                     patch.object(bot, "download_fxtwitter_media", return_value=([], "", 0)), \
                     patch.object(bot, "download_media", return_value=([], "", "", False)) as download:
                    service.process_url(200, 1, 200, "https://x.com/example/status/123")
                    download.assert_called_once()
                    messages = [call.args[1] for call in service.api.send_message.call_args_list]
                    self.assertIn("example", messages[0])
                    if bot.fxtwitter_media({"media": media}):
                        self.assertEqual(messages[1:], [bot.public_text("zh", "media_skipped")])
                    else:
                        self.assertEqual(len(messages), 1)
                service.stop()
                service.inline_executor.shutdown(wait=True)

    def test_video_skip_notices_follow_final_recovery_and_actual_rejection_reason(self):
        cases = [
            # Source videos, fallback videos, fallback photos, expected notice.
            (1, [5], [], None),
            (2, [5], [], "media_skipped"),
            (1, [], [5], "video_limited"),
            (1, [11], [], "video_limited"),
        ]
        for language in bot.PUBLIC_TEXT:
            for sources, video_sizes, photo_sizes, notice in cases:
                with self.subTest(language=language, sources=sources, notice=notice), tempfile.TemporaryDirectory() as directory:
                    store = bot.ACLStore(Path(directory) / "acl.json", 100)
                    store.add(200)
                    store.set_language(200, language)
                    service = bot.Bot(MagicMock(), store)
                    files = []
                    for suffix, sizes in (("mp4", video_sizes), ("jpg", photo_sizes)):
                        for index, size in enumerate(sizes):
                            path = Path(directory) / f"{index}.{suffix}"
                            path.write_bytes(b"x" * size)
                            files.append(path)
                    tweet = {"id": "123", "text": "example", "media": {"all": [
                        {"type": "video", "url": f"https://video.twimg.com/{index}.mp4"}
                        for index in range(sources)
                    ]}}
                    with patch.object(bot, "fetch_fxtwitter", return_value=tweet), \
                         patch.object(bot, "TMP_DIR", Path(directory)), \
                         patch.object(bot, "media_disk_available", return_value=True), \
                         patch.object(bot, "prepare_image", side_effect=lambda path: path), \
                         patch.object(bot, "download_fxtwitter_media", return_value=([], "", sources)), \
                         patch.object(bot, "download_media", return_value=(files, "", "fallback", False)), \
                         patch.object(bot, "MAX_VIDEO_BYTES", 10):
                        service.process_url(200, 1, 200, "https://x.com/example/status/123")
                    messages = [call.args[1] for call in service.api.send_message.call_args_list]
                    self.assertEqual(messages[-1] if notice else messages, bot.public_text(language, notice, count=1) if notice else [])
                    if notice == "media_skipped":
                        self.assertFalse(any("50 MB" in message for message in messages))
                    service.stop()
                    service.inline_executor.shutdown(wait=True)

    def test_total_media_limit_does_not_claim_telegram_single_video_limit(self):
        for language in bot.PUBLIC_TEXT:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as directory:
                store = bot.ACLStore(Path(directory) / "acl.json", 100)
                store.add(200)
                store.set_language(200, language)
                service = bot.Bot(MagicMock(), store)
                files = [Path(directory) / f"{index}.mp4" for index in range(2)]
                for path in files:
                    path.write_bytes(b"x" * 8)
                with patch.object(bot, "fetch_fxtwitter", return_value={"id": "123", "text": "example"}), \
                     patch.object(bot, "TMP_DIR", Path(directory)), \
                     patch.object(bot, "media_disk_available", return_value=True), \
                     patch.object(bot, "download_media", return_value=(files, "", "fallback", False)), \
                     patch.object(bot, "MAX_VIDEO_BYTES", 10), patch.object(bot, "MAX_TOTAL_BYTES", 12):
                    service.process_url(200, 1, 200, "https://x.com/example/status/123")
                service.api.send_previews.assert_called_once()
                self.assertEqual(service.api.send_previews.call_args.args[1], files[:1])
                service.api.send_message.assert_called_once_with(200, bot.public_text(language, "media_skipped"))
                service.stop()
                service.inline_executor.shutdown(wait=True)

    def test_deeply_invalid_acl_cannot_replace_a_good_backup(self):
        cases = [
            {"pending_jobs": []}, {"owner_id": True},
            {"users": {"200": {"quota": "50"}}},
            {"users": {"200": {"quota": True}}},
            {"users": {"200": {"usage_count": "1"}}},
            {"users": {"200": {"user_id": 201}}},
            {"users": {"0200": {"quota": 50}}},
            {"users": {"200": {"first_name": []}}},
            {"users": {"200": {"quota_updated_at": -1}}},
            {"pending_applications": {"200": []}},
            {"pending_applications": {"200": {"requested_at": float("nan")}}},
            {"pending_applications": {"200": {"requested_at": True}}},
        ]
        for changes in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "acl.json"
                store = bot.ACLStore(path, 100)
                store.add(200)
                good = path.read_bytes()
                store.backup_path.write_bytes(good)
                bad = {**json.loads(good), **changes}
                path.write_text(json.dumps(bad), encoding="utf-8")
                with self.assertLogs(bot.LOG, level="ERROR"):
                    store._save()
                self.assertEqual(store.backup_path.read_bytes(), good)
                path.write_text(json.dumps(bad), encoding="utf-8")
                with self.assertLogs(bot.LOG, level="ERROR"):
                    recovered = bot.ACLStore(path, 100)
                self.assertEqual(recovered.quota(200), bot.DEFAULT_DAILY_LIMIT)
                self.assertEqual(recovered.backup_path.read_bytes(), good)

    def test_access_snapshot_requires_explicit_strict_fields_without_partial_changes(self):
        valid = {"user_id": 200, "quota": 50, "updated_at": 1}
        cases = [{key: value for key, value in valid.items() if key != missing}
                 for missing in valid]
        cases += [{**valid, field: value} for field, values in (
            ("user_id", (True, "200", 200.5)), ("quota", (True, "50", 50.5)),
            ("updated_at", (True, "1", 1.5)),
        ) for value in values]
        with tempfile.TemporaryDirectory() as directory:
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            before = store.path.read_bytes()
            for item in cases:
                with self.subTest(item=item), self.assertRaises(ValueError):
                    store.import_access({"version": 1, "default_daily_limit": 100,
                                         "users": [valid, item]})
                self.assertEqual(store.path.read_bytes(), before)
                self.assertFalse(store.has_user(200))
            for version in (True, 1.0, "1"):
                with self.assertRaises(ValueError):
                    store.import_access({"version": version, "users": [valid]})
            with self.assertRaises(ValueError):
                store.import_access({"version": 1, "users": [valid, valid]})
            self.assertEqual(store.import_access({"version": 1, "users": [{**valid, "quota": None}]}), 1)
            self.assertTrue(store.is_admin(200))

    def test_cookie_download_always_closes_and_masks_stream_errors(self):
        api = bot.TelegramAPI("YOUR_API_TOKEN")
        api.call = MagicMock(return_value={"file_path": "example.txt"})
        for chunks, status, expected in (([b"ok"], 200, None), ([b"oversized"], 200, ValueError),
                                         ([], 403, RuntimeError),
                                         (bot.requests.ConnectionError(api.file_base + "/example.txt"), 200, RuntimeError)):
            with self.subTest(status=status, chunks=chunks):
                response = MagicMock(status_code=status)
                response.__enter__.return_value = response
                if isinstance(chunks, Exception):
                    response.iter_content.side_effect = chunks
                else:
                    response.iter_content.return_value = iter(chunks)
                with patch.object(api, "session", return_value=MagicMock(get=MagicMock(return_value=response))):
                    if expected:
                        with self.assertRaises(expected) as error:
                            api.download_file("example", 2)
                        self.assertNotIn("YOUR_API_TOKEN", str(error.exception))
                    else:
                        self.assertEqual(api.download_file("example", 2), b"ok")
                response.__exit__.assert_called_once()
        with tempfile.TemporaryDirectory() as directory:
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            service = bot.Bot(api, store)
            service.pending_cookie_uploads.add(100)
            api.send_message = MagicMock()
            with patch.object(api, "session", return_value=MagicMock(get=MagicMock(return_value=response))), self.assertLogs(bot.LOG, level="WARNING") as logs:
                service.handle_document(100, 1, 100, {"file_id": "example", "file_size": 2})
            self.assertNotIn("YOUR_API_TOKEN", " ".join(logs.output))
            service.stop()
            service.inline_executor.shutdown(wait=True)

    def test_unexpected_external_json_uses_existing_fallback(self):
        for payload in (None, [], "unexpected", 1):
            with self.subTest(payload=payload):
                response = MagicMock()
                response.json.return_value = payload
                with patch.object(bot, "http_session", return_value=MagicMock(get=MagicMock(return_value=response))):
                    self.assertIsNone(bot.fetch_fxtwitter("https://x.com/example/status/123"))
                    self.assertEqual(bot.fetch_tweet_text("https://x.com/example/status/123"), ("", "", ""))

    def test_consolidated_advanced_toggles_preserve_roles_and_localized_notices(self):
        actions = (
            ("debugtoggle", "implementation", "advanced_owner_only", lambda store: store.debug_mode(100)),
            ("externaltoggle", "access_switch", "access_owner_only", lambda store: store.external_access_enabled),
            ("autoapprovetoggle", "auto_approve", "auto_owner_only", lambda store: store.auto_approve_enabled),
        )
        for language in bot.PUBLIC_TEXT:
            with tempfile.TemporaryDirectory() as directory:
                store = bot.ACLStore(Path(directory) / "acl.json", 100)
                store.add(200)
                store.set_quota(200, None)
                store.add(300)
                for actor in (100, 200, 300):
                    store.set_language(actor, language)
                service = bot.Bot(MagicMock(), store)
                for action, label, error, current in actions:
                    for actor in (200, 300, 100, 100):
                        with self.subTest(language=language, action=action, actor=actor):
                            before = current(store)
                            service.api.reset_mock()
                            service.handle_callback({
                                "id": "toggle", "data": action + ":0", "from": {"id": actor},
                                "message": {"message_id": 1, "chat": {"id": actor}},
                            })
                            with bot.language_scope(language):
                                if actor != 100:
                                    self.assertEqual(current(store), before)
                                    service.api.edit_message.assert_not_called()
                                    service.api.answer_callback.assert_called_with(
                                        "toggle", bot.admin_text(error if actor == 200 else "admin_only"), alert=True,
                                    )
                                else:
                                    self.assertEqual(current(store), not before)
                                    states = ("open", "paused") if action == "externaltoggle" else ("on", "off")
                                    state = bot.admin_text(states[0] if current(store) else states[1])
                                    notice = f"{bot.admin_text(label)}已{state}。" if language == "zh" else f"{bot.admin_text(label)}: {state}"
                                    service.api.answer_callback.assert_called_with("toggle", notice)
                                    expected_keyboard = (bot.advanced_status_keyboard(store.debug_mode(100))
                                                         if action == "debugtoggle" else
                                                         bot.user_controls_keyboard(store.external_access_enabled, store.auto_approve_enabled))
                                    self.assertEqual(service.api.edit_message.call_args.args[3], expected_keyboard)
                service.stop()
                service.inline_executor.shutdown(wait=True)

    def test_single_approval_and_denial_bind_to_the_current_application(self):
        for action in ("approve", "deny"):
            for change in ("renewed", "legacy", "current"):
                for language in bot.PUBLIC_TEXT:
                    with self.subTest(action=action, change=change, language=language), tempfile.TemporaryDirectory() as directory:
                        store = bot.ACLStore(Path(directory) / "acl.json", 100)
                        store.set_language(100, language)
                        store.observe({"id": 200})
                        store.request_access(200, now=1.0)
                        row = bot.pending_keyboard(store.pending())["inline_keyboard"][0]
                        data = row[0 if action == "approve" else 1]["callback_data"]
                        self.assertLessEqual(len(data.encode()), 64)
                        if change == "renewed":
                            store.deny(200)
                            store.request_access(200, now=2.0)
                        elif change == "legacy":
                            data = f"{action}:200:0"
                        before = store.pending()
                        service = bot.Bot(MagicMock(), store)
                        service.handle_callback({"id": "check", "data": data, "from": {"id": 100},
                                                 "message": {"message_id": 1, "chat": {"id": 100}}})
                        if change == "current":
                            self.assertFalse(store.pending())
                            self.assertEqual(store.is_allowed(200), action == "approve")
                        else:
                            self.assertEqual(store.pending(), before)
                            self.assertEqual(store.quota(200), 0)
                            with bot.language_scope(language):
                                service.api.answer_callback.assert_called_with("check", bot.admin_text("requests_changed"), alert=True)
                        service.stop()
                        service.inline_executor.shutdown(wait=True)
        with tempfile.TemporaryDirectory() as directory:
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            store.request_access(200, now=2.0)
            with patch.object(store, "_save") as save:
                self.assertFalse(store.approve(200, requested_at=1.0))
                self.assertFalse(store.deny(200, requested_at=1.0))
                save.assert_not_called()
            record = {"user_id": bot.MAX_TELEGRAM_USER_ID, "requested_at": 1790999999.1234567}
            for button in bot.pending_keyboard([record])["inline_keyboard"][0]:
                self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_invalid_acl_booleans_are_rejected_and_valid_backup_is_used(self):
        for setting in ("external_access_enabled", "ordinary_user_cookies_enabled", "auto_approve_enabled", "debug_mode"):
            for value in ("false", 0, 1, None, [], {}):
                with self.subTest(setting=setting, value=value):
                    store = bot.ACLStore.__new__(bot.ACLStore)
                    with self.assertRaises(ValueError):
                        store._apply_state({"users": {}, "pending_applications": {}, setting: value}, 1)
                    with self.assertRaises(ValueError):
                        store._apply_state({"users": {"100": {"debug_mode": value}}, "pending_applications": {}}, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acl.json"
            store = bot.ACLStore(path, 100)
            store.toggle_external_access(100)
            store._save()
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["external_access_enabled"] = "false"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertLogs(bot.LOG, level="ERROR"):
                recovered = bot.ACLStore(path, 100)
            self.assertFalse(recovered.external_access_enabled)
            self.assertTrue(list(Path(directory).glob("acl.json.corrupt-*")))
            path.write_text(json.dumps(payload), encoding="utf-8")
            recovered.backup_path.write_text(json.dumps(payload), encoding="utf-8")
            before = path.read_bytes()
            with self.assertLogs(bot.LOG, level="ERROR"), self.assertRaises(RuntimeError):
                bot.ACLStore(path, 100)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(recovered.backup_path.read_bytes(), before)

    def test_failed_job_retries_notice_without_reprocessing_or_recharging(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            store.add(200)
            job = (200, 42, 200, "https://x.com/test/status/123")
            store.consume(200, job=job)
            api = MagicMock()
            api.send_message.side_effect = [bot.TelegramAPIError("sendMessage", 503), None]
            service = bot.Bot(api, store)
            service.process_url = MagicMock(side_effect=RuntimeError("temporary failure"))
            with patch.object(service.stop_event, "wait", return_value=False) as delay:
                service.workers[0].start()
                deadline = bot.time.monotonic() + 3
                while service.jobs.unfinished_tasks and bot.time.monotonic() < deadline:
                    bot.time.sleep(0.01)
                service.stop()
                service.workers[0].join(timeout=2)
                self.assertEqual(service.jobs.unfinished_tasks, 0)
                delay.assert_called_with(5)
            self.assertEqual(api.send_message.call_count, 2)
            service.process_url.assert_called_once_with(*job)
            self.assertFalse(store.data["pending_jobs"])
            self.assertEqual(store.data["users"]["200"]["usage_count"], 1)
            service.inline_executor.shutdown(wait=True)

    def test_failed_job_shutdown_interrupts_retry_and_preserves_pending_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            job = (100, 42, 100, "https://x.com/test/status/123")
            store.consume(100, job=job)
            api = MagicMock()
            service = bot.Bot(api, store)
            service.process_url = MagicMock(side_effect=RuntimeError("temporary failure"))
            def fail(*args, **kwargs):
                service.stop()
                raise bot.TelegramAPIError("sendMessage", 503)
            api.send_message.side_effect = fail
            service.workers[0].start()
            service.workers[0].join(timeout=2)
            self.assertFalse(service.workers[0].is_alive())
            self.assertEqual(len(store.data["pending_jobs"]), 1)
            self.assertEqual(api.send_message.call_count, 1)
            service.inline_executor.shutdown(wait=True)

    def test_job_finalization_failure_never_resends_successful_media(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            job = (100, 42, 100, "https://x.com/test/status/123")
            store.consume(100, job=job)
            service = bot.Bot(MagicMock(), store)
            service.process_url = MagicMock()
            finish = store.finish_job
            calls = []
            def interrupted(*args):
                calls.append(args)
                if len(calls) == 1:
                    raise OSError("temporary storage failure")
                finish(*args)
            with patch.object(store, "finish_job", side_effect=interrupted), patch.object(service.stop_event, "wait", return_value=False):
                service.workers[0].start()
                deadline = bot.time.monotonic() + 3
                while service.jobs.unfinished_tasks and bot.time.monotonic() < deadline:
                    bot.time.sleep(0.01)
                service.stop()
                service.workers[0].join(timeout=2)
            self.assertEqual(service.jobs.unfinished_tasks, 0)
            service.process_url.assert_called_once_with(*job)
            service.api.send_message.assert_not_called()
            self.assertFalse(store.data["pending_jobs"])
            service.inline_executor.shutdown(wait=True)

    def test_navigation_is_silent_and_menu_labels_align_in_all_languages(self):
        for language in ("zh-cn", "zh", "en", "ja"):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as directory:
                store = bot.ACLStore(Path(directory) / "acl.json", 100)
                store.set_language(100, language)
                service = bot.Bot(MagicMock(), store)
                for action in ("noop:0", "userspage:0", "requestspage:0", "nav:main"):
                    service.handle_callback({"id": "navigate", "data": action, "from": {"id": 100},
                                             "message": {"message_id": 1, "chat": {"id": 100}}})
                    service.api.answer_callback.assert_called_with("navigate", "")
                with bot.language_scope(language):
                    cookie_rows = bot.cookie_menu_keyboard()["inline_keyboard"]
                    self.assertTrue(cookie_rows[1][0]["text"].startswith("🍪 "))
                    separator = ": " if language == "en" else "："
                    self.assertIn(bot.admin_text("back") + separator, cookie_rows[-1][0]["text"])
                    self.assertIn(bot.admin_text("default_limit"), bot.admin_text("default_quota"))
                    if language in ("zh", "zh-cn"):
                        self.assertNotIn("Owner", bot.owner_help_text())
                service.stop()

    def test_stale_approval_never_changes_quota_or_creates_users(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            for quota in (75, 0, -1, None):
                store.set_quota(200, quota)
                before = store.path.read_bytes()
                with patch.object(store, "_save") as save:
                    self.assertFalse(store.approve(200))
                    self.assertFalse(store.approve(999))
                    save.assert_not_called()
                self.assertEqual(store.quota(200), quota)
                self.assertFalse(store.has_user(999))
                self.assertEqual(store.path.read_bytes(), before)
            service = bot.Bot(MagicMock(), store)
            store.set_quota(200, 75)
            service.handle_callback({"id": "stale", "data": "approve:200:0",
                                     "from": {"id": 100}, "message": {"message_id": 1, "chat": {"id": 100}}})
            self.assertEqual(store.quota(200), 75)
            service.api.send_message.assert_not_called()
            service.api.answer_callback.assert_called_with("stale", bot.admin_text("approve_missing"), alert=True)
            service.stop()

    def test_stale_batch_rejects_unseen_and_renewed_applications(self):
        for change in ("approved", "renewed", "legacy"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                store = bot.ACLStore(Path(directory) / "acl.json", 100)
                for user_id in range(200, 225):
                    store.observe({"id": user_id})
                    store.request_access(user_id)
                callback = bot.pending_keyboard(store.pending())["inline_keyboard"][-3][0]["callback_data"]
                self.assertLessEqual(len(callback.encode()), 64)
                if change == "approved":
                    for user_id in range(200, 220):
                        store.approve(user_id)
                elif change == "renewed":
                    store.deny(200)
                    store.request_access(200)
                else:
                    callback = "approvepage:0"
                before = (store.export_access(), store.pending())
                service = bot.Bot(MagicMock(), store)
                service.handle_callback({"id": "stale", "data": callback, "from": {"id": 100},
                                         "message": {"message_id": 1, "chat": {"id": 100}}})
                self.assertEqual((store.export_access(), store.pending()), before)
                service.api.send_message.assert_not_called()
                service.api.answer_callback.assert_called_with("stale", bot.admin_text("requests_changed"), alert=True)
                service.stop()

    def test_leaving_input_clears_all_modes_without_changing_access(self):
        transitions = ("/start", "/cancel", "nav:main", "nav:users", "public:language", "lang:en",
                       "statusrefresh:0", "userspage:0", "requestspage:0", "quotamenu:300")
        for actor in (100, 200):
            for transition in transitions:
                with self.subTest(actor=actor, transition=transition), tempfile.TemporaryDirectory() as directory:
                    store = bot.ACLStore(Path(directory) / "acl.json", 100)
                    store.set_quota(200, None)
                    service = bot.Bot(MagicMock(), store)
                    service.pending_cookie_uploads.add(actor)
                    service.pending_user_searches.add(actor)
                    service.pending_default_quotas[actor] = None
                    service.pending_bulk_quotas[actor] = None
                    if transition.startswith("/"):
                        service.handle_update({"message": {"message_id": 1, "chat": {"id": actor},
                                                           "from": {"id": actor}, "text": transition}})
                    else:
                        service.handle_callback({"id": "leave", "data": transition, "from": {"id": actor},
                                                 "message": {"message_id": 1, "chat": {"id": actor}}})
                    self.assertNotIn(actor, service.pending_cookie_uploads)
                    self.assertNotIn(actor, service.pending_user_searches)
                    self.assertNotIn(actor, service.pending_default_quotas)
                    self.assertNotIn(actor, service.pending_bulk_quotas)
                    self.assertIsNone(store.quota(200))
                    self.assertEqual(store.owner_id, 100)
                    service.stop()

    def test_cookie_failure_notification_is_serialized_and_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            service = bot.Bot(MagicMock(), store)
            path = Path(directory) / "cookie-alert.json"
            due, record = bot.cookie_alert_due, bot.record_cookie_alert
            gate = bot.threading.Barrier(4)
            def notify():
                gate.wait(timeout=5)
                service.notify_cookie_failure()
            with patch.object(bot, "cookie_alert_due", side_effect=lambda: due(path)), patch.object(
                bot, "record_cookie_alert", side_effect=lambda: record(path)
            ):
                workers = [bot.threading.Thread(target=notify) for _ in range(4)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
                service.api.send_message.assert_called_once()
                path.unlink()
                service.api.send_message.side_effect = RuntimeError("send failed")
                with self.assertLogs(bot.LOG, level="ERROR"):
                    service.notify_cookie_failure()
                self.assertFalse(path.exists())
                service.api.send_message.side_effect = None
                service.notify_cookie_failure()
                self.assertTrue(path.exists())
                self.assertFalse(list(Path(directory).glob("*.tmp")))
            service.stop()

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
            with patch.object(bot, "load_update_offset", return_value=0), patch.object(bot, "save_update_offset") as save, patch.object(bot, "cleanup_stale_media"):
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

    def test_permanent_delivery_failure_discards_pending_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bot.ACLStore(Path(directory) / "acl.json", 100)
            job = (100, 42, 100, "https://x.com/test/status/123")
            self.assertTrue(store.consume(100, job=job)[0])
            api = MagicMock()
            api.send_message.side_effect = bot.TelegramAPIError("sendMessage", 403)
            service = bot.Bot(api, store)
            service.process_url = MagicMock(side_effect=RuntimeError("delivery failed"))
            service.workers[0].start()
            service.jobs.join()
            service.stop()
            service.workers[0].join(timeout=2)
            self.assertFalse(store.data["pending_jobs"])

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
    @unittest.skipUnless(os.name != "nt" and getattr(os, "geteuid", lambda: -1)() == 0
                         and Path("/run/systemd/system").exists(), "requires Linux root and systemd")
    def test_environment_parser_and_writer_match_systemd(self):
        text = "A=Owner's contact\nB='line  \nnext'\nC=\"one\\q\\$\\`\"\nD=one\\\n  two\nE=one\\  \nF=\"tail  \"\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.env"
            path.write_text(text, encoding="utf-8")
            with patch.object(config_cli, "ENV_PATH", path):
                expected = config_cli.load_env()
                for saved in (False, True):
                    if saved:
                        config_cli.save_env(expected)
                    result = subprocess.run([
                        "systemd-run", "--quiet", "--wait", "--pipe", "--collect",
                        f"--property=EnvironmentFile={path}", sys.executable, "-c",
                        "import json,os; print(json.dumps({k:os.environ[k] for k in 'ABCDEF'}))",
                    ], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), expected)

    def test_environment_file_literal_quotes_continuations_and_multiline_values(self):
        text = "OWNER_CONTACT_LABEL=Owner's contact\nLITERAL=one\"two\nJOIN=one\\\n  two\nSINGLE='line  \nnext\\value'\nDOUBLE=\"line\\\nnext\\q\\$\\`\"\nTRAIL=one  \nQUOTED=\"one  \" \n"
        expected = {"OWNER_CONTACT_LABEL": "Owner's contact", "LITERAL": 'one"two',
                    "JOIN": "one  two", "SINGLE": "line  \nnext\\value",
                    "DOUBLE": "linenext\\q$`", "TRAIL": "one", "QUOTED": "one  "}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.env"
            path.write_text(text, encoding="utf-8")
            with patch.object(config_cli, "ENV_PATH", path):
                self.assertEqual(config_cli.load_env(), expected)
                config_cli.save_env(expected)
                self.assertEqual(config_cli.load_env(), expected)
                for broken in ("X='unfinished", "X=unfinished\\"):
                    path.write_text(broken, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        config_cli.load_env()

    def test_env_quotes_spaces_and_special_values_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.env"
            path.write_text(' # comment\n; comment\n DEFAULT_DAILY_LIMIT = "100" \nOWNER_USER_ID=\'200\'\nOWNER_CONTACT_LABEL=Two  words # literal\n', encoding="utf-8")
            with patch.object(config_cli, "ENV_PATH", path):
                values = config_cli.load_env()
                self.assertEqual(config_cli.default_limit_status(values, {}), (100, "env"))
                self.assertEqual(values["OWNER_CONTACT_LABEL"], "Two  words # literal")
                with patch.object(config_cli, "ACLStore") as store:
                    config_cli.acl_store(values)
                    store.assert_called_once_with(config_cli.STATE_PATH, 200)
                values["OWNER_CONTACT_LABEL"] = 'Owner\'s "contact" \\ example'
                config_cli.save_env(values)
                self.assertEqual(config_cli.load_env(), values)

    def test_status_returns_failure_for_an_inactive_service(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(config_cli.os, "geteuid", return_value=0, create=True), patch.object(config_cli.sys, "argv", ["config", "status"]), patch.object(config_cli, "load_env", return_value={}), patch.object(config_cli, "STATE_PATH", Path(temporary) / "acl.json"), patch.object(config_cli, "COOKIE_PATH", Path(temporary) / "cookies.txt"), patch("builtins.print"):
            for code in (0, 3, 4):
                with patch.object(config_cli.subprocess, "run", return_value=subprocess.CompletedProcess([], code)):
                    self.assertEqual(config_cli.main(), code)

    def test_verify_rejects_inactive_service_and_zero_pid(self):
        bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
        if not bash:
            self.skipTest("Requires bash")
        script = (Path(__file__).parent / "verify_deploy.sh").read_text(encoding="utf-8")
        check = script.split('echo "PROCESS_NETWORK"\n', 1)[1]
        for active, pid, expected in ((0, 42, 0), (1, 0, 1), (0, 0, 1), (1, 42, 1)):
            shell = (f'SERVICE=test; text_rc=0; media_rc=0; state_rc=0\n'
                     f'systemctl() {{ if [[ $1 == show ]]; then echo {pid}; else return {active}; fi; }}\n'
                     'ss() { return 0; }\nx-tweet-bot-config() { return 0; }\n' + check)
            result = subprocess.run([bash, "-s"], input=shell.encode(), capture_output=True)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_deployment_text_check_accepts_escaped_and_truncated_author_names(self):
        script = (Path(__file__).parent / "verify_deploy.sh").read_text(encoding="utf-8")
        text_check = script.split('echo "TEXT_TEST"\n', 1)[1].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        for name in ("A & <B>", 'Author "Name"', "A" * 2000):
            with self.subTest(name=name[:20]), patch.object(bot, "fetch_fxtwitter", return_value={
                "text": "Test post", "author": {"name": name, "screen_name": "example"}
            }), patch.object(sys, "argv", ["verify", "https://x.com/example/status/123"]), patch("builtins.print"):
                exec(compile(text_check, "verify_deploy.sh:TEXT_TEST", "exec"), {})

    def test_deployment_timezone_and_reset_read_quoted_values(self):
        bash = shutil.which("bash")
        if os.name == "nt" and Path("C:/Program Files/Git/bin/bash.exe").exists():
            bash = "C:/Program Files/Git/bin/bash.exe"
        if not bash:
            self.skipTest("Requires bash")
        script = (Path(__file__).parent / "verify_deploy.sh").read_text(encoding="utf-8")
        header = script.split('if [[ -n $TEST_URL ]]; then', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.env"
            for quote in ('"', "'", ""):
                with self.subTest(quote=quote):
                    path.write_text(f"  BOT_TIMEZONE = {quote}Asia/Tokyo{quote}  \n  DAILY_RESET_HOUR = {quote}6{quote}  \n", encoding="utf-8")
                    shell = header.replace("/etc/x-tweet-telegram-bot.env", path.as_posix())
                    shell += '\nprintf "%s\\n" "$BOT_TIMEZONE" "$DAILY_RESET_HOUR"\n'
                    result = subprocess.run([bash, "-s"], input=shell.encode(), capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                    self.assertEqual(result.stdout.decode().splitlines(), ["Asia/Tokyo", "6"])
        self.assertIn('DAILY_RESET_HOUR="$DAILY_RESET_HOUR"', script)

    @unittest.skipIf(os.name == "nt", "Requires POSIX permissions and bash")
    def test_deployment_runtime_permissions_do_not_inherit_private_umask(self):
        script = (Path(__file__).parent / "deploy.sh").read_text(encoding="utf-8")
        provision = 'export UV_PYTHON_INSTALL_DIR' + script.split('export UV_PYTHON_INSTALL_DIR', 1)[1].split('\nPYTHONPATH=', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            shell = '''set -eu
umask 077
INSTALL_DIR=$1
SOURCE_DIR=$1
uv() {
  if [ "$1" = python ]; then mkdir -p "$UV_PYTHON_INSTALL_DIR/runtime"; fi
  if [ "$1" = venv ]; then mkdir -p "$4/bin" "$4/lib"; fi
}
''' + provision + '''
for path in "$UV_PYTHON_INSTALL_DIR/runtime" "$candidate_venv/bin" "$candidate_venv/lib"; do
  test "$(stat -c %a "$path")" = 755
done
test "$(umask)" = 0077
'''
            result = subprocess.run(["bash", "-s", "--", directory], input=shell, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(script.index('install -o root -g root -m 0600 /dev/null'), script.index('cat >/etc/x-tweet-telegram-bot.env'))
        self.assertLess(script.index('runuser -u x-tweet-bot'), script.index('systemctl stop "$SERVICE"\nfi'))

    def test_save_env_is_atomic_and_preserves_existing_values_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.env"
            path.write_text('BOT_TIMEZONE="Asia/Tokyo"\nWORKER_COUNT=4\n', encoding="utf-8")
            before = path.read_bytes()
            with patch.object(config_cli, "ENV_PATH", path):
                values = config_cli.load_env()
                for failed_operation in ("replace", "fsync"):
                    target = patch.object(Path, "replace", side_effect=OSError("interrupted")) if failed_operation == "replace" else patch.object(bot.os, "fsync", side_effect=OSError("interrupted"))
                    with target, self.assertRaises(OSError):
                        config_cli.save_env({**values, "WORKER_COUNT": "2"})
                    self.assertEqual(path.read_bytes(), before)
                    self.assertFalse(list(Path(directory).glob("*.tmp")))
                config_cli.save_env(values)
                self.assertEqual(config_cli.load_env(), values)
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cli_cookies_share_bot_validation_and_preserve_old_file_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination, alert = [Path(directory) / name for name in ("input.txt", "cookies.txt", "alert.json")]
            destination.write_text("previous", encoding="utf-8")
            alert.write_text("previous-alert", encoding="utf-8")
            good = b"\xef\xbb\xbf# Netscape HTTP Cookie File\r\n.x.com\tTRUE\t/\tTRUE\t0\tauth_token\tYOUR_COOKIE_VALUE\r\n"
            cases = (b"", b"# Netscape HTTP Cookie File\n", b"# Netscape HTTP Cookie File\n\xff", b"x" * (bot.MAX_COOKIE_BYTES + 1), good)
            for content in cases:
                source.write_bytes(content)
                with patch.object(config_cli.os, "geteuid", return_value=0, create=True), patch.object(config_cli.sys, "argv", ["config", "set-cookies", str(source)]), patch.object(config_cli, "load_env", return_value={}), patch.object(config_cli, "COOKIE_PATH", destination), patch.object(config_cli, "COOKIE_ALERT_PATH", alert), patch.object(config_cli.shutil, "chown", create=True) as chown, patch.object(config_cli, "restart") as restart:
                    if content != good:
                        with self.assertRaises(SystemExit):
                            config_cli.main()
                        self.assertEqual(destination.read_text(), "previous")
                        self.assertTrue(alert.exists())
                        chown.assert_not_called()
                        restart.assert_not_called()
                    else:
                        self.assertEqual(config_cli.main(), 0)
                        self.assertEqual(destination.read_text(), bot.validate_cookie_file(content))
                        self.assertFalse(alert.exists())
                        chown.assert_called_once_with(destination, user="x-tweet-bot", group="x-tweet-bot")
                        restart.assert_called_once()
                        if os.name != "nt":
                            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_status_reports_effective_default_and_source_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            for state, expected in (({"users": {}, "default_daily_limit": 100}, "default_daily_limit=100 source=acl"), ({"users": {}}, "default_daily_limit=75 source=env")):
                path.write_text(json.dumps(state), encoding="utf-8")
                before = path.read_bytes()
                with patch.object(config_cli.os, "geteuid", return_value=0, create=True), patch.object(config_cli.sys, "argv", ["config", "status"]), patch.object(config_cli, "load_env", return_value={"DEFAULT_DAILY_LIMIT": "75"}), patch.object(config_cli, "STATE_PATH", path), patch.object(config_cli, "COOKIE_PATH", Path(temporary) / "cookies.txt"), patch.object(config_cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), patch("builtins.print") as output:
                    self.assertEqual(config_cli.main(), 0)
                    output.assert_any_call(expected)
                self.assertEqual(path.read_bytes(), before)
            for invalid in (0, 100001, True, "100", None):
                path.write_text(json.dumps({"default_daily_limit": invalid}), encoding="utf-8")
                before = path.read_bytes()
                with patch.object(config_cli.os, "geteuid", return_value=0, create=True), patch.object(config_cli.sys, "argv", ["config", "status"]), patch.object(config_cli, "load_env", return_value={}), patch.object(config_cli, "STATE_PATH", path), patch.object(config_cli, "COOKIE_PATH", Path(temporary) / "cookies.txt"), patch("builtins.print") as output:
                    self.assertEqual(config_cli.main(), 1)
                    output.assert_any_call("state=invalid")
                self.assertEqual(path.read_bytes(), before)

    def test_export_uses_configured_environment_default_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            bot.ACLStore(path, 100)
            before = path.read_bytes()
            with patch.object(config_cli.os, "geteuid", return_value=0, create=True), patch.object(config_cli.sys, "argv", ["config", "export-access"]), patch.object(config_cli, "load_env", return_value={"DEFAULT_DAILY_LIMIT": "75"}), patch.object(config_cli, "STATE_PATH", path), patch.object(config_cli, "APP_DIR", Path(__file__).parent), patch("builtins.print") as output:
                self.assertEqual(config_cli.main(), 0)
                snapshot = json.loads(output.call_args.args[0])
            self.assertEqual(snapshot["default_daily_limit"], 75)
            self.assertEqual(snapshot["default_daily_limit_updated_at"], 0)
            self.assertEqual(path.read_bytes(), before)

    def test_verify_deploy_checks_default_limit_in_primary_and_backup(self):
        script = (Path(__file__).parent / "verify_deploy.sh").read_text(encoding="utf-8")
        validator = script.split('echo "STATE_JSON"\n', 1)[1].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            base = {"users": {}, "pending_applications": {}}
            cases = [({}, 0), ({"default_daily_limit": 1}, 0), ({"default_daily_limit": 100000, "default_daily_limit_updated_at": 0}, 0)]
            cases += [({"default_daily_limit": value}, 1) for value in (0, 100001, True, "100", None)]
            cases += [({"default_daily_limit": 100, "default_daily_limit_updated_at": value}, 1) for value in (-1, True, "1", None)]
            cases += [({"default_daily_limit_updated_at": 1}, 1)]
            for values, code in cases:
                path.write_text(json.dumps({**base, **values}), encoding="utf-8")
                before = path.read_bytes()
                result = subprocess.run([sys.executable, "-B", "-"], input=validator, capture_output=True, text=True, env={**os.environ, "STATE_DIR": temporary, "PYTHONPATH": str(Path(__file__).parent)})
                self.assertEqual(result.returncode, code, values)
                self.assertEqual(path.read_bytes(), before)
            path.write_text(json.dumps(base), encoding="utf-8")
            path.with_name("acl.json.bak").write_text(json.dumps({**base, "default_daily_limit": 0}), encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", "-"], input=validator, capture_output=True, text=True, env={**os.environ, "STATE_DIR": temporary, "PYTHONPATH": str(Path(__file__).parent)})
            self.assertEqual(result.returncode, 1)

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

    def test_unavailable_sources_log_expected_status_without_tracebacks(self):
        url = "https://x.com/NASA/status/123"
        for fetch, status, expected in (
            (bot.fetch_tweet_text, 403, ("", "", "")),
            (bot.fetch_fxtwitter, 404, None),
        ):
            with self.subTest(fetch=fetch.__name__), patch.object(
                bot, "http_session"
            ) as session, patch.object(bot.LOG, "info") as info, patch.object(
                bot.LOG, "exception"
            ) as exception:
                response = session.return_value.get.return_value
                response.status_code = status
                response.raise_for_status.side_effect = bot.requests.HTTPError(response=response)
                self.assertEqual(fetch(url), expected)
                info.assert_called_once()
                exception.assert_not_called()

    def test_unexpected_source_http_failure_keeps_error_traceback(self):
        response = MagicMock(status_code=500)
        response.raise_for_status.side_effect = bot.requests.HTTPError(response=response)
        with patch.object(bot, "http_session") as session, patch.object(
            bot.LOG, "exception"
        ) as exception:
            session.return_value.get.return_value = response
            self.assertEqual(bot.fetch_tweet_text("https://x.com/NASA/status/123"), ("", "", ""))
            exception.assert_called_once_with("oEmbed text extraction failed")

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

    def test_caption_without_text_or_author_shows_url_once(self):
        url = "https://x.com/example/status/123"
        self.assertEqual(bot.tweet_html("", "", "", url, 1024), url)
        self.assertEqual(
            bot.tweet_html("", "", "", url, 1024, "No media"),
            url + "\n\nNo media",
        )


class ACLTests(unittest.TestCase):
    def test_invalid_empty_containers_recover_valid_backup_or_refuse_overwrite(self):
        for key in ("users", "pending_applications"):
            for invalid in ([], None, False, ""):
                with self.subTest(key=key, invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "acl.json"
                    store = bot.ACLStore(path, 100)
                    store.set_quota(200, 75)
                    good = path.read_bytes()
                    store.backup_path.write_bytes(good)
                    bad = json.loads(good)
                    bad[key] = invalid
                    path.write_text(json.dumps(bad), encoding="utf-8")
                    with self.assertLogs(bot.LOG, level="ERROR"):
                        recovered = bot.ACLStore(path, 100)
                    self.assertEqual(recovered.quota(200), 75)
                    self.assertEqual(store.backup_path.read_bytes(), good)
                    path.write_text(json.dumps(bad), encoding="utf-8")
                    store.backup_path.write_bytes(path.read_bytes())
                    before = path.read_bytes()
                    with self.assertLogs(bot.LOG, level="ERROR"), self.assertRaises(RuntimeError):
                        bot.ACLStore(path, 100)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(store.backup_path.read_bytes(), before)

    def test_save_does_not_replace_good_backup_with_bad_empty_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            good = path.read_bytes()
            store.backup_path.write_bytes(good)
            path.write_text('{"users": [], "pending_applications": {}}', encoding="utf-8")
            with self.assertLogs(bot.LOG, level="ERROR"):
                store.observe({"id": 200})
            self.assertEqual(store.backup_path.read_bytes(), good)

    def test_default_limit_preserves_existing_users_and_applies_to_new_users(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            for user_id, quota in ((200, 50), (201, 100), (202, 1500), (300, None), (400, 0), (500, -1)):
                store.set_quota(user_id, quota)
            store.observe({"id": 600, "first_name": "Pending"})
            store.request_access(600)
            store.consume(200)
            before = json.loads(json.dumps(store.data))
            store.set_default_daily_limit(100, 100)
            self.assertEqual([store.quota(user_id) for user_id in (200, 201, 202)], [50, 100, 1500])
            self.assertEqual(store.data["users"], before["users"])
            self.assertEqual(store.data["pending_applications"], before["pending_applications"])
            for field in ("usage_date", "usage_count"):
                self.assertEqual(store.data["users"]["200"][field], before["users"]["200"][field])
            for field in ("external_access_enabled", "ordinary_user_cookies_enabled", "auto_approve_enabled", "pending_jobs", "last_daily_report_date"):
                self.assertEqual(store.data[field], before[field])
            with patch.object(bot, "DEFAULT_DAILY_LIMIT", 75):
                store = bot.ACLStore(path, 100)
                self.assertEqual(store.default_daily_limit, 100)
            store.approve(600)
            store.toggle_auto_approve(100)
            self.assertEqual(store.request_access(700), "auto_approved")
            store.ensure_managed_user(800)
            store.ensure_managed_user(801, None)
            store.add(900)
            self.assertEqual([store.quota(user_id) for user_id in (600, 700, 800, 900)], [100] * 4)
            self.assertTrue(store.is_admin(801))
            self.assertEqual(store.export_access()["default_daily_limit"], 100)
            self.assertEqual(set(store.export_access()), {"version", "users", "default_daily_limit", "default_daily_limit_updated_at"})
            store.set_quota(250, 50)
            store.set_default_daily_limit(100, 200)
            self.assertEqual([store.quota(user_id) for user_id in (200, 201, 600, 700, 800, 900)], [50, 100, 100, 100, 100, 100])
            self.assertEqual([store.quota(user_id) for user_id in (202, 250)], [1500, 50])
            before_repeat = json.loads(json.dumps(store.data))
            store.set_default_daily_limit(100, 200)
            self.assertEqual(store.data, before_repeat)
            self.assertEqual(bot.ACLStore(path, 100).default_daily_limit, 200)

    def test_bulk_quota_updates_only_matching_regular_users_and_preserves_other_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            for user_id, quota in ((200, 50), (201, 50), (202, 1500), (300, None), (400, 0), (500, -1)):
                store.set_quota(user_id, quota)
            store.request_access(600)
            store.observe({"id": 200, "first_name": "Example", "username": "example_user"})
            store.set_language(200, "ja")
            store.consume(200)
            store.set_default_daily_limit(100, 777)
            before = json.loads(json.dumps(store.data))
            targets = store.bulk_quota_targets(50)
            self.assertEqual(set(targets), {"200", "201"})
            self.assertEqual(store.bulk_set_quota(100, 50, 100, targets), 2)
            for key, record in before["users"].items():
                actual = store.data["users"][key]
                if key in targets:
                    self.assertEqual(actual, {**record, "quota": 100, "quota_updated_at": actual["quota_updated_at"]})
                    self.assertGreater(actual["quota_updated_at"], record["quota_updated_at"])
                else:
                    self.assertEqual(actual, record)
            for key in before.keys() - {"users"}:
                self.assertEqual(store.data[key], before[key])
            reloaded = bot.ACLStore(path, 100)
            normalized = json.loads(json.dumps(store.data))
            normalized["users"]["100"].setdefault("debug_mode", False)
            self.assertEqual(reloaded.data, normalized)
            reloaded.add(700)
            self.assertEqual(reloaded.quota(700), 777)
            self.assertEqual(reloaded.export_access()["default_daily_limit"], 777)

    def test_bulk_quota_rejects_invalid_values_unauthorized_and_changed_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 50)
            store.set_quota(300, None)
            expected = store.bulk_quota_targets(50)
            before = store.path.read_bytes()
            cases = ((300, 50, 100), (400, 50, 100), (100, 0, 100), (100, -1, 100),
                     (100, 50, 0), (100, 50, -1), (100, 100001, 100), (100, 50, 100001),
                     (100, True, 100), (100, 50, True), (100, "50", 100), (100, 50, "100"), (100, 50, 50))
            for actor, source, target in cases:
                with self.subTest(actor=actor, source=source, target=target), self.assertRaises(ValueError):
                    store.bulk_set_quota(actor, source, target, expected)
                self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(store.bulk_set_quota(100, 999, bot.MAX_DAILY_LIMIT, {}), 0)
            self.assertEqual(store.path.read_bytes(), before)
        for change in ("added", "different", "renewed", "blocked", "administrator"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_quota(200, 50)
                expected = store.bulk_quota_targets(50)
                store.set_quota(201 if change == "added" else 200,
                                {"added": 50, "different": 75, "renewed": 50, "blocked": -1, "administrator": None}[change])
                before = store.path.read_bytes()
                with self.assertRaises(ValueError):
                    store.bulk_set_quota(100, 50, 100, expected)
                self.assertEqual(store.path.read_bytes(), before)

    def test_default_limit_rejects_invalid_values_and_non_owner_without_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            store.set_quota(200, None)
            before = path.read_bytes()
            for actor, value in ((200, 100), (300, 100), (100, -1), (100, 0), (100, 100001), (100, True), (100, "100")):
                with self.subTest(actor=actor, value=value), self.assertRaises(ValueError):
                    store.set_default_daily_limit(actor, value)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(store.default_daily_limit, bot.DEFAULT_DAILY_LIMIT)
            store.set_default_daily_limit(100, bot.MAX_DAILY_LIMIT)
            self.assertEqual(bot.ACLStore(path, 100).default_daily_limit, bot.MAX_DAILY_LIMIT)

    def test_default_limit_falls_back_to_environment_and_preserves_usage_when_lowered(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bot, "DEFAULT_DAILY_LIMIT", 75):
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            self.assertNotIn("default_daily_limit", store.data)
            self.assertEqual(store.default_daily_limit, 75)
            store.observe({"id": 200, "first_name": "Example", "username": "example"})
            store.set_language(200, "ja")
            store.add(200)
            self.assertEqual(store.quota(200), 75)
            for _ in range(3):
                store.consume(200)
            before = dict(store.data["users"]["200"])
            store.set_default_daily_limit(100, 1)
            self.assertEqual(store.data["users"]["200"], before)
            store.bulk_set_quota(100, 75, 1, store.bulk_quota_targets(75))
            self.assertEqual(store.consume(200), (False, 3, 1))
            self.assertEqual(store.data["users"]["200"], {**before, "quota": 1, "quota_updated_at": store.data["users"]["200"]["quota_updated_at"]})

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

    def test_access_snapshot_restores_default_without_batch_updates_and_rejects_stale_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = bot.ACLStore(Path(temporary) / "source.json", 100)
            target = bot.ACLStore(Path(temporary) / "target.json", 100)
            source.set_default_daily_limit(100, 100)
            snapshot = source.export_access()
            target.set_quota(200, 50)
            target.set_quota(201, 150)
            target.consume(200)
            before = json.loads(json.dumps(target.data))
            self.assertEqual(target.import_access(snapshot), 0)
            self.assertEqual(target.data["users"], before["users"])
            self.assertEqual(bot.ACLStore(target.path, 100).default_daily_limit, 100)
            target.add(202)
            self.assertEqual(target.quota(202), 100)
            target.set_default_daily_limit(100, 200)
            self.assertEqual(target.import_access(snapshot), 0)
            self.assertEqual(target.default_daily_limit, 200)
            legacy = {"version": 1, "users": []}
            target.import_access(legacy)
            self.assertEqual(target.default_daily_limit, 200)
            fresh = bot.ACLStore(Path(temporary) / "fresh.json", 100)
            fresh.import_access(bot.ACLStore.read_access_snapshot(target.path))
            self.assertEqual(fresh.default_daily_limit, 200)
            self.assertEqual(fresh.quota(201), 150)

    def test_access_snapshot_rejects_invalid_defaults_before_any_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 50)
            before = store.path.read_bytes()
            cases = [{"default_daily_limit": value} for value in (0, 100001, True, "100", None)]
            cases += [{"default_daily_limit": 100, "default_daily_limit_updated_at": value} for value in (-1, True, "1", None)]
            cases += [{"default_daily_limit_updated_at": 1}]
            for values in cases:
                with self.assertRaises(ValueError):
                    store.import_access({"version": 1, "users": [{"user_id": 200, "quota": 100, "updated_at": 2**63}], **values})
                self.assertEqual(store.path.read_bytes(), before)
                self.assertEqual(store.quota(200), 50)
                self.assertNotIn("default_daily_limit", store.data)

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
                    {"user_id": 300, "quota": 100001, "updated_at": 2},
                ],
            }
            with self.assertRaises(ValueError):
                store.import_access(snapshot)
            self.assertFalse(store.has_user(200))

    def test_maximum_daily_limit_survives_access_export_and_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = bot.ACLStore(Path(temporary) / "source.json", 100)
            target = bot.ACLStore(Path(temporary) / "target.json", 100)
            source.set_quota(200, bot.MAX_DAILY_LIMIT)
            self.assertEqual(target.import_access(source.export_access()), 1)
            self.assertEqual(target.quota(200), bot.MAX_DAILY_LIMIT)

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
            self.assertIn(f"每日使用簡報（{bot.bot_date(report_time)}）", text)
            self.assertNotIn(bot.BOT_TIMEZONE_NAME, text)
            self.assertIn("活躍用戶：1", text)
            self.assertIn("用量：1", text)
            self.assertIn("待審批：0", text)
            self.assertIsNone(api.send_message.call_args.kwargs["reply_markup"])
            service.stop()

    def test_midnight_report_keeps_completed_day_after_new_usage_and_restart(self):
        boundary = datetime(2026, 10, 7, tzinfo=bot.timezone.utc).timestamp()
        for language in bot.PUBLIC_TEXT:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary, \
                 patch.object(bot, "BOT_TIMEZONE", bot.timezone.utc), \
                 patch.object(bot, "DAILY_RESET_HOUR", 0), patch.object(bot, "DAILY_REPORT_HOUR", 0):
                path = Path(temporary) / "acl.json"
                store = bot.ACLStore(path, 100)
                store.set_language(100, language)
                store.set_quota(200, 100)
                for _ in range(8):
                    store.consume(200, now=boundary - 1)
                store.consume(200, now=boundary + 1)
                reloaded = bot.ACLStore(path, 100)
                self.assertEqual(reloaded.usage_summary("2026-10-06"), (1, 8))
                self.assertEqual(reloaded.usage_summary("2026-10-07"), (1, 1))
                api = MagicMock()
                service = bot.Bot(api, reloaded)
                with patch.object(bot.time, "time", return_value=boundary + 30):
                    service.maybe_send_daily_report()
                    service.maybe_send_daily_report()
                api.send_message.assert_called_once()
                text = api.send_message.call_args.args[1]
                self.assertIn("2026-10-06", text.splitlines()[0])
                self.assertIn({"zh": "用量：8", "zh-cn": "用量：8", "en": "Usage: 8", "ja": "使用量：8"}[language], text)
                self.assertEqual(reloaded.quota(200), 100)
                self.assertEqual(reloaded.data["users"]["200"]["usage_count"], 1)
                service.stop()
                service.inline_executor.shutdown(wait=True)

    def test_report_migration_skips_incomplete_past_day_and_keeps_empty_days(self):
        boundary = datetime(2026, 10, 7, tzinfo=bot.timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as temporary, patch.object(bot, "BOT_TIMEZONE", bot.timezone.utc), \
             patch.object(bot, "DAILY_RESET_HOUR", 0), patch.object(bot, "DAILY_REPORT_HOUR", 0):
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, 100)
            for _ in range(4):
                store.consume(200, now=boundary + 1)
            store.data["daily_usage"] = {}  # Old versions did not retain per-day totals.
            api = MagicMock()
            service = bot.Bot(api, store)
            with patch.object(bot.time, "time", return_value=boundary + 30):
                service.maybe_send_daily_report()
            api.send_message.assert_not_called()
            self.assertEqual(store.usage_summary("2026-10-07"), (1, 4))
            with patch.object(bot.time, "time", return_value=boundary + 86430):
                service.maybe_send_daily_report()
            self.assertIn("2026-10-07", api.send_message.call_args.args[1])
            self.assertIn("用量：4", api.send_message.call_args.args[1])
            with patch.object(bot.time, "time", return_value=boundary + 2 * 86400 + 30):
                service.maybe_send_daily_report()
            self.assertIn("2026-10-08", api.send_message.call_args.args[1])
            self.assertIn("用量：0", api.send_message.call_args.args[1])
            self.assertIsNone(api.send_message.call_args.kwargs["reply_markup"])
            service.stop()
            service.inline_executor.shutdown(wait=True)

    def test_daily_usage_history_is_bounded_and_export_does_not_include_it(self):
        first_day = datetime(2026, 10, 1, tzinfo=bot.timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as temporary, patch.object(bot, "BOT_TIMEZONE", bot.timezone.utc), patch.object(bot, "DAILY_RESET_HOUR", 0):
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            for offset in range(10):
                store.consume(100, now=first_day + offset * 86400)
            self.assertEqual(set(store.data["daily_usage"]), {"2026-10-08", "2026-10-09", "2026-10-10"})
            self.assertNotIn("daily_usage", store.export_access())
            self.assertEqual(bot.ACLStore(path, 100).usage_summary("2026-10-09"), (1, 1))

    def test_daily_usage_history_rejects_invalid_dates_counts_and_unbounded_entries(self):
        invalid = [[], {"invalid": {"active": 0, "total": 0}}, {"2026-02-30": {"active": 0, "total": 0}},
                   {"2026-10-07": {"active": True, "total": 1}}, {"2026-10-07": {"active": 2, "total": 1}},
                   {"2026-10-07": {"active": 0, "total": -1}},
                   {f"2026-10-0{day}": {"active": 0, "total": 0} for day in range(1, 5)}]
        for history in invalid:
            with self.subTest(history=history), self.assertRaises(ValueError):
                bot.ACLStore.__new__(bot.ACLStore)._apply_state({"daily_usage": history}, 0)

    def test_report_at_custom_reset_hour_uses_completed_quota_day(self):
        zone = bot.timezone(bot.timedelta(hours=9))
        boundary = datetime(2026, 10, 7, 6, tzinfo=zone).timestamp()
        with tempfile.TemporaryDirectory() as temporary, patch.object(bot, "BOT_TIMEZONE", zone), \
             patch.object(bot, "DAILY_RESET_HOUR", 6), patch.object(bot, "DAILY_REPORT_HOUR", 6):
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.consume(100, now=boundary - 1)
            store.consume(100, now=boundary + 1)
            api = MagicMock()
            service = bot.Bot(api, store)
            with patch.object(bot.time, "time", return_value=boundary + 30):
                service.maybe_send_daily_report()
            self.assertIn("2026-10-06", api.send_message.call_args.args[1])
            self.assertEqual(store.usage_summary("2026-10-07"), (1, 1))
            service.stop()
            service.inline_executor.shutdown(wait=True)

    def test_daily_report_title_has_date_without_timezone_in_all_languages(self):
        for language in bot.PUBLIC_TEXT:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary:
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_language(100, language)
                api = MagicMock()
                service = bot.Bot(api, store)
                service.maybe_send_daily_report(force=True)
                title = api.send_message.call_args.args[1].splitlines()[0]
                self.assertIn(bot.bot_date(), title)
                self.assertNotIn(bot.BOT_TIMEZONE_NAME, title)
                self.assertNotIn("Asia/Tokyo", title)
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
    def test_status_counts_are_separate_and_advanced_pages_do_not_repeat_statistics(self):
        for language in bot.PUBLIC_TEXT:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as directory, bot.language_scope(language):
                store = bot.ACLStore(Path(directory) / "acl.json", 100)
                store.set_language(100, language)
                store.add(200)
                api = MagicMock()
                service = bot.Bot(api, store)
                status = service.system_status_text(100)
                self.assertNotIn(bot.BOT_TIMEZONE_NAME, status)
                self.assertNotIn("00:00", status)
                self.assertNotIn("22:00", status)
                for label in ("處理佇列", "Queue:", "待機列", "处理队列"):
                    self.assertNotIn(label, status)
                counts = bot.admin_text("status_users").format(
                    total=2, ordinary=1, administrators=0, initialized=0, pending=0,
                    banned=0, active_today=0, interactions=0, exhausted=0,
                ).splitlines()[1:]
                self.assertEqual(len(counts), 9)
                for line in counts:
                    self.assertIn(line, status.splitlines())
                    self.assertNotIn("｜", line)
                    self.assertNotIn(" | ", line)
                for detail in ("Cookies", bot.admin_text("access_switch"), bot.admin_text("auto_approve"), bot.admin_text("implementation")):
                    self.assertNotIn(detail, status)
                for action in ("nav:advanced", "debugtoggle:0", "externaltoggle:0", "autoapprovetoggle:0", "nav:cookies", "nav:cookiehelp", "nav:usercontrols", "nav:quotamanagement", "nav:defaultquota", "nav:bulkquota"):
                    api.reset_mock()
                    service.handle_callback({
                        "id": "page", "data": action, "from": {"id": 100},
                        "message": {"message_id": 1, "chat": {"id": 100}},
                    })
                    text = api.edit_message.call_args.args[2]
                    for line in counts:
                        self.assertNotIn(line, text)
                    if action in {"nav:advanced", "debugtoggle:0"}:
                        self.assertEqual(text, bot.admin_text("advanced"))
                        self.assertIn("nav:status", str(api.edit_message.call_args.args[3]))
                    elif action in {"nav:usercontrols", "externaltoggle:0", "autoapprovetoggle:0"}:
                        self.assertEqual(text, bot.admin_text("user_controls"))
                        self.assertIn("nav:advanced", str(api.edit_message.call_args.args[3]))
                service.handle_callback({
                    "id": "back", "data": "nav:status", "from": {"id": 100},
                    "message": {"message_id": 1, "chat": {"id": 100}},
                })
                for line in counts:
                    self.assertIn(line, api.edit_message.call_args.args[2].splitlines())
                service.stop()
                service.inline_executor.shutdown(wait=True)

    def test_usage_labels_and_chinese_user_terms_are_consistent_in_four_languages(self):
        for language, usage_label, old_label in (("zh-cn", "用量", "处理次数"), ("zh", "用量", "處理次數"), ("ja", "使用量", "処理回数"), ("en", "Usage", "Processed")):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary, bot.language_scope(language):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_language(100, language)
                store.add(200)
                store.consume(200)
                api = MagicMock()
                service = bot.Bot(api, store)
                service.maybe_send_daily_report(force=True)
                report = api.send_message.call_args.args[1]
                status = service.system_status_text(100)
                page = service.users_page(store.records(), 0)[0]
                card = service.user_search_result(200, 100)[0]
                for text in (report, status, card):
                    self.assertIn(usage_label, text)
                    self.assertNotIn(old_label, text)
                    self.assertNotIn("使用者", text)
                self.assertIn("1/50", page)
                self.assertNotIn(old_label, page)
                self.assertNotIn("使用者", page)
                service.stop()
        self.assertTrue(all("使用者" not in values[0] for values in bot.ADMIN_TEXT.values()))

    def test_owner_default_limit_flow_is_localized_confirmed_and_cancellable(self):
        for language in ("zh-cn", "zh", "en", "ja"):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary, bot.language_scope(language):
                self.assertTrue(bot.quota_management_keyboard(50)["inline_keyboard"][0][0]["text"].startswith("🎯 "))
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_language(100, language)
                store.add(200)
                api = MagicMock()
                service = bot.Bot(api, store)
                def callback(data, chat_id=100):
                    service.handle_callback({"id": data, "data": data, "from": {"id": 100},
                                             "message": {"message_id": 1, "chat": {"id": chat_id}}})
                def message(text):
                    service.handle_update({"message": {"message_id": 2, "from": {"id": 100},
                                                       "chat": {"id": 100}, "text": text}})
                callback("nav:defaultquota")
                self.assertEqual(api.edit_message.call_args.args[2], bot.admin_text("default_limit_prompt").format(limit=50))
                for invalid in ("0", "-1", "100001", "1.5", "hello"):
                    message(invalid)
                    self.assertEqual(api.send_message.call_args.args[1], bot.admin_text("default_limit_range"))
                    self.assertEqual(store.quota(200), 50)
                message("100")
                self.assertEqual(api.send_message.call_args.args[1], bot.admin_text("default_limit_confirm").format(limit=100))
                self.assertEqual(store.quota(200), 50)
                callback("defaultquota:101")
                self.assertEqual(store.quota(200), 50)
                callback("defaultquota:100", -100)
                self.assertEqual(store.quota(200), 50)
                callback("nav:advanced")
                callback("defaultquota:100")
                self.assertEqual(store.quota(200), 50)
                callback("nav:defaultquota")
                message("100")
                callback("defaultquota:100")
                self.assertEqual(store.default_daily_limit, 100)
                self.assertEqual(store.quota(200), 50)
                self.assertEqual(api.edit_message.call_args.args[2], bot.admin_text("default_limit_changed").format(limit=100))
                self.assertIn("nav:defaultquota", str(api.edit_message.call_args.args[3]))
                self.assertIn("quota:200:100", str(service.user_search_result(200, 100)[1]))
                callback("defaultquota:100")
                api.answer_callback.assert_called_with("defaultquota:100", bot.admin_text("invalid_action"), alert=True)
                callback("nav:defaultquota")
                message("/start")
                self.assertNotIn(100, service.pending_default_quotas)
                service.stop()

    def test_quota_management_and_bulk_flow_are_localized_confirmed_and_cancellable(self):
        for language in bot.PUBLIC_TEXT:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary, bot.language_scope(language):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_language(100, language)
                for user_id, quota in ((200, 50), (201, 50), (202, 1500)):
                    store.set_quota(user_id, quota)
                store.consume(200)
                api = MagicMock()
                service = bot.Bot(api, store)
                def callback(data, chat_id=100):
                    service.handle_callback({"id": data, "data": data, "from": {"id": 100},
                                             "message": {"message_id": 1, "chat": {"id": chat_id}}})
                def message(text):
                    service.handle_update({"message": {"message_id": 2, "from": {"id": 100},
                                                       "chat": {"id": 100}, "text": text}})
                callback("nav:advanced")
                self.assertEqual([row[0]["callback_data"] for row in api.edit_message.call_args.args[3]["inline_keyboard"]],
                                 ["nav:cookies", "nav:usercontrols", "debugtoggle:0", "nav:status"])
                callback("nav:usercontrols")
                self.assertEqual(api.edit_message.call_args.args[2], bot.admin_text("user_controls"))
                self.assertEqual([row[0]["callback_data"] for row in api.edit_message.call_args.args[3]["inline_keyboard"]],
                                 ["externaltoggle:0", "autoapprovetoggle:0", "nav:quotamanagement", "nav:advanced"])
                callback("nav:quotamanagement")
                self.assertEqual(api.edit_message.call_args.args[2], bot.admin_text("quota_management"))
                self.assertEqual([row[0]["callback_data"] for row in api.edit_message.call_args.args[3]["inline_keyboard"]],
                                 ["nav:defaultquota", "nav:bulkquota", "nav:usercontrols"])
                callback("nav:bulkquota")
                self.assertEqual(api.edit_message.call_args.args[2], bot.admin_text("bulk_quota_prompt"))
                for invalid in ("50", "50 50", "0 100", "50 -1", "50 100001", "1.5 100", "５０ 100"):
                    message(invalid)
                    self.assertEqual(api.send_message.call_args.args[1], bot.admin_text("bulk_quota_range"))
                    self.assertEqual(store.quota(200), 50)
                message("50 100")
                self.assertEqual(api.send_message.call_args.args[1], bot.admin_text("bulk_quota_confirm").format(source=50, target=100, count=2))
                for invalid in ("bulkquota:50:101", "bulkquota:50", "bulkquota:50:100:extra"):
                    callback(invalid)
                    self.assertTrue(api.answer_callback.call_args.kwargs["alert"])
                    self.assertEqual(store.quota(200), 50)
                callback("bulkquota:50:100", -100)
                self.assertEqual(store.quota(200), 50)
                callback("nav:quotamanagement")
                callback("bulkquota:50:100")
                self.assertEqual(store.quota(200), 50)
                callback("nav:bulkquota")
                message("50 100")
                callback("bulkquota:50:100")
                self.assertEqual([store.quota(user_id) for user_id in (200, 201, 202)], [100, 100, 1500])
                self.assertEqual(store.default_daily_limit, 50)
                self.assertEqual(store.data["users"]["200"]["usage_count"], 1)
                self.assertEqual(api.edit_message.call_args.args[2], bot.admin_text("bulk_quota_changed").format(source=50, target=100, count=2))
                self.assertNotIn(100, service.pending_bulk_quotas)
                store.add(900)
                self.assertEqual(store.quota(900), 50)
                callback("bulkquota:50:100")
                self.assertTrue(api.answer_callback.call_args.kwargs["alert"])
                callback("nav:bulkquota")
                message("55 100")
                self.assertEqual(api.send_message.call_args.args[1], bot.admin_text("no_filtered_users"))
                self.assertNotIn(100, service.pending_bulk_quotas)
                service.stop()
                service.inline_executor.shutdown(wait=True)

    def test_bulk_confirmation_rejects_changed_preview_and_invalid_replacement_input(self):
        for language in bot.PUBLIC_TEXT:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary, bot.language_scope(language):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_language(100, language)
                store.set_quota(200, 50)
                store.set_quota(201, 50)
                api = MagicMock()
                service = bot.Bot(api, store)
                def callback(data):
                    service.handle_callback({"id": data, "data": data, "from": {"id": 100},
                                             "message": {"message_id": 1, "chat": {"id": 100}}})
                def message(text):
                    service.handle_update({"message": {"message_id": 2, "from": {"id": 100},
                                                       "chat": {"id": 100}, "text": text}})
                callback("nav:bulkquota")
                message("50 100")
                store.set_quota(200, 75)
                before = store.path.read_bytes()
                callback("bulkquota:50:100")
                self.assertEqual(store.path.read_bytes(), before)
                self.assertEqual(api.edit_message.call_args.args[2], bot.admin_text("bulk_quota_stale"))
                callback("nav:bulkquota")
                message("50 100")
                message("bad input")
                callback("bulkquota:50:100")
                self.assertEqual(store.quota(201), 50)
                self.assertTrue(api.answer_callback.call_args.kwargs["alert"])
                service.stop()
                service.inline_executor.shutdown(wait=True)

    def test_start_keyboard_localizes_access_and_language_selection(self):
        keyboard = bot.start_keyboard("ja", False, False)["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["text"], "利用を申請")
        self.assertEqual(
            [button["callback_data"] for button in keyboard[1]],
            ["public:language", "public:help"],
        )
        self.assertEqual(keyboard[1][0]["text"], "🌐 語言/Language")
        self.assertEqual(
            bot.public_text("zh", "start_allowed"),
            "請傳送有效的 X/Twitter 單篇貼文網址。",
        )
        self.assertIn("使い方", bot.public_text("ja", "help_allowed"))
        for language in bot.PUBLIC_TEXT:
            guide = bot.public_text(language, "help_allowed")
            self.assertEqual(len(guide.splitlines()), 4)
            self.assertIn(bot.BOT_MENTION, guide)
            self.assertNotIn("50 MB", guide)
            for detail in ("/id", "引用", "quoted", "未壓縮", "未压缩", "uncompressed", "インライン"):
                self.assertNotIn(detail, guide)
        expected_menus = {
            "zh-cn": ["🌐 語言/Language", "ℹ️ 使用说明"],
            "zh": ["🌐 語言/Language", "ℹ️ 使用說明"],
            "en": ["🌐 語言/Language", "ℹ️ How to use"],
            "ja": ["🌐 語言/Language", "ℹ️ 使い方"],
        }
        for language in ("zh-cn", "zh", "en", "ja"):
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
            for language in ("zh-cn", "zh", "en", "ja"):
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
                ["lang:zh-cn", "lang:zh", "lang:en", "lang:ja"],
            )
            self.assertEqual(
                language_keyboard[-1][0]["callback_data"], "public:main"
            )

    def test_administrator_start_keyboard_has_a_language_menu(self):
        keyboard = bot.start_keyboard("zh", True, True)["inline_keyboard"]
        callbacks = {
            button["callback_data"]
            for row in keyboard
            for button in row
        }
        self.assertIn("public:language", callbacks)
        self.assertEqual(keyboard, bot.owner_keyboard()["inline_keyboard"])


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
                ["nav:users", "public:language"],
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
            {"user_id": 1000 + index, "first_name": f"User {index}", "requested_at": float(index + 1)}
            for index in range(45)
        ]
        pending = bot.pending_keyboard(records, 1)["inline_keyboard"]
        action_rows = pending[:-3]
        batch_action = pending[-3]
        navigation = pending[-2]

        self.assertEqual(len(action_rows), 20)
        self.assertTrue(all("ban:" not in str(row) for row in action_rows))
        self.assertEqual(batch_action[0]["callback_data"], f"approvepage:1:{bot.pending_page_fingerprint(records, 1)}")
        self.assertEqual(
            [button["callback_data"] for button in navigation],
            ["requestspage:0", "noop:0", "requestspage:2"],
        )
        user_navigation = bot.users_page_keyboard(1, 45)["inline_keyboard"][-2]
        self.assertEqual(
            [button["callback_data"] for button in user_navigation],
            ["userspage:0:all", "noop:0", "userspage:2:all"],
        )

    def test_user_list_copies_id_and_shows_fraction_language_and_name_without_status(self):
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
            user_line = next(line for line in text.splitlines() if "<code>200</code>" in line)
            self.assertEqual(
                user_line,
                '<code>200</code>｜0/50｜CNT｜<a href="https://t.me/example_user">Example</a>',
            )
            self.assertNotIn("@example_user", user_line)
            self.assertNotIn("狀態", text.splitlines()[1])
            self.assertEqual(len(keyboard["inline_keyboard"]), 4)
            self.assertEqual([len(row) for row in keyboard["inline_keyboard"]], [3, 3, 1, 1])

    def test_user_list_copies_id_when_username_is_missing(self):
        records = [{
            "user_id": 9876543210123456,
            "first_name": "ABCDEFGHIJ",
            "quota": 50,
        }]
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            service = bot.Bot(MagicMock(), store)
            text, _, _ = service.users_page(records, 0)
        self.assertIn(
            "<code>9876543210123456</code>｜0/50｜CNT｜ABCDEFGHIJ",
            text,
        )
        self.assertNotIn("https://t.me/", text)

    def test_user_details_use_profile_refreshed_on_next_day(self):
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
            text, _ = service.user_search_result(200, 100)
            self.assertIn(
                'New Name (<a href="https://t.me/new_user">@new_user</a>)',
                text,
            )
            self.assertNotIn('https://t.me/old_user', text)
            self.assertEqual(store.data["users"]["200"]["profile_checked_date"], bot.bot_date(186_400))
            page = service.users_page(store.records(), 0)[0]
            self.assertIn('<code>200</code>｜0/50｜CNT｜<a href="https://t.me/new_user">New Name</a>', page)
            self.assertNotIn("https://t.me/old_user", page)

    def test_user_label_without_name_does_not_repeat_user_id(self):
        record = {"user_id": 987654321, "quota": 0}
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            service = bot.Bot(MagicMock(), store)
            text, _, _ = service.users_page([record], 0, "initialized")
        user_line = text.splitlines()[-1]
        self.assertNotIn("（未提供名稱）", user_line)
        self.assertTrue(user_line.endswith("｜0/0｜CNT｜—"))
        self.assertEqual(user_line.count("987654321"), 1)

    def test_user_details_escape_names_and_reject_invalid_username_links(self):
        record = {
            "user_id": 200,
            "first_name": "<b>Not markup</b>",
            "username": "invalid/name",
            "quota": 50,
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.data["users"]["200"] = record
            service = bot.Bot(MagicMock(), store)
            text, _ = service.user_search_result(200, 100)
            self.assertIn("&lt;b&gt;Not markup&lt;/b&gt;", text)
            self.assertNotIn("https://t.me/", text)
            self.assertNotIn("https://t.me/", service.users_page([record], 0)[0])
            record["username"] = "valid_user"
            linked_text, _ = service.user_search_result(200, 100)
            self.assertIn('<a href="https://t.me/valid_user">@valid_user</a>', linked_text)

    def test_user_details_keep_full_long_names_on_one_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.observe({
                "id": 200,
                "first_name": "♣ 闇猫\n・ヴィクトリカ・ド・ブロワ ♣",
                "username": "example_long_username",
            })
            store.set_quota(200, 50)
            service = bot.Bot(MagicMock(), store)
            text, _ = service.user_search_result(200, 100)
            self.assertTrue(text.startswith('♣ 闇猫 ・ヴィクトリカ・ド・ブロワ ♣ (<a href="https://t.me/example_long_username">@example_long_username</a>)\n'))
            self.assertNotIn("…", text)

    def test_user_list_language_codes_and_fraction_in_four_languages(self):
        for language, code in bot.LANGUAGE_CODES.items():
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary, bot.language_scope(language):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_quota(200, 100)
                store.set_language(200, language)
                store.observe({"id": 200, "first_name": "Example", "username": "example_user"})
                for _ in range(8):
                    store.consume(200)
                service = bot.Bot(MagicMock(), store)
                text, _, _ = service.users_page(store.records(), 0)
                self.assertIn(f'<code>200</code>｜8/100｜{code}｜<a href="https://t.me/example_user">Example</a>', text)
                self.assertIn('<code>100</code>', text)
                self.assertIn('0/∞', service.users_page(store.records(), 0, "admin")[0])
                self.assertEqual(len(text.splitlines()[-1].split("｜")), 4)
                service.stop()
                service.inline_executor.shutdown(wait=True)

    def test_user_list_default_role_filters_are_disjoint_and_keep_usage_sorting(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            for user_id, quota in ((101, None), (200, 50), (201, 100), (202, 100), (203, 0), (204, 0), (205, -1)):
                store.set_quota(user_id, quota)
            store.request_access(204)
            for user_id, count in ((200, 1), (201, 3), (202, 8)):
                for _ in range(count):
                    store.consume(user_id)
            service = bot.Bot(MagicMock(), store)
            expected = {"all": {100, 101, 200, 201, 202, 203, 204, 205}, "admin": {100, 101},
                        "ordinary": {200, 201, 202}, "initialized": {203}, "pending": {204}, "blocked": {205}}
            import re
            for role in bot.USER_ROLE_FILTERS:
                text, keyboard, _ = service.users_page(store.records(), 0, role)
                ids = {int(value) for value in re.findall(r"<code>(\d+)</code>", text)}
                self.assertEqual(ids, expected[role])
                selected = [button for row in keyboard["inline_keyboard"][:2] for button in row if button["text"].startswith("✅ ")]
                self.assertEqual([button["callback_data"] for button in selected], [f"userspage:0:{role}"])
            text = service.users_page(store.records(), 0)[0]
            self.assertEqual({int(value) for value in re.findall(r"<code>(\d+)</code>", text)}, expected["all"])
            ordinary_text = service.users_page(store.records(), 0, "ordinary")[0]
            self.assertEqual(re.findall(r"<code>(\d+)</code>", ordinary_text), ["202", "201", "200"])
            self.assertNotIn("狀態", text.splitlines()[1])
            service.stop()
            service.inline_executor.shutdown(wait=True)

    def test_user_filter_pagination_preserves_role_and_clamps_filtered_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            for user_id in range(200, 225):
                store.set_quota(user_id, None)
            for user_id in range(300, 341):
                store.set_quota(user_id, 50)
            api = MagicMock()
            service = bot.Bot(api, store)
            text, keyboard, page = service.users_page(store.records(), 99, "admin")
            self.assertEqual(page, 1)
            self.assertIn("第 2/2 頁，共 26 位", text)
            self.assertEqual(keyboard["inline_keyboard"][-2][0]["callback_data"], "userspage:0:admin")
            for action, expected_role in (("userspage:1:ordinary", "ordinary"), ("userspage:0:admin", "admin"), ("userspage:0:owner", "admin"), ("userspage:0", "all")):
                api.reset_mock()
                service.handle_callback({"id": "callback", "data": action, "from": {"id": 100},
                                         "message": {"message_id": 1, "chat": {"id": 100}}})
                self.assertEqual(api.edit_message.call_args.kwargs["parse_mode"], "HTML")
                keyboard = api.edit_message.call_args.args[3]
                selected = [button["callback_data"] for row in keyboard["inline_keyboard"][:2]
                            for button in row if button["text"].startswith("✅ ")]
                self.assertEqual(selected, [f"userspage:0:{expected_role}"])
            for invalid in ("userspage:0:unknown", "userspage:0:ordinary:owner", "userspage:-1:ordinary"):
                api.reset_mock()
                service.handle_callback({"id": "callback", "data": invalid, "from": {"id": 100},
                                         "message": {"message_id": 1, "chat": {"id": 100}}})
                api.edit_message.assert_not_called()
                self.assertTrue(api.answer_callback.call_args.kwargs["alert"])
            service.stop()
            service.inline_executor.shutdown(wait=True)

    def test_user_list_command_and_menu_default_to_all_users_for_admins(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(101, None)
            store.set_quota(200, 100)
            store.set_quota(300, 0)
            api = MagicMock()
            service = bot.Bot(api, store)
            for user_id in (100, 101):
                api.reset_mock()
                service.handle_update({"message": {"message_id": 1, "from": {"id": user_id},
                                                   "chat": {"id": user_id}, "text": "/users"}})
                command_page = api.send_message.call_args.args[1]
                api.reset_mock()
                service.handle_callback({"id": "callback", "data": "nav:userlist", "from": {"id": user_id},
                                         "message": {"message_id": 1, "chat": {"id": user_id}}})
                menu_page = api.edit_message.call_args.args[2]
                for text in (command_page, menu_page):
                    self.assertIn("<code>200</code>", text)
                    for included in (100, 101, 300):
                        self.assertIn(f"<code>{included}</code>", text)
            service.stop()
            service.inline_executor.shutdown(wait=True)

    def test_user_filters_localize_empty_lists_and_reset_page_in_four_languages(self):
        for language in bot.PUBLIC_TEXT:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary, bot.language_scope(language):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                service = bot.Bot(MagicMock(), store)
                text, keyboard, page = service.users_page(store.records(), 99, "blocked")
                self.assertEqual(page, 0)
                self.assertIn(bot.admin_text("no_filtered_users"), text)
                self.assertEqual(keyboard["inline_keyboard"][-2], [{"text": "1/1", "callback_data": "noop:0"}])
                buttons = [button for row in keyboard["inline_keyboard"][:2] for button in row]
                self.assertEqual([button["callback_data"] for button in buttons], [f"userspage:0:{role}" for role in bot.USER_ROLE_FILTERS])
                for role, button in zip(bot.USER_ROLE_FILTERS, buttons):
                    self.assertEqual(button["text"], ("✅ " if role == "blocked" else "") + bot.admin_text(role))
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)
                service.stop()
                service.inline_executor.shutdown(wait=True)

    def test_user_filters_remain_private_admin_only_and_do_not_change_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            for user_id, quota in ((101, None), (200, 75)):
                store.set_quota(user_id, quota)
            for user_id in (100, 101, 200):
                store.observe({"id": user_id})
            before = json.dumps(store.data, sort_keys=True)
            api = MagicMock()
            service = bot.Bot(api, store)
            for user_id, chat_id, permitted in ((100, 100, True), (101, 101, True), (200, 200, False), (101, -500, False)):
                api.reset_mock()
                service.handle_callback({"id": "callback", "data": "userspage:0:owner", "from": {"id": user_id},
                                         "message": {"message_id": 1, "chat": {"id": chat_id}}})
                self.assertEqual(api.edit_message.called, permitted)
                self.assertEqual(json.dumps(store.data, sort_keys=True), before)
            service.stop()
            service.inline_executor.shutdown(wait=True)

    def test_user_list_names_have_bounded_width_and_details_keep_full_escaped_name(self):
        cases = {"ABCDEFGHIJ": "ABCDEFGHIJ", "ABCDEFGHIJK": "ABCDEFGH…", "中文測試名字": "中文測試…",
                 "AB.CDEFGHI": "AB.CDEF…", "": "—"}
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(bot.user_name({"first_name": name}, bot.USER_LIST_NAME_WIDTH), expected)
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.observe({"id": 200, "first_name": "<b>Not markup</b>\nvery long name", "username": "example_user"})
            store.set_quota(200, 100)
            service = bot.Bot(MagicMock(), store)
            row = service.users_page(store.records(), 0)[0].splitlines()[-1]
            self.assertIn("&lt;b&gt;", row)
            self.assertNotIn("<b>", row)
            self.assertEqual(len(row.split("｜")), 4)
            name = bot.html.unescape(bot.re.sub(r"<[^>]+>", "", row.split("｜")[-1]))
            width = sum(1 if char.isascii() and (char.isalnum() or char == " ") else 2 for char in name)
            self.assertLessEqual(width, bot.USER_LIST_NAME_WIDTH)
            details = service.user_search_result(200, 100)[0]
            self.assertIn("&lt;b&gt;Not markup&lt;/b&gt; very long name", details)
            self.assertIn('<a href="https://t.me/example_user">@example_user</a>', details)
            service.stop()
            service.inline_executor.shutdown(wait=True)

    def test_user_detail_sends_and_all_edit_paths_use_html(self):
        for language in bot.PUBLIC_TEXT:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary, bot.language_scope(language):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_language(100, language)
                store.observe({"id": 200, "first_name": "Full <Name>", "last_name": "Surname", "username": "example_user"})
                store.set_quota(200, 100)
                api = MagicMock()
                service = bot.Bot(api, store)
                service.send_user_search_result(100, 1, 200, 100)
                self.assertEqual(api.send_message.call_args.kwargs["parse_mode"], "HTML")
                self.assertIn('Full &lt;Name&gt; Surname (<a href="https://t.me/example_user">@example_user</a>)', api.send_message.call_args.args[1])
                for action in ("findresult:200", "quota:200:100", "confirmquota:200:100", "createuser:200:100"):
                    api.reset_mock()
                    service.handle_callback({"id": "callback", "data": action, "from": {"id": 100},
                                             "message": {"message_id": 1, "chat": {"id": 100}}})
                    self.assertEqual(api.edit_message.call_args.kwargs["parse_mode"], "HTML")
                    self.assertIn('<a href="https://t.me/example_user">@example_user</a>', api.edit_message.call_args.args[2])
                service.stop()
                service.inline_executor.shutdown(wait=True)

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
                "data": bot.pending_keyboard(store.pending())["inline_keyboard"][-3][0]["callback_data"],
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
                "data": bot.pending_keyboard(store.pending())["inline_keyboard"][0][1]["callback_data"],
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
                100, 77, "用戶管理", bot.user_menu_keyboard()
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
            self.assertIn("今日用量：1", status)
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
                "🎛️ 用戶控制管理",
            )
            self.assertEqual(
                advanced[2][0]["text"],
                "🐞 實現方式：關閉",
            )
            controls = bot.user_controls_keyboard(True, False)["inline_keyboard"]
            self.assertEqual(
                controls[0][0]["text"],
                "🌐 使用開關：開放",
            )
            self.assertEqual(
                controls[1][0]["text"],
                "✅ 自動通過：關閉",
            )
            self.assertEqual(controls[2][0]["text"], "🎯 額度管理")
            cookies = bot.cookie_menu_keyboard(True)["inline_keyboard"]
            self.assertEqual(
                cookies[1][0]["text"], "🍪 Cookies 使用：開啟"
            )
            self.assertNotIn("Cookies", status)
            self.assertNotIn("自動通過", status)
            self.assertNotIn("管理模式", status)

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
            for language, implementation in (("zh-cn", "实现方式"), ("zh", "實現方式"), ("ja", "実装方法"), ("en", "Implementation details")):
                store.set_language(200, language)
                with bot.language_scope(language):
                    for data in ("nav:advanced", "debugtoggle:0", "nav:usercontrols", "nav:quotamanagement", "nav:defaultquota", "nav:bulkquota", "defaultquota:100", "bulkquota:50:100"):
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
            self.assertIn("普通用戶", api.edit_message.call_args.args[2])

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
            ("en", ("Owner guide", "Administrator guide"), "User ID quota"),
            ("zh-cn", ("所有者管理说明", "管理员使用说明"), "User ID 额度"),
        ):
            with self.subTest(language=language), bot.language_scope(language):
                owner_help = bot.owner_help_text(True)
                admin_help = bot.owner_help_text(False)
                self.assertTrue(owner_help.startswith(titles[0]))
                self.assertTrue(admin_help.startswith(titles[1]))
                for text in (owner_help, admin_help):
                    self.assertIn(shortcut, text)
                    self.assertNotIn(bot.BOT_MENTION, text)
                    self.assertLess(len(text), 750)
                    for detail in ("FxTwitter", "gallery-dl", "yt-dlp", "50 MB", "52-bit", bot.BOT_TIMEZONE_NAME):
                        self.assertNotIn(detail, text)
                self.assertNotEqual(owner_help, admin_help)
                self.assertIn(bot.admin_text("advanced").lower(), owner_help.lower())
                self.assertNotIn(bot.admin_text("default_limit").lower(), admin_help.lower())
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
                bot.start_keyboard("zh", True, True),
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
        api.configure_commands()

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
        command_calls = api.call.call_args_list[:4]
        self.assertEqual(
            [call.args[1].get("language_code", "") for call in command_calls],
            ["", "zh", "en", "ja"],
        )
        self.assertEqual(
            json.loads(command_calls[1].args[1]["commands"])[0]["description"],
            "启动并显示操作菜单",
        )
        self.assertEqual(
            json.loads(command_calls[2].args[1]["commands"])[0]["description"],
            "Start and show the options",
        )
        self.assertEqual(
            json.loads(command_calls[3].args[1]["commands"])[0]["description"],
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
        localized = {call.args[1].get("language_code", ""): call.args[1]["description"]
                     for call in api.call.call_args_list if call.args[0] == "setMyDescription"}
        self.assertIn("发送", localized["zh"])
        self.assertIn("傳送", localized[""])
        self.assertNotIn("zh-cn", localized)


class MediaTests(unittest.TestCase):
    def test_each_extractor_stage_preserves_completed_files_on_failure(self):
        methods = ["gallery-dl（匿名）", "gallery-dl（Cookies）", "yt-dlp（匿名）", "yt-dlp（Cookies）"]
        errors = [subprocess.TimeoutExpired("extractor", 240), OSError("interrupted"), ValueError("directory limit")]
        for stage, method in enumerate(methods):
            for error in errors:
                with self.subTest(stage=stage, error=type(error).__name__), tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    cookies = directory / "cookies.txt"
                    cookies.write_text("example", encoding="utf-8")
                    complete = directory / "complete.jpg"
                    attempts = []
                    def extract(command, timeout, directory):
                        attempts.append(command)
                        if len(attempts) == stage + 1:
                            complete.write_bytes(b"complete")
                            (directory / "incomplete.mp4.part").write_bytes(b"partial")
                            raise error
                        return subprocess.CompletedProcess(command, 0, "no media")
                    with patch.object(bot, "COOKIES_PATH", cookies), \
                         patch.object(bot, "run_command", side_effect=extract), \
                         patch.object(bot.shutil, "disk_usage", return_value=MagicMock(free=10**12)), \
                         self.assertLogs(bot.LOG, level="WARNING"):
                        files, log, actual_method, invalid = bot.download_media("https://x.com/example/status/123", directory)
                    self.assertEqual(files, [complete])
                    self.assertEqual(actual_method, method)
                    self.assertEqual(len(attempts), stage + 1)
                    self.assertIn(type(error).__name__, log)
                    self.assertFalse(invalid)

    def test_failed_extractor_falls_back_or_still_delivers_fetched_text(self):
        for recovered in (False, True):
            with self.subTest(recovered=recovered), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                tweet = {"id": "123", "text": "already fetched text", "media": {"all": [
                    {"type": "photo", "url": "https://pbs.twimg.com/example.jpg"}
                ]}}
                service = bot.Bot(MagicMock(), bot.ACLStore(directory / "acl.json", 100))
                attempts = []
                def extract(command, timeout, directory):
                    attempts.append(command)
                    if recovered and len(attempts) == 2:
                        (directory / "complete.jpg").write_bytes(b"complete")
                        return subprocess.CompletedProcess(command, 0, "ok")
                    raise subprocess.TimeoutExpired(command, timeout)
                with patch.object(bot, "TMP_DIR", directory), \
                     patch.object(bot, "COOKIES_PATH", directory / "missing-cookies.txt"), \
                     patch.object(bot, "media_disk_available", return_value=True), \
                     patch.object(bot.shutil, "disk_usage", return_value=MagicMock(free=10**12)), \
                     patch.object(bot, "fetch_fxtwitter", return_value=tweet), \
                     patch.object(bot, "download_fxtwitter_media", return_value=([], "", 0)), \
                     patch.object(bot, "run_command", side_effect=extract), \
                     patch.object(bot, "prepare_image", side_effect=lambda path: path), \
                     self.assertLogs(bot.LOG, level="WARNING"):
                    service.process_url(100, 1, 100, "https://x.com/example/status/123")
                self.assertEqual(len(attempts), 2)
                if recovered:
                    self.assertIn("already fetched text", service.api.send_previews.call_args.args[2])
                    service.api.send_message.assert_not_called()
                else:
                    self.assertIn("already fetched text", service.api.send_message.call_args_list[0].args[1])
                    self.assertEqual(service.api.send_message.call_args.args[1], bot.public_text("zh", "media_skipped"))
                service.stop()
                service.inline_executor.shutdown(wait=True)

    def test_low_disk_space_does_not_start_extractor_retries(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bot, "COOKIES_PATH", Path(temporary) / "missing"), \
             patch.object(bot.shutil, "disk_usage", return_value=MagicMock(free=0)), \
             patch.object(bot, "run_command") as runner, self.assertLogs(bot.LOG, level="WARNING"):
            files, _, _, _ = bot.download_media("https://x.com/example/status/123", Path(temporary))
            self.assertEqual(files, [])
            runner.assert_not_called()

    def test_text_only_and_document_only_posts_use_message_not_caption_limit(self):
        for language in bot.PUBLIC_TEXT:
            for suffix in (None, ".webm", ".jpg"):
                with self.subTest(language=language, suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    files = []
                    if suffix:
                        path = directory / ("media" + suffix)
                        path.write_bytes(b"media")
                        files.append(path)
                    text = "a" * 2990 + "END_MARKER"
                    tweet = {"id": "123", "text": text, "author": {"name": "Example", "screen_name": "example"}}
                    store = bot.ACLStore(directory / "acl.json", 100)
                    store.set_language(100, language)
                    store.toggle_debug_mode(100)
                    api = bot.TelegramAPI("YOUR_API_TOKEN")
                    api.call = MagicMock(return_value={"message_id": 1})
                    service = bot.Bot(api, store)
                    with patch.object(bot, "TMP_DIR", directory), patch.object(bot, "media_disk_available", return_value=True), \
                         patch.object(bot, "fetch_fxtwitter", return_value=tweet), \
                         patch.object(bot, "download_media", return_value=(files, "", "gallery-dl（匿名）", False)), \
                         patch.object(bot, "prepare_image", side_effect=lambda path: path):
                        service.process_url(100, 1, 100, "https://x.com/example/status/123")
                    calls = [call for call in api.call.call_args_list if call.args[0] in {"sendMessage", "sendPhoto"}]
                    self.assertEqual(len(calls), 1)
                    method, data = calls[0].args[:2]
                    output = data["caption"] if method == "sendPhoto" else data["text"]
                    self.assertEqual("END_MARKER" in output, suffix != ".jpg")
                    self.assertIn('<a href="https://x.com/example">Example</a>:', output)
                    self.assertIn("https://x.com/example/status/123", output)
                    self.assertEqual(data["parse_mode"], "HTML")
                    plain = bot.html.unescape(bot.re.sub("<[^>]*>", "", output))
                    self.assertLessEqual(len(plain), 1024 if suffix == ".jpg" else 4096)
                    self.assertIn("gallery-dl", output)
                    service.stop()
                    service.inline_executor.shutdown(wait=True)

    def test_custom_video_limit_notice_does_not_blame_telegram(self):
        for language in bot.PUBLIC_TEXT:
            for limit in (10_000_000, 50_000_000):
                for direct in (False, True):
                    with self.subTest(language=language, limit=limit, direct=direct), tempfile.TemporaryDirectory() as temporary:
                        directory = Path(temporary)
                        video = directory / "video.mp4"
                        video.write_bytes(b"video")
                        tweet = {"id": "123", "text": "example", "media": {"all": [
                            {"type": "video", "url": "https://video.twimg.com/example.mp4"}
                        ]}}
                        store = bot.ACLStore(directory / "acl.json", 100)
                        store.set_language(100, language)
                        service = bot.Bot(MagicMock(), store)
                        stat = Path.stat
                        def oversized(path, *args, **kwargs):
                            return MagicMock(st_size=limit + 1) if path == video else stat(path, *args, **kwargs)
                        with patch.object(bot, "TMP_DIR", directory), patch.object(bot, "media_disk_available", return_value=True), \
                             patch.object(bot, "MAX_VIDEO_BYTES", limit), patch.object(bot, "fetch_fxtwitter", return_value=tweet), \
                             patch.object(bot, "download_fxtwitter_media", return_value=([], "", 1 if direct else 0)), \
                             patch.object(bot, "download_media", return_value=([] if direct else [video], "", "", False)), \
                             patch.object(Path, "stat", oversized):
                            service.process_url(100, 1, 100, "https://x.com/example/status/123")
                        key = "video_oversized" if limit == 50_000_000 else "video_limited"
                        self.assertEqual(service.api.send_message.call_args.args[1], bot.public_text(language, key, count=1))
                        service.stop()
                        service.inline_executor.shutdown(wait=True)

    def test_direct_media_keeps_completed_files_when_resources_run_out(self):
        for exhausted in ("between_items", "during_item", "disk"):
            with self.subTest(exhausted=exhausted), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                tweet = {"media": {"all": [
                    {"type": "photo", "url": f"https://pbs.twimg.com/{index}.jpg"}
                    for index in range(3)
                ]}}
                clock = MagicMock(return_value=0)
                disk = MagicMock(return_value=MagicMock(free=10**12))
                first = MagicMock(headers={})
                first.__enter__.return_value = first
                def first_chunks(*args, **kwargs):
                    if first_chunks.done:
                        if exhausted == "between_items":
                            clock.return_value = 241
                        elif exhausted == "disk":
                            disk.return_value.free = 0
                        return b""
                    first_chunks.done = True
                    return b"complete"
                first_chunks.done = False
                first.raw.read1.side_effect = first_chunks
                second = MagicMock(headers={})
                second.__enter__.return_value = second
                def second_chunks(*args, **kwargs):
                    clock.return_value = 241
                    return b"partial"
                second.raw.read1.side_effect = second_chunks
                with patch.object(bot, "trusted_twimg_response", side_effect=[first, second]) as download, \
                     patch.object(bot.time, "monotonic", clock), \
                     patch.object(bot.shutil, "disk_usage", disk), patch.object(bot.LOG, "warning"):
                    files, _, oversized = bot.download_fxtwitter_media(tweet, directory)
                self.assertEqual(files, [directory / "fxtwitter_1.jpg"])
                self.assertEqual(files[0].read_bytes(), b"complete")
                self.assertEqual(list(directory.iterdir()), files)
                self.assertEqual(oversized, 0)
                self.assertEqual(download.call_count, 2 if exhausted == "during_item" else 1)

    def test_partial_direct_failure_delivers_success_and_localized_skip_notice(self):
        for language in bot.PUBLIC_TEXT:
            for failure in ("connection", "deadline"):
                with self.subTest(language=language, failure=failure), tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    store = bot.ACLStore(directory / "acl.json", 100)
                    store.add(200)
                    store.set_language(200, language)
                    service = bot.Bot(MagicMock(), store)
                    tweet = {"id": "123", "text": "example text", "media": {"all": [
                        {"type": "photo", "url": f"https://pbs.twimg.com/{index}.jpg"}
                        for index in range(2)
                    ]}}
                    clock = MagicMock(return_value=0)
                    response = MagicMock(headers={})
                    response.__enter__.return_value = response
                    def chunks(*args, **kwargs):
                        if chunks.done:
                            if failure == "deadline":
                                clock.return_value = 241
                            return b""
                        chunks.done = True
                        return b"image"
                    chunks.done = False
                    response.raw.read1.side_effect = chunks
                    with patch.object(bot, "TMP_DIR", directory), \
                         patch.object(bot, "fetch_fxtwitter", return_value=tweet), \
                         patch.object(bot, "trusted_twimg_response", side_effect=[response, bot.requests.ConnectionError("failed")]), \
                         patch.object(bot.time, "monotonic", clock), \
                         patch.object(bot.shutil, "disk_usage", return_value=MagicMock(free=10**12)), \
                         patch.object(bot, "prepare_image", side_effect=lambda path: path), \
                         patch.object(bot, "download_media") as fallback, patch.object(bot.LOG, "warning"):
                        service.process_url(200, 1, 200, "https://x.com/example/status/123")
                    fallback.assert_not_called()
                    service.api.send_previews.assert_called_once()
                    self.assertEqual(len(service.api.send_previews.call_args.args[1]), 1)
                    self.assertIn("example text", service.api.send_previews.call_args.args[2])
                    service.api.send_documents.assert_called_once()
                    service.api.send_message.assert_called_once_with(200, bot.public_text(language, "media_skipped"))
                    self.assertFalse(list(directory.glob("tweet-*")))
                    service.stop()
                    service.inline_executor.shutdown(wait=True)

    def test_missing_media_notice_does_not_duplicate_known_rejection(self):
        for language in bot.PUBLIC_TEXT:
            for missing in (False, True):
                with self.subTest(language=language, missing=missing), tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    image = directory / "image.jpg"
                    image.write_bytes(b"image")
                    tweet = {"id": "123", "text": "example", "media": {"all": [
                        {"type": "photo", "url": "https://pbs.twimg.com/image.jpg"},
                        {"type": "video", "url": "https://video.twimg.com/large.mp4"},
                    ] + ([{"type": "photo", "url": "https://pbs.twimg.com/missing.jpg"}] if missing else [])}}
                    store = bot.ACLStore(directory / "acl.json", 100)
                    store.set_language(100, language)
                    service = bot.Bot(MagicMock(), store)
                    with patch.object(bot, "TMP_DIR", directory), \
                         patch.object(bot, "media_disk_available", return_value=True), \
                         patch.object(bot, "fetch_fxtwitter", return_value=tweet), \
                         patch.object(bot, "download_fxtwitter_media", return_value=([image], "", 1)), \
                         patch.object(bot, "prepare_image", side_effect=lambda path: path):
                        service.process_url(100, 1, 100, "https://x.com/example/status/123")
                    messages = [call.args[1] for call in service.api.send_message.call_args_list]
                    expected = [bot.public_text(language, "video_oversized", count=1)]
                    if missing:
                        expected.append(bot.public_text(language, "media_skipped"))
                    self.assertEqual(messages, expected)
                    service.stop()
                    service.inline_executor.shutdown(wait=True)

    def test_extractor_rename_race_cannot_bypass_deadline(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bot.subprocess, "Popen") as start, patch.object(bot.time, "monotonic", side_effect=[0, 2]), patch.object(Path, "rglob", side_effect=FileNotFoundError), patch.object(bot.os, "killpg", create=True) as kill_group:
            process = start.return_value
            process.poll.return_value = None
            with self.assertRaises(subprocess.TimeoutExpired):
                bot.run_command(["extractor"], timeout=1, directory=Path(temporary))
            if os.name == "nt":
                process.kill.assert_called_once()
            else:
                kill_group.assert_called_once()

    def test_direct_media_stream_error_removes_partial_file(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.raw.read1.side_effect = [b"partial", bot.StreamHTTPError("interrupted")]
        with tempfile.TemporaryDirectory() as temporary, patch.object(bot, "fxtwitter_media", return_value=[{"type": "photo", "url": "https://pbs.twimg.com/media/example.jpg"}]), patch.object(bot, "trusted_twimg_response", return_value=response), patch.object(bot.shutil, "disk_usage", return_value=MagicMock(free=10**12)), self.assertLogs(bot.LOG, level="WARNING"):
            files, _, _ = bot.download_fxtwitter_media({}, Path(temporary))
            self.assertEqual(files, [])
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_disk_scan_ignores_files_removed_by_another_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            victim = root / "partial.bin"
            victim.write_bytes(b"x")
            original = Path.is_file
            def disappearing(path):
                result = original(path)
                if path == victim and result:
                    victim.unlink()
                return result
            with patch.object(bot, "TMP_DIR", root), patch.object(Path, "is_file", disappearing), patch.object(bot.shutil, "disk_usage", return_value=MagicMock(free=bot.MIN_FREE_DISK_BYTES)):
                self.assertTrue(bot.media_disk_available())

    def test_direct_media_stops_on_deadline_or_low_space_and_cleans_partial(self):
        for exhausted in ("deadline", "disk"):
            with self.subTest(exhausted=exhausted), tempfile.TemporaryDirectory() as temporary:
                clock = MagicMock(return_value=0)
                disk = MagicMock(return_value=MagicMock(free=10**12))
                response = MagicMock()
                response.__enter__.return_value = response
                response.headers = {}
                def chunks():
                    yield b"first"
                    if exhausted == "deadline":
                        clock.return_value = 241
                    else:
                        disk.return_value.free = 0
                    yield b"second"
                iterator = chunks()
                response.raw.read1.side_effect = lambda *args, **kwargs: next(iterator, b"")
                with patch.object(bot, "fxtwitter_media", return_value=[{"type": "photo", "url": "https://pbs.twimg.com/media/example.jpg"}]), patch.object(bot, "trusted_twimg_response", return_value=response), patch.object(bot.time, "monotonic", clock), patch.object(bot.shutil, "disk_usage", disk), self.assertLogs(bot.LOG, level="WARNING"):
                    files, _, _ = bot.download_fxtwitter_media({}, Path(temporary))
                self.assertEqual(files, [])
                self.assertEqual(list(Path(temporary).iterdir()), [])
                response.__exit__.assert_called_once()

    def test_disk_soft_limits_reject_downloads_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bot, "TMP_DIR", Path(temporary)), patch.object(
            bot, "TEMP_SOFT_LIMIT_BYTES", 4096
        ), patch.object(bot.shutil, "disk_usage", return_value=MagicMock(free=bot.MIN_FREE_DISK_BYTES)) as usage:
            self.assertTrue(bot.media_disk_available())
            path = Path(temporary) / "media.mp4"
            path.write_bytes(b"x" * 4096)
            self.assertFalse(bot.media_disk_available())
            path.unlink()
            usage.return_value.free = bot.MIN_FREE_DISK_BYTES - 1
            self.assertFalse(bot.media_disk_available())
            usage.side_effect = OSError("unavailable")
            with self.assertLogs(bot.LOG, level="WARNING"):
                self.assertFalse(bot.media_disk_available())

    def test_stale_cleanup_preserves_live_recent_and_unrelated_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            root = state / "tmp"
            root.mkdir()
            old = bot.time.time() - bot.TEMP_RETENTION_HOURS * 3600 - 10
            for name in ("tweet-expired", "tweet-active", "tweet-recent-file", "unrelated"):
                directory = root / name
                directory.mkdir()
                file = directory / "media.mp4"
                file.write_bytes(b"data")
                if name != "tweet-recent-file":
                    os.utime(file, (old, old))
                os.utime(directory, (old, old))
            recent = root / "tweet-new"
            recent.mkdir()
            for name in ("acl.json", "cookies.txt", "acl.json.corrupt-1"):
                (state / name).write_bytes(b"preserve")
            with patch.object(bot, "STATE_DIR", state), patch.object(bot, "TMP_DIR", root), patch.object(
                bot, "ACTIVE_MEDIA_DIRS", {root / "tweet-active"}
            ):
                bot.cleanup_stale_media()
            self.assertFalse((root / "tweet-expired").exists())
            self.assertEqual({path.name for path in root.iterdir()}, {
                "tweet-active", "tweet-recent-file", "tweet-new", "unrelated",
            })
            for name in ("acl.json", "cookies.txt", "acl.json.corrupt-1"):
                self.assertEqual((state / name).read_bytes(), b"preserve")

    @unittest.skipIf(os.name == "nt", "symlink protection is verified on Linux")
    def test_media_cleanup_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            root, outside = state / "tmp", state / "outside"
            root.mkdir()
            outside.mkdir()
            target = outside / "important"
            target.write_bytes(b"preserve")
            (root / "tweet-link").symlink_to(outside, target_is_directory=True)
            nested = root / "tweet-expired"
            nested.mkdir()
            (nested / "link").symlink_to(outside, target_is_directory=True)
            old = bot.time.time() - bot.TEMP_RETENTION_HOURS * 3600 - 10
            os.utime(nested / "link", (old, old), follow_symlinks=False)
            os.utime(nested, (old, old))
            with patch.object(bot, "STATE_DIR", state), patch.object(bot, "TMP_DIR", root):
                bot.cleanup_stale_media()
            self.assertFalse(nested.exists())
            self.assertTrue((root / "tweet-link").is_symlink())
            self.assertEqual(target.read_bytes(), b"preserve")
            with patch.object(bot, "TMP_DIR", root / "tweet-link"):
                self.assertFalse(bot.media_disk_available())
                with self.assertLogs(bot.LOG, level="WARNING"):
                    bot.cleanup_stale_media()
            self.assertEqual(target.read_bytes(), b"preserve")

    def test_media_temporary_directory_is_registered_and_removed_on_error(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bot, "TMP_DIR", Path(temporary)), patch.object(
            bot, "ACTIVE_MEDIA_DIRS", set()
        ):
            with self.assertRaisesRegex(ValueError, "test failure"):
                with bot.media_temporary_directory() as directory:
                    self.assertIn(directory, bot.ACTIVE_MEDIA_DIRS)
                    (directory / "media.mp4").write_bytes(b"data")
                    raise ValueError("test failure")
            self.assertFalse(directory.exists())
            self.assertFalse(bot.ACTIVE_MEDIA_DIRS)

    def test_low_disk_admission_preserves_quota_and_localized_notice(self):
        for language in bot.PUBLIC_TEXT:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary:
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.add(200)
                store.set_language(200, language)
                service = bot.Bot(MagicMock(), store)
                with patch.object(bot, "media_disk_available", return_value=False), patch.object(store, "consume") as consume:
                    service.handle_update({"message": {"message_id": 1, "chat": {"id": 200, "type": "private"},
                                                       "from": {"id": 200}, "text": "https://x.com/example/status/123456789"}})
                consume.assert_not_called()
                self.assertFalse(store.data["pending_jobs"])
                self.assertTrue(service.jobs.empty())
                service.api.send_message.assert_called_with(200, bot.public_text(language, "queue_full"), 1)
                service.stop()
                service.inline_executor.shutdown(wait=True)

    def test_extractor_cache_is_temporary_and_not_sent_as_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = bot.run_command([sys.executable, "-c", "import os; print(os.environ['XDG_CACHE_HOME'])"],
                                     timeout=5, directory=directory)
            self.assertEqual(result.stdout.strip(), str(directory / ".cache"))
            cache = directory / ".cache" / "gallery-dl"
            cache.mkdir(parents=True)
            (cache / "cache.sqlite3").write_bytes(b"cache")
            media = directory / "image.jpg"
            media.write_bytes(b"image")
            self.assertEqual(bot.media_files(directory), [media])

    def test_running_extractor_stops_when_free_space_is_low(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot.shutil, "disk_usage", return_value=MagicMock(free=bot.MIN_FREE_DISK_BYTES - 1)
        ):
            with self.assertRaisesRegex(ValueError, "directory limit"):
                bot.run_command([sys.executable, "-c", "import time; time.sleep(10)"],
                                timeout=5, directory=Path(temporary))

    def test_extractor_stops_when_temporary_media_exceeds_limit(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "MAX_TOTAL_BYTES", 1024
        ):
            directory = Path(temporary)
            command = [sys.executable, "-c", (
                "from pathlib import Path; "
                f"Path({str(directory / 'large.mp4')!r}).write_bytes(b'x' * 4096)"
            )]
            with self.assertRaisesRegex(ValueError, "directory limit"):
                bot.run_command(command, timeout=5, directory=directory)

    def test_extractor_stops_a_running_download_at_the_limit(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            bot, "MAX_TOTAL_BYTES", 2048
        ):
            directory = Path(temporary)
            script = (
                "from pathlib import Path\nimport time\n"
                f"with Path({str(directory / 'growing.mp4')!r}).open('wb') as out:\n"
                "    while True:\n"
                "        out.write(b'x' * 512)\n        out.flush()\n"
                "        time.sleep(0.01)\n"
            )
            with self.assertRaisesRegex(ValueError, "directory limit"):
                bot.run_command(
                    [sys.executable, "-c", script], timeout=5, directory=directory
                )

    def test_extractor_returns_bounded_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = bot.run_command(
                [sys.executable, "-c", "print('ok')"],
                timeout=5, directory=Path(temporary),
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "ok")

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
        smaller.raw.read1.side_effect = [b"video", b""]
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
            self.assertIn("--filesize-max", command)
            self.assertIn("1-10", command)
            self.assertEqual(runner.call_args.kwargs["directory"], Path(temporary))
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
                api = bot.TelegramAPI("YOUR_API_TOKEN")
                api.call = MagicMock()
                service = bot.Bot(api, store)
                service.process_url(100, 10, 100, "https://x.com/author/status/123")

            methods = [call.args[0] for call in api.call.call_args_list]
            self.assertEqual(methods, ["sendChatAction", "sendMessage", "sendDocument"])
            message = api.call.call_args_list[1].args[1]
            self.assertIn('<a href="https://x.com/author">Author</a>:\nwebm', message["text"])
            self.assertIn("https://x.com/author/status/123", message["text"])
            self.assertEqual(message["parse_mode"], "HTML")
            service.stop()
            service.inline_executor.shutdown(wait=True)

    def test_mixed_preview_and_document_media_does_not_duplicate_caption(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            photo = directory / "photo.jpg"
            document = directory / "video.webm"
            photo.write_bytes(b"photo")
            document.write_bytes(b"video")
            api = bot.TelegramAPI("YOUR_API_TOKEN")
            api.call = MagicMock(return_value={"message_id": 1})
            api.send_previews(100, [document, photo], "example text", parse_mode="HTML")
            api.call.assert_called_once()
            self.assertEqual(api.call.call_args.args[0], "sendPhoto")
            self.assertEqual(api.call.call_args.args[1]["caption"], "example text")

    def test_document_only_previews_keep_caption_in_every_language(self):
        for language in bot.PUBLIC_TEXT:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary, bot.language_scope(language):
                path = Path(temporary) / "video.webm"
                path.write_bytes(b"video")
                caption = bot.media_caption("Example", "https://x.com/example", "example text", "https://x.com/example/status/123")
                api = bot.TelegramAPI("YOUR_API_TOKEN")
                api.call = MagicMock()
                self.assertEqual(api.send_previews(100, [path, path], caption, parse_mode="HTML"), [])
                api.call.assert_called_once()
                self.assertEqual(api.call.call_args.args[0], "sendMessage")
                self.assertEqual(api.call.call_args.args[1]["text"], caption)
                self.assertEqual(api.call.call_args.args[1]["parse_mode"], "HTML")
                api.call.reset_mock()
                self.assertEqual(api.send_previews(100, [], caption, parse_mode="HTML"), [])
                api.call.assert_not_called()

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
            ), patch.object(bot, "prepare_image", side_effect=lambda path: path), bot.language_scope("ja"):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_language(100, "ja")
                api = MagicMock()
                api.send_documents.side_effect = RuntimeError("upload failed")
                service = bot.Bot(api, store)
                service.process_url(100, 10, 100, "https://x.com/author/status/123")

            self.assertIn("元ファイル", api.send_message.call_args.args[1])

    def test_unavailable_post_reports_reason_in_each_user_language(self):
        url = "https://x.com/author/status/123"
        for language in ("zh-cn", "zh", "en", "ja"):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary, patch.object(
                bot, "TMP_DIR", Path(temporary)
            ), patch.object(
                bot, "fetch_fxtwitter", return_value=None
            ), patch.object(
                bot, "fetch_tweet_text", return_value=("", "", "")
            ), patch.object(
                bot, "download_media", return_value=([], "unavailable", "", False)
            ):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_quota(200, 50)
                store.set_language(200, language)
                api = MagicMock()
                bot.Bot(api, store).process_url(200, 10, 200, url)
                self.assertEqual(
                    api.send_message.call_args.args[1],
                    bot.public_text(language, "post_unavailable"),
                )
                api.send_previews.assert_not_called()
                api.send_documents.assert_not_called()

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
            ), bot.language_scope("en"):
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_language(100, "en")
                api = MagicMock()
                api.send_previews.side_effect = RuntimeError("connection failed")
                service = bot.Bot(api, store)
                service.process_url(
                    100, 10, 100, "https://x.com/author/status/123"
                )

            fallback = api.send_message.call_args.args[1]
            self.assertIn("media preview", fallback.lower())
            self.assertIn("try again later", fallback.lower())
            api.send_documents.assert_called_once_with(100, [])

    def test_owner_keeps_cookie_access_when_regular_cookie_access_is_off(self):
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
            self.assertEqual(rejected, [("large.mp4", "video_oversized")])

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
    def test_inline_deduplication_uses_configured_quota_day(self):
        for zone, hour in ((bot.timezone.utc, 0), (bot.timezone(bot.timedelta(hours=9)), 6)):
            with self.subTest(zone=zone, hour=hour), tempfile.TemporaryDirectory() as temporary:
                store = bot.ACLStore(Path(temporary) / "acl.json", 100)
                store.set_quota(200, 1)
                service = bot.Bot(MagicMock(), store)
                boundary = datetime(2026, 10, 4, hour, tzinfo=zone).timestamp()
                with patch.object(bot, "BOT_TIMEZONE", zone), patch.object(bot, "DAILY_RESET_HOUR", hour):
                    for now in (boundary - 60, boundary - 50, boundary + 60, boundary + 70):
                        with patch.object(bot.time, "time", return_value=now):
                            self.assertTrue(service.consume_inline_once(200, "https://x.com/example/status/123"))
                            self.assertEqual(store.usage_summary(bot.bot_date()), (1, 1))
                    with patch.object(bot.time, "time", return_value=boundary + 80):
                        self.assertFalse(service.consume_inline_once(200, "https://x.com/example/status/456"))
                service.stop()
                service.inline_executor.shutdown(wait=True)

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

    def test_reply_survives_deleted_original_message(self):
        api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
        api.call = MagicMock()
        api.send_message(100, "done", reply_to=42)
        payload = api.call.call_args.args[1]
        self.assertEqual(json.loads(payload["reply_parameters"]), {
            "message_id": 42, "allow_sending_without_reply": True,
        })

    def test_deploy_runs_tests_before_replacing_installed_bot(self):
        script = self.deploy_script().read_text(encoding="utf-8")
        self.assertLess(
            script.index('"$candidate_venv/bin/python" -m unittest -q test_bot.py'),
            script.index('install -o root -g root -m 0755 "$SOURCE_DIR/bot.py"'),
        )
        self.assertLess(
            script.index('"$candidate_venv/bin/python" -m unittest -q test_bot.py'),
            script.index('ln -s "$candidate_venv" "$INSTALL_DIR/venv"'),
        )
        self.assertIn('mv -- "$rollback_dir/venv" "$INSTALL_DIR/venv"', script)
        self.assertNotIn('SYNC_ROLE', script)
        self.assertNotIn('access_sync_endpoint.sh', script)
        self.assertIn('rollback_dir=$(mktemp -d "$INSTALL_DIR/.deploy-rollback.', script)
        self.assertIn("rollback_deploy()", script)
        self.assertIn("trap rollback_deploy ERR", script)
        self.assertIn('chmod 0755 "$candidate_venv"', script)
        self.assertLess(
            script.index('chmod 0755 "$candidate_venv"'),
            script.index('ln -s "$candidate_venv" "$INSTALL_DIR/venv"'),
        )
        self.assertIn('sleep 3\nsystemctl is-active --quiet "$SERVICE"', script)
        self.assertIn('systemctl show "$SERVICE" -p MainPID --value', script)
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
            ) as save_offset, patch.object(bot, "cleanup_stale_media"):
                service.start()

            self.assertEqual(service.handle_update.call_count, 2)
            self.assertEqual(
                [call.args[0] for call in save_offset.call_args_list], [2, 3]
            )


class PublicReleaseLanguageTests(unittest.TestCase):
    def test_regular_user_copy_is_short_and_does_not_expose_implementation_details(self):
        private_terms = ("FxTwitter", "gallery-dl", "yt-dlp", "Cookies", "Netscape", "STATE_DIR",
                         "佇列", "队列", "キュー", "queue", "自動", "自动", "automatically")
        for language, messages in bot.PUBLIC_TEXT.items():
            with self.subTest(language=language):
                self.assertEqual(messages["apply_auto_approved"], messages["approved"])
                self.assertNotIn("50 MB", messages["help_allowed"])
                self.assertIn("Telegram", messages["video_oversized"])
                self.assertIn("50 MB", messages["video_oversized"])
                self.assertNotIn("{names}", messages["images_skipped"])
                self.assertLessEqual(len(messages["help_allowed"]), 260)
                for key, text in messages.items():
                    if key == "start_owner":
                        continue
                    for term in private_terms:
                        self.assertNotIn(term.lower(), text.lower(), (language, key, term))
        api = bot.TelegramAPI("12345678:test-token-value-for-unit-tests")
        api.call = MagicMock()
        api.configure_profile()
        for call in api.call.call_args_list:
            data = call.args[1]
            text = data.get("description") or data["short_description"]
            self.assertLessEqual(len(text), 220)
            for term in private_terms + ("uncompressed", "未壓縮", "未压缩", "インライン"):
                self.assertNotIn(term.lower(), text.lower())

    def test_every_role_can_select_four_languages_and_preferences_survive_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            store = bot.ACLStore(path, 100)
            store.set_quota(300, None)
            store.set_quota(200, 75)
            store.consume(200)
            api = MagicMock()
            service = bot.Bot(api, store)
            for user_id in (100, 300, 200, 400):
                self.assertEqual(store.language(user_id), "zh")
                for language in ("zh-cn", "zh", "en", "ja"):
                    with self.subTest(user_id=user_id, language=language):
                        callback = {"id": "language", "from": {"id": user_id, "language_code": "zh-hans"},
                                    "message": {"message_id": 1, "chat": {"id": user_id}}}
                        service.handle_callback({**callback, "data": "public:language"})
                        row = api.edit_message.call_args.args[3]["inline_keyboard"][0]
                        self.assertEqual([item["callback_data"] for item in row], ["lang:zh-cn", "lang:zh", "lang:en", "lang:ja"])
                        service.handle_callback({**callback, "data": "lang:" + language})
                        self.assertEqual(store.language(user_id), language)
                        self.assertEqual(bot.ACLStore(path, 100).language(user_id), language)
                        self.assertEqual(api.answer_callback.call_args.args[1], bot.public_text(language, "language_set"))
                        if store.is_admin(user_id):
                            with bot.language_scope(language):
                                self.assertEqual(api.edit_message.call_args.args[3], bot.owner_keyboard())
                            service.handle_callback({**callback, "data": "nav:help"})
                            with bot.language_scope(language):
                                self.assertEqual(api.edit_message.call_args.args[2], bot.owner_help_text(user_id == 100))
                                self.assertEqual(api.edit_message.call_args.args[3], bot.owner_keyboard())
                store.set_language(user_id, "zh")
            self.assertEqual([store.quota(user_id) for user_id in (100, 300, 200, 400)], [None, None, 75, 0])
            self.assertEqual(store.data["users"]["200"]["usage_count"], 1)
            service.stop()

    def test_default_language_ignores_old_deployment_setting_and_telegram_language(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"OWNER_LANGUAGE": "en"}):
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            store.observe({"id": 300, "language_code": "zh-hans"})
            self.assertEqual([store.language(user_id) for user_id in (100, 200, 300)], ["zh"] * 3)
            self.assertFalse(hasattr(bot, "OWNER_LANGUAGE"))

    def test_legacy_mode_flags_no_longer_change_admin_access_or_regular_user_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "acl.json"
            path.write_text(json.dumps({"owner_id": 100, "external_access_enabled": False,
                "users": {"100": {"quota": None, "management_mode": False},
                          "200": {"quota": None, "management_mode": False},
                          "300": {"quota": 75, "management_mode": True}}}), encoding="utf-8")
            before = path.read_bytes()
            bot.ACLStore.read_access_snapshot(path)
            self.assertEqual(path.read_bytes(), before)
            store = bot.ACLStore(path, 100)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(all("management_mode" not in row for row in persisted["users"].values()))
            self.assertTrue(all("management_mode" not in row for row in store.data["users"].values()))
            self.assertFalse(hasattr(store, "toggle_management_mode"))
            api = MagicMock()
            service = bot.Bot(api, store)
            self.assertTrue(service.can_process(100))
            self.assertTrue(service.can_process(200))
            self.assertFalse(service.can_process(300))
            service.handle_callback({"id": "retired", "data": "managementtoggle:0", "from": {"id": 200},
                                     "message": {"message_id": 1, "chat": {"id": 200}}})
            api.edit_message.assert_not_called()
            self.assertEqual(api.answer_callback.call_args.args[1], bot.admin_text("invalid_action"))
            service.handle_callback({"id": "admin", "data": "nav:users", "from": {"id": 200},
                                     "message": {"message_id": 1, "chat": {"id": 200}}})
            self.assertEqual(api.edit_message.call_args.args[2], "用戶管理")
            self.assertEqual(store.quota(300), 75)
            for language in bot.PUBLIC_TEXT:
                store.set_language(200, language)
                self.assertNotIn("managementtoggle", str(bot.owner_keyboard()))
                for retired in ("管理模式", "管理モード", "Management mode"):
                    self.assertNotIn(retired, service.system_status_text(200))
            service.stop()

    def test_admin_language_changes_are_private_and_do_not_grant_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            api = MagicMock()
            service = bot.Bot(api, store)
            for user_id in (100, 200):
                for data in ("lang:zh-cn", "public:language"):
                    service.handle_callback({"id": "group", "data": data, "from": {"id": user_id},
                                             "message": {"message_id": 1, "chat": {"id": -1000}}})
                    self.assertEqual(store.language(user_id), "zh")
                    self.assertEqual(api.answer_callback.call_args.args[1], bot.admin_text("private_only"))
            service.handle_callback({"id": "ordinary", "data": "lang:zh-cn", "from": {"id": 300},
                                     "message": {"message_id": 1, "chat": {"id": 300}}})
            self.assertEqual(store.language(300), "zh-cn")
            self.assertEqual(store.quota(300), 0)
            self.assertFalse(store.is_admin(300))
            service.handle_callback({"id": "ordinary", "data": "lang:invalid", "from": {"id": 300},
                                     "message": {"message_id": 1, "chat": {"id": 300}}})
            self.assertEqual(store.language(300), "zh-cn")
            self.assertEqual(api.answer_callback.call_args.args[1], bot.public_text("zh-cn", "unsupported_language"))
            service.stop()

    def test_owner_and_admin_runtime_outputs_follow_their_own_language(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            store.set_language(200, "en")
            api = MagicMock()
            service = bot.Bot(api, store)
            for language, status, report in (("zh-cn", "系统状态", "每日使用简报"), ("zh", "系統狀態", "每日使用簡報"),
                                               ("en", "System status", "Daily usage report"), ("ja", "システム状態", "日次利用レポート")):
                store.set_language(100, language)
                service.handle_update({"message": {"message_id": 1, "chat": {"id": 100}, "from": {"id": 100}, "text": "/start"}})
                self.assertEqual(api.send_message.call_args.args[1], bot.public_text(language, "start_owner"))
                service.handle_owner_command(100, 2, 100, "/status", "")
                self.assertIn(status, api.send_message.call_args.args[1])
                service.handle_owner_command(100, 2, 100, "/help", "")
                with bot.language_scope(language):
                    self.assertEqual(api.send_message.call_args.args[1], bot.owner_help_text())
                service.handle_owner_command(200, 2, 200, "/status", "")
                self.assertIn("System status", api.send_message.call_args.args[1])
                service.maybe_send_daily_report(force=True)
                self.assertIn(report, api.send_message.call_args.args[1])
                self.assertEqual(bot.ui_language(), "zh")
            service.stop()

    def test_concurrent_admin_callbacks_do_not_mix_languages_and_restore_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.set_quota(200, None)
            store.set_language(100, "zh-cn")
            store.set_language(200, "en")
            api = MagicMock()
            service = bot.Bot(api, store)
            barrier = bot.threading.Barrier(2)
            original = bot.user_menu_keyboard
            def keyboard():
                barrier.wait(timeout=3)
                return original()
            def callback(user_id):
                service.handle_callback({"id": str(user_id), "data": "nav:users", "from": {"id": user_id},
                                         "message": {"message_id": 1, "chat": {"id": user_id}}})
                self.assertEqual(bot.ui_language(), "zh")
            with patch.object(bot, "user_menu_keyboard", side_effect=keyboard), bot.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(callback, user_id) for user_id in (100, 200)]
                for future in futures:
                    future.result(timeout=5)
            outputs = {call.args[0]: call.args for call in api.edit_message.call_args_list}
            self.assertIn("用户列表", str(outputs[100][3]))
            self.assertIn("Users", str(outputs[200][3]))
            api.edit_message.side_effect = RuntimeError("send failed")
            with bot.language_scope("ja"), self.assertRaises(RuntimeError):
                service.handle_callback({"id": "error", "data": "nav:status", "from": {"id": 100},
                                         "message": {"message_id": 1, "chat": {"id": 100}}})
            self.assertEqual(bot.ui_language(), "zh")
            service.stop()

    def test_inline_cache_keeps_language_specific_results_separate(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bot, "build_inline_results", side_effect=lambda url, debug: [{"id": bot.ui_language()}]) as builder:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            store.add(200)
            store.add(300)
            store.set_language(200, "zh-cn")
            store.set_language(300, "en")
            api = MagicMock()
            service = bot.Bot(api, store)
            for user_id, expected in ((200, "zh-cn"), (300, "en"), (200, "zh-cn")):
                service._process_inline_query(str(user_id), "https://x.com/example/status/123", False, user_id)
                self.assertEqual(api.answer_inline_query.call_args.args[1], [{"id": expected}])
                self.assertEqual(bot.ui_language(), "zh")
            self.assertEqual(builder.call_count, 2)
            service.stop()

    def test_language_navigation_cancels_pending_admin_input_without_changing_quotas(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = bot.ACLStore(Path(temporary) / "acl.json", 100)
            api = MagicMock()
            service = bot.Bot(api, store)
            for data in ("public:language", "lang:zh-cn"):
                service.pending_default_quotas[100] = 100
                service.pending_bulk_quotas[100] = None
                service.pending_user_searches.add(100)
                service.handle_callback({"id": "language", "data": data, "from": {"id": 100},
                                         "message": {"message_id": 1, "chat": {"id": 100}}})
                self.assertNotIn(100, service.pending_default_quotas)
                self.assertNotIn(100, service.pending_bulk_quotas)
                self.assertNotIn(100, service.pending_user_searches)
                self.assertEqual(store.default_daily_limit, 50)
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
        self.assertTrue(all(len(values) == 4 for values in bot.ADMIN_TEXT.values()))
        for key, values in bot.ADMIN_TEXT.items():
            placeholders = [set(bot.re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^{}]+)?\}", value)) for value in values]
            self.assertEqual(placeholders[0], placeholders[1], key)
            self.assertEqual(placeholders[0], placeholders[2], key)
            self.assertEqual(placeholders[0], placeholders[3], key)


if __name__ == "__main__":
    unittest.main()
