"""GM-005: characterize recovery risks; these tests do not fix delivery semantics.

Real scraper comparison, notifier and state code run against synthetic pages and
mock HTTP responses. Git push rejection uses only temporary local repositories.
Known unsafe behavior is asserted deliberately so the runbook stays accurate.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
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
    def __init__(self, payload):
        self.payload = payload

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
            "TELEGRAM_BOT_TOKEN": "fixture-token", "TELEGRAM_CHAT_ID": "fixture-chat",
        }.items():
            self.stack.enter_context(mock.patch.object(gm, key, value))
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
        action = self.actions.pop(0) if self.actions else "accepted"
        if action == "rejected":
            return Response({"ok": False, "error_code": 400, "description": "fixture rejection"})
        self.delivered.append((endpoint, kwargs["json"]))
        if action == "accepted_timeout":
            raise requests.Timeout("fixture acknowledgement lost after acceptance")
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

    def test_accepted_send_with_lost_ack_resends_after_restart(self):
        self.actions = ["accepted_timeout"]
        self.cycle()
        self.assertEqual(self.chapter_on_disk(), "10")
        self.cycle()
        self.assertEqual(len(self.delivered), 2)

    def test_send_then_failed_state_save_resends_after_restart(self):
        with mock.patch.object(gm.StateManager, "save", side_effect=OSError("fixture disk full")):
            with self.assertRaises(OSError):
                self.cycle()
        self.assertEqual(self.chapter_on_disk(), "10")
        self.cycle()
        self.assertEqual(len(self.delivered), 2)

    def test_accepted_photo_with_lost_ack_falls_back_to_duplicate_text(self):
        self.manga.cover_image = "https://example.invalid/cover.jpg"
        self.actions = ["accepted_timeout", "accepted"]
        self.cycle()
        self.assertEqual([item[0] for item in self.delivered], ["sendPhoto", "sendMessage"])
        self.assertEqual(self.chapter_on_disk(), "11")

    def test_sent_card_but_rejected_favorites_summary_is_not_retried(self):
        self.bookmarks.write_text(json.dumps({URL: {"title": self.manga.title}}))
        self.actions = ["accepted", "rejected"]
        self.cycle()
        self.cycle()
        self.assertEqual(self.calls, ["sendMessage", "sendMessage"])
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(), "11")

    def test_targeted_rejection_still_advances_baseline_known_gap(self):
        self.actions = ["rejected"]
        self.run_main(CHECK_URLS=URL)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.chapter_on_disk(), "11")
        self.assertIn("notified", self.out.getvalue())

    def test_backfill_dryrun_skips_messages_but_calls_startup_apis(self):
        original = self.path.read_bytes()
        update = {"manga": self.manga, "new_chapters": self.manga.chapters}
        with mock.patch.object(self.scraper, "collect_updates_for_date", return_value=[update]):
            self.run_main(BACKFILL_TODAY="true", BACKFILL_DRYRUN="true", BACKFILL_DATE=DAY)
        self.assertEqual(self.calls, ["setMyCommands", "getChat"])
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_dryrun_flag_alone_does_not_stop_normal_delivery(self):
        self.run_main(BACKFILL_DRYRUN="true")
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.chapter_on_disk(), "11")

    def test_targeted_mode_takes_precedence_over_backfill_dryrun(self):
        self.run_main(CHECK_URLS=URL, BACKFILL_TODAY="true", BACKFILL_DRYRUN="true")
        self.assertEqual(len(self.delivered), 1)

    def test_repeated_backfill_resends_without_changing_baseline(self):
        original = self.path.read_bytes()
        update = {"manga": self.manga, "new_chapters": self.manga.chapters}
        with mock.patch.object(self.scraper, "collect_updates_for_date", return_value=[update]):
            for _ in range(2):
                self.run_main(BACKFILL_TODAY="true", BACKFILL_DATE=DAY)
        self.assertEqual(len(self.delivered), 2)
        self.assertEqual(self.path.read_bytes(), original)

    def test_scrape_error_is_logged_but_main_returns_normally(self):
        with mock.patch.object(self.scraper, "check_updates", side_effect=OSError("fixture failure")):
            self.assertIsNone(self.run_main())
        self.assertIn("[ERROR] scrape cycle", self.err.getvalue())
        self.assertEqual(self.delivered, [])

    def test_push_rejected_after_send_leaves_remote_old_and_next_runner_resends(self):
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
        self.assertEqual(len(self.delivered), 2)

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

    def test_dryrun_does_not_disable_self_chain_or_propagate_to_child(self):
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
        self.assertEqual(result.state, "accepted")
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], "POST")
        self.assertEqual(request.call_args.args[3], {
            "ref": "master", "inputs": {"chained": "yes", "chain_request_id": "1-1"}})


if __name__ == "__main__":
    unittest.main()
