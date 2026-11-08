"""GM-005 recovery cases evolved into GM-007 delivery/preview regressions.

Real scraper comparison, notifier and state code run against synthetic pages and
mock HTTP responses. Git push rejection uses only temporary local repositories.
Remaining merge limitations are asserted deliberately for the recovery runbook.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import requests
from tests.test_delivery_ledger import ContentsAPI

# Do not load operator configuration when importing production modules.
with mock.patch("dotenv.load_dotenv"):
    import bot_realtime as rt
    import go_manga_monitor as gm

ROOT = Path(__file__).resolve().parents[1]
URL = "https://example.invalid/manga/recovery-fixture/"
DAY = "2026-11-08"


def baseline(chapter):
    return {"title": "Recovery fixture", "last_chapter": str(chapter),
            "last_chapter_date": DAY, "last_check": DAY + "T00:00:00"}


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class RecoveryScenarios(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.out, self.err = io.StringIO(), io.StringIO()
        self.stack.enter_context(redirect_stdout(self.out))
        self.stack.enter_context(redirect_stderr(self.err))
        # Any unmocked outbound HTTP fails the test even if bot code catches it.
        self.network = self.stack.enter_context(mock.patch(
            "requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP")))
        self.urlopen = self.stack.enter_context(mock.patch(
            "urllib.request.OpenerDirector.open", side_effect=AssertionError("Unexpected URL open")))
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "CHECK_URLS": "", "BACKFILL_TODAY": "", "BACKFILL_DATE": "",
            "BACKFILL_DRYRUN": "", "CHAIN_PAT": "", "WORKFLOW_PAT": "",
            "GITHUB_ACTIONS": "", "GITHUB_TOKEN": "",
        }))
        self.path = self.tmp / "monitor_state.json"
        self.path.write_text(json.dumps({URL: baseline(10)}))
        self.bookmarks = self.tmp / "bookmarks.json"
        self.bookmarks.write_text("{}")
        self.bot_state = self.tmp / "bot_state.json"
        self.bot_state.write_text("{}")
        for key, value in {
            "STATE_FILE": self.path, "BOOKMARKS_FILE": self.bookmarks,
            "BOT_STATE_FILE": self.bot_state, "REQUEST_DELAY": 0,
            "DELIVERY_FILE": self.tmp / "delivery_state.json",
            "TELEGRAM_BOT_TOKEN": "fixture-token", "TELEGRAM_CHAT_ID": "fixture-chat",
        }.items():
            self.stack.enter_context(mock.patch.object(gm, key, value, create=True))
        self.stack.enter_context(mock.patch.object(gm, "site_today", return_value=DAY))
        self.stack.enter_context(mock.patch.object(rt.time, "sleep"))
        chapter = gm.Chapter("11", "Chapter 11", URL + "11/", DAY)
        self.manga = gm.Manga("Recovery fixture", URL, "11", chapter.url, DAY,
                              "MANGA", "Ongoing", "N/A", "", [chapter])
        self.scraper = gm.GoMangaScraper()
        self.stack.enter_context(mock.patch.object(self.scraper, "scrape_list_page", return_value=[
            {"url": URL, "title": self.manga.title, "latest_chapter": "11"}]))
        self.stack.enter_context(mock.patch.object(self.scraper, "scrape_manga_detail", return_value=self.manga))
        self.calls, self.delivered, self.actions = [], [], []
        self.stack.enter_context(mock.patch.object(requests, "post", side_effect=self.post))
        self.stack.enter_context(mock.patch.object(requests, "get", side_effect=self.get))

    def tearDown(self):
        self.network.assert_not_called()
        self.urlopen.assert_not_called()

    def post(self, url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        if endpoint == "setMyCommands":
            return Response({"ok": True, "result": True})
        self.assertIn(endpoint, ("sendMessage", "sendPhoto"))
        if endpoint == "sendMessage" and len(kwargs["json"]["text"]) > 4096:
            return Response({"ok": False, "error_code": 400, "description": "message too long"})
        action = self.actions.pop(0) if self.actions else "accepted"
        if action == "rejected":
            return Response({"ok": False, "error_code": 400, "description": "fixture rejection"})
        if action == "server_error":
            return Response({"ok": False, "error_code": 500, "description": "fixture server failure"}, 500)
        if action == "redirect":
            return Response({"ok": False, "error_code": 400, "description": "fixture redirect"}, 302)
        self.delivered.append((endpoint, kwargs["json"]))
        if action == "accepted_timeout":
            raise requests.Timeout("fixture acknowledgement lost after acceptance")
        if action == "malformed":
            return Response({"ok": True, "result": {}})
        return Response({"ok": True, "result": {"message_id": len(self.delivered)}})

    def get(self, url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        self.assertEqual(endpoint, "getChat")
        return Response({"ok": True, "result": {"type": "private", "first_name": "Fixture"}})

    def cycle(self, path=None):
        state = gm.StateManager(path or self.path)
        rt._scrape_and_notify(self.scraper, state, gm.BookmarkManager(self.bookmarks),
                             gm.TelegramNotifier("fixture-token", "fixture-chat"))
        return state

    def cycle_on_day(self, day):
        with mock.patch.object(gm, "site_today", return_value=day), \
                mock.patch.object(gm, "datetime", wraps=gm.datetime) as clock:
            clock.now.return_value = datetime.fromisoformat(day + "T12:00:00+07:00")
            return self.cycle()

    def run_main(self, **env):
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(gm, "GoMangaScraper", return_value=self.scraper), \
                mock.patch.object(rt, "MAX_RUNTIME", 1), \
                mock.patch.object(rt, "SCRAPE_INTERVAL", 0), \
                mock.patch.object(rt.time, "time", side_effect=[1000, 1000, 1001, 1002]):
            return rt.main()

    def chapter_on_disk(self, path=None):
        return json.loads((path or self.path).read_text())[URL]["last_chapter"]

    def test_normal_success_persists_and_restart_does_not_resend(self):
        self.cycle()
        self.assertEqual(self.chapter_on_disk(), "11")
        self.cycle()
        self.assertEqual(len(self.delivered), 1)

    def test_normal_explicit_rejection_keeps_baseline_for_retry(self):
        self.actions = ["rejected"]
        self.cycle()
        self.assertEqual(self.chapter_on_disk(), "10")
        self.assertEqual(self.delivered, [])
        self.cycle()
        self.assertEqual(self.chapter_on_disk(), "11")

    def test_accepted_send_with_lost_ack_is_held_after_restart(self):
        self.actions = ["accepted_timeout"]
        self.cycle()
        self.assertEqual(self.chapter_on_disk(), "10")
        self.cycle()
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(), "10")

    def test_send_then_failed_state_save_reuses_receipt_after_restart(self):
        with mock.patch.object(gm.StateManager, "save", side_effect=OSError("fixture disk full")):
            with self.assertRaises(OSError):
                self.cycle()
        self.assertEqual(self.chapter_on_disk(), "10")
        self.cycle()
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(), "11")

    def test_accepted_photo_with_lost_ack_is_held_without_text_fallback(self):
        self.manga.cover_image = "https://example.invalid/cover.jpg"
        self.actions = ["accepted_timeout", "accepted"]
        self.cycle()
        self.cycle()
        self.assertEqual([item[0] for item in self.delivered], ["sendPhoto"])
        self.assertEqual(self.chapter_on_disk(), "10")

    def test_rejected_favorites_summary_retries_without_resending_card(self):
        self.bookmarks.write_text(json.dumps({URL: {"title": self.manga.title}}))
        self.actions = ["accepted", "rejected"]
        self.cycle()
        self.cycle()
        self.assertEqual(self.calls, ["sendMessage", "sendMessage", "sendMessage"])
        self.assertEqual(len(self.delivered), 2)
        self.assertEqual(self.chapter_on_disk(), "11")

    def test_targeted_rejection_keeps_baseline_for_retry(self):
        self.actions = ["rejected"]
        self.run_main(CHECK_URLS=URL)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.chapter_on_disk(), "10")
        self.assertNotIn("notified", self.out.getvalue())

    def test_targeted_first_seen_future_only_does_not_send_or_create_baseline(self):
        self.path.write_text("{}")
        self.manga.chapters[0].date = "2026-11-09"
        self.run_main(CHECK_URLS=URL)
        self.assertEqual(self.delivered, [])
        self.assertEqual(json.loads(self.path.read_text()), {})

    def test_targeted_future_only_does_not_lower_a_renumbered_baseline(self):
        self.path.write_text(json.dumps({URL: baseline(20)}))
        self.manga.chapters[0].date = "2026-11-09"
        self.run_main(CHECK_URLS=URL)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.chapter_on_disk(), "20")

    def test_targeted_mixed_dates_uses_latest_visible_chapter(self):
        self.manga.chapters.insert(0, gm.Chapter("12", "Future chapter", URL + "12/", "2026-11-09"))
        self.manga.latest_chapter = "12"
        self.run_main(CHECK_URLS=URL)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(), "11")
        self.assertNotIn(URL + "12/", self.delivered[0][1]["text"])

    def test_targeted_photo_caption_reports_selected_visible_chapter(self):
        self.manga.cover_image = "https://example.invalid/cover.jpg"
        self.manga.chapters.insert(0, gm.Chapter("12", "Future chapter", URL + "12/", "2026-11-09"))
        self.manga.latest_chapter = "12"
        self.run_main(CHECK_URLS=URL)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.delivered[0][0], "sendPhoto")
        self.assertIn("10 → 11", self.delivered[0][1]["caption"])
        self.assertNotIn("10 → 12", self.delivered[0][1]["caption"])

    def test_normal_first_seen_future_only_notifies_on_release_day(self):
        self.path.write_text("{}")
        self.manga.chapters[0].date = "2026-11-09"
        self.cycle_on_day("2026-11-08")
        self.assertEqual(json.loads(self.path.read_text()), {})
        self.assertEqual(self.delivered, [])
        self.cycle_on_day("2026-11-09")
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(), "11")

    def test_normal_renumber_future_only_notifies_on_release_day(self):
        self.path.write_text(json.dumps({URL: baseline(20)}))
        self.manga.chapters[0].date = "2026-11-09"
        self.cycle_on_day("2026-11-08")
        self.assertEqual(self.chapter_on_disk(), "20")
        self.assertEqual(self.delivered, [])
        self.cycle_on_day("2026-11-09")
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(), "11")

    def test_normal_first_seen_missing_detail_is_retried(self):
        self.path.write_text("{}")
        self.scraper.scrape_manga_detail.side_effect = [None, self.manga]
        self.cycle_on_day(DAY)
        self.assertEqual(json.loads(self.path.read_text()), {})
        self.cycle_on_day(DAY)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(), "11")

    def test_normal_renumber_missing_detail_keeps_baseline_until_confirmation(self):
        self.path.write_text(json.dumps({URL: baseline(20)}))
        self.scraper.scrape_manga_detail.side_effect = [None, self.manga]
        self.cycle_on_day(DAY)
        self.assertEqual(self.chapter_on_disk(), "20")
        self.cycle_on_day(DAY)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(), "11")

    def test_invalid_startup_state_blocks_all_telegram_and_preserves_bytes(self):
        for path in (self.path, self.bookmarks, self.bot_state):
            for invalid in (b'{"broken":', b'[]', b'null', b'{"bad": NaN}'):
                with self.subTest(file=path.name, invalid=invalid):
                    before = {p: p.read_bytes() for p in (self.path, self.bookmarks, self.bot_state)}
                    path.write_bytes(invalid)
                    self.calls.clear()
                    try:
                        with self.assertRaises(ValueError):
                            self.run_main()
                        self.assertEqual(self.calls, [])
                        self.assertEqual(path.read_bytes(), invalid)
                    finally:
                        for p, raw in before.items():
                            p.write_bytes(raw)

    def test_unreadable_existing_startup_state_blocks_before_telegram(self):
        original_open = open
        def denied(file, mode="r", *args, **kwargs):
            if isinstance(file, (str, Path)) and Path(file) == self.path and "r" in mode:
                raise PermissionError("fixture-sensitive-error-not-for-logs")
            return original_open(file, mode, *args, **kwargs)
        before = self.path.read_bytes()
        with mock.patch("builtins.open", side_effect=denied):
            with self.assertRaises(ValueError) as error:
                self.run_main()
        self.assertNotIn("fixture-sensitive-error-not-for-logs", str(error.exception))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_actions_state_is_not_silently_initialized(self):
        for path in (self.path, self.bookmarks, self.bot_state):
            with self.subTest(file=path.name):
                raw = path.read_bytes()
                path.unlink()
                self.calls.clear()
                try:
                    with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
                        with self.assertRaises(ValueError):
                            self.run_main()
                    self.assertEqual(self.calls, [])
                    self.assertFalse(path.exists())
                finally:
                    path.write_bytes(raw)

    def test_failed_startup_leaves_corrupt_original_for_recovery_snapshot(self):
        raw = b'{"broken":'
        self.path.write_bytes(raw)
        with self.assertRaises(ValueError):
            self.run_main()
        destination = self.tmp / "recovery"
        result = subprocess.run([sys.executable, str(ROOT / ".github/scripts/snapshot_state.py"),
            "--source", str(self.tmp), "--destination", str(destination)],
            text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        manifest = json.loads((destination / "artifact/manifest.json").read_text())
        self.assertEqual(manifest["files"]["monitor_state.json"]["status"], "invalid_json")
        self.assertEqual((destination / "original/monitor_state.json").read_bytes(), raw)
        self.assertEqual((destination / "artifact/monitor_state.json").read_bytes(), raw)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.calls, [])

    def test_symlink_startup_state_is_rejected_without_touching_its_target(self):
        for path in (self.path, self.bookmarks, self.bot_state):
            with self.subTest(file=path.name):
                raw = path.read_bytes()
                target = self.tmp / ("target-" + path.name)
                target.write_bytes(raw)
                path.unlink()
                path.symlink_to(target)
                self.calls.clear()
                try:
                    with self.assertRaises(ValueError):
                        self.run_main()
                    self.assertEqual(self.calls, [])
                    self.assertEqual(target.read_bytes(), raw)
                finally:
                    path.unlink()
                    path.write_bytes(raw)

    def test_missing_local_state_remains_available_for_first_installation(self):
        path = self.tmp / "new-state.json"
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}):
            self.assertEqual(gm.StateManager(path).data, {})
            self.assertEqual(gm.BookmarkManager(path).data, {})
            self.assertEqual(gm.load_bot_state(path), {})
        self.assertFalse(path.exists())

    def test_backfill_preview_has_no_telegram_calls_or_state_writes(self):
        original = self.path.read_bytes()
        update = {"manga": self.manga, "new_chapters": self.manga.chapters}
        with mock.patch.object(self.scraper, "collect_updates_for_date", return_value=[update]):
            self.run_main(BACKFILL_TODAY="true", BACKFILL_DRYRUN="true", BACKFILL_DATE=DAY)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_preview_flag_alone_stops_normal_delivery(self):
        self.run_main(BACKFILL_DRYRUN="true")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.chapter_on_disk(), "10")

    def test_targeted_preview_has_no_telegram_calls(self):
        self.run_main(CHECK_URLS=URL, BACKFILL_TODAY="true", BACKFILL_DRYRUN="true")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.chapter_on_disk(), "10")

    def test_preview_needs_no_credentials_and_preserves_all_runtime_bytes(self):
        before = {p: p.read_bytes() for p in (self.path, self.bookmarks, self.bot_state)}
        with mock.patch.object(gm, "TELEGRAM_BOT_TOKEN", ""), mock.patch.object(gm, "TELEGRAM_CHAT_ID", ""):
            self.run_main(BACKFILL_DRYRUN="true")
        self.assertEqual(self.calls, [])
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_one_shot_entrypoint_respects_preview(self):
        before = {p: p.read_bytes() for p in (self.path, self.bookmarks, self.bot_state)}
        with mock.patch.dict(os.environ, {"BACKFILL_DRYRUN": "true"}), \
                mock.patch.object(gm, "GoMangaScraper", return_value=self.scraper):
            gm.main()
        self.assertEqual(self.calls, [])
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_repeated_backfill_does_not_resend_accepted_events(self):
        original = self.path.read_bytes()
        update = {"manga": self.manga, "new_chapters": self.manga.chapters}
        with mock.patch.object(self.scraper, "collect_updates_for_date", return_value=[update]):
            for _ in range(2):
                self.run_main(BACKFILL_TODAY="true", BACKFILL_DATE=DAY)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.path.read_bytes(), original)

    def test_scrape_error_is_logged_but_main_returns_normally(self):
        with mock.patch.object(self.scraper, "check_updates", side_effect=OSError("fixture failure")):
            self.assertIsNone(self.run_main())
        self.assertIn("[ERROR] scrape cycle", self.err.getvalue())
        self.assertEqual(self.delivered, [])

    def test_push_rejected_after_send_uses_remote_receipt_on_next_runner(self):
        from delivery_ledger import GitHubStore
        api = ContentsAPI()
        self.stack.enter_context(mock.patch.object(gm, "make_store",
            side_effect=lambda _: GitHubStore("fixture/repo", "fixture-token", client=api), create=True))
        def git(*args, cwd, check=True):
            return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)

        remote, runner = self.tmp / "remote.git", self.tmp / "runner"
        git("init", "--bare", str(remote), cwd=self.tmp)
        runner.mkdir()
        git("init", "-b", "master", cwd=runner)
        git("config", "user.name", "Fixture", cwd=runner)
        git("config", "user.email", "fixture@example.invalid", cwd=runner)
        state = runner / "monitor_state.json"
        state.write_bytes(self.path.read_bytes())
        git("add", ".", cwd=runner)
        git("commit", "-m", "baseline", cwd=runner)
        git("remote", "add", "origin", str(remote), cwd=runner)
        git("push", "origin", "master", cwd=runner)
        hook = remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.cycle(state)
        self.assertEqual(self.chapter_on_disk(state), "11")
        git("add", ".", cwd=runner)
        git("commit", "-m", "sent locally", cwd=runner)
        failed = git("push", "origin", "master", cwd=runner, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("hook declined", failed.stderr)
        next_runner = self.tmp / "next-runner"
        git("clone", "-b", "master", str(remote), str(next_runner), cwd=self.tmp)
        next_state = next_runner / "monitor_state.json"
        self.assertEqual(self.chapter_on_disk(next_state), "10")
        self.cycle(next_state)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(next_state), "11")

    def test_rejected_photo_can_fall_back_to_one_accepted_text(self):
        self.manga.cover_image = "https://example.invalid/cover.jpg"
        self.actions = ["rejected", "accepted"]
        self.cycle()
        self.cycle()
        self.assertEqual(self.calls, ["sendPhoto", "sendMessage"])
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(), "11")

    def test_malformed_accepted_response_is_held(self):
        self.actions = ["malformed"]
        self.cycle()
        self.cycle()
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(), "10")

    def test_server_error_is_held_without_automatic_retry(self):
        self.actions = ["server_error"]
        self.cycle()
        self.cycle()
        self.assertEqual(self.calls, ["sendMessage"])
        self.assertEqual(self.chapter_on_disk(), "10")

    def test_redirect_is_unknown_and_never_triggers_photo_fallback(self):
        self.manga.cover_image = "https://example.invalid/cover.jpg"
        self.actions = ["redirect", "accepted"]
        self.cycle()
        self.cycle()
        self.assertEqual(self.calls, ["sendPhoto"])
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.chapter_on_disk(), "10")

    def test_one_shot_retries_pending_summary_without_new_cards(self):
        self.bookmarks.write_text(json.dumps({URL: {"title": self.manga.title}}))
        self.actions = ["accepted", "rejected"]
        self.cycle()
        with mock.patch.object(gm, "GoMangaScraper", return_value=self.scraper), \
                mock.patch.object(gm, "process_telegram_commands", return_value=0):
            gm.main()
        self.assertEqual(len(self.delivered), 2)

    def test_empty_backfill_retries_queued_summary(self):
        self.bookmarks.write_text(json.dumps({URL: {"title": self.manga.title}}))
        self.actions = ["accepted", "rejected"]
        self.cycle()
        with mock.patch.object(self.scraper, "collect_updates_for_date", return_value=[]):
            self.run_main(BACKFILL_TODAY="true")
        self.assertEqual(len(self.delivered), 2)

    def test_large_favorite_backlog_drains_in_telegram_sized_batches(self):
        notifier = gm.TelegramNotifier("fixture-token", "fixture-chat")
        for n in range(12):
            manga = gm.Manga.from_dict(self.manga.to_dict())
            manga.url = URL + str(n) + "/"
            manga.title = str(n) + "x" * 400
            self.assertTrue(notifier.send_update({"manga": manga, "new_chapters": manga.chapters,
                                                  "is_bookmarked": True}))
        for _ in range(12):
            self.assertTrue(notifier.flush_favorites_summary())
        self.assertEqual(notifier.delivery_ledger().pending_favorites(), [])
        self.assertTrue(all(len(payload.get("text", "")) <= 4096 for _, payload in self.delivered))

    def test_summary_lost_ack_is_not_retried_after_card_baseline_advance(self):
        self.bookmarks.write_text(json.dumps({URL: {"title": self.manga.title}}))
        self.actions = ["accepted", "accepted_timeout"]
        self.cycle()
        self.cycle()
        self.assertEqual(len(self.delivered), 2)
        self.assertEqual(self.calls, ["sendMessage", "sendMessage"])

    def test_failed_remote_intent_write_prevents_telegram(self):
        from delivery_ledger import GitHubStore
        api = ContentsAPI()
        api.put_status = 403
        self.stack.enter_context(mock.patch.object(gm, "make_store",
            side_effect=lambda _: GitHubStore("fixture/repo", "fixture-token", client=api), create=True))
        self.cycle()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.chapter_on_disk(), "10")

    def test_new_chapter_is_not_blocked_by_accepted_previous_chapter(self):
        self.cycle()
        self.manga.chapters.insert(0, gm.Chapter("12", "Chapter 12", URL + "12/", DAY))
        self.scraper.scrape_list_page.return_value[0]["latest_chapter"] = "12"
        self.cycle()
        self.assertEqual(len(self.delivered), 2)
        self.assertEqual(self.chapter_on_disk(), "12")

    def test_recovery_merge_preserves_newer_remote_chapter_entries_and_offset(self):
        for filename, base, ours, theirs, expected in [
            ("monitor_state.json", {URL: baseline(10)}, {URL: baseline(11)},
             {URL: baseline(12), "remote-only": baseline(7)},
             {URL: baseline(12), "remote-only": baseline(7)}),
            ("bot_state.json", {"telegram_offset": 100}, {"telegram_offset": 101},
             {"telegram_offset": 110, "remote-only": True},
             {"telegram_offset": 110, "remote-only": True}),
        ]:
            with self.subTest(filename=filename):
                target, b, t = (self.tmp / n for n in (filename, "base.json", "theirs.json"))
                for path, data in [(target, ours), (b, base), (t, theirs)]:
                    path.write_text(json.dumps(data))
                subprocess.run([sys.executable, str(ROOT / "merge_state.py"), str(target), str(b), str(t)],
                               check=True, capture_output=True, text=True)
                self.assertEqual(json.loads(target.read_text()), expected)

    def test_merge_is_not_a_global_monotonic_guard_when_remote_equals_base(self):
        target, base, theirs = (self.tmp / n for n in ("monitor_state.json", "base.json", "theirs.json"))
        for path, chapter in [(target, 11), (base, 12), (theirs, 12)]:
            path.write_text(json.dumps({URL: baseline(chapter)}))
        subprocess.run([sys.executable, str(ROOT / "merge_state.py"), str(target), str(base), str(theirs)],
                       check=True, capture_output=True, text=True)
        self.assertEqual(self.chapter_on_disk(target), "11")

    def test_preview_disables_self_chain_even_with_pat(self):
        spec = importlib.util.spec_from_file_location("gm005_chain", ROOT / ".github/scripts/dispatch_next_run.py")
        chain = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {spec.name: chain}):
            spec.loader.exec_module(chain)
        request = mock.Mock(return_value=chain.ApiResponse(204))
        result = chain.dispatch_next({
            "CHAIN_PAT": "fixture-token", "GITHUB_REPOSITORY": "fixture/repo",
            "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1",
            "BACKFILL_TODAY": "true", "BACKFILL_DRYRUN": "true",
        }, request=request, sleep=mock.Mock())
        self.assertEqual(result.state, "disabled")
        self.assertEqual(result.reason, "preview_requested")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
