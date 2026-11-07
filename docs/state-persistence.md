# Bounded state persistence (GM-003)

The commit step runs `.github/scripts/persist_state.sh` after GM-002 snapshot
validation. The helper and its dependencies are copied outside the checkout
before Git resets can replace files. Original state remains available through
the GM-002 recovery path if persistence fails.

## Retry policy

- At most five persistence attempts; a failed fetch also consumes an attempt.
- Transient server/network failures use 5, 10, 20, then 40 seconds of backoff,
  plus a fresh 0–3 second jitter. No sleep occurs after the last attempt.
- Non-fast-forward failures fetch and remerge original state onto the current
  remote tip without a network-error backoff.
- Permission/rule failures and unknown failures stop rather than retry blindly.
- A failed fetch never authorizes a new push.

The complete process group receives TERM after 175 seconds and KILL five seconds
later. The helper also uses an internal 170-second budget and caps each network
command at the smaller of 20 seconds or the remaining budget, with a two-second
kill grace. Local commands remain inside the outer deadline. The environment
variable `PERSIST_BUDGET_SECONDS` can reduce the internal budget to 1–170 seconds;
invalid or larger values fail validation.

## Uncertain push results

A connection failure or server error does not prove the server rejected a push.
The helper retains the pending commit SHA and requires a successful fetch before
another commit or push. If the remote contains that SHA, even as an ancestor of a
newer commit, persistence is confirmed and no second push occurs.

If the fetched tip is unchanged and does not contain the pending commit, the same
commit is retried. If the tip advanced independently, the original state is
remerged with the existing three-way helper and a new commit is built.
If confirmation stays unavailable, the step fails and recovery remains selected.

This does not force-push, change state merge rules, replay Telegram notifications,
or create a durable delivery transaction across Telegram and GitHub. A
server-accepted push that cannot be confirmed before the deadline is reported as
failure, leaving operators to compare remote state with the recovery manifest.

## Verification and rollback

Run `python3 -m unittest discover -v` with the test dependencies installed.
Tests use local Git remotes, controlled transport failures and recorded sleeps.
They cover accepted-but-timed-out pushes, remote descendants/concurrent changes,
permission/unknown failures, retry bounds, and a real reduced-budget timeout.
No GitHub workflow dispatch, Telegram message or real artifact upload is required.

Rollback: revert GM-003 while retaining GM-001 diagnostics and GM-002 snapshots.
