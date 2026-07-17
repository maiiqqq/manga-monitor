#!/usr/bin/env python3
"""
Go-Manga Update Monitor
Monitors https://www.go-manga.com/manga/?order=update for new chapter updates
and sends notifications via Telegram.
"""

import html as html_lib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# ============== CONFIGURATION ==============
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_URL = "https://www.go-manga.com"
LIST_URL = f"{BASE_URL}/manga/?order=update"
STATE_FILE = Path(__file__).parent / "monitor_state.json"
BOOKMARKS_FILE = Path(__file__).parent / "bookmarks.json"
BOT_STATE_FILE = Path(__file__).parent / "bot_state.json"
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 1.0  # Be polite to the server

# go-manga is a Thai site and labels chapter dates in Indochina Time (UTC+7).
# The runner (GitHub Actions) is on UTC, so "today" must be computed in the
# site's timezone — otherwise, for the ~7h each evening/night in UTC, a chapter
# released "today" in Thailand carries tomorrow's date relative to UTC and gets
# wrongly filtered out as anomalous/future-dated.
SITE_TZ = timezone(timedelta(hours=7))


def site_today() -> str:
    """Current calendar date in the site's timezone (ICT / UTC+7), ISO format."""
    return datetime.now(SITE_TZ).date().isoformat()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
}


# ============== DATA MODELS ==============
@dataclass
class Chapter:
    number: str
    title: str
    url: str
    date: str

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


@dataclass
class Manga:
    title: str
    url: str
    latest_chapter: str
    latest_chapter_url: str
    latest_chapter_date: str
    type_: str  # MANHWA, MANGA, MANHUA
    status: str  # Ongoing, Completed
    rating: str
    cover_image: str
    chapters: list[Chapter]

    def to_dict(self):
        d = asdict(self)
        d["chapters"] = [c.to_dict() for c in self.chapters]
        return d

    @classmethod
    def from_dict(cls, d):
        chapters = [Chapter.from_dict(c) for c in d.get("chapters", [])]
        return cls(
            title=d["title"],
            url=d["url"],
            latest_chapter=d["latest_chapter"],
            latest_chapter_url=d["latest_chapter_url"],
            latest_chapter_date=d["latest_chapter_date"],
            type_=d["type_"],
            status=d["status"],
            rating=d["rating"],
            cover_image=d.get("cover_image", ""),
            chapters=chapters,
        )


# ============== STATE MANAGEMENT ==============
class StateManager:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.data = self._load()

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def save(self):
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get_last_chapter(self, manga_url: str) -> Optional[str]:
        return self.data.get(manga_url, {}).get("last_chapter")

    def get_last_chapter_date(self, manga_url: str) -> Optional[str]:
        return self.data.get(manga_url, {}).get("last_chapter_date")

    def update_manga(self, manga_url: str, chapter: str, chapter_date: str, title: str):
        self.data[manga_url] = {
            "title": title,
            "last_chapter": chapter,
            "last_chapter_date": chapter_date,
            "last_check": datetime.now().isoformat(),
        }
        self.save()

    def get_all_tracked(self) -> dict:
        return self.data


# ============== BOOKMARKS ==============
class BookmarkManager:
    """Stores the user's favourite manga in bookmarks.json."""

    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def add(self, url: str, title: str) -> bool:
        existed = url in self.data
        self.data[url] = {"title": title}
        self.save()
        return not existed  # True if newly added

    def remove(self, url: str) -> bool:
        if url in self.data:
            del self.data[url]
            self.save()
            return True
        return False

    def is_bookmarked(self, url: str) -> bool:
        return url in self.data

    def all(self) -> dict:
        return self.data


