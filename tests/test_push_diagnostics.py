"""Offline regression tests for push classification and safe log output."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".github/scripts/push_diagnostics.py"


class PushDiagnosticsTests(unittest.TestCase):
    def diagnose(self, output, exit_code=1, extra_env=None):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "push.log"
            log.write_text(output, encoding="utf-8")
            env = os.environ.copy()
            for key in ("GITHUB_TOKEN", "GH_TOKEN"):
                env.pop(key, None)
            env.update(extra_env or {})
            return subprocess.run(
                [sys.executable, str(HELPER), str(log), str(exit_code),
                 "2", "a" * 40, "b" * 40],
                text=True, capture_output=True, env=env, timeout=5,
            )

    def expect_category(self, message, category, exit_code=1):
        result = self.diagnose(message, exit_code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("category=" + category, result.stdout)
        self.assertIn("exit_code=" + str(exit_code), result.stdout)
        self.assertIn("attempt=2", result.stdout)
        self.assertIn("local_sha=" + "a" * 40, result.stdout)
        self.assertIn("remote_sha=" + "b" * 40, result.stdout)
        return result.stdout

    def test_commit_refs_rules_is_server_failure_not_branch_movement(self):
        out = self.expect_category(
            "remote: fatal error in commit_refs_rules\n"
            " ! [remote rejected] HEAD -> master (fatal error in commit_refs_rules)\n"
            "error: failed to push some refs", "transient_server")
        self.assertNotIn("remote moved", out)

    def test_http_5xx_is_server_failure(self):
        self.expect_category("error: RPC failed; HTTP 502 curl 22", "transient_server")

    def test_network_timeout(self):
        self.expect_category("fatal: unable to access URL: Connection timed out", "transient_network")

    def test_dns_failure(self):
        self.expect_category("fatal: Could not resolve host: github.com", "transient_network")

    def test_non_fast_forward(self):
        self.expect_category(" ! [rejected] HEAD -> master (non-fast-forward)", "non_fast_forward")

    def test_fetch_first(self):
        self.expect_category(" ! [rejected] HEAD -> master (fetch first)", "non_fast_forward")

    def test_permission_denied(self):
        self.expect_category("remote: Permission to owner/repo.git denied to bot.", "permission_or_rule")

    def test_http_403_is_permission_failure(self):
        self.expect_category("fatal: The requested URL returned error: 403", "permission_or_rule")

    def test_rule_violation_is_not_generic_remote_rejection(self):
        self.expect_category("remote: error: GH013: Repository rule violations found", "permission_or_rule")

    def test_unknown_error_stays_unknown(self):
        out = self.expect_category(
            " ! [remote rejected] HEAD -> master (unrecognized hook error)\n"
            "error: failed to push some refs", "unknown")
        self.assertIn("unrecognized hook error", out)

    def test_generic_failure_is_not_a_network_failure(self):
        self.expect_category("error: failed to push some refs", "unknown")

    def test_success_preserves_exit_status(self):
        self.expect_category("Everything up-to-date", "success", 0)

    def test_exit_code_does_not_become_diagnostics_exit_code(self):
        self.expect_category("fatal: unexpected failure", "unknown", 128)

    def test_secrets_and_credentials_are_redacted_without_changing_category(self):
        result = self.diagnose(
            "fatal: HTTP 500 https://x-access-token:url-secret@github.com/repo"
            "?token=query-secret&foo=bar\n"
            "Authorization: Bearer bearer-secret\n"
            "Authorization: Basic basic-secret\n"
            "env secret is env-secret",
            extra_env={"GITHUB_TOKEN": "env-secret"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("category=transient_server", result.stdout)
        for secret in ("url-secret", "query-secret", "bearer-secret", "basic-secret", "env-secret"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_remote_lines_cannot_emit_github_workflow_commands(self):
        result = self.diagnose("::warning::remote message\r\x1b[31m::error::another")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        self.assertFalse(any(line.startswith("::") for line in result.stdout.splitlines()))

    def test_missing_log_reports_only_safe_metadata(self):
        result = subprocess.run(
            [sys.executable, str(HELPER), "/missing/push.log", "128", "1", "a" * 40, "b" * 40],
            text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("category=unknown", result.stdout)
        self.assertIn("unavailable", result.stdout)


if __name__ == "__main__":
    unittest.main()
