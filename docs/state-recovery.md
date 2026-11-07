# State recovery snapshots (GM-002)

The workflow snapshots `monitor_state.json`, `bookmarks.json`, and
`bot_state.json` after the bot step and before state persistence or Git reset.
Original bytes are kept outside the checkout under
`$RUNNER_TEMP/state-recovery-<run_id>-<run_attempt>/original/`.

The snapshot step validates that each file is a JSON object. Missing, unreadable,
nonregular, invalid JSON, and non-object files fail validation. Nonstandard JSON
constants such as NaN are rejected. Original bytes are retained when readable;
a failed validation blocks the persistence step instead of replacing the input
with an empty object or silently discarding corrupted state.

## Export and retention

A separate `artifact/` directory contains only the three allowed state filenames
and `manifest.json`. It never includes .env, raw logs, scripts, or merge inputs.
The manifest records file status, byte count, SHA-256, whether a file was exported,
and run identifiers. Raw state contents and parser exception messages are not logged.

Before export, the helper checks known workflow secret values, common GitHub and
Telegram token patterns, credential URLs/headers, and sensitive JSON key names.
It checks both raw bytes and Unicode-unescaped text, keeping duplicate keys and
malformed fragments visible to screening. A detected credential blocks
persistence and excludes the entire affected file from the export; its original
remains only in the runner's private temporary directory. This is screening for
known forms, not a guarantee that arbitrary unrecognized private data is absent.

The recovery artifact is selected when snapshot validation, persistence, or the
bot step fails. Its name includes both run ID and run attempt, and retention is
7 days. Artifact upload errors are nonblocking so they cannot replace the original
job failure. If upload also fails, recovery from an artifact is unavailable.

Artifacts follow repository access rules; an artifact in this public repository
should not be treated as private storage. Bookmark preferences may be present.
Files excluded for sensitive content cannot be recovered from the downloadable
artifact. Runner-only originals disappear when the hosted runner is discarded.

## Inspecting a recovery

1. Download `manga-state-recovery-<run_id>-<run_attempt>` from the failed run.
2. Read `manifest.json`; check each status and `artifact_included` flag.
3. Verify each included file against its recorded SHA-256 before using it.
4. Compare the snapshot with the latest remote state and notification history.
   Do not overwrite newer state or rerun notification delivery automatically.
   GM-005 covers the full recovery/replay procedure.

For malformed JSON, the artifact may include the exact invalid bytes for diagnosis
when credential screening permits it. No repair is performed automatically.

## Offline verification

Install test dependencies with `python3 -m pip install -r requirements-dev.txt`,
then run `python3 -m unittest discover -v`. Tests use temporary local Git
repositories and synthetic state; they do not send Telegram messages, dispatch
Actions, or upload artifacts to GitHub.

Rollback: revert the GM-002 change set. It does not alter the tracked state schema
or require deleting state files.
