#!/usr/bin/env python3
"""
Go-Manga real-time bot (always-on / looped runner).

Runs a single loop that does two things:

  1. Long-polls Telegram (getUpdates held open ~30s) so /commands and inline
     bookmark buttons are handled almost instantly.
  2. Every SCRAPE_INTERVAL seconds it checks go-manga for new chapters and sends
     the update cards + favourites summary.

MAX_RUNTIME_SECONDS controls how long it runs:
  - 0 (default)  -> run forever (a real 24/7 host: VM / container).
  - N seconds    -> exit after N seconds. Used on GitHub Actions so each
                    scheduled run loops for a few minutes (answering commands in
                    near real-time) then hands off to the next run.

Reuses all the logic in go_manga_monitor.py.
"""
import os
import sys
import time

import requests

import go_manga_monitor as gm

# --- tuning ---
# Seconds between go-manga checks. Kept below the per-run budget so each Actions
# run scrapes several times (at ~0/90/180s) instead of only once, for fresher
# detection. Tunable via env; on an always-on host you can lower it further.
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL_SECONDS", "90"))
LONGPOLL_TIMEOUT = 30     # seconds Telegram holds the getUpdates connection open
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME_SECONDS", "0"))  # 0 = forever


def _get_updates_longpoll(notifier, offset):
    """Blocking long-poll: returns the moment a message/button arrives."""
    params = {"timeout": LONGPOLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"{notifier.api_url}/getUpdates",
                         params=params, timeout=LONGPOLL_TIMEOUT + 15)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print(f"[ERROR] long-poll getUpdates: {e}", file=sys.stderr)
        time.sleep(3)
        return []


def _handle_batch(updates, notifier, state, bookmarks, bot_state):
    """Process a batch of Telegram updates (commands + button taps)."""
    for u in updates:
        bot_state["telegram_offset"] = u["update_id"]

        if "callback_query" in u:
            gm._handle_callback(u["callback_query"], notifier, state, bookmarks)
            continue

        msg = u.get("message") or u.get("edited_message") or u.get("channel_post")
        if not msg:
            continue
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not text.startswith("/") or not chat_id:
            continue
        parts = text.split(maxsplit=1)
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        reply = gm._handle_command(cmd, arg, state, bookmarks)
        notifier.send_message(reply, chat_id=chat_id)
        print(f"[CMD] /{cmd} {arg}")


def _scrape_and_notify(scraper, state, bookmarks, notifier):
    """One go-manga check cycle: send cards + favourites summary."""
    updates = scraper.check_updates(state)
    if not updates:
        print("[INFO] no new chapters this cycle")
        return
    print(f"[INFO] {len(updates)} manga with updates")
    for upd in updates:
        upd["is_bookmarked"] = bookmarks.is_bookmarked(upd["manga"].url)
        notifier.send_update(upd)
        time.sleep(0.5)
    favs = [u for u in updates if u.get("is_bookmarked")]
    if favs:
        notifier.send_favorites_summary(favs)


def main():
    if not (gm.TELEGRAM_BOT_TOKEN and gm.TELEGRAM_CHAT_ID):
        print("[FATAL] set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars", file=sys.stderr)
        sys.exit(1)

    state = gm.StateManager(gm.STATE_FILE)
    bookmarks = gm.BookmarkManager(gm.BOOKMARKS_FILE)
    bot_state = gm.load_bot_state(gm.BOT_STATE_FILE)
    scraper = gm.GoMangaScraper()
    notifier = gm.TelegramNotifier(gm.TELEGRAM_BOT_TOKEN, gm.TELEGRAM_CHAT_ID)

    notifier.register_commands()
    notifier.describe_chat()  # log where notifications will be delivered

    # One-off backfill: resend every manga that dropped a chapter today, then
    # exit. Triggered by the workflow's backfill_today input. Does not touch
    # state, so it never affects the normal detection loop.
    if os.environ.get("BACKFILL_TODAY", "").strip().lower() in ("1", "true", "yes"):
        target = os.environ.get("BACKFILL_DATE", "").strip()
        ups = scraper.collect_updates_for_date(target)
        print(f"[BACKFILL] {len(ups)} manga updated on {target or gm.site_today()}")
        for u in ups:
            u["is_bookmarked"] = bookmarks.is_bookmarked(u["manga"].url)
            notifier.send_update(u)
            time.sleep(0.5)
        favs = [u for u in ups if u.get("is_bookmarked")]
        if favs:
            notifier.send_favorites_summary(favs)
        print("[BACKFILL] done")
        return

    started = time.time()
    print(f"[INFO] Real-time bot started — long-poll {LONGPOLL_TIMEOUT}s, "
          f"scrape every {SCRAPE_INTERVAL}s, max_runtime {MAX_RUNTIME or 'forever'}")

    last_scrape = 0.0
    while True:
        # 1) periodic go-manga check (runs immediately on the first loop)
        if time.time() - last_scrape >= SCRAPE_INTERVAL:
            try:
                _scrape_and_notify(scraper, state, bookmarks, notifier)
            except Exception as e:
                print(f"[ERROR] scrape cycle: {e}", file=sys.stderr)
            last_scrape = time.time()

        # 2) stop before the next long-poll if we've hit the per-run time budget
        if MAX_RUNTIME and (time.time() - started) >= MAX_RUNTIME:
            print("[INFO] max runtime reached — handing off to next run")
            break

        # 3) near-instant command / button handling (blocks up to LONGPOLL_TIMEOUT)
        offset = bot_state.get("telegram_offset")
        start = (offset + 1) if isinstance(offset, int) else None
        updates = _get_updates_longpoll(notifier, start)
        if updates:
            _handle_batch(updates, notifier, state, bookmarks, bot_state)
            gm.save_bot_state(gm.BOT_STATE_FILE, bot_state)


if __name__ == "__main__":
    main()