"""GM-006: real CLI fixtures for concurrent changes and explicit deletions.

These characterize existing merge policy; they do not claim the policy orders
two human actions or makes a manual monitor deletion preserve chapter progress.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
A = "https://example.invalid/manga/a/"
B = "https://example.invalid/manga/b/"
C = "https://example.invalid/manga/c/"


def chapter(number, date="2026-11-08", check="2026-11-08T08:00:00"):
    return {"last_chapter": str(number), "last_chapter_date": date, "last_check": check}


class MergeStateAuditTests(unittest.TestCase):
    def merge(self, name, base, ours, theirs):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / name
            b, t = Path(folder) / "base.json", Path(folder) / "theirs.json"
            for path, value in ((b, base), (target, ours), (t, theirs)):
                path.write_text(json.dumps(value), encoding="utf-8")
            original = (b.read_bytes(), t.read_bytes())
            completed = subprocess.run(
                [sys.executable, str(ROOT / "merge_state.py"), str(target), str(b), str(t)],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((b.read_bytes(), t.read_bytes()), original)
            return json.loads(target.read_text(encoding="utf-8"))

    def test_disjoint_monitor_changes_and_remote_additions_survive(self):
        result = self.merge("monitor_state.json",
            {A: chapter(10), B: chapter(20)},
            {A: chapter(11), B: chapter(20)},
            {A: chapter(10), B: chapter(21), C: chapter(1)})
        self.assertEqual(result, {A: chapter(11), B: chapter(21), C: chapter(1)})

    def test_same_manga_conflict_retains_higher_chapter_in_both_orders(self):
        for ours, theirs in ((11, 12), (12, 11)):
            with self.subTest(ours=ours, theirs=theirs):
                self.assertEqual(self.merge("monitor_state.json", {A: chapter(10)},
                    {A: chapter(ours)}, {A: chapter(theirs)}), {A: chapter(12)})

    def test_equal_chapter_conflict_uses_date_then_last_check(self):
        self.assertEqual(self.merge("monitor_state.json", {A: chapter(10)},
            {A: chapter(11, "2026-11-07", "2026-11-08T09:00:00")},
            {A: chapter(11, "2026-11-08", "2026-11-08T08:00:00")}),
            {A: chapter(11, "2026-11-08", "2026-11-08T08:00:00")})
        self.assertEqual(self.merge("monitor_state.json", {A: chapter(10)},
            {A: chapter(11, check="2026-11-08T09:00:00")},
            {A: chapter(11, check="2026-11-08T08:00:00")}),
            {A: chapter(11, check="2026-11-08T09:00:00")})

    def test_read_only_bookmark_copy_keeps_remote_edit_delete_and_add(self):
        base = {A: {"title": "A"}, B: {"title": "B"}}
        self.assertEqual(self.merge("bookmarks.json", base, base,
            {A: {"title": "A updated"}, C: {"title": "C new"}}),
            {A: {"title": "A updated"}, C: {"title": "C new"}})

    def test_explicit_local_bookmark_removal_wins_over_concurrent_edit(self):
        self.assertEqual(self.merge("bookmarks.json", {A: {"title": "A"}}, {},
            {A: {"title": "A updated"}, B: {"title": "B new"}}),
            {B: {"title": "B new"}})

    def test_conflicting_bookmark_edits_use_ours_without_claiming_time_order(self):
        self.assertEqual(self.merge("bookmarks.json", {A: {"title": "A"}},
            {A: {"title": "local edit"}}, {A: {"title": "remote edit"}}),
            {A: {"title": "local edit"}})

    def test_characterize_monitor_deletion_overrides_remote_advance(self):
        # Deliberate deletion policy, not a monotonic recovery guarantee.
        self.assertEqual(self.merge("monitor_state.json", {A: chapter(10)}, {},
            {A: chapter(12)}), {})

    def test_bot_offset_never_regresses_and_registration_or_is_preserved(self):
        self.assertEqual(self.merge("bot_state.json",
            {"telegram_offset": 100, "commands_registered": True},
            {"telegram_offset": 105, "commands_registered": False},
            {"telegram_offset": 120, "commands_registered": False, "remote_only": "keep"}),
            {"telegram_offset": 120, "commands_registered": True, "remote_only": "keep"})


if __name__ == "__main__":
    unittest.main()
