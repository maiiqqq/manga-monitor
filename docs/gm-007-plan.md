# GM-007 Implementation Plan

> For agentic workers: use superpowers:executing-plans task by task.

**Goal:** implement the four GM-007 items with offline proof before a ready PR.
**Architecture:** global preview plus a write-ahead per-chapter delivery ledger.
**Tech Stack:** Python 3.11+, requests, existing GitHub contents:write, unittest.
**Spec:** [gm-007-design.md](gm-007-design.md), GM-007 in the ticket record.

## Global Constraints

- No live Telegram, workflow dispatch, replay, token changes or state restore.
- No exactly-once claim. Unknown delivery never auto retries.
- Current master is the remote base; preserve newer runtime state at publication.
- User asked to continue directly; implement inline and review once at the end.

## Review Focus

- CAS losers, response loss and invalid receipts must not cause a second send.
- Overlapping chapter batches must share identities; photo fallback is one card.
- Failed push must retain the remote ledger; no stale ledger staged by persistence.
- Preview must avoid startup API/state writes and self-chain across flags/reset.
- Summary retry must survive a cycle with no new cards and not replay cards.

### Task 1: CHECK_URLS and preview

Files: bot_realtime.py, .github/scripts/dispatch_next_run.py, workflow YAML,
tests/test_recovery_scenarios.py, tests/test_dispatch_next_run.py,
tests/test_state_commit_step.py.
Interfaces: `preview_requested(env) -> bool`; realtime selects preview before
initializing notifier; self-chain checks BACKFILL_DRYRUN before credentials.

- [ ] Change former unsafe-characterization assertions to the desired behavior;
  add credential-free preview and unchanged-state checks. Run focused tests;
  expect rejection baseline 10 vs old 11, preview no calls vs old API calls.
- [ ] Implement success-gated CHECK_URLS and global preview on a copied state.
  Workflow passes flag to self-chain and gates snapshot/persist/upload in preview.
- [ ] Run focused tests and existing suite, expect green. Commit task.

### Task 2: ledger stores and claims

Files: new delivery_ledger.py, delivery_state.json, tests/test_delivery_ledger.py.
Interfaces: stores `read() -> (document, version)`,
`compare_and_set(document, version) -> new_version | None`;
`DeliveryLedger.claim(items) -> Claim`; `complete(claim, result, favorites=())`.
Document schema version 1, entries dictionary, max serialized bytes 750000.

- [ ] Write failing store/claim tests: accepted suppresses resend, unknown holds,
  rejection allows retry, CAS contention serializes overlapping keys, invalid
  state/missing remote/no credentials/redirect/timeout block sending. Run RED.
- [ ] Implement validated local and GitHub stores, hashed identities, bounded
  conflict retry and claim ownership. Keep unknown before external send.
- [ ] Run store tests GREEN, commit.

### Task 3: notifier, summaries and fault injection

Files: go_manga_monitor.py, bot_realtime.py, ledger module and tests.
Interfaces: structured Telegram result accepted/rejected/unknown plus method and
message ID; guarded send_update returns True only if all input card events are
accepted durably; `flush_favorites_summary()` drains queued/rejected entries.

- [ ] Update loss/save/push/photo/repeated-backfill tests to expect no duplicate;
  add new card on same manga, partial overlap, concurrent writers, receipt failure,
  summary restart retry, malformed Telegram payload/HTTP5xx. Run RED.
- [ ] Implement write-ahead claims, safe photo fallback on explicit rejection,
  accepted receipts, persistent favorite queue and store selection. Require
  GITHUB_TOKEN in Actions; VM local ledger path is fixed relative to module.
- [ ] Workflow passes existing GITHUB_TOKEN to bot. Persistence keeps remote
  ledger on reset and never stages stale checkout data (local Git integration).
- [ ] Focused tests GREEN, full suite GREEN. Commit task.

### Task 4: operator docs and review

Files: docs/recovery-replay.md, docs/delivery-safety.md, DEPLOY.md and plan.

- [ ] Update current behavior, outage/unknown handling, CAS overhead/capacity,
  migration, preview scope and rollback. Keep GM-005 historical evidence clear.
- [ ] Full unittest discovery, py_compile and diff --check must pass. Independent
  fresh-context review; important fixes get RED→GREEN and a green suite.
- [ ] Publish ready PR using fresh master base, verify exact remote files/scope,
  update persistent ticket record. Do not merge new GM-007 PR or replay live.
