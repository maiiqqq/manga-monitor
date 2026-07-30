#!/usr/bin/env python3
"""3-way merge for the bot's JSON state files.

The workflow commits monitor_state.json / bookmarks.json / bot_state.json back to
the repo. When two runs overlap (e.g. a manual check_urls dispatch alongside the
scheduled chain) both edit the same file, and a plain `git rebase` hits a text
conflict it can never resolve — the push then fails forever.

These files are flat JSON dicts, so they merge cleanly per key instead. Given the
version we started from (base), our version (ours) and the current remote one
(theirs), each key is resolved as:

  - untouched by us            -> take theirs (keeps concurrent updates)
  - untouched by them          -> take ours
  - removed on one side only   -> honour the removal
  - changed on both sides      -> per-file rule (see the resolvers below)

Usage:  merge_state.py <target.json> <base.json|-> <theirs.json|->

`target` is read as "ours" and overwritten with the merged result. A missing or
unreadable base/theirs is treated as absent.
"""
import json
import sys
from pathlib import Path

MISSING = object()


def load(path: str):
    """Parse a JSON dict, or return None when absent/unreadable/not a dict."""
    if not path or path == "-":
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def merge_keys(base: dict, ours: dict, theirs: dict, resolve) -> dict:
    """Per-key 3-way merge. `resolve(key, ours, theirs)` breaks real conflicts."""
    merged = dict(theirs)
    for key in set(ours) | set(theirs):
        o = ours.get(key, MISSING)
        t = theirs.get(key, MISSING)
        b = base.get(key, MISSING)
        if o == t:
            continue
        if o is MISSING:
            # We dropped the key (or never saw it). Their add survives only if we
            # never had it to begin with.
            if b is MISSING:
                merged[key] = t
            else:
                merged.pop(key, None)
        elif t is MISSING:
            # They dropped it: honour that only if we left it untouched.
            if o == b:
                merged.pop(key, None)
            else:
                merged[key] = o
        elif o == b:
            merged[key] = t
        elif t == b:
            merged[key] = o
        else:
            merged[key] = resolve(key, o, t)
    return merged


def _baseline_rank(entry):
    """Sort key putting the further-along chapter baseline first."""
    entry = entry or {}
    chapter = str(entry.get("last_chapter", ""))
    return (
        int(chapter) if chapter.isdigit() else -1,
        str(entry.get("last_chapter_date") or ""),
        str(entry.get("last_check") or ""),
    )


def _resolve_monitor(_url, ours, theirs):
    # Both runs advanced the same manga. Keep the higher baseline: a lower one
    # would re-announce a chapter the user was already notified about.
    return max((ours, theirs), key=_baseline_rank)


def _resolve_ours(_key, ours, _theirs):
    # Bookmarks are driven by the user's button taps, and only the run that is
    # long-polling Telegram sees them, so its version wins.
    return ours


def _merge_bot_state(base, ours, theirs):
    merged = merge_keys(base, ours, theirs, _resolve_ours)
    # telegram_offset must never move backwards — that would replay old commands
    # and answer them twice.
    offsets = [d.get("telegram_offset") for d in (base, ours, theirs)]
    offsets = [o for o in offsets if isinstance(o, int)]
    if offsets:
        merged["telegram_offset"] = max(offsets)
    if base.get("commands_registered") or ours.get("commands_registered") \
            or theirs.get("commands_registered"):
        merged["commands_registered"] = True
    return merged


MERGERS = {
    "monitor_state.json": lambda b, o, t: merge_keys(b, o, t, _resolve_monitor),
    "bookmarks.json": lambda b, o, t: merge_keys(b, o, t, _resolve_ours),
    "bot_state.json": _merge_bot_state,
}


def main():
    if len(sys.argv) != 4:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)
    target, base_path, theirs_path = sys.argv[1:4]

    ours = load(target)
    theirs = load(theirs_path)
    base = load(base_path)

    if ours is None:
        # Nothing usable of ours (missing or corrupted): keep the remote version
        # rather than pushing junk over it.
        if theirs is None:
            print(f"[MERGE] {target}: nothing to merge")
            return
        Path(target).write_text(
            json.dumps(theirs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[MERGE] {target}: ours unreadable, kept remote version")
        return

    if theirs is None:
        print(f"[MERGE] {target}: no remote version, kept ours ({len(ours)} keys)")
        return

    merge = MERGERS.get(Path(target).name,
                        lambda b, o, t: merge_keys(b, o, t, _resolve_ours))
    merged = merge(base or {}, ours, theirs)

    Path(target).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[MERGE] {target}: ours={len(ours)} remote={len(theirs)} "
          f"-> merged={len(merged)} keys")


if __name__ == "__main__":
    main()
