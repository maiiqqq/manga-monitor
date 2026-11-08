"""Execute the real YAML commit step using local Git and simulated push failures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
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
    def run_step(self, failure=None, change=True, state_overrides=None, bot_outcome="success",
                 scenario=None, persist_budget=None, slow_copy=False, short_deadline=False):
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
        shutil.copy2(ROOT / "tests/fake_git.py", git_wrapper)
        git_wrapper.chmod(0o755)
        sleep_wrapper = bin_dir / "sleep"
        sleep_wrapper.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$PERSIST_TEST_SLEEPS"\n')
        sleep_wrapper.chmod(0o755)
        if slow_copy:
            cp_wrapper = bin_dir / "cp"
            cp_wrapper.write_text(
                "#!/usr/bin/env python3\nimport subprocess,sys,time\n"
                "if sys.argv[1] == '.github/scripts/persist_state.sh': time.sleep(4)\n"
                f"sys.exit(subprocess.call([{shutil.which('cp')!r}, *sys.argv[1:]]))\n")
            cp_wrapper.chmod(0o755)
        if short_deadline:
            timeout_wrapper = bin_dir / "timeout"
            timeout_wrapper.write_text(
                "#!/usr/bin/env python3\nimport subprocess,sys\n"
                "args=['1s' if arg == '175s' else arg for arg in sys.argv[1:]]\n"
                f"sys.exit(subprocess.call([{shutil.which('timeout')!r}, *args]))\n")
            timeout_wrapper.chmod(0o755)
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
            PERSIST_TEST_SCENARIO=json.dumps(scenario or {}),
            PERSIST_TEST_COUNTS=str(tmp / "counts.json"),
            PERSIST_TEST_TRACE=str(tmp / "trace.jsonl"),
            PERSIST_TEST_SLEEPS=str(tmp / "sleeps.txt"),
            PERSIST_TEST_SEED=str(seed),
            GITHUB_RUN_ID="1234",
            GITHUB_RUN_ATTEMPT="2",
            GITHUB_SHA=self.initial_sha,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        if persist_budget is not None:
            env["PERSIST_BUDGET_SECONDS"] = str(persist_budget)
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
                text=True, capture_output=True, timeout=30,
            )
            outcomes["persist_state"] = "success" if result.returncode == 0 else "failure"
        else:
            result = subprocess.CompletedProcess([], 1, "Persistence skipped", "")
        self.backup_step = next((s for s in workflow["jobs"]["monitor"]["steps"]
                                 if s.get("id") == "upload_state_recovery"), None)
        self.backup_selected = bool(self.backup_step and enabled(self.backup_step["if"]))
        self.trace = [json.loads(line) for line in (tmp / "trace.jsonl").read_text().splitlines()] \
            if (tmp / "trace.jsonl").exists() else []
        self.pushes = [entry for entry in self.trace if entry["operation"] == "push"]
        self.delays = [int(value) for value in (tmp / "sleeps.txt").read_text().splitlines()] \
            if (tmp / "sleeps.txt").exists() else []
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

    def test_permission_failure_stops_after_one_push_without_sleep(self):
        result, _, _ = self.run_step("fatal: The requested URL returned error: 403")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.delays, [])

    def test_unknown_failure_is_not_retried(self):
        result, _, _ = self.run_step("remote: custom unrecognized hook failure")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.delays, [])

    def test_server_failure_then_success_reuses_unaccepted_commit(self):
        result, state, _ = self.run_step(scenario={"pushes": [
            {"error": "remote: fatal error in commit_refs_rules"}, {}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.pushes), 2)
        self.assertEqual(self.pushes[0]["local_sha"], self.pushes[1]["local_sha"])
        self.assertEqual(len(self.delays), 1)
        self.assertTrue(5 <= self.delays[0] <= 8)
        self.assertEqual(state["entry"]["last_check"], "2026-01-02")

    def test_non_fast_forward_fetches_remerges_and_preserves_concurrent_state(self):
        result, state, _ = self.run_step(scenario={"pushes": [{"advance": True}, {}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.pushes), 2)
        self.assertNotEqual(self.pushes[0]["local_sha"], self.pushes[1]["local_sha"])
        self.assertEqual(state["entry"]["last_check"], "2026-01-02")
        self.assertEqual(state["other"]["last_check"], "2026-01-03")
        self.assertEqual(self.delays, [])

    def test_accepted_push_with_timeout_is_confirmed_without_second_push(self):
        result, state, _ = self.run_step(scenario={"pushes": [
            {"accept": True, "error": "fatal: Connection timed out"}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.delays, [])
        self.assertIn("confirmed", result.stdout.lower())
        self.assertEqual(state["entry"]["last_check"], "2026-01-02")

    def test_accepted_commit_is_confirmed_when_remote_has_a_descendant(self):
        result, state, _ = self.run_step(scenario={"pushes": [
            {"accept": True, "advance": True, "error": "fatal: Connection timed out"}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(state["other"]["last_check"], "2026-01-03")

    def test_unverifiable_remote_never_allows_another_push(self):
        result, _, _ = self.run_step(scenario={
            "pushes": [{"error": "fatal: Connection timed out"}], "fetch_failure_from": 2})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.pushes), 1)
        self.assertTrue(self.backup_selected)

    def test_five_transient_attempts_have_only_four_bounded_delays(self):
        result, _, _ = self.run_step("remote: fatal error in commit_refs_rules")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.pushes), 5)
        self.assertEqual(len(self.delays), 4)
        for delay, base in zip(self.delays, (5, 10, 20, 40)):
            self.assertTrue(base <= delay <= base + 3)

    def test_command_timeout_respects_reduced_budget_and_keeps_recovery(self):
        started = time.monotonic()
        result, _, _ = self.run_step(scenario={"pushes": [{"delay": 5}]}, persist_budget=1)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(self.backup_selected)
        self.assertEqual(self.delays, [])

    def test_outer_deadline_includes_initial_helper_copy(self):
        started = time.monotonic()
        result, _, _ = self.run_step(slow_copy=True, short_deadline=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.pushes, [])
        self.assertTrue(self.backup_selected)
        self.assertTrue((self.recovery_dir / "artifact/manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
