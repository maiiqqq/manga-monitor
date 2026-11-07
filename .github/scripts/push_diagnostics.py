#!/usr/bin/env python3
"""Classify git push diagnostics without changing retry policy or exit status."""
import os
from pathlib import Path
import re
import sys


def classify_push_error(output: str) -> str:
    """Use explicit Git/server signals; generic remote rejection stays unknown."""
    text = output.lower()
    if re.search(
        r"gh006|gh013|repository rule violations|protected branch|"
        r"permission (?:to .+? )?denied|denied to |authentication failed|"
        r"could not read username|write access .*not granted|"
        r"(?:http(?:/\S+)?[ :]+|returned error: ?)(?:401|403)\b",
        text,
    ):
        return "permission_or_rule"
    if re.search(
        r"fatal error in commit_refs(?:_rules)?|"
        r"(?:http(?:/\S+)?[ :]+|returned error: ?)5\d\d\b|"
        r"internal server error|bad gateway|service unavailable|gateway timeout",
        text,
    ):
        return "transient_server"
    if re.search(r"non-fast-forward|\(fetch first\)", text):
        return "non_fast_forward"
    if re.search(
        r"could not resolve (?:host|proxy)|connection (?:timed out|reset)|"
        r"failed to connect|network is unreachable|"
        r"remote end hung up unexpectedly|operation timed out",
        text,
    ):
        return "transient_network"
    return "unknown"


def redact_push_output(output: str) -> str:
    """Remove credentials before printing; never print the original exception."""
    # Strip ANSI and normalize line breaks before adding a safe prefix per line.
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    output = output.replace("\r", "\n")
    output = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", output)
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        secret = os.environ.get(name)
        if secret:
            output = output.replace(secret, "[REDACTED]")
    output = re.sub(
        r"(?i)(https?://)[^\s/@]+(?:[^\s@]*?)@",
        r"\1[REDACTED]@",
        output,
    )
    output = re.sub(
        r"(?i)([?&](?:access_token|token|auth|key|password)=)[^&\s]+",
        r"\1[REDACTED]",
        output,
    )
    return re.sub(
        r"(?i)(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)\S+",
        r"\1[REDACTED]",
        output,
    )


def main() -> None:
    log_path, exit_code, attempt, local_sha, remote_sha = sys.argv[1:]
    try:
        output = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        output = "[push diagnostics unavailable]"
    category = "success" if exit_code == "0" else classify_push_error(output)
    print(
        f"Push result: attempt={attempt} exit_code={exit_code} category={category} "
        f"local_sha={local_sha} remote_sha={remote_sha}"
    )
    for line in redact_push_output(output).splitlines():
        print(f"push | {line}")


if __name__ == "__main__":
    main()
