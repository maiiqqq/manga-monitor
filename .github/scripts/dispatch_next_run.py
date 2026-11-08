#!/usr/bin/env python3
"""Report one self-chain dispatch accurately without retrying an uncertain POST."""
from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
WORKFLOW = "manga-monitor.yml"
MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ApiResponse:
    status: int | None
    payload: dict | None = None


@dataclass(frozen=True)
class DispatchResult:
    state: str
    reason: str
    request_id: str = ""
    next_run_id: int | None = None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_request(method, url, token, payload=None):
    # Never forward the dispatch credential to another host or a redirect.
    if not url.startswith(API + "/repos/"):
        return ApiResponse(None)
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "manga-monitor-self-chain",
    }
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
            status = response.status
            if method != "GET":
                return ApiResponse(status)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return ApiResponse(status)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                body = None
            return ApiResponse(status, body if isinstance(body, dict) else None)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return ApiResponse(status)
    except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError):
        return ApiResponse(None)


def dispatch_next(env, *, request, sleep):
    if env.get("BACKFILL_DRYRUN", "").strip().lower() in ("1", "true", "yes"):
        return DispatchResult("disabled", "preview_requested")
    token = env.get("CHAIN_PAT", "").strip()
    if not token:
        return DispatchResult("disabled", "workflow_pat_not_configured")
    repo = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    attempt = env.get("GITHUB_RUN_ATTEMPT", "")
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or any(part in (".", "..") for part in repo.split("/"))
            or not re.fullmatch(r"[0-9]{1,20}", run_id)
            or not re.fullmatch(r"[0-9]{1,20}", attempt)
            or "\n" in token or "\r" in token):
        return DispatchResult("failed", "configuration_invalid")
    request_id = f"{run_id}-{attempt}"
    title = f"Go-Manga Monitor (chain {request_id})"
    workflow_url = f"{API}/repos/{repo}/actions/workflows/{WORKFLOW}"
    response = request("POST", workflow_url + "/dispatches", token, {
        "ref": "master",
        "inputs": {"chained": "yes", "chain_request_id": request_id},
    })
    if response.status == 204:
        return DispatchResult("accepted", "http_204", request_id)
    if response.status in (401, 403):
        return DispatchResult("failed", "permission_denied", request_id)
    if response.status is not None and 400 <= response.status < 500:
        return DispatchResult("failed", "request_rejected", request_id)
    # Network/server errors can occur after acceptance. Poll reads only, not POST.
    query = workflow_url + "/runs?branch=master&event=workflow_dispatch&per_page=20"
    for poll in range(3):
        if poll:
            sleep(2)
        lookup = request("GET", query, token)
        runs = lookup.payload.get("workflow_runs") if lookup.status == 200 and lookup.payload else None
        if not isinstance(runs, list):
            continue
        for run in runs:
            if not isinstance(run, dict):
                continue
            observed_id = run.get("id")
            if (run.get("display_title") == title
                    and run.get("head_branch") == "master"
                    and run.get("event") == "workflow_dispatch"
                    and type(observed_id) is int and observed_id > 0):
                return DispatchResult("accepted", "observed_correlated_run", request_id, observed_id)
    return DispatchResult("outcome_unknown", "acceptance_not_confirmed", request_id)


def main():
    result = dispatch_next(os.environ, request=github_request, sleep=time.sleep)
    persistence = os.environ.get("PERSISTENCE_OUTCOME", "")
    if persistence not in ("success", "failure", "skipped", "cancelled"):
        persistence = "unknown"
    print(f"Self-chain: state={result.state} reason={result.reason}")
    if result.state in ("failed", "outcome_unknown"):
        print("::warning::Self-chain was not confirmed; no POST retry. Cron remains the best-effort fallback.")
    summary = (
        "### Go-Manga handoff\n\n"
        "| Component | Outcome |\n|---|---|\n"
        f"| Persistence | {persistence} |\n"
        f"| Self-chain | {result.state} |\n\n"
        f"Reason: {result.reason}\n\n"
        f"Correlation: {result.request_id or 'none'}\n\n"
        f"Observed next run: {result.next_run_id or 'not observed'}\n\n"
        "Cron fallback remains configured (every 5 minutes, best effort).\n"
    )
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with Path(path).open("a", encoding="utf-8") as stream:
                stream.write(summary)
        except OSError:
            print("::warning::Could not write self-chain summary; no dispatch retry.")
            return 1
    return 0 if result.state in ("accepted", "disabled") else 1


if __name__ == "__main__":
    raise SystemExit(main())
