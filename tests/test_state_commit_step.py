"""Execute the real YAML commit step using local Git and simulated push failures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/manga-monitor.yml"
REAL_GIT = shutil.which("git")


def git(*args, cwd):
    return subprocess.run(
        [REAL_GIT, *args], cwd=cwd, text=True, capture_output=True, check=True,
    ).stdout.strip()


class StateCommitStepTests(unittest.TestCase):
    def run_step(self, failure=None, change=True, state_overrides=None, bot_outcome="success"):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        remote = tmp / "remote.git"
        seed = tmp / "seed"
        runner = tmp / "runner"
        bin_dir = tmp / "bin"
        for folder in (seed, runner, bin_dir):
            folder.mkdir()
        git("init", "--bare", str(remote), cwd=tmp)
        git("init", "-b", "master", cwd=seed)
        git("config", "user.name", "Test", cwd=seed)
        git("config", "user.email", "test@example.invalid", cwd=seed)
        shutil.copy2(ROOT / "merge_state.py", seed / "merge_state.py")
        shutil.copytree(ROOT / ".github/scripts", seed / ".github/scripts")
        (seed / "monitor_state.json").write_text(json.dumps(
            {"entry": {"last_check": "2026-01-01"}}, indent=2))
        (seed / "bookmarks.json").write_text("{}")
        (seed / "bot_state.json").write_text("{}")
        git("add", ".", cwd=seed)
        git("commit", "-m", "initial", cwd=seed)
        self.initial_sha = git("rev-parse", "HEAD", cwd=seed)
        git("remote", "add", "origin", str(remote), cwd=seed)
        git("push", "origin", "master", cwd=seed)
        git("clone", "-b", "master", str(remote), str(runner), cwd=tmp)
        if change:
            (runner / "monitor_state.json").write_text(
                '{"entry": {"last_check": "2026-01-02"}}\n')
        for name, value in (state_overrides or {}).items():
            if value is None:
                (runner / name).unlink(missing_ok=True)
            else:
                (runner / name).write_bytes(value)
        git_wrapper = bin_dir / "git"
        git_wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, subprocess, sys\n"
            "if sys.argv[1:2] == ['push']:\n"
            "    if os.environ.get('SIMULATED_PUSH_FAILURE'):\n"
            "        print(os.environ['SIMULATED_PUSH_FAILURE'])\n"
            "        print('diagnostic stderr marker', file=sys.stderr)\n"
            "        sys.exit(128)\n"
            "sys.exit(subprocess.call([os.environ['REAL_GIT'], *sys.argv[1:]]))\n")
        git_wrapper.chmod(0o755)
        sleep_wrapper = bin_dir / "sleep"
        sleep_wrapper.write_text("#!/bin/sh\nexit 0\n")
        sleep_wrapper.chmod(0o755)
        workflow = yaml.safe_load(WORKFLOW.read_text())
        step = next(
            s for s in workflow["jobs"]["monitor"]["steps"]
            if s["name"] == "Commit state changes (if any)"
        )
        script = tmp / "commit-state.sh"
        script.write_text(step["run"])
        env = os.environ.copy()
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WORKFLOW_PAT", "CHAIN_PAT"):
            env.pop(name, None)
        env.update(
            PATH=str(bin_dir) + os.pathsep + env["PATH"],
            REAL_GIT=REAL_GIT,
            RUNNER_TEMP=str(tmp / "runner-temp"),
            GITHUB_TOKEN="fixture-token-do-not-print",
            SIMULATED_PUSH_FAILURE=failure or "",
            GITHUB_RUN_ID="1234",
            GITHUB_RUN_ATTEMPT="2",
            GITHUB_SHA=self.initial_sha,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        (tmp / "runner-temp").mkdir()
        outcomes = {"run_bot": bot_outcome, "snapshot_state": "skipped", "persist_state": "skipped"}
        def enabled(condition):
            steps = SimpleNamespace(**{k: SimpleNamespace(outcome=v) for k, v in outcomes.items()})
            return eval(condition.replace("&&", " and ").replace("||", " or "),
                        {"__builtins__": {}}, {"always": lambda: True, "steps": steps})
        snapshot = next((s for s in workflow["jobs"]["monitor"]["steps"]
                         if s.get("id") == "snapshot_state"), None)
        self.recovery_dir = tmp / "runner-temp/state-recovery-1234-2"
        if snapshot and enabled(snapshot["if"]):
            self.snapshot_result = subprocess.run(
                ["bash", "-e", "-c", snapshot["run"]], cwd=runner, env=env,
                text=True, capture_output=True, timeout=5)
            outcomes["snapshot_state"] = "success" if self.snapshot_result.returncode == 0 else "failure"
        if enabled(step["if"]):
            result = subprocess.run(
                ["bash", "-e", str(script)], cwd=runner, env=env,
                text=True, capture_output=True, timeout=20,
            )
            outcomes["persist_state"] = "success" if result.returncode == 0 else "failure"
        else:
            result = subprocess.CompletedProcess([], 1, "Persistence skipped", "")
        self.backup_step = next((s for s in workflow["jobs"]["monitor"]["steps"]
                                 if s.get("id") == "upload_state_recovery"), None)
        self.backup_selected = bool(self.backup_step and enabled(self.backup_step["if"]))
        persisted = json.loads(git("show", "master:monitor_state.json", cwd=remote))
        return result, persisted, tmp

    def test_server_error_remains_failure_and_is_not_reported_as_remote_moved(self):
        result, persisted, _ = self.run_step(
            "remote: fatal error in commit_refs_rules"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("category=transient_server", result.stdout)
        self.assertIn("exit_code=128", result.stdout)
        self.assertIn("diagnostic stderr marker", result.stdout)
        self.assertNotIn("remote moved", result.stdout)
        self.assertEqual(persisted["entry"]["last_check"], "2026-01-01")

    def test_permission_error_is_classified(self):
        result, _, _ = self.run_step("remote: error: GH013: Repository rule violations found")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("category=permission_or_rule", result.stdout)

    def test_unknown_error_keeps_failure_and_redacts_credentials(self):
        result, _, _ = self.run_step(
            "remote: custom hook failure fixture-token-do-not-print "
            "https://user:embedded-secret@github.com/owner/repo"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("category=unknown", result.stdout)
        self.assertNotIn("fixture-token-do-not-print", result.stdout + result.stderr)
        self.assertNotIn("embedded-secret", result.stdout + result.stderr)

    def test_success_persists_state_to_local_remote(self):
        result, persisted, _ = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("category=success", result.stdout)
        self.assertIn("remote_sha=" + self.initial_sha, result.stdout)
        self.assertIn("Push succeeded", result.stdout)
        self.assertEqual(persisted["entry"]["last_check"], "2026-01-02")

    def test_no_changes_needs_no_push(self):
        result, persisted, _ = self.run_step(change=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No state changes", result.stdout)
        self.assertNotIn("Push result:", result.stdout)
        self.assertEqual(persisted["entry"]["last_check"], "2026-01-01")

    def test_failed_push_retains_original_state_and_selects_backup(self):
        result, _, _ = self.run_step("remote: fatal error in commit_refs_rules")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(self.backup_selected)
        raw = (self.recovery_dir / "original/monitor_state.json").read_bytes()
        self.assertEqual(raw, b'{"entry": {"last_check": "2026-01-02"}}\n')
        self.assertEqual((self.recovery_dir / "artifact/monitor_state.json").read_bytes(), raw)

    def test_successful_push_does_not_select_backup(self):
        result, _, _ = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.backup_selected)

    def test_corrupted_state_blocks_persistence_and_preserves_original(self):
        raw = b'{"broken":'
        result, persisted, _ = self.run_step(state_overrides={"monitor_state.json": raw})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.backup_selected)
        self.assertEqual(persisted["entry"]["last_check"], "2026-01-01")
        self.assertEqual((self.recovery_dir / "original/monitor_state.json").read_bytes(), raw)

    def test_missing_state_selects_backup_without_overwriting_remote(self):
        result, persisted, _ = self.run_step(state_overrides={"bookmarks.json": None})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.backup_selected)
        self.assertEqual(persisted["entry"]["last_check"], "2026-01-01")

    def test_bot_failure_still_snapshots_and_selects_backup_after_successful_push(self):
        result, persisted, _ = self.run_step(bot_outcome="failure")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.backup_selected)
        self.assertTrue((self.recovery_dir / "artifact/manifest.json").is_file())
        self.assertEqual(persisted["entry"]["last_check"], "2026-01-02")


if __name__ == "__main__":
    unittest.main()
