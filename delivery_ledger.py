"""Write-ahead evidence for chapter cards and favorite summaries.

An unknown intent is durable before Telegram is called. Only an explicit
rejection can be retried automatically. Storage errors always stop delivery.
"""
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit
import uuid

import requests

MAX_BYTES = 750_000


class LedgerUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SendResult:
    state: str
    method: str
    message_id: int | None = None


def target_scope(bot_token, chat_id):
    # The numeric bot ID remains stable when a Telegram token is rotated.
    return hashlib.sha256(json.dumps([bot_token.split(":", 1)[0], str(chat_id)]).encode()).hexdigest()


def canonical_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password:
        raise LedgerUnavailable("invalid event URL")
    if parsed.query or parsed.fragment:
        raise LedgerUnavailable("event URL must not contain query or fragment")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") + "/", "", ""))


def event_key(item):
    return hashlib.sha256(json.dumps([item[k] for k in ("scope", "kind", "url", "chapter")],
                                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate(document):
    if (not isinstance(document, dict) or set(document) != {"version", "entries"}
            or type(document["version"]) is not int or document["version"] != 1
            or not isinstance(document["entries"], dict)):
        raise LedgerUnavailable("invalid ledger schema")
    required = {"kind", "scope", "url", "chapter", "title", "status", "claim_id"}
    for key, entry in document["entries"].items():
        if (not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key)
                or not isinstance(entry, dict) or not required <= entry.keys()
                or set(entry) - required - {"method", "message_id"}):
            raise LedgerUnavailable("invalid ledger entry")
        if (any(not isinstance(entry[k], str) for k in required)
                or entry["kind"] not in ("card", "favorite")
                or entry["status"] not in ("queued", "accepted", "rejected", "unknown")
                or not re.fullmatch(r"[a-f0-9]{64}", entry["scope"])
                or not re.fullmatch(r"[a-f0-9]{32}", entry["claim_id"])
                or not 1 <= len(entry["chapter"]) <= 128 or len(entry["title"]) > 512
                or len(entry["url"]) > 4096 or canonical_url(entry["url"]) != entry["url"]
                or event_key(entry) != key
                or (entry["status"] == "queued" and entry["kind"] != "favorite")):
            raise LedgerUnavailable("invalid ledger values")
        if entry.get("method") not in (None, "sendMessage", "sendPhoto"):
            raise LedgerUnavailable("invalid receipt method")
        if entry["status"] == "accepted" and (
                entry.get("method") not in ("sendMessage", "sendPhoto")
                or type(entry.get("message_id")) is not int or entry["message_id"] <= 0):
            raise LedgerUnavailable("accepted entry requires a receipt")
    return document


def encode(document):
    validate(document)
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(raw) > MAX_BYTES:
        raise LedgerUnavailable("ledger capacity reached")
    return raw


def decode(raw):
    if len(raw) > MAX_BYTES:
        raise LedgerUnavailable("ledger capacity reached")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LedgerUnavailable("duplicate ledger key")
            result[key] = value
        return result

    try:
        return validate(json.loads(raw, object_pairs_hook=unique,
                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError())))
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise LedgerUnavailable("unreadable ledger") from error


class LocalStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        if self.path.is_symlink():
            raise LedgerUnavailable("ledger must be a regular file")
        if not self.path.exists():
            return {"version": 1, "entries": {}}, None
        with self.path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        return decode(raw), hashlib.sha256(raw).hexdigest()

    def read(self):
        with self.locked():
            return self._read()

    def compare_and_set(self, document, version):
        raw = encode(document)
        with self.locked():
            if self._read()[1] != version:
                return None
            fd, name = tempfile.mkstemp(prefix=".delivery-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.path)
                directory = os.open(self.path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                Path(name).unlink(missing_ok=True)
        return hashlib.sha256(raw).hexdigest()


class GitHubStore:
    def __init__(self, repository, token, *, client=requests):
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or any(p in (".", "..") for p in repository.split("/"))
                or not token.strip() or "\n" in token or "\r" in token):
            raise LedgerUnavailable("remote ledger configuration missing or invalid")
        self.url = f"https://api.github.com/repos/{repository}/contents/delivery_state.json"
        self.client = client
        self.headers = {"Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def request(self, method, **kwargs):
        try:
            return getattr(self.client, method)(self.url, headers=self.headers,
                                                timeout=8, allow_redirects=False, **kwargs)
        except requests.RequestException as error:
            raise LedgerUnavailable("remote ledger request uncertain") from error

    def read(self):
        response = self.request("get", params={"ref": "master"})
        if response.status_code != 200:
            raise LedgerUnavailable("remote ledger read failed")
        try:
            body = response.json()
            if (body.get("type") != "file" or body.get("encoding") != "base64"
                    or not re.fullmatch(r"[a-f0-9]{40}", body.get("sha", ""))
                    or not isinstance(body.get("content"), str)
                    or len(body["content"]) > 1_050_000):
                raise LedgerUnavailable("invalid remote ledger response")
            raw = base64.b64decode("".join(body["content"].split()), validate=True)
            return decode(raw), body["sha"]
        except (ValueError, TypeError, AttributeError) as error:
            raise LedgerUnavailable("invalid remote ledger response") from error

    def compare_and_set(self, document, version):
        if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{40}", version):
            raise LedgerUnavailable("remote ledger must already exist")
        response = self.request("put", json={"branch": "master", "sha": version,
            "message": "Record notification delivery evidence",
            "content": base64.b64encode(encode(document)).decode()})
        if response.status_code == 409:
            return None
        if response.status_code != 200:
            raise LedgerUnavailable("remote ledger write uncertain")
        try:
            version = response.json()["content"]["sha"]
        except (ValueError, TypeError, KeyError) as error:
            raise LedgerUnavailable("remote ledger write uncertain") from error
        if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{40}", version):
            raise LedgerUnavailable("remote ledger write uncertain")
        return version


def make_store(path):
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        return GitHubStore(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""))
    return LocalStore(path)


class DeliveryLedger:
    def __init__(self, store, scope):
        self.store, self.scope = store, scope

    def item(self, kind, url, chapter, title):
        number = str(chapter).strip()
        if number.isdigit():
            number = str(int(number))
        return {"kind": kind, "scope": self.scope, "url": canonical_url(url),
                "chapter": number, "title": str(title)[:512]}

    def pending_favorites(self):
        document, _ = self.store.read()
        return [{k: e[k] for k in ("kind", "scope", "url", "chapter", "title")}
                for e in document["entries"].values()
                if e["scope"] == self.scope and e["kind"] == "favorite"
                and e["status"] in ("queued", "rejected")]

    def deliver(self, items, send, *, queue_favorites=False):
        """Return True only when every requested event has a durable receipt."""
        try:
            return self._deliver(items, send, queue_favorites)
        except (LedgerUnavailable, OSError, ValueError, TypeError, RecursionError):
            print("[ERROR] Delivery evidence unavailable; notification held", file=sys.stderr)
            return False

    def _deliver(self, items, send, queue_favorites):
        wanted = {event_key(item): item for item in items}
        if not wanted or any(i["scope"] != self.scope for i in items):
            return False
        claim_id = uuid.uuid4().hex
        for _ in range(3):
            document, version = self.store.read()
            entries = document["entries"]
            selected = {k: i for k, i in wanted.items()
                        if entries.get(k, {}).get("status") in (None, "queued", "rejected")}
            if not selected:
                return all(entries.get(k, {}).get("status") == "accepted" for k in wanted)
            for key, item in selected.items():
                entries[key] = {**item, "status": "unknown", "claim_id": claim_id}
            if self.store.compare_and_set(document, version) is not None:
                break
        else:
            raise LedgerUnavailable("claim contention")
        try:
            result = send(list(selected.values()))
        except Exception:
            # An exception is never proof of non-delivery.
            return False
        if (not isinstance(result, SendResult) or result.state not in ("accepted", "rejected")
                or result.method not in ("sendMessage", "sendPhoto")):
            return False
        if result.state == "accepted" and (type(result.message_id) is not int or result.message_id <= 0):
            return False
        for _ in range(3):
            document, version = self.store.read()
            entries = document["entries"]
            if any(entries.get(k, {}).get("claim_id") != claim_id
                   or entries[k]["status"] != "unknown" for k in selected):
                raise LedgerUnavailable("claim ownership changed")
            for key in selected:
                entries[key].update(status=result.state, method=result.method)
                if result.state == "accepted":
                    entries[key]["message_id"] = result.message_id
                    if queue_favorites:
                        favorite = {**selected[key], "kind": "favorite"}
                        entries.setdefault(event_key(favorite), {**favorite, "status": "queued", "claim_id": claim_id})
            if self.store.compare_and_set(document, version) is not None:
                return all(entries.get(k, {}).get("status") == "accepted" for k in wanted)
        raise LedgerUnavailable("receipt contention")
