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
# New chapters always jump to the top of the update listing, so we check the
# first page(s) frequently (fast cycle) for near-real-time detection, and do a
# full-depth scan only once at the start of each run for completeness/discovery.
# This keeps the site load about the same as before while ~tripling how often we
# notice a new chapter. All tunable via env.
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL_SECONDS", "30"))  # fast cadence
LIST_PAGES_FAST = int(os.environ.get("LIST_PAGES_FAST", "1"))  # pages per fast cycle
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


def _scrape_and_notify(scraper, state, bookmarks, notifier, pages=None):
    """One go-manga check cycle: send cards + favourites summary.

    `pages` limits how deep the listing is scanned (small = fast top-of-list
    check; None = full-depth scan)."""
    updates = scraper.check_updates(state, pages=pages)
    if not updates:
        print("[INFO] no new chapters this cycle")
        return
    print(f"[INFO] {len(updates)} manga with updates")
    sent = []
    for upd in updates:
        upd["is_bookmarked"] = bookmarks.is_bookmarked(upd["manga"].url)
        # Advance the baseline only after a successful send so an interrupted
        # run (e.g. cancelled mid-loop) re-sends the rest next time instead of
        # silently marking them done.
        if notifier.send_update(upd):
            if upd.get("_commit"):
                state.update_manga(*upd["_commit"])
            sent.append(upd)
        time.sleep(0.5)
    favs = [u for u in sent if u.get("is_bookmarked")]
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

    # One-off targeted recheck: fetch specific manga detail pages directly
    # (comma-separated CHECK_URLS), report their real latest chapter vs the
    # stored baseline, and notify + advance the baseline if the site is ahead.
    # Reaches manga that fell out of the recently-updated pages entirely.
    check_urls = os.environ.get("CHECK_URLS", "").strip()
    if check_urls:
        today = gm.site_today()
        for url in [u.strip() for u in check_urls.split(",") if u.strip()]:
            manga = scraper.scrape_manga_detail(url)
            if not manga or not manga.chapters:
                print(f"[CHECK] {url}: could not fetch / no chapters")
                continue
            # The detail page <h1> is the site's SEO header, not the manga name;
            # keep the real title already stored in state when we have one.
            kept_title = state.get_all_tracked().get(url, {}).get("title")
            if kept_title:
                manga.title = kept_title
            visible = [c for c in manga.chapters
                       if c.number.isdigit() and not (c.date and c.date > today)]
            newest = max(visible, key=lambda c: int(c.number)) if visible else manga.chapters[0]
            base = state.get_last_chapter(url)
            site_num = int(newest.number) if newest.number.isdigit() else None
            base_num = int(base) if (base and str(base).isdigit()) else None
            print(f"[CHECK] {manga.title}: site latest=ตอนที่ {newest.number} "
                  f"({newest.date or 'no date'}) | baseline={base}")
            if site_num is None:
                print("[CHECK] -> latest chapter has no number, skipping")
            elif base_num is not None and site_num == base_num:
                print("[CHECK] -> up to date, nothing to notify")
            else:
                # site ahead (site_num > base_num) OR baseline corrupted/renumbered
                # (site_num < base_num) OR first time (base_num None) -> notify.
                if base_num is not None and site_num > base_num:
                    new_ch = [c for c in visible if c.number.isdigit() and int(c.number) > base_num]
                    reason = "ahead of baseline"
                else:
                    new_ch = [newest]
                    reason = ("baseline corrupted/renumbered, resynced"
                              if base_num is not None else "first check")
                new_ch.sort(key=lambda c: int(c.number))
                prev = str(base_num) if (base_num is not None and site_num > base_num) else str(site_num - 1)
                upd = {"manga": manga, "new_chapters": new_ch, "previous_chapter": prev}
                upd["is_bookmarked"] = bookmarks.is_bookmarked(url)
                notifier.send_update(upd)
                newest_date = max((c.date for c in visible if c.date), default="")
                state.update_manga(url, newest.number, newest_date, manga.title)
                print(f"[CHECK] -> {reason}: notified {len(new_ch)} ตอน, baseline set to {newest.number}")
            time.sleep(gm.REQUEST_DELAY)
        print("[CHECK] done")
        return

    # One-off backfill: resend every manga that dropped a chapter today, then
    # exit. Triggered by the workflow's backfill_today input. Does not touch
    # state, so it never affects the normal detection loop.
    if os.environ.get("BACKFILL_TODAY", "").strip().lower() in ("1", "true", "yes"):
        target = os.environ.get("BACKFILL_DATE", "").strip()
        dryrun = os.environ.get("BACKFILL_DRYRUN", "").strip().lower() in ("1", "true", "yes")
        day = target or gm.site_today()
        ups = scraper.collect_updates_for_date(target)
        print(f"[BACKFILL] {len(ups)} manga updated on {day}"
              + (" (DRY-RUN, not sending)" if dryrun else ""))
        for u in ups:
            m = u["manga"]
            chs = ", ".join(c.number for c in u["new_chapters"])
            print(f"[BACKFILL-LIST] {m.title} — ตอนที่ {chs}")
            if dryrun:
                continue
            u["is_bookmarked"] = bookmarks.is_bookmarked(m.url)
            notifier.send_update(u)
            time.sleep(0.5)
        if not dryrun:
            favs = [u for u in ups if u.get("is_bookmarked")]
            if favs:
                notifier.send_favorites_summary(favs)
        print("[BACKFILL] done")
        return

    started = time.time()
    print(f"[INFO] Real-time bot started — long-poll {LONGPOLL_TIMEOUT}s, "
          f"fast scrape every {SCRAPE_INTERVAL}s (top {LIST_PAGES_FAST}p), "
          f"deep scan at run start, max_runtime {MAX_RUNTIME or 'forever'}")

    last_scrape = 0.0
    deep_done = False
    while True:
        # 1) go-manga check. The first cycle of the run does a full-depth scan
        #    (completeness/discovery); the rest are fast top-of-list checks for
        #    near-real-time detection of freshly posted chapters.
        if time.time() - last_scrape >= SCRAPE_INTERVAL:
            pages = None if not deep_done else LIST_PAGES_FAST
            try:
                _scrape_and_notify(scraper, state, bookmarks, notifier, pages=pages)
            except Exception as e:
                print(f"[ERROR] scrape cycle: {e}", file=sys.stderr)
            deep_done = True
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