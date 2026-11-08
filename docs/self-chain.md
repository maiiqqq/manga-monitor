# Self-chain outcomes (GM-004)

The handoff makes at most one workflow-dispatch POST per invocation. It retains
the existing WORKFLOW_PAT configuration and the best-effort five-minute cron.
The helper is restored from the initial GITHUB_SHA Git object into private
RUNNER_TEMP so a persistence reset cannot replace or remove the running revision.

## Outcomes

| Self-chain outcome | Meaning |
|---|---|
| accepted | GitHub returned HTTP 204, or a correlated workflow run was observed |
| failed | An explicit rejection, invalid configuration, or unavailable helper |
| outcome_unknown | A server/transport response did not establish acceptance, and read-only checks could not confirm it |
| disabled | WORKFLOW_PAT is not configured |

HTTP 401/403 reports permission_denied; it does not change token scopes.
Other HTTP 4xx responses are explicit failures. For 5xx, connection failures or
unexpected responses, the helper checks this workflow's recent dispatch runs.

An internal chain_request_id combines the parent run ID and run attempt. The
workflow run-name exposes it as Go-Manga Monitor (chain <run>-<attempt>).
Confirmation requires the exact title, workflow_dispatch event, master branch,
and a positive numeric run ID. The endpoint is scoped to manga-monitor.yml.

Lookup uses up to three GET requests, with two-second pauses and a recent-20-run
window. An eight-second socket timeout and one-MiB read limit bound each request;
the Actions step has a one-minute timeout. No ambiguous outcome causes a POST
retry. Late visibility, pagination beyond the window, or lookup errors can leave
an accepted request unconfirmed; cron remains fallback.

Accepted means the request was accepted, not that the next job ran successfully.
Other schedule/manual runs and queue cancellation are not controlled here.

## Reporting and credentials

The step summary separately records the persistence outcome and self-chain
outcome, reason, correlation ID and observed next-run ID. Failed/unknown handoffs
return nonzero, but the self-chain step is continue-on-error so the original
persistence outcome is preserved. Its failure remains visible in step outcome,
warning and summary. Disabled/accepted handoffs return zero.

Only fixed diagnostic reasons and validated identifiers are printed. Tokens,
HTTP response bodies, request headers and exception messages are never logged.
Requests go only to the public GitHub API repository path; redirects are not
followed. The credential policy and workflow permissions are unchanged.

## Validation and rollout

Tests mock the HTTP boundary and cover 204, 401/403, 422, 500, timeout after
acceptance, delayed visibility, mismatched runs, malformed/truncated responses,
missing credentials, and summary reporting. A real YAML bootstrap test resets a
local Git checkout to a revision without the helper and recovers it from the
initial Git object with dispatch disabled. No live dispatch is needed.

The new chain_request_id input and run-name must be present on master before
the production helper can send that input. This is why a pre-merge branch
dispatch is not a substitute for offline verification.

Rollback: revert GM-004. Snapshot/upload/persistence and their retry policies
are unchanged.

References:
- https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#run-name
- https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
- https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs
