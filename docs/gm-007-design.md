# GM-007: delivery safety

Approved task intent: user asked to merge GM-005 and continue GM-007 after its
four work items were explained. Implementation choices below are the author's
choices within that scope, not separate user decisions.

## Goal and constraints

Do not advance CHECK_URLS state after rejection. Preview must select its mode
before Telegram startup, never change tracked state or chain a live child.
For chapter cards and favorites summaries, record intent before sending and
retain accepted/rejected/unknown evidence across restart and failed state push.
No live Telegram, workflow dispatch or replay during development. No increased
permissions, force push or restoration of old state. No exactly-once claim.
Command replies/button acknowledgements are outside notification dedup scope.

## Approaches and choice

- Only fix CHECK_URLS and preview: small, but leaves the observed send/save/push
  duplicate windows open.
- Local outbox only: survives process restart on a VM, but an ephemeral Actions
  runner loses it on failed push. Insufficient for the reported incident.
- Chosen: write-ahead delivery ledger, local durable file on a VM and GitHub
  Contents API compare-and-set on master for Actions. An intent becomes unknown
  durably before Telegram POST; only explicit rejection allows auto retry.
  Accepted responses require a valid message ID. An uncertain write/send stops
  further delivery of that event. Costs extra GitHub requests/commits and pauses
  notifications during GitHub/storage outages. Existing contents:write suffices.

## Boundaries

`delivery_ledger.py` owns validated schema, local atomic CAS, remote CAS and
event claims/completion. `delivery_state.json` starts as an empty versioned
ledger. Existing persistence merges only its three state files, so it must
retain the latest remote ledger without adding a stale checkout copy.

Event identity is target scope (hash of stable bot ID and chat ID), manga URL,
chapter number, and kind (card/favorite). Each chapter has its own key, even if
sent in a batch. Card identity is shared across photo/text fallback. A partial
overlap sends only unclaimed/rejected chapters. An unknown chapter prevents
baseline advancement over that update. Known accepted chapters are not resent.

Remote store reads/writes only delivery_state.json on master via api.github.com,
requires the existing file, validates blob SHA and schema, uses bounded timeouts,
rejects redirects, and retries only explicit CAS conflicts (at most three).
Missing credentials, invalid ledger, permission failures, uncertain writes and
size cap exhaustion fail closed. It never falls back from Actions to local-only.
Local store uses file locking, fsync and atomic replace; a missing local file
can initialize empty only outside Actions. Remote initialization is in this PR.

Card accepted completion atomically queues per-chapter favorite events with
minimal public manga metadata. Each cycle drains queued/rejected summaries even
when no new cards were detected. Unknown summary outcomes remain held too.
Queued events are safe to send because they have no prior attempt; claiming one
changes it to unknown before HTTP. No automatic pruning of delivery evidence.

Preview (`BACKFILL_DRYRUN` truthy) is global: CHECK_URLS selects targeted preview,
otherwise BACKFILL_TODAY selects date preview, otherwise normal preview. It uses
a read-only in-memory baseline copy and can scrape the manga site. It never
constructs a Telegram notifier, writes local runtime state, snapshots/persists
state or self-chains. Offline tests mock site HTTP as well. Preview is not an
offline network guarantee. The workflow passes the flag independently to the
self-chain helper so a reset cannot turn preview into a live child.

## Evidence, capacity, migration and rollback

Schema records kind, scope, URL/chapter/title, status, claim ID and receipt
(method/message ID). It never stores bot tokens, raw chat IDs or message bodies.
Public manga/bookmark preferences remain public repository data. Hard cap the
serialized ledger at 750 KB; stop sends when full rather than evict history.
This trades availability for duplicate avoidance; future sharding/retention
requires a separately reviewed migration.

Existing deliveries have no ledger evidence: do not backfill receipts from
baselines. Existing baseline normally suppresses old cards; historical unknown
attempts still require manual reconciliation per GM-005. External/manual sends,
old code, changed event identities, operator deletion/rollback of evidence, and
multiple uncoordinated stores are outside the guarantee. A timed-out intent
write may hold an event that was never sent; never retry unknown automatically.

Recovery is a reviewed diff on latest ledger: match a real receipt to accept an
unknown, or prove non-delivery before marking rejected. Do not delete evidence
or change unknown merely because it is old. No live reconciliation CLI/replay
is added. Rollback code must preserve ledger and pause delivery first: reverting
to old unguarded senders reintroduces duplicates. Do not revert runtime state.

## Verification

TDD covers CHECK_URLS rejection/success, all preview flag combinations,
no-credential preview, workflow gates and helper dispatch behavior; ledger CAS,
malformed/oversize/redirect/permission/timeout paths; real local disk restart,
overlapping chapter batches, photo ambiguity, failed intent/receipt/state writes,
failed push and concurrent writers; separate summary rejection/retry/unknown.
All HTTP is mocked; local Git rejection proves state push independence.
Run full unittest suite and independent whole-branch review before ready PR.
