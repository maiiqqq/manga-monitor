"""Execute the real YAML commit step using local Git and simulated push failures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
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
    def run_step(self, failure=None, change=True):
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
        helper = ROOT / ".github/scripts/push_diagnostics.py"
        if helper.exists():
            dest = seed / ".github/scripts"
            dest.mkdir(parents=True)
            shutil.copy2(helper, dest)
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
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        (tmp / "runner-temp").mkdir()
        result = subprocess.run(
            ["bash", "-e", str(script)], cwd=runner, env=env,
            text=True, capture_output=True, timeout=20,
        )
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


if __name__ == "__main__":
    unittest.main()