# ============== BOT STATE (Telegram offset) ==============
def load_bot_state(path: Path) -> dict:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_bot_state(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============== SCRAPER ==============
class GoMangaScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _get(self, url: str) -> Optional[BeautifulSoup]:
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            # Force UTF-8 encoding (server incorrectly returns ISO-8859-1)
            resp.encoding = "utf-8"
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f"[ERROR] Failed to fetch {url}: {e}", file=sys.stderr)
            return None

    def _parse_chapter_number(self, text: str) -> str:
        """Extract chapter number from text like 'ตอนที่ 44' or 'Chapter 44'"""
        # Thai chapter format: ตอนที่ 44, ตอนที่ 0, ตอนที่ 44กรกฎาคม (no space)
        match = re.search(r"ตอนที่\s*(\d+)", text)
        if match:
            return match.group(1)
        # English chapter format: Chapter 44, EP 44, Ch. 44
        match = re.search(r"(?:Chapter|EP|Ch\.?)\s*(\d+)", text, re.IGNORECASE)
        if match:
            return match.group(1)
        return ""

    def _parse_date(self, text: str) -> str:
        """Parse Thai date format to ISO format"""
        # Thai months
        thai_months = {
            "มกราคม": "01",
            "กุมภาพันธ์": "02",
            "มีนาคม": "03",
            "เมษายน": "04",
            "พฤษภาคม": "05",
            "มิถุนายน": "06",
            "กรกฎาคม": "07",
            "สิงหาคม": "08",
            "กันยายน": "09",
            "ตุลาคม": "10",
            "พฤศจิกายน": "11",
            "ธันวาคม": "12",
            # Short forms
            "ม.ค.": "01",
            "ก.พ.": "02",
            "มี.ค.": "03",
            "เม.ย.": "04",
            "พ.ค.": "05",
            "มิ.ย.": "06",
            "ก.ค.": "07",
            "ส.ค.": "08",
            "ก.ย.": "09",
            "ต.ค.": "10",
            "พ.ย.": "11",
            "ธ.ค.": "12",
        }

        # Try Thai format: "ตอนที่ 44 กรกฎาคม 15, 2026"
        for th_month, num_month in thai_months.items():
            if th_month in text:
                # Extract day and year
                match = re.search(rf"{re.escape(th_month)}\s*(\d{{1,2}}),?\s*(\d{{4}})", text)
                if match:
                    day = int(match.group(1))
                    year = int(match.group(2))
                    # Convert Buddhist year to Gregorian if needed
                    if year > 2500:
                        year -= 543
                    return f"{year:04d}-{num_month}-{day:02d}"

        # Try English format: "July 15, 2026"
        en_months = {
            "January": "01",
            "February": "02",
            "March": "03",
            "April": "04",
            "May": "05",
            "June": "06",
            "July": "07",
            "August": "08",
            "September": "09",
            "October": "10",
            "November": "11",
            "December": "12",
        }
        for en_month, num_month in en_months.items():
            if en_month in text:
                match = re.search(rf"{en_month}\s*(\d{{1,2}}),?\s*(\d{{4}})", text)
                if match:
                    day = int(match.group(1))
                    year = int(match.group(2))
                    return f"{year:04d}-{num_month}-{day:02d}"

        # Try ISO format already
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _extract_img_src(img) -> str:
        """Get the real image URL, accounting for lazy-loading attributes."""
        if not img:
            return ""
        for attr in ("data-src", "data-lazy-src", "data-original", "src"):
            val = img.get(attr, "")
            # Skip 1x1 placeholder / data-uri lazy placeholders
            if val and not val.startswith("data:") and "close.png" not in val:
                return urljoin(BASE_URL, val)
        return ""

    def scrape_list_page(self) -> list[dict]:
        """Scrape the manga listing page sorted by update.

        Uses the theme's structured markup (div.bsx) so we can reliably grab
        the title, type, latest chapter, rating and cover image for each item.
        """
        print(f"[INFO] Fetching list page: {LIST_URL}")
        soup = self._get(LIST_URL)
        if not soup:
            return []

        manga_list = []
        for item in soup.select("div.bsx"):
            link = item.find("a", href=True)
            if not link:
                continue
            full_url = urljoin(BASE_URL, link.get("href", ""))
            if not full_url.startswith(BASE_URL):
                continue

            # Title: prefer the .tt block, fall back to the link title attribute
            title_el = item.select_one("div.tt")
            title = title_el.get_text(strip=True) if title_el else link.get("title", "").strip()
            title = re.sub(r"\s+", " ", title).strip()
            if not title or len(title) < 2:
                continue

            # Type (Manhwa / Manga / Manhua)
            type_el = item.select_one("span.typename")
            type_ = type_el.get_text(strip=True).upper() if type_el else "UNKNOWN"

            # Latest chapter from the .epxs badge (e.g. "ตอนที่ 15")
            ep_el = item.select_one("div.epxs")
            ep_text = ep_el.get_text(strip=True) if ep_el else ""
            latest_chapter = self._parse_chapter_number(ep_text)

            # Rating from the .numscore element
            score_el = item.select_one("div.numscore")
            rating = score_el.get_text(strip=True) if score_el else "N/A"

            # Cover image
            cover_image = self._extract_img_src(item.find("img"))

            manga_list.append({
                "title": title,
                "url": full_url,
                "latest_chapter": latest_chapter,
                "latest_chapter_url": full_url,
                "latest_chapter_date": "",
                "type_": type_,
                "rating": rating,
                "cover_image": cover_image,
            })

        # Deduplicate by URL
        seen = set()
        unique = []
        for m in manga_list:
            if m["url"] not in seen:
                seen.add(m["url"])
                unique.append(m)

        print(f"[INFO] Found {len(unique)} manga on list page")
        return unique

    def scrape_manga_detail(self, manga_url: str) -> Optional[Manga]:
            """Scrape a manga detail page for full chapter list"""
            print(f"[INFO] Fetching detail: {manga_url}")
            soup = self._get(manga_url)
            if not soup:
                return None

            # Extract title
            title_elem = soup.find("h1") or soup.find("h2", class_=lambda x: x and "title" in x.lower())
            title = title_elem.get_text(strip=True) if title_elem else "Unknown"

            # Extract type
            type_ = "UNKNOWN"
            type_elem = soup.find(text=re.compile(r"(MANHWA|MANGA|MANHUA)", re.IGNORECASE))
            if type_elem:
                match = re.search(r"(MANHWA|MANGA|MANHUA)", str(type_elem), re.IGNORECASE)
                if match:
                    type_ = match.group(1).upper()

            # Extract status
            status = "Unknown"
            status_elem = soup.find(text=re.compile(r"(Ongoing|Completed|Hiatus|จบ|อนโยน)", re.IGNORECASE))
            if status_elem:
                status = str(status_elem).strip()

            # Extract rating
            rating = "N/A"
            score_el = soup.select_one("div.num, div.numscore, span.num")
            if score_el:
                m = re.search(r"(\d+(?:\.\d+)?)", score_el.get_text(strip=True))
                if m:
                    rating = m.group(1)
            if rating == "N/A":
                rating_elem = soup.find(text=re.compile(r"\d\.\d"))
                if rating_elem:
                    match = re.search(r"(\d\.\d)", str(rating_elem))
                    if match:
                        rating = match.group(1)

            # Cover image (thumbnail on the detail page)
            cover_image = self._extract_img_src(
                soup.select_one("div.thumb img")
                or soup.select_one("img.wp-post-image")
                or soup.select_one("div.thumbook img")
            )

            # Extract chapters from the theme's chapter list (#chapterlist li)
            chapters = []
            chapter_items = soup.select("#chapterlist li") or soup.select("div.eplister ul li")

            if chapter_items:
                for li in chapter_items:
                    a = li.find("a", href=True)
                    if not a:
                        continue
                    text = li.get_text(" ", strip=True)
                    chapter_num = self._parse_chapter_number(text) or self._parse_chapter_number(a.get("href", ""))
                    if not chapter_num:
                        continue
                    chapters.append(Chapter(
                        number=chapter_num,
                        title=f"ตอนที่ {chapter_num}",
                        url=urljoin(BASE_URL, a.get("href", "")),
                        date=self._parse_date(text),
                    ))
            else:
                # Fallback: scan all links for chapter patterns
                for link in soup.find_all("a", href=True):
                    href = link.get("href", "")
                    text = link.get_text(strip=True)
                    if not (href and ("ตอนที่" in text or "chapter" in href.lower())):
                        continue
                    chapter_num = self._parse_chapter_number(text) or self._parse_chapter_number(href)
                    if not chapter_num:
                        continue
                    date = self._parse_date(link.parent.get_text(strip=True)) if link.parent else ""
                    chapters.append(Chapter(number=chapter_num, title=text,
                                            url=urljoin(BASE_URL, href), date=date))

            # Deduplicate chapters
            seen = set()
            unique_chapters = []
            for c in chapters:
                if c.number not in seen:
                    seen.add(c.number)
                    unique_chapters.append(c)

            # Sort chapters by number (descending - newest first)
            unique_chapters.sort(key=lambda c: int(c.number) if c.number.isdigit() else 0, reverse=True)

            # Get latest chapter info
            latest_chapter = unique_chapters[0].number if unique_chapters else ""
            latest_chapter_url = unique_chapters[0].url if unique_chapters else ""
            latest_chapter_date = unique_chapters[0].date if unique_chapters else ""

            print(f"[DEBUG] Found {len(unique_chapters)} chapters for {title[:50]}")

            return Manga(
                title=title,
                url=manga_url,
                latest_chapter=latest_chapter,
                latest_chapter_url=latest_chapter_url,
                latest_chapter_date=latest_chapter_date,
                type_=type_,
                status=status,
                rating=rating,
                cover_image=cover_image,
                chapters=unique_chapters,
            )

    @staticmethod
    def _select_new_chapters(chapters, last_num, last_date: str, today: str) -> list:
        """Decide which chapters count as "new".

        Primary rule: a chapter is new if its update date is later than the
        last update date we recorded, up to today. Chapter number is used as a
        safeguard so we don't miss multiple chapters released on the same day
        (go-manga often drops several at once) or when no date baseline exists.
        """
        result = []
        for c in chapters:
            if not c.number.isdigit():
                continue
            num = int(c.number)
            # Ignore anomalous future-dated chapters
            if c.date and c.date > today:
                continue

            is_new = False
            if last_date:
                if c.date and c.date > last_date:
                    is_new = True  # updated after our last recorded date
                elif c.date == last_date and last_num is not None and num > last_num:
                    is_new = True  # same-day release we haven't seen yet
                elif not c.date and last_num is not None and num > last_num:
                    is_new = True  # no date on chapter, fall back to number
            else:
                # No date baseline yet -> pure number comparison
                if last_num is not None and num > last_num:
                    is_new = True

            if is_new:
                result.append(c)

        result.sort(key=lambda c: int(c.number))
        return result

    def check_updates(self, state: StateManager) -> list[dict]:
        """Check for new chapters across all manga (date-based detection)."""
        updates = []
        today = site_today()

        # Get list of manga from the update page
        manga_list = self.scrape_list_page()

        for manga_info in manga_list:
            manga_url = manga_info["url"]
            title = manga_info["title"]
            latest_chapter = manga_info["latest_chapter"]

            if not latest_chapter:
                continue

            last_chapter = state.get_last_chapter(manga_url)

            if last_chapter is None:
                # First time seeing this manga - record baseline, no notification
                state.update_manga(manga_url, latest_chapter, "", title)
                print(f"[INFO] First time tracking: {title} - Chapter {latest_chapter}")
                continue

            # Parse chapter numbers for the cheap "should we look deeper?" gate
            try:
                current_num = int(latest_chapter)
            except ValueError:
                current_num = None
            try:
                last_num = int(last_chapter)
            except (ValueError, TypeError):
                last_num = None

            # Only fetch the detail page when the list suggests something changed
            if current_num is not None and last_num is not None and current_num <= last_num:
                continue

            manga = self.scrape_manga_detail(manga_url)
            if not manga:
                continue

            # Fill any gaps using the richer list-page data
            if not manga.cover_image:
                manga.cover_image = manga_info.get("cover_image", "")
            if manga.type_ == "UNKNOWN":
                manga.type_ = manga_info.get("type_", "UNKNOWN")
            if manga.rating in ("N/A", ""):
                manga.rating = manga_info.get("rating", "N/A")
            # The list-page title is the reliable one; the detail page <h1> is
            # the site's SEO header, not the manga name.
            if manga_info.get("title"):
                manga.title = manga_info["title"]

            last_date = state.get_last_chapter_date(manga_url) or ""
            new_chapters = self._select_new_chapters(manga.chapters, last_num, last_date, today)

            # Advance the stored baseline only over chapters we can actually
            # "see" today (skip anomalous future-dated ones). If we advanced past
            # a future-dated chapter here it would be swallowed: the next run's
            # cheap number gate would match the stored baseline and never look
            # again, so a genuine release that momentarily parsed as future would
            # be lost forever. Excluding them keeps that chapter in play until it
            # is no longer future, at which point it is detected and notified.
            visible = [c for c in manga.chapters
                       if c.number.isdigit() and not (c.date and c.date > today)]
            if visible:
                newest_num = max(int(c.number) for c in visible)
                if last_num is not None:
                    newest_num = max(newest_num, last_num)
                new_latest = str(newest_num)
                newest_date = max((c.date for c in visible if c.date), default=last_date)
            else:
                # Nothing visible yet (all future-dated / undated) — hold baseline
                new_latest = last_chapter
                newest_date = last_date

            if new_chapters:
                updates.append({
                    "manga": manga,
                    "new_chapters": new_chapters,
                    "previous_chapter": last_chapter,
                })
                print(f"[UPDATE] {title}: {len(new_chapters)} new chapter(s) "
                      f"({last_chapter}/{last_date or '-'} -> {new_latest}/{newest_date or '-'})")
            else:
                print(f"[INFO] {title}: number changed but no new dated chapters")

            state.update_manga(manga_url, new_latest, newest_date, title)
            time.sleep(REQUEST_DELAY)  # Be polite

        return updates

    def collect_today_updates(self) -> list[dict]:
        """Backfill helper: every manga on the update list whose detail page has
        chapters dated today (site tz), regardless of the stored baseline.

        Used for an on-demand "resend everything that dropped today" pass. It
        does not read or advance state, so it never interferes with the normal
        detection loop and can be run at any time.
        """
        today = site_today()
        results = []
        for manga_info in self.scrape_list_page():
            manga = self.scrape_manga_detail(manga_info["url"])
            if not manga:
                continue
            # Prefer the reliable list-page fields for display
            if not manga.cover_image:
                manga.cover_image = manga_info.get("cover_image", "")
            if manga.type_ == "UNKNOWN":
                manga.type_ = manga_info.get("type_", "UNKNOWN")
            if manga.rating in ("N/A", ""):
                manga.rating = manga_info.get("rating", "N/A")
            if manga_info.get("title"):
                manga.title = manga_info["title"]

            todays = [c for c in manga.chapters if c.number.isdigit() and c.date == today]
            if todays:
                todays.sort(key=lambda c: int(c.number))
                results.append({
                    "manga": manga,
                    "new_chapters": todays,
                    "previous_chapter": str(int(todays[0].number) - 1),
                })
                print(f"[BACKFILL] {manga.title}: {len(todays)} chapter(s) dated {today}")
            time.sleep(REQUEST_DELAY)  # Be polite
        return results


# ============== TELEGRAM NOTIFIER ==============
class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"

    @staticmethod
    def bookmark_keyboard(manga_url: str, is_bookmarked: bool) -> dict:
        """Inline keyboard with a single add/remove-bookmark button."""
        slug = manga_url.rstrip("/").split("/")[-1]
        if is_bookmarked:
            btn = {"text": "❌ เอาออกจากเรื่องโปรด", "callback_data": f"u:{slug}"[:64]}
        else:
            btn = {"text": "📌 เพิ่มเป็นเรื่องโปรด", "callback_data": f"b:{slug}"[:64]}
        return {"inline_keyboard": [[btn]]}

    def send_message(self, text: str, parse_mode: str = "HTML", chat_id: str = None,
                     reply_markup: dict = None) -> bool:
        target = chat_id or self.chat_id
        if not self.bot_token or not target:
            print("[WARN] Telegram not configured, skipping notification")
            return False

        url = f"{self.api_url}/sendMessage"
        payload = {
            "chat_id": target,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            # Telegram returns HTTP 200 with {"ok": false, ...} for logical
            # failures (bad chat_id, bot not in chat, blocked). raise_for_status
            # does not catch those, so check the payload explicitly — otherwise
            # a rejected message looks like a success and no card is delivered.
            data = resp.json()
            if not data.get("ok"):
                print(f"[ERROR] Telegram sendMessage rejected: "
                      f"{data.get('error_code')} {data.get('description')}", file=sys.stderr)
                return False
            return True
        except Exception as e:
            print(f"[ERROR] Failed to send Telegram message: {e}", file=sys.stderr)
            return False

    def describe_chat(self) -> None:
        """Log which chat notifications are delivered to (startup diagnostic).

        The numeric chat_id is a secret and GitHub masks it, but the chat's
        type and name are not — so this reveals *where* the cards land (e.g. a
        group you no longer watch) without leaking the id, and turns an invalid
        chat_id into a clear getChat error instead of silent non-delivery.
        """
        if not (self.bot_token and self.chat_id):
            return
        try:
            r = requests.get(f"{self.api_url}/getChat",
                             params={"chat_id": self.chat_id}, timeout=10)
            data = r.json()
            if data.get("ok"):
                c = data.get("result", {})
                name = c.get("title") or c.get("username") or c.get("first_name") or "?"
                print(f"[INFO] Notification target -> type={c.get('type')} name={name!r}")
            else:
                print(f"[ERROR] getChat rejected: {data.get('error_code')} "
                      f"{data.get('description')} (check TELEGRAM_CHAT_ID)", file=sys.stderr)
        except Exception as e:
            print(f"[ERROR] getChat: {e}", file=sys.stderr)

    def register_commands(self) -> bool:
        """Register the bot's command menu so users see it when typing '/'."""
        if not self.bot_token:
            return False
        commands = [
            {"command": "bookmark", "description": "เพิ่มเรื่องโปรด (เช่น /bookmark painter)"},
            {"command": "unbookmark", "description": "เอาเรื่องออกจากโปรด"},
            {"command": "list", "description": "ดูรายการเรื่องโปรดทั้งหมด"},
            {"command": "help", "description": "แสดงคำสั่งทั้งหมด"},
        ]
        try:
            resp = requests.post(f"{self.api_url}/setMyCommands",
                                 json={"commands": commands}, timeout=15)
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[ERROR] Failed to register commands: {e}", file=sys.stderr)
            return False

    def get_updates(self, offset: Optional[int] = None) -> list:
        """Fetch new messages sent to the bot (for command handling)."""
        if not self.bot_token:
            return []
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = requests.get(f"{self.api_url}/getUpdates", params=params, timeout=20)
            resp.raise_for_status()
            return resp.json().get("result", [])
        except Exception as e:
            print(f"[ERROR] Failed to getUpdates: {e}", file=sys.stderr)
            return []

    def send_photo(self, photo_url: str, caption: str, parse_mode: str = "HTML",
                   reply_markup: dict = None) -> bool:
        """Send an image card (cover) with an HTML caption."""
        if not self.bot_token or not self.chat_id:
            print("[WARN] Telegram not configured, skipping photo")
            return False

        url = f"{self.api_url}/sendPhoto"
        payload = {
            "chat_id": self.chat_id,
            "photo": photo_url,
            "caption": caption[:1024],  # Telegram caption hard limit
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            # Telegram can answer HTTP 200 with {"ok": false, ...} — e.g. when it
            # fails to fetch/hotlink the cover image, or the chat_id is wrong.
            # Treat that as a failure so send_update falls back to a text card.
            data = resp.json()
            if not data.get("ok"):
                print(f"[ERROR] Telegram sendPhoto rejected: "
                      f"{data.get('error_code')} {data.get('description')}", file=sys.stderr)
                return False
            return True
        except Exception as e:
            print(f"[ERROR] Failed to send Telegram photo: {e}", file=sys.stderr)
            return False

    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        """Acknowledge a button tap (shows a toast to the user)."""
        try:
            requests.post(f"{self.api_url}/answerCallbackQuery",
                          json={"callback_query_id": callback_id, "text": text},
                          timeout=10)
            return True
        except Exception as e:
            print(f"[ERROR] answerCallbackQuery: {e}", file=sys.stderr)
            return False

    def edit_reply_markup(self, chat_id: str, message_id: int, reply_markup: dict) -> bool:
        """Update the inline keyboard on an already-sent message."""
        try:
            requests.post(f"{self.api_url}/editMessageReplyMarkup",
                          json={"chat_id": chat_id, "message_id": message_id,
                                "reply_markup": reply_markup}, timeout=10)
            return True
        except Exception as e:
            print(f"[ERROR] editMessageReplyMarkup: {e}", file=sys.stderr)
            return False

    def format_update_message(self, update: dict) -> str:
        manga = update["manga"]
        new_chapters = update["new_chapters"]
        prev_chapter = update.get("previous_chapter", "?")

        # Type emoji mapping
        type_emoji = {"MANHWA": "🇰🇷", "MANGA": "🇯🇵", "MANHUA": "🇨🇳", "UNKNOWN": "📖"}
        type_emoji_str = type_emoji.get(manga.type_.upper(), "📖")
        
        # Status emoji mapping
        status_lower = manga.status.lower()
        status_emoji = "🔄" if "ongo" in status_lower or "ongo" in status_lower else ("✅" if "complet" in status_lower or "จบ" in status_lower else "⏸️")

        # Type badge
        type_badge = f"{type_emoji_str} {manga.type_}" if manga.type_ != "UNKNOWN" else "📖 Manga"

        lines = [
            f"🔔 <b>มีการ์ตูนอัปเดตใหม่!</b>",
            f"",
            f"━━━━━━━━━━━━━━━━━━",
            f"📖 <b>{manga.title}</b>",
            f"",
            f"🏷️ <b>ประเภท:</b> {type_badge}  |  {status_emoji} <b>สถานะ:</b> {manga.status}  |  ⭐ <b>Rating:</b> {manga.rating}",
            f"━━━━━━━━━━━━━━━━━━",
            f"",
            f"📚 <b>ตอนใหม่ {len(new_chapters)} ตอน</b>  <i>(จากตอนที่ {prev_chapter})</i>",
            f"",
        ]

        for i, ch in enumerate(new_chapters, 1):
            date_str = f" <i>({ch.date})</i>" if ch.date else ""
            # Use bold for chapter number, link on the number
            lines.append(f"   <b>{i}.</b> <a href=\"{ch.url}\"><b>ตอนที่ {ch.number}</b></a>{date_str}")

        lines.extend([
            f"",
            f"━━━━━━━━━━━━━━━━━━",
            f"🔗 <a href=\"{manga.url}\"><b>📖 เปิดอ่านที่ Go-Manga</b></a>",
            f"⏰ <i>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</i>",
            f"━━━━━━━━━━━━━━━━━━",
        ])

        return "\n".join(lines)

    def format_update_caption(self, update: dict) -> str:
        """Compact card caption (used with sendPhoto). Kept under 1024 chars."""
        manga = update["manga"]
        new_chapters = update["new_chapters"]
        prev_chapter = update.get("previous_chapter", "?")

        type_emoji = {"MANHWA": "🇰🇷", "MANGA": "🇯🇵", "MANHUA": "🇨🇳"}.get(
            manga.type_.upper(), "📖"
        )
        type_label = manga.type_.title() if manga.type_ != "UNKNOWN" else "Manga"
        title = html_lib.escape(manga.title)
        pin = "📌 " if update.get("is_bookmarked") else ""

        lines = [
            f"{pin}🔔 <b>อัปเดตใหม่!</b>" + ("  <i>(เรื่องโปรด)</i>" if pin else ""),
            "",
            f"📖 <b>{title}</b>",
            f"{type_emoji} {type_label}  ·  ⭐ {manga.rating}",
            "",
            f"✨ <b>+{len(new_chapters)} ตอนใหม่</b>  (ตอนที่ {prev_chapter} → {manga.latest_chapter})",
        ]

        # List new chapters as clickable numbers. Cap the list so the caption
        # stays well under Telegram's 1024-char limit.
        max_show = 12
        shown = new_chapters[-max_show:] if len(new_chapters) > max_show else new_chapters
        links = [f'<a href="{c.url}">{c.number}</a>' for c in shown]
        prefix = "📑 ตอนที่: "
        if len(new_chapters) > max_show:
            prefix = f"📑 ตอนล่าสุด {max_show} ตอน: "
        lines.append(prefix + ", ".join(links))

        latest = new_chapters[-1] if new_chapters else None
        latest_date = f"  <i>({latest.date})</i>" if latest and latest.date else ""
        lines += [
            "",
            f'🔗 <a href="{manga.url}"><b>อ่านต่อที่ Go-Manga</b></a>{latest_date}',
        ]
        return "\n".join(lines)

    def send_update(self, update: dict) -> bool:
        manga = update["manga"]
        # Button to add/remove this manga from bookmarks straight from the card
        markup = self.bookmark_keyboard(manga.url, update.get("is_bookmarked", False))
        # Preferred: image card with the cover
        if getattr(manga, "cover_image", ""):
            caption = self.format_update_caption(update)
            if self.send_photo(manga.cover_image, caption, reply_markup=markup):
                return True
            print("[WARN] Photo card failed, falling back to text message")
        # Fallback: rich text message (no cover available)
        return self.send_message(self.format_update_message(update), reply_markup=markup)

    def send_favorites_summary(self, fav_updates: list) -> bool:
        """Send a separate summary listing bookmarked manga that updated."""
        if not fav_updates:
            return False
        lines = ["📌 <b>สรุปเรื่องโปรดที่อัปเดต</b>", ""]
        for u in fav_updates:
            m = u["manga"]
            n = len(u["new_chapters"])
            title = html_lib.escape(m.title)
            lines.append(f'📖 <a href="{m.url}">{title}</a>')
            lines.append(f"    └ +{n} ตอนใหม่ (ล่าสุด ตอนที่ {m.latest_chapter})")
        lines += ["", f"⭐ ทั้งหมด {len(fav_updates)} เรื่อง  ·  ⏰ {datetime.now().strftime('%d/%m/%Y %H:%M')}"]
        return self.send_message("\n".join(lines))

    def send_startup_message(self, tracked_count: int) -> bool:
        """Send startup notification"""
        message = (
            f"🚀 <b>Go-Manga Monitor เริ่มทำงานแล้ว</b>\n"
            f"\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>สถานะ:</b> กำลังตรวจสอบการอัปเดต...\n"
            f"📚 <b>ติดตาม:</b> {tracked_count} เรื่อง\n"
            f"⏰ <b>เริ่มเวลา:</b> <i>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</i>\n"
            f"⏱ <b>ความถี่:</b> ทุก 30 นาที\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        return self.send_message(message)

    def send_shutdown_message(self, tracked_count: int, updates_found: int) -> bool:
        """Send shutdown notification with summary"""
        if updates_found > 0:
            status_emoji = "✅"
            status_text = f"พบอัปเดตใหม่ {updates_found} เรื่อง"
        else:
            status_emoji = "💤"
            status_text = "ไม่พบอัปเดตใหม่"

        message = (
            f"🛑 <b>Go-Manga Monitor หยุดทำงาน</b>\n"
            f"\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{status_emoji} <b>ผลลัพธ์:</b> {status_text}\n"
            f"📚 <b>ติดตาม:</b> {tracked_count} เรื่อง\n"
            f"⏰ <b>จบเวลา:</b> <i>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</i>\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        return self.send_message(message)


# ============== TELEGRAM COMMANDS ==============
HELP_TEXT = (
    "🤖 <b>คำสั่ง Go-Manga Bot</b>\n"
    "\n"
    "📌 <b>/bookmark</b> &lt;ชื่อเรื่อง&gt; — เพิ่มเรื่องโปรด (ค้นจากชื่อ)\n"
    "❌ <b>/unbookmark</b> &lt;ชื่อเรื่อง&gt; — เอาออกจากโปรด\n"
    "📚 <b>/list</b> — ดูรายการเรื่องโปรดทั้งหมด\n"
    "❓ <b>/help</b> — แสดงคำสั่งทั้งหมด\n"
    "\n"
    "<i>ตัวอย่าง:</i> <code>/bookmark painter</code>"
)


def _find_manga(state: StateManager, keyword: str) -> list:
    """Return [(url, title)] of tracked manga whose title matches the keyword."""
    kw = keyword.lower().strip()
    out = []
    for url, v in state.get_all_tracked().items():
        title = v.get("title", "")
        if kw in title.lower() or kw in url.lower():
            out.append((url, title))
    return out


def _handle_command(cmd: str, arg: str, state: StateManager, bookmarks: BookmarkManager) -> str:
    if cmd in ("start", "help"):
        return HELP_TEXT

    if cmd in ("list", "bookmarks"):
        if not bookmarks.all():
            return "📭 ยังไม่มีเรื่องโปรด\nใช้ <code>/bookmark ชื่อเรื่อง</code> เพื่อเพิ่ม"
        lines = ["📌 <b>เรื่องโปรดของคุณ</b>", ""]
        for i, (url, v) in enumerate(bookmarks.all().items(), 1):
            lines.append(f'{i}. <a href="{url}">{html_lib.escape(v.get("title", url))}</a>')
        return "\n".join(lines)

    if cmd in ("bookmark", "bm", "fav"):
        if not arg:
            return "⚠️ ระบุชื่อเรื่อง เช่น <code>/bookmark painter</code>"
        matches = _find_manga(state, arg)
        if not matches:
            return f"🔍 ไม่พบเรื่องที่ตรงกับ \"{html_lib.escape(arg)}\"\n(ต้องเป็นเรื่องที่ระบบติดตามอยู่)"
        if len(matches) > 1:
            lines = [f"พบ {len(matches)} เรื่องที่ตรงกัน โปรดระบุให้ชัดเจนขึ้น:", ""]
            for _, t in matches[:10]:
                lines.append(f"• {html_lib.escape(t)}")
            return "\n".join(lines)
        url, title = matches[0]
        newly = bookmarks.add(url, title)
        prefix = "✅ เพิ่มเรื่องโปรดแล้ว" if newly else "ℹ️ เรื่องนี้อยู่ในโปรดอยู่แล้ว"
        return f"{prefix}:\n📌 {html_lib.escape(title)}"

    if cmd in ("unbookmark", "unbm", "unfav", "remove"):
        if not arg:
            return "⚠️ ระบุชื่อเรื่อง เช่น <code>/unbookmark painter</code>"
        kw = arg.lower().strip()
        matches = [(u, v.get("title", u)) for u, v in bookmarks.all().items()
                   if kw in v.get("title", "").lower() or kw in u.lower()]
        if not matches:
            return f"🔍 ไม่พบเรื่องโปรดที่ตรงกับ \"{html_lib.escape(arg)}\""
        if len(matches) > 1:
            lines = [f"พบ {len(matches)} เรื่อง โปรดระบุให้ชัดเจนขึ้น:", ""]
            for _, t in matches[:10]:
                lines.append(f"• {html_lib.escape(t)}")
            return "\n".join(lines)
        url, title = matches[0]
        bookmarks.remove(url)
        return f"❌ เอาออกจากเรื่องโปรดแล้ว:\n{html_lib.escape(title)}"

    return f"❓ ไม่รู้จักคำสั่ง <code>/{html_lib.escape(cmd)}</code>\nพิมพ์ /help เพื่อดูคำสั่งทั้งหมด"


def _resolve_slug(state: StateManager, bookmarks: BookmarkManager, slug: str):
    """Map a callback slug back to (url, title) from tracked manga or bookmarks."""
    for source in (state.get_all_tracked(), bookmarks.all()):
        for url, v in source.items():
            if url.rstrip("/").split("/")[-1] == slug:
                return url, v.get("title", url)
    return None, None


def _handle_callback(cq: dict, notifier: "TelegramNotifier", state: StateManager,
                     bookmarks: BookmarkManager):
    """Handle an inline-button tap on an update card."""
    cq_id = cq.get("id", "")
    data = cq.get("data", "")
    msg = cq.get("message", {}) or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    message_id = msg.get("message_id")

    action, _, slug = data.partition(":")
    url, title = _resolve_slug(state, bookmarks, slug)
    if not url:
        notifier.answer_callback(cq_id, "ไม่พบเรื่องนี้ในระบบ")
        return

    if action == "b":
        bookmarks.add(url, title)
        toast = "📌 เพิ่มเป็นเรื่องโปรดแล้ว"
        new_markup = notifier.bookmark_keyboard(url, True)
    elif action == "u":
        bookmarks.remove(url)
        toast = "❌ เอาออกจากเรื่องโปรดแล้ว"
        new_markup = notifier.bookmark_keyboard(url, False)
    else:
        notifier.answer_callback(cq_id, "")
        return

    notifier.answer_callback(cq_id, toast)
    if chat_id and message_id:
        notifier.edit_reply_markup(chat_id, message_id, new_markup)
    print(f"[CALLBACK] {action}:{slug} -> {toast}")


def process_telegram_commands(notifier: "TelegramNotifier", state: StateManager,
                              bookmarks: BookmarkManager, bot_state: dict) -> int:
    """Poll for new Telegram messages and act on any bot commands."""
    offset = bot_state.get("telegram_offset")
    start = (offset + 1) if isinstance(offset, int) else None
    updates = notifier.get_updates(start)
    handled = 0
    for u in updates:
        bot_state["telegram_offset"] = u["update_id"]

        # Inline button taps (add/remove bookmark straight from an update card)
        if "callback_query" in u:
            _handle_callback(u["callback_query"], notifier, state, bookmarks)
            handled += 1
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
        reply = _handle_command(cmd, arg, state, bookmarks)
        notifier.send_message(reply, chat_id=chat_id)
        handled += 1
        print(f"[CMD] /{cmd} {arg} -> replied")
    return handled


# ============== MAIN ==============
def main():
    print("=" * 60)
    print("Go-Manga Update Monitor")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Initialize components
    state = StateManager(STATE_FILE)
    bookmarks = BookmarkManager(BOOKMARKS_FILE)
    bot_state = load_bot_state(BOT_STATE_FILE)
    scraper = GoMangaScraper()
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    tracked_count = len(state.get_all_tracked())
    telegram_ready = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

    if not telegram_ready:
        print("[WARN] Telegram credentials not set. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.")
        print("      Notifications will be printed to console only.")
    else:
        # Register the command menu once (so "/" shows suggestions in Telegram)
        if not bot_state.get("commands_registered"):
            if notifier.register_commands():
                bot_state["commands_registered"] = True
                print("[INFO] Registered Telegram command menu")
        # Handle any pending bookmark commands / button taps
        try:
            n = process_telegram_commands(notifier, state, bookmarks, bot_state)
            if n:
                print(f"[INFO] Handled {n} Telegram command(s)")
        except Exception as e:
            print(f"[ERROR] Command processing failed: {e}", file=sys.stderr)
        # NOTE: no startup "heartbeat" message — at a 5-minute cadence it would spam.
        # Only real events (new chapters, command replies, button taps) notify.

    updates_found = 0

    try:
        # Check for updates
        updates = scraper.check_updates(state)

        if updates:
            updates_found = len(updates)
            print(f"\n[INFO] Found {updates_found} manga with updates")
            for update in updates:
                manga = update["manga"]
                new_chapters = update["new_chapters"]
                update["is_bookmarked"] = bookmarks.is_bookmarked(manga.url)

                # Print to console
                star = " 📌" if update["is_bookmarked"] else ""
                print(f"\n📖 {manga.title}{star}")
                for ch in new_chapters:
                    print(f"   ✨ ตอนที่ {ch.number} - {ch.url}")

                # Send Telegram notification
                if telegram_ready:
                    notifier.send_update(update)
                    time.sleep(0.5)  # Rate limit

            # Separate favourites summary
            fav_updates = [u for u in updates if u.get("is_bookmarked")]
            if telegram_ready and fav_updates:
                notifier.send_favorites_summary(fav_updates)
        else:
            updates_found = 0
            print("[INFO] No new updates found")

    finally:
        # Persist Telegram offset; ensure bookmarks file exists so CI can commit it.
        # No routine shutdown "heartbeat" message (would spam at 5-min cadence).
        save_bot_state(BOT_STATE_FILE, bot_state)
        bookmarks.save()

    print("\n" + "=" * 60)
    print(f"Completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Tracked manga: {len(state.get_all_tracked())}  |  Bookmarks: {len(bookmarks.all())}")
    print("=" * 60)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test-msg":
        # Test message formatting
        state = StateManager(STATE_FILE)
        notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        
        tracked = len(state.get_all_tracked())
        print(f"Tracked manga: {tracked}")
        print("Sending startup message...")
        notifier.send_startup_message(tracked)
        print("Startup message sent!")
        
        # Also test update message format
        import time
        time.sleep(1)
        
        # Send test shutdown
        print("Sending shutdown message (3 updates found)...")
        notifier.send_shutdown_message(tracked, 3)
        print("Shutdown message sent!")
        
        import time
        time.sleep(1)
        print("Sending shutdown message (0 updates)...")
        notifier.send_shutdown_message(tracked, 0)
        print("Shutdown message sent!")
    else:
        main()