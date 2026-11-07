"""Offline tests for exact-byte state recovery and artifact export hygiene."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".github/scripts/snapshot_state.py"
NAMES = ("monitor_state.json", "bookmarks.json", "bot_state.json")


class SnapshotStateTests(unittest.TestCase):
    def capture(self, overrides=None, extra_env=None, source_links=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = root / "source"
        source.mkdir()
        originals = {name: b'{"example": {"value": 1}}\n' for name in NAMES}
        originals.update(overrides or {})
        for name, raw in originals.items():
            if raw is not None:
                (source / name).write_bytes(raw)
        # These must never enter the export.
        (source / ".env").write_text("TOKEN=do-not-export")
        (source / "monitor.log").write_text("raw log do-not-export")
        for name in source_links or ():
            (source / name).unlink(missing_ok=True)
            (source / name).symlink_to(source / ".env")
        env = os.environ.copy()
        for key in ("GITHUB_TOKEN", "GH_TOKEN", "TELEGRAM_BOT_TOKEN", "WORKFLOW_PAT", "TELEGRAM_CHAT_ID"):
            env.pop(key, None)
        env.update(GITHUB_RUN_ID="1234", GITHUB_RUN_ATTEMPT="2", GITHUB_SHA="a" * 40)
        env.update(extra_env or {})
        destination = root / "recovery"
        result = subprocess.run(
            [sys.executable, str(HELPER), "--source", str(source), "--destination", str(destination)],
            capture_output=True, text=True, env=env, timeout=5,
        )
        return result, source, destination, originals

    def manifest(self, destination):
        path = destination / "artifact/manifest.json"
        self.assertTrue(path.is_file(), "Recovery manifest must be produced")
        return json.loads(path.read_text())

    def test_exact_original_bytes_and_sha256_are_recoverable(self):
        raw = '{"title":"ทดสอบ","chapter":1}\r\n'.encode()
        result, _, dest, _ = self.capture({"monitor_state.json": raw})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((dest / "original/monitor_state.json").read_bytes(), raw)
        self.assertEqual((dest / "artifact/monitor_state.json").read_bytes(), raw)
        entry = self.manifest(dest)["files"]["monitor_state.json"]
        self.assertEqual(entry["status"], "valid")
        self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(entry["artifact_included"])

    def test_malformed_json_is_preserved_and_fails_validation(self):
        raw = b'{"broken":\xff'
        result, _, dest, _ = self.capture({"monitor_state.json": raw})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((dest / "original/monitor_state.json").read_bytes(), raw)
        self.assertEqual((dest / "artifact/monitor_state.json").read_bytes(), raw)
        self.assertEqual(self.manifest(dest)["files"]["monitor_state.json"]["status"], "invalid_json")
        self.assertNotIn("broken", result.stdout + result.stderr)

    def test_non_object_json_is_invalid(self):
        result, _, dest, _ = self.capture({"bookmarks.json": b"[]"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.manifest(dest)["files"]["bookmarks.json"]["status"], "not_object")
        self.assertEqual((dest / "original/bookmarks.json").read_bytes(), b"[]")

    def test_nonstandard_json_number_is_invalid(self):
        result, _, dest, _ = self.capture({"bot_state.json": b'{"telegram_offset": NaN}'})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.manifest(dest)["files"]["bot_state.json"]["status"], "invalid_json")

    def test_escaped_surrogate_does_not_abort_other_file_recovery(self):
        raw = b'{"title":"\\ud800"}'
        result, _, dest, _ = self.capture({"monitor_state.json": raw})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((dest / "artifact/monitor_state.json").read_bytes(), raw)
        self.assertEqual(self.manifest(dest)["files"]["bookmarks.json"]["status"], "valid")

    def test_excessive_json_nesting_is_preserved_as_invalid(self):
        raw = b"[" * 10000 + b"0" + b"]" * 10000
        result, _, dest, _ = self.capture({"monitor_state.json": raw})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.manifest(dest)["files"]["monitor_state.json"]["status"], "invalid_json")
        self.assertEqual((dest / "original/monitor_state.json").read_bytes(), raw)

    def test_missing_file_is_reported_without_creating_empty_replacement(self):
        result, _, dest, _ = self.capture({"bookmarks.json": None})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.manifest(dest)["files"]["bookmarks.json"]["status"], "missing")
        self.assertFalse((dest / "original/bookmarks.json").exists())
        self.assertFalse((dest / "artifact/bookmarks.json").exists())

    def test_only_allowlisted_files_and_minimal_manifest_are_exported(self):
        result, _, dest, _ = self.capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            {p.name for p in (dest / "artifact").iterdir()},
            {"monitor_state.json", "bookmarks.json", "bot_state.json", "manifest.json"},
        )
        manifest = self.manifest(dest)
        self.assertEqual(manifest["run_id"], "1234")
        self.assertEqual(manifest["run_attempt"], "2")
        self.assertEqual(manifest["source_sha"], "a" * 40)

    def test_known_secret_excludes_whole_file_but_preserves_private_original(self):
        raw = b'{"title": "fixture-sensitive-value"}'
        result, _, dest, _ = self.capture(
            {"monitor_state.json": raw}, {"TELEGRAM_BOT_TOKEN": "fixture-sensitive-value"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((dest / "original/monitor_state.json").read_bytes(), raw)
        self.assertFalse((dest / "artifact/monitor_state.json").exists())
        entry = self.manifest(dest)["files"]["monitor_state.json"]
        self.assertFalse(entry["artifact_included"])
        self.assertEqual(entry["exclusion_reason"], "sensitive_content")
        export = b"".join(p.read_bytes() for p in (dest / "artifact").iterdir())
        self.assertNotIn(b"fixture-sensitive-value", export)
        self.assertNotIn("fixture-sensitive-value", result.stdout + result.stderr)

    def test_invalid_json_with_sensitive_key_is_not_exported(self):
        result, _, dest, _ = self.capture({"bot_state.json": b'{"token": "opaque-value", broken'})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse((dest / "artifact/bot_state.json").exists())
        self.assertFalse(self.manifest(dest)["files"]["bot_state.json"]["artifact_included"])

    def test_escaped_secret_in_duplicate_key_cannot_be_exported(self):
        raw = b'{"value":"\\u0066ixture-sensitive-value","value":"safe"}'
        result, _, dest, _ = self.capture(
            {"monitor_state.json": raw}, {"TELEGRAM_BOT_TOKEN": "fixture-sensitive-value"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((dest / "original/monitor_state.json").read_bytes(), raw)
        self.assertFalse((dest / "artifact/monitor_state.json").exists())
        self.assertTrue((dest / "artifact/bookmarks.json").exists())

    def test_escaped_sensitive_key_in_malformed_json_is_not_exported(self):
        raw = b'{"\\u0074oken":"opaque-value", broken'
        result, _, dest, _ = self.capture({"bot_state.json": raw})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse((dest / "artifact/bot_state.json").exists())

    def test_common_token_patterns_are_not_exported(self):
        for token in ("ghp_" + "x" * 36, "123456789:" + "x" * 35):
            with self.subTest(token_type=token[:4]):
                result, _, dest, _ = self.capture({"bookmarks.json": json.dumps({"value": token}).encode()})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse((dest / "artifact/bookmarks.json").exists())

    def test_snapshot_is_not_changed_when_source_is_overwritten(self):
        result, source, dest, originals = self.capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        (source / "monitor_state.json").write_text("{}")
        self.assertEqual((dest / "original/monitor_state.json").read_bytes(), originals["monitor_state.json"])

    def test_symlink_is_not_followed_or_exported(self):
        result, _, dest, _ = self.capture(source_links=("bookmarks.json",))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.manifest(dest)["files"]["bookmarks.json"]["status"], "not_regular_file")
        self.assertFalse((dest / "original/bookmarks.json").exists())
        self.assertNotIn(b"do-not-export", b"".join(p.read_bytes() for p in (dest / "artifact").iterdir()))

    def test_private_files_have_no_group_or_world_permissions(self):
        result, _, dest, _ = self.capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (dest, *dest.rglob("*")):
            self.assertEqual(path.stat().st_mode & 0o077, 0, path.name)


if __name__ == "__main__":
    unittest.main()
