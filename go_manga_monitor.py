#!/usr/bin/env python3
"""
Go-Manga Update Monitor
Monitors https://www.go-manga.com/manga/?order=update for new chapter updates
and sends notifications via Telegram.
"""

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
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
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 1.0  # Be polite to the server

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

    def scrape_list_page(self) -> list[dict]:
        """Scrape the manga listing page sorted by update"""
        print(f"[INFO] Fetching list page: {LIST_URL}")
        soup = self._get(LIST_URL)
        if not soup:
            return []

        manga_list = []
        # The manga items are links directly under the listing area
        # From browser inspection, they're <a> tags with full manga info in text
        links = soup.find_all("a", href=True)

        for link in links:
            href = link.get("href", "")
            if not href:
                continue

            # Only process manga detail links (not chapter links)
            full_url = urljoin(BASE_URL, href)
            if not full_url.startswith(BASE_URL):
                continue

            # Skip chapter links - they have patterns like /manga-name-ตอนที่-44/
            # Manga links are like /manga-name/ or /manga-name
            # But from browser, the listing shows links like /became-rogue-first-prince-.../
            # which are actually the manga detail pages
            
            # Skip obvious chapter links
            if re.search(r"(ตอนที่|Chapter|EP)\s*\d+", href):
                continue
            
            # Skip navigation/pagination links
            if href in ["/manga/?order=update", "/manga/", "/", "/genres/"] or href.startswith("/genres/") or href.startswith("/page/") or href.startswith("?page="):
                continue

            text = link.get_text(strip=True)
            if not text or len(text) < 5:
                continue

            # Extract info from text
            # Format: "MANHWA COLOR Title ตอนที่ 44 ⭐⭐⭐⭐⭐ ⭐⭐⭐⭐⭐ 7.4"
            type_match = re.search(r"(MANHWA|MANGA|MANHUA)", text, re.IGNORECASE)
            type_ = type_match.group(1).upper() if type_match else "UNKNOWN"

            # Extract title (everything before chapter info)
            title = text
            chapter_match = re.search(r"(ตอนที่\s*\d+|Chapter\s*\d+|EP\s*\d+)", text)
            if chapter_match:
                title = text[:chapter_match.start()].strip()

            # Extract latest chapter
            latest_chapter = ""
            latest_chapter_url = ""
            if chapter_match:
                latest_chapter = self._parse_chapter_number(chapter_match.group(1))
                latest_chapter_url = full_url  # This is actually the manga detail URL

            # Extract rating (at the end of text)
            rating_match = re.search(r"(\d+\.\d+)$", text)
            rating = rating_match.group(1) if rating_match else "N/A"

            # Clean up title - remove type badges
            title = re.sub(r"^(MANHWA|MANGA|MANHUA)\s*(?:🔥|COLOR)?\s*", "", title, flags=re.IGNORECASE).strip()

            if title and len(title) > 2:
                # Extract date if present in text
                latest_chapter_date = ""
                date_match = re.search(r"(\d{1,2}\s+(?:มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม|January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})", text)
                if not date_match:
                    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
                if date_match:
                    latest_chapter_date = self._parse_date(date_match.group(1))

                manga_list.append({
                    "title": title,
                    "url": full_url,
                    "latest_chapter": latest_chapter,
                    "latest_chapter_url": latest_chapter_url,
                    "latest_chapter_date": latest_chapter_date,
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
            rating_elem = soup.find(text=re.compile(r"\d\.\d"))
            if rating_elem:
                match = re.search(r"(\d\.\d)", str(rating_elem))
                if match:
                    rating = match.group(1)

            # Extract chapters - look for chapter links in the page
            chapters = []

            # Chapter links have patterns like /manga-name-ตอนที่-44/ or /manga-name-44/
            # They also appear as text "ตอนที่ 44" in link text
            chapter_links = soup.find_all("a", href=True)
            for link in chapter_links:
                href = link.get("href", "")
                text = link.get_text(strip=True)

                # Skip non-chapter links
                if not (href and ("ตอนที่" in text or "chapter" in href.lower() or "ep" in href.lower() or "ตอน" in text)):
                    continue

                # Extract chapter number from text or href
                chapter_num = self._parse_chapter_number(text)
                if not chapter_num:
                    chapter_num = self._parse_chapter_number(href)
                if not chapter_num:
                    continue

                chapter_url = urljoin(BASE_URL, href)
            
                # Try to find date near this link
                date = ""
                parent = link.parent
                if parent:
                    parent_text = parent.get_text(strip=True)
                    date = self._parse_date(parent_text)

                chapters.append(Chapter(
                    number=chapter_num,
                    title=text,
                    url=chapter_url,
                    date=date
                ))

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
                chapters=unique_chapters,
            )

    def check_updates(self, state: StateManager) -> list[dict]:
        """Check for new chapters across all manga"""
        updates = []

        # Get list of manga from the update page
        manga_list = self.scrape_list_page()

        for manga_info in manga_list:
            manga_url = manga_info["url"]
            title = manga_info["title"]
            latest_chapter = manga_info["latest_chapter"]

            if not latest_chapter:
                continue

            # Check if we have seen this chapter before
            last_chapter = state.get_last_chapter(manga_url)

            if last_chapter is None:
                # First time seeing this manga - record current state
                state.update_manga(manga_url, latest_chapter, manga_info.get("latest_chapter_date", ""), title)
                print(f"[INFO] First time tracking: {title} - Chapter {latest_chapter}")
                continue

            # Compare chapter numbers
            try:
                current_num = int(latest_chapter)
                last_num = int(last_chapter)
                if current_num > last_num:
                    # New chapter(s) detected!
                    # Get full detail to find all new chapters
                    manga = self.scrape_manga_detail(manga_url)
                    if manga:
                        new_chapters = [c for c in manga.chapters if int(c.number) > last_num]
                        new_chapters.sort(key=lambda c: int(c.number))

                        updates.append({
                            "manga": manga,
                            "new_chapters": new_chapters,
                            "previous_chapter": last_chapter,
                        })
                        print(f"[UPDATE] {title}: {len(new_chapters)} new chapter(s) - {last_chapter} -> {latest_chapter}")

                        # Update state to latest
                        state.update_manga(manga_url, latest_chapter, manga_info.get("latest_chapter_date", ""), title)

                    time.sleep(REQUEST_DELAY)  # Be polite
                elif current_num == last_num:
                    # Same chapter - check if date updated (re-upload)
                    last_date = state.get_last_chapter_date(manga_url)
                    current_date = manga_info.get("latest_chapter_date", "")
                    if current_date and last_date and current_date != last_date:
                        print(f"[INFO] {title}: Chapter {latest_chapter} re-uploaded ({last_date} -> {current_date})")
                        state.update_manga(manga_url, latest_chapter, current_date, title)
                else:
                    # Our state is ahead (shouldn't happen normally)
                    print(f"[WARN] State ahead for {title}: state={last_chapter}, site={latest_chapter}")
                    state.update_manga(manga_url, latest_chapter, manga_info.get("latest_chapter_date", ""), title)
            except ValueError:
                # Non-numeric chapter, compare as strings
                if latest_chapter != last_chapter:
                    print(f"[UPDATE] {title}: Chapter changed {last_chapter} -> {latest_chapter}")
                    state.update_manga(manga_url, latest_chapter, manga_info.get("latest_chapter_date", ""), title)

        return updates


# ============== TELEGRAM NOTIFIER ==============
class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self.bot_token or not self.chat_id:
            print("[WARN] Telegram not configured, skipping notification")
            return False

        url = f"{self.api_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[ERROR] Failed to send Telegram message: {e}", file=sys.stderr)
            return False

    def format_update_message(self, update: dict) -> str:
        manga = update["manga"]
        new_chapters = update["new_chapters"]

        lines = [
            f"🔔 <b>มีการ์ตูนอัพเดทใหม่!</b>",
            f"",
            f"📖 <b>{manga.title}</b>",
            f"🔖 ประเภท: {manga.type_} | สถานะ: {manga.status} | ⭐ {manga.rating}",
            f"",
            f"📚 <b>ตอนใหม่ {len(new_chapters)} ตอน:</b>",
        ]

        for ch in new_chapters:
            date_str = f" ({ch.date})" if ch.date else ""
            lines.append(f"  • <a href=\"{ch.url}\">ตอนที่ {ch.number}</a>{date_str}")

        lines.extend([
            f"",
            f"🔗 <a href=\"{manga.url}\">เปิดอ่านที่ Go-Manga</a>",
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ])

        return "\n".join(lines)

    def send_update(self, update: dict) -> bool:
        message = self.format_update_message(update)
        return self.send_message(message)


# ============== MAIN ==============
def main():
    print("=" * 60)
    print("Go-Manga Update Monitor")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Initialize components
    state = StateManager(STATE_FILE)
    scraper = GoMangaScraper()
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram credentials not set. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.")
        print("      Notifications will be printed to console only.")

    # Check for updates
    updates = scraper.check_updates(state)

    if updates:
        print(f"\n[INFO] Found {len(updates)} manga with updates")
        for update in updates:
            manga = update["manga"]
            new_chapters = update["new_chapters"]

            # Print to console
            print(f"\n📖 {manga.title}")
            for ch in new_chapters:
                print(f"   ✨ ตอนที่ {ch.number} - {ch.url}")

            # Send Telegram notification
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                notifier.send_update(update)
                time.sleep(0.5)  # Rate limit
    else:
        print("[INFO] No new updates found")

    print("\n" + "=" * 60)
    print(f"Completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Tracked manga: {len(state.get_all_tracked())}")
    print("=" * 60)


if __name__ == "__main__":
    main()