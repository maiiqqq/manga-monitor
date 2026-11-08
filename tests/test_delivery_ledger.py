"""Write-ahead notification evidence: real CAS code, only HTTP is simulated."""
import base64
import copy
import hashlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parents[1]


class HTTPResponse:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


class ContentsAPI:
    """CAS boundary fixture; holds remote state independently of any checkout."""
    def __init__(self):
        self.document = {"version": 1, "entries": {}}
        self.calls = []
        self.get_status = 200
        self.put_status = 200
        self.lose_write_response = False
        self.fail_after_writes = None
        self.writes = 0
        self.lock = threading.Lock()

    def raw(self):
        return json.dumps(self.document, sort_keys=True, separators=(",", ":")).encode()

    def sha(self):
        return hashlib.sha1(self.raw()).hexdigest()

    def get(self, url, **kwargs):
        with self.lock:
            self.calls.append(("GET", url, kwargs))
            return HTTPResponse(self.get_status, {"type": "file", "encoding": "base64", "sha": self.sha(),
                "content": base64.b64encode(self.raw()).decode()})

    def put(self, url, **kwargs):
        with self.lock:
            self.calls.append(("PUT", url, kwargs))
            if self.fail_after_writes is not None and self.writes >= self.fail_after_writes:
                raise requests.Timeout("fixture write outage")
            if self.put_status != 200:
                return HTTPResponse(self.put_status, {})
            payload = kwargs["json"]
            if payload["sha"] != self.sha():
                return HTTPResponse(409, {})
            self.document = json.loads(base64.b64decode(payload["content"]))
            self.writes += 1
            if self.lose_write_response:
                raise requests.Timeout("fixture write acknowledgement lost")
            return HTTPResponse(200, {"content": {"sha": self.sha()}})


class DeliveryLedgerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "delivery_ledger.py").exists(), "write-ahead ledger not implemented")
        self.d = importlib.import_module("delivery_ledger")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.json"
        self.store = self.d.LocalStore(self.path)
        self.ledger = self.d.DeliveryLedger(self.store, "a" * 64)
        self.sent = []
        self.errors = io.StringIO()
        self.error_patch = mock.patch("sys.stderr", self.errors)
        self.error_patch.start()
        self.addCleanup(self.error_patch.stop)

    def item(self, chapter="11", kind="card"):
        return self.ledger.item(kind, "https://example.invalid/manga/fixture/", chapter, "Fixture")

    def accepted(self, items):
        self.sent.append(items)
        return self.d.SendResult("accepted", "sendMessage", len(self.sent))

    def remote(self, api=None):
        api = api or ContentsAPI()
        store = self.d.GitHubStore("fixture/repo", "fixture-token", client=api)
        return api, self.d.DeliveryLedger(store, "a" * 64)

    def test_accepted_receipt_survives_new_process_view_and_batch_overlap(self):
        self.assertTrue(self.ledger.deliver([self.item("10"), self.item("11")], self.accepted))
        restarted = self.d.DeliveryLedger(self.d.LocalStore(self.path), "a" * 64)
        self.assertTrue(restarted.deliver([self.item("11"), self.item("12")], self.accepted))
        self.assertEqual([[i["chapter"] for i in batch] for batch in self.sent], [["10", "11"], ["12"]])
        entries = json.loads(self.path.read_text())["entries"].values()
        self.assertEqual({e["status"] for e in entries}, {"accepted"})
        self.assertEqual({e["message_id"] for e in entries}, {1, 2})

    def test_unknown_survives_restart_and_never_auto_retries(self):
        def timeout(items):
            self.sent.append(items)
            raise requests.Timeout("fixture accepted but no ack")
        self.assertFalse(self.ledger.deliver([self.item()], timeout))
        restarted = self.d.DeliveryLedger(self.d.LocalStore(self.path), "a" * 64)
        self.assertFalse(restarted.deliver([self.item()], self.accepted))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(next(iter(self.store.read()[0]["entries"].values()))["status"], "unknown")

    def test_explicit_rejection_allows_retry(self):
        self.assertFalse(self.ledger.deliver([self.item()], lambda _: self.d.SendResult("rejected", "sendMessage")))
        self.assertTrue(self.ledger.deliver([self.item()], self.accepted))
        self.assertEqual(len(self.sent), 1)

    def test_intent_save_failure_prevents_send(self):
        with mock.patch("os.replace", side_effect=OSError("fixture disk full")):
            self.assertFalse(self.ledger.deliver([self.item()], self.accepted))
        self.assertEqual(self.sent, [])

    def test_remote_receipt_failure_keeps_unknown_after_fresh_store(self):
        api, ledger = self.remote()
        api.fail_after_writes = 1
        self.assertFalse(ledger.deliver([self.item()], self.accepted))
        api.fail_after_writes = None
        _, restarted = self.remote(api)
        self.assertFalse(restarted.deliver([self.item()], self.accepted))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(next(iter(api.document["entries"].values()))["status"], "unknown")

    def test_intent_accepted_but_write_response_lost_does_not_send(self):
        api, ledger = self.remote()
        api.lose_write_response = True
        self.assertFalse(ledger.deliver([self.item()], self.accepted))
        api.lose_write_response = False
        _, restarted = self.remote(api)
        self.assertFalse(restarted.deliver([self.item()], self.accepted))
        self.assertEqual(self.sent, [])

    def test_remote_errors_and_redirects_fail_closed(self):
        for method, statuses in [("get", (302, 403, 404, 500)), ("put", (302, 403, 422, 500))]:
            for status in statuses:
                with self.subTest(method=method, status=status):
                    api, ledger = self.remote()
                    setattr(api, method + "_status", status)
                    self.assertFalse(ledger.deliver([self.item()], self.accepted))
                    self.assertEqual(self.sent, [])
                    for _, url, kwargs in api.calls:
                        self.assertEqual(url, "https://api.github.com/repos/fixture/repo/contents/delivery_state.json")
                        self.assertFalse(kwargs["allow_redirects"])
                        self.assertLessEqual(kwargs["timeout"], 10)
                        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fixture-token")

    def test_remote_conflicts_are_bounded_and_do_not_send(self):
        api, ledger = self.remote()
        api.put_status = 409
        self.assertFalse(ledger.deliver([self.item()], self.accepted))
        self.assertEqual(len([c for c in api.calls if c[0] == "PUT"]), 3)
        self.assertEqual(self.sent, [])

    def test_actions_missing_token_never_falls_back_to_local_store(self):
        with mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_TOKEN": "", "GITHUB_REPOSITORY": "fixture/repo"}):
            with self.assertRaises(self.d.LedgerUnavailable):
                self.d.make_store(self.path)
        self.assertFalse(self.path.exists())

    def test_duplicate_json_keys_and_invalid_receipts_are_rejected(self):
        self.path.write_text('{"version":1,"entries":{},"entries":{}}')
        self.assertFalse(self.ledger.deliver([self.item()], self.accepted))
        self.assertEqual(self.sent, [])

    def test_corrupt_local_or_remote_evidence_is_not_reinitialized(self):
        self.path.write_text("broken")
        self.assertFalse(self.ledger.deliver([self.item()], self.accepted))
        self.assertEqual(self.path.read_text(), "broken")
        for document in ({}, {"version": 2, "entries": {}}, {"version": 1, "entries": {"bad": {}}}):
            api, ledger = self.remote()
            api.document = document
            self.assertFalse(ledger.deliver([self.item()], self.accepted))
        self.assertEqual(self.sent, [])

    def test_size_cap_fails_before_send_without_evicting_receipts(self):
        self.assertTrue(self.ledger.deliver([self.item()], self.accepted))
        before = self.path.read_bytes()
        with mock.patch.object(self.d, "MAX_BYTES", len(before)):
            self.assertFalse(self.ledger.deliver([self.item("12")], self.accepted))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.sent), 1)

    def test_stale_local_cas_cannot_overwrite_newer_event(self):
        doc, version = self.store.read()
        self.ledger.deliver([self.item()], self.accepted)
        self.assertIsNone(self.store.compare_and_set(doc, version))
        self.assertEqual(len(self.store.read()[0]["entries"]), 1)

    def test_concurrent_writers_send_each_shared_chapter_once(self):
        api, first = self.remote()
        _, second = self.remote(api)
        reached, release = threading.Event(), threading.Event()
        def slow_send(items):
            self.sent.append(items)
            reached.set()
            self.assertTrue(release.wait(3))
            return self.d.SendResult("accepted", "sendMessage", 9)
        result = []
        worker = threading.Thread(target=lambda: result.append(first.deliver([self.item()], slow_send)))
        worker.start()
        self.assertTrue(reached.wait(3))
        try:
            self.assertFalse(second.deliver([self.item()], self.accepted))
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [True])
        self.assertEqual(len(self.sent), 1)

    def test_remote_cas_race_on_stale_reads_does_not_double_send(self):
        api, first = self.remote()
        _, second = self.remote(api)
        barrier = threading.Barrier(2)
        original_get = api.get
        reads = []
        lock = threading.Lock()
        def simultaneous_read(*args, **kwargs):
            response = original_get(*args, **kwargs)
            with lock:
                reads.append(1)
                wait = len(reads) <= 2
            if wait:
                barrier.wait(3)
            return response
        api.get = simultaneous_read
        results = []
        threads = [threading.Thread(target=lambda ledger=l: results.append(ledger.deliver([self.item()], self.accepted)))
                   for l in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertIn(True, results)
        self.assertEqual(len(self.sent), 1)

    def test_queued_favorite_is_separate_and_survives_card_baseline_advance(self):
        self.assertTrue(self.ledger.deliver([self.item()], self.accepted, queue_favorites=True))
        restarted = self.d.DeliveryLedger(self.d.LocalStore(self.path), "a" * 64)
        pending = restarted.pending_favorites()
        self.assertEqual([i["chapter"] for i in pending], ["11"])
        self.assertFalse(restarted.deliver(pending, lambda _: self.d.SendResult("rejected", "sendMessage")))
        self.assertTrue(restarted.deliver(restarted.pending_favorites(), self.accepted))
        self.assertEqual(restarted.pending_favorites(), [])
        self.assertTrue(restarted.deliver([self.item()], self.accepted, queue_favorites=True))
        self.assertEqual(restarted.pending_favorites(), [])
        self.assertEqual(len(self.sent), 2)

    def test_target_scope_is_stable_across_token_rotation_but_separates_chats(self):
        self.assertEqual(self.d.target_scope("123:old", "chat-a"), self.d.target_scope("123:new", "chat-a"))
        self.assertNotEqual(self.d.target_scope("123:old", "chat-a"), self.d.target_scope("123:old", "chat-b"))
        self.ledger.deliver([self.item()], self.accepted)
        self.assertNotIn("fixture-token", self.path.read_text())


if __name__ == "__main__":
    unittest.main()
