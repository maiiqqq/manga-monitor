#!/usr/bin/env python3
"""Preserve exact state bytes and prepare a narrow, credential-screened export."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

STATE_FILES = ("monitor_state.json", "bookmarks.json", "bot_state.json")
SECRET_ENV = ("GITHUB_TOKEN", "GH_TOKEN", "TELEGRAM_BOT_TOKEN", "WORKFLOW_PAT", "TELEGRAM_CHAT_ID")
SENSITIVE = re.compile(
    rb'(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}|'
    rb'\b\d{6,}:[A-Za-z0-9_-]{20,}|'
    rb'"(?:access_token|token|bot_token|telegram_bot_token|github_token|workflow_pat|'
    rb'password|secret|authorization|chat_id)"\s*:|'
    rb'https?://[^\s/@]+(?::[^\s@]*)?@|'
    rb'[?&](?:access_token|token|auth|password)=[^&\s]+|'
    rb'authorization\s*:\s*(?:basic|bearer)\s+\S+',
    re.IGNORECASE,
)


def reject_constant(_value):
    raise ValueError("Nonstandard JSON constant")


def contains_credentials(raw: bytes) -> bool:
    # Screen the entire text, preserving duplicate keys and malformed fragments.
    # Conservative Unicode unescaping can over-exclude, but cannot discard a
    # secret-bearing duplicate as a dictionary parser would.
    payload = raw.replace(b"\x00", b"")
    text = payload.decode("utf-8", errors="replace")
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text)
    payload += text.replace(r"\/", "/").encode("utf-8", errors="backslashreplace")
    return bool(SENSITIVE.search(payload)) or any(
        value.encode("utf-8") in payload
        for name in SECRET_ENV if (value := os.environ.get(name))
    )


def capture(source: Path, destination: Path) -> int:
    os.umask(0o077)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    original = destination / "original"
    export = destination / "artifact"
    original.mkdir(mode=0o700)
    export.mkdir(mode=0o700)
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    source_sha = os.environ.get("GITHUB_SHA", "")
    manifest = {
        "schema_version": 1,
        "run_id": run_id if run_id.isdecimal() else "unknown",
        "run_attempt": attempt if attempt.isdecimal() else "unknown",
        "source_sha": source_sha if re.fullmatch(r"[0-9a-fA-F]{40}", source_sha) else "unknown",
        "files": {},
    }
    failed = False
    for name in STATE_FILES:
        entry = {"status": "unreadable", "artifact_included": False}
        manifest["files"][name] = entry
        raw = None
        try:
            path = source / name
            if not stat.S_ISREG(path.lstat().st_mode):
                entry["status"] = "not_regular_file"
            else:
                raw = path.read_bytes()
        except FileNotFoundError:
            entry["status"] = "missing"
        except OSError:
            pass
        if raw is not None:
            # Keep untouched bytes even when validation or credential screening fails.
            (original / name).write_bytes(raw)
            entry.update(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            parsed = None
            try:
                parsed = json.loads(raw, parse_constant=reject_constant)
                entry["status"] = "valid" if isinstance(parsed, dict) else "not_object"
            except (ValueError, UnicodeError, RecursionError):
                entry["status"] = "invalid_json"
            if contains_credentials(raw):
                entry["exclusion_reason"] = "sensitive_content"
                failed = True
            else:
                (export / name).write_bytes(raw)
                entry["artifact_included"] = True
        failed |= entry["status"] != "valid"
        print(
            f"State snapshot: file={name} status={entry['status']} "
            f"artifact_included={entry['artifact_included']}"
        )
    (export / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    return capture(args.source, args.destination)


if __name__ == "__main__":
    raise SystemExit(main())
