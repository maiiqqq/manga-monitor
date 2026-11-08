"""Offline self-chain outcomes, correlation, secret safety and YAML bootstrap."""
import importlib.util
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".github/scripts/dispatch_next_run.py"


class SelfChainTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HELPER.exists(), "Self-chain helper is not implemented")
        spec = importlib.util.spec_from_file_location("self_chain_under_test", HELPER)
        self.chain = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.chain
        spec.loader.exec_module(self.chain)
        self.env = {
            "CHAIN_PAT": "fixture-pat-do-not-print",
            "GITHUB_REPOSITORY": "maiiqqq/manga-monitor",
            "GITHUB_RUN_ID": "1234",
            "GITHUB_RUN_ATTEMPT": "2",
            "PERSISTENCE_OUTCOME": "failure",
        }

    def exercise(self, responses):
        calls = []
        sleeps = []
        def client(method, url, token, payload=None):
            calls.append((method, url, token, payload))
            index = len(calls) - 1
            return responses[index] if index < len(responses) else self.chain.ApiResponse(None)
        result = self.chain.dispatch_next(self.env, request=client, sleep=sleeps.append)
        return result, calls, sleeps

    def response(self, status, payload=None):
        return self.chain.ApiResponse(status, payload)

    def matching_run(self, **changes):
        run = {"id": 9876, "event": "workflow_dispatch", "head_branch": "master",
               "display_title": "Go-Manga Monitor (chain 1234-2)", "status": "queued"}
        run.update(changes)
        return run

    def test_204_is_accepted_with_one_post_and_unique_correlation(self):
        result, calls, sleeps = self.exercise([self.response(204)])
        self.assertEqual(result.state, "accepted")
        self.assertEqual([c[0] for c in calls], ["POST"])
        self.assertEqual(calls[0][3], {
            "ref": "master", "inputs": {"chained": "yes", "chain_request_id": "1234-2"}})
        self.assertEqual(sleeps, [])

    def test_401_and_403_are_failed_without_retry_or_lookup(self):
        for status in (401, 403):
            with self.subTest(status=status):
                result, calls, sleeps = self.exercise([self.response(status)])
                self.assertEqual(result.state, "failed")
                self.assertEqual(result.reason, "permission_denied")
                self.assertEqual(len(calls), 1)
                self.assertEqual(sleeps, [])

    def test_explicit_rejection_is_failed(self):
        result, calls, _ = self.exercise([self.response(422)])
        self.assertEqual(result.state, "failed")
        self.assertEqual(len(calls), 1)

    def test_500_without_observed_run_is_unknown_and_never_reposts(self):
        result, calls, sleeps = self.exercise(
            [self.response(500)] + [self.response(200, {"workflow_runs": []})] * 3)
        self.assertEqual(result.state, "outcome_unknown")
        self.assertEqual([c[0] for c in calls], ["POST", "GET", "GET", "GET"])
        self.assertEqual(sleeps, [2, 2])

    def test_timeout_after_server_acceptance_is_confirmed_by_correlated_run(self):
        result, calls, _ = self.exercise([
            self.response(None),
            self.response(200, {"workflow_runs": [self.matching_run()]})])
        self.assertEqual(result.state, "accepted")
        self.assertEqual(result.reason, "observed_correlated_run")
        self.assertEqual(result.next_run_id, 9876)
        self.assertEqual([c[0] for c in calls], ["POST", "GET"])

    def test_delayed_run_visibility_is_polled_read_only(self):
        result, calls, sleeps = self.exercise([
            self.response(503), self.response(200, {"workflow_runs": []}),
            self.response(200, {"workflow_runs": [self.matching_run()]})])
        self.assertEqual(result.state, "accepted")
        self.assertEqual([c[0] for c in calls], ["POST", "GET", "GET"])
        self.assertEqual(sleeps, [2])

    def test_unrelated_branch_event_or_request_title_cannot_confirm_acceptance(self):
        bad = [self.matching_run(head_branch="other"),
               self.matching_run(event="schedule"),
               self.matching_run(display_title="Go-Manga Monitor (chain 1234-1)")]
        result, calls, _ = self.exercise(
            [self.response(500)] + [self.response(200, {"workflow_runs": bad})] * 3)
        self.assertEqual(result.state, "outcome_unknown")
        self.assertEqual(len([c for c in calls if c[0] == "POST"]), 1)

    def test_unavailable_or_malformed_lookup_remains_unknown(self):
        result, calls, _ = self.exercise([
            self.response(None), self.response(403), self.response(200, {"unexpected": []}),
            self.response(200, {"workflow_runs": [None, {"id": "not-a-number"}]})])
        self.assertEqual(result.state, "outcome_unknown")
        self.assertLessEqual(len(calls), 4)

    def test_missing_token_is_disabled_without_any_request(self):
        self.env["CHAIN_PAT"] = ""
        result, calls, sleeps = self.exercise([])
        self.assertEqual(result.state, "disabled")
        self.assertEqual(calls, [])
        self.assertEqual(sleeps, [])

    def test_invalid_repository_is_rejected_before_network(self):
        self.env["GITHUB_REPOSITORY"] = "owner/repo/../../other"
        result, calls, _ = self.exercise([])
        self.assertEqual(result.state, "failed")
        self.assertEqual(calls, [])

    def test_summary_separates_persistence_and_unknown_dispatch_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            self.env["GITHUB_STEP_SUMMARY"] = str(summary)
            responses = [self.response(500)] + [self.response(200, {"workflow_runs": []})] * 3
            with mock.patch.dict(os.environ, self.env, clear=True), \
                 mock.patch.object(self.chain, "github_request", side_effect=responses), \
                 mock.patch.object(self.chain.time, "sleep"), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                code = self.chain.main()
            self.assertEqual(code, 1)
            text = summary.read_text() + output.getvalue()
            self.assertIn("| Persistence | failure |", text)
            self.assertIn("| Self-chain | outcome_unknown |", text)
            self.assertIn("cron", text.lower())
            self.assertNotIn("fixture-pat-do-not-print", text)

    def test_disabled_summary_does_not_hide_successful_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.env.update(CHAIN_PAT="", PERSISTENCE_OUTCOME="success",
                            GITHUB_STEP_SUMMARY=str(Path(tmp) / "summary.md"))
            with mock.patch.dict(os.environ, self.env, clear=True), \
                 mock.patch.object(self.chain, "github_request") as request:
                code = self.chain.main()
            self.assertEqual(code, 0)
            self.assertIn("| Persistence | success |", Path(self.env["GITHUB_STEP_SUMMARY"]).read_text())
            request.assert_not_called()

    def test_http_adapter_bounds_requests_and_does_not_follow_redirects(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'{"workflow_runs": []}'
        with mock.patch.object(self.chain.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            try:
                result = self.chain.github_request("GET", "https://api.github.com/repos/o/r/actions/runs", "secret")
            except http.client.HTTPException:
                self.fail("A truncated response must become an unknown result, not abort summary reporting")
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 8)
            self.assertIsNone(build.call_args.args[0].redirect_request(None, None, None, None, None, None))
        self.assertEqual(result.status, 200)
        self.assertEqual(result.payload, {"workflow_runs": []})

    def test_http_adapter_keeps_network_exception_text_out_of_output(self):
        with mock.patch.object(self.chain.urllib.request, "build_opener") as build, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            build.return_value.open.side_effect = TimeoutError("fixture-pat-do-not-print")
            result = self.chain.github_request("POST", "https://api.github.com/repos/o/r/actions/dispatches", "secret", {})
        self.assertIsNone(result.status)
        self.assertEqual(output.getvalue(), "")

    def test_http_adapter_rejects_non_github_destination_before_using_token(self):
        with mock.patch.object(self.chain.urllib.request, "build_opener") as build:
            result = self.chain.github_request("GET", "https://other.example/repos/o/r", "secret")
        self.assertIsNone(result.status)
        build.assert_not_called()

    def test_http_adapter_handles_truncated_response_without_leaking_body(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"secret-body", 30)
        with mock.patch.object(self.chain.urllib.request, "build_opener") as build, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            build.return_value.open.return_value = response
            result = self.chain.github_request("GET", "https://api.github.com/repos/o/r/actions/runs", "secret")
        self.assertIsNone(result.status)
        self.assertEqual(output.getvalue(), "")

    def test_http_adapter_reports_http_error_status_without_response_body(self):
        error = self.chain.urllib.error.HTTPError(
            "https://api.github.com/repos/o/r/actions/runs", 403, "secret-body", {}, io.BytesIO(b"secret-body"))
        with mock.patch.object(self.chain.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = error
            result = self.chain.github_request("GET", error.url, "secret")
        self.assertEqual(result.status, 403)
        self.assertIsNone(result.payload)

    def test_dot_repository_path_is_rejected_before_network(self):
        self.env["GITHUB_REPOSITORY"] = "owner/.."
        result, calls, _ = self.exercise([])
        self.assertEqual(result.state, "failed")
        self.assertEqual(calls, [])


class SelfChainBootstrapTests(unittest.TestCase):
    def test_initial_commit_helper_survives_reset_and_writes_summary(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/manga-monitor.yml").read_text())
        step = next(s for s in workflow["jobs"]["monitor"]["steps"]
                    if s["name"] == "Keep the loop alive (self-chain next run)")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()
            git("init", "-b", "master")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            (repo / "placeholder").write_text("old")
            git("add", ".")
            git("commit", "-m", "old master")
            old = git("rev-parse", "HEAD")
            if HELPER.exists():
                folder = repo / ".github/scripts"
                folder.mkdir(parents=True)
                shutil.copy2(HELPER, folder)
            git("add", ".")
            git("commit", "--allow-empty", "-m", "workflow branch with helper")
            original_sha = git("rev-parse", "HEAD")
            git("reset", "--hard", old)
            env = os.environ.copy()
            env.update(CHAIN_PAT="", GITHUB_SHA=original_sha, GITHUB_RUN_ID="1234",
                       GITHUB_RUN_ATTEMPT="2", RUNNER_TEMP=tmp,
                       GITHUB_REPOSITORY="maiiqqq/manga-monitor",
                       PERSISTENCE_OUTCOME="success",
                       GITHUB_STEP_SUMMARY=str(Path(tmp) / "summary.md"))
            result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=repo, env=env,
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = Path(env["GITHUB_STEP_SUMMARY"])
            self.assertTrue(summary.exists(), "Real YAML step must write a self-chain summary")
            self.assertIn("| Self-chain | disabled |", summary.read_text())
            self.assertIn("| Persistence | success |", summary.read_text())


if __name__ == "__main__":
    unittest.main()
