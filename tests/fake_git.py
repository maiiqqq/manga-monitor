#!/usr/bin/env python3
"""Test-only Git transport failures; all successful operations use local real Git."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
real_git = os.environ["REAL_GIT"]


def real(*command, cwd=None):
    return subprocess.run(
        [real_git, *command], cwd=cwd, text=True, capture_output=True, check=True,
    ).stdout.strip()


def advance_remote():
    seed = os.environ["PERSIST_TEST_SEED"]
    real("fetch", "origin", "master", cwd=seed)
    real("reset", "--hard", "origin/master", cwd=seed)
    path = Path(seed) / "monitor_state.json"
    state = json.loads(path.read_text())
    state["other"] = {"last_check": "2026-01-03"}
    path.write_text(json.dumps(state, indent=2))
    real("add", "monitor_state.json", cwd=seed)
    real("commit", "-m", "concurrent state update", cwd=seed)
    real("push", "origin", "master", cwd=seed)


if args and args[0] in ("push", "fetch"):
    operation = args[0]
    state_path = Path(os.environ["PERSIST_TEST_COUNTS"])
    counts = json.loads(state_path.read_text()) if state_path.exists() else {}
    counts[operation] = counts.get(operation, 0) + 1
    state_path.write_text(json.dumps(counts))
    record = {"operation": operation, "ordinal": counts[operation], "args": args}
    if operation == "push":
        record["local_sha"] = real("rev-parse", "HEAD")
    with open(os.environ["PERSIST_TEST_TRACE"], "a") as log:
        log.write(json.dumps(record) + "\n")
    scenario = json.loads(os.environ.get("PERSIST_TEST_SCENARIO", "{}"))
    event = {}
    if operation == "fetch" and counts[operation] >= scenario.get("fetch_failure_from", 10000):
        event = {"error": "fatal: unable to access URL: Connection timed out"}
    if operation == "push":
        events = scenario.get("pushes", [])
        if counts[operation] <= len(events):
            event = events[counts[operation] - 1]
        elif os.environ.get("SIMULATED_PUSH_FAILURE"):
            event = {"error": os.environ["SIMULATED_PUSH_FAILURE"]}
    if event.get("accept"):
        real(*args)
    if event.get("advance"):
        advance_remote()
    if event.get("delay"):
        time.sleep(event["delay"])
    if event.get("error"):
        print(event["error"])
        print("diagnostic stderr marker", file=sys.stderr)
        sys.exit(event.get("exit", 128))
    if event.get("accept"):
        sys.exit(0)
sys.exit(subprocess.call([real_git, *args]))
