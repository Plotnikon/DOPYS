import hashlib
import html
import json
import os
import pathlib
import re
import time

import feedparser
import requests

# ────────────────────────────────────────────────
#  НАЛАШТУВАННЯ — міняй тільки цей блок
# ────────────────────────────────────────────────

FEEDS = [
    "https://wccftech.com/topic/games/feed/",
    "https://www.polygon.com/rss/gaming/index.xml",
]

MAX_PER_FEED = 15   # скільки останніх записів перевіряти в кожній стрічці

# ────────────────────────────────────────────────

TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT = os.environ["TG_CHAT"]

STATE_FILE = pathlib.Path("rss_state.json")
IMG_TAG = re.compile(r'<img[^>]+src="([^"]+)"')


def entry_id(feed_url, entry):
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(f"{feed_url}|{raw}".encode()).hexdigest()[:16]


def find_image(entry):
    # 1. media_content / media_thumbnail (частий випадок)
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if media:
            url = media[0].get("url")
            if url:
                return url
    # 2. картинка у прикріплених файлах
    for enc in entry.get("links", []):
        if enc.get("type", "").startswith("image/"):
            return enc.get("href")
    # 3. картинка всередині HTML опису
    summary = entry.get("summary", "") or entry.get("description", "")
    m = IMG_TAG.search(summary)
    if m:
        return m.group(1)
    return None


def clean_text(raw, limit=600):
    text = re.sub("<[^<]+?>", "", raw or "")
    text = html.unescape(text).strip()
    return text[:limit]


def caption(entry, feed_title):
    title = html.escape(entry.get("title", "").strip())[:250]
    summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
    link = entry.get("link", "")
    parts = [f"<b>{title}</b>"]
    if summary:
        parts.append(html.escape(summary))
    parts.append(f"\n{feed_title} · <a href=\"{link}\">джерело</a>")
    return "\n\n".join(parts)


def tg(method, payload):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
        json=payload,
        timeout=60,
    )
    if not r.ok:
        print(f"  ! Telegram {method}: {r.text[:200]}")
    return r.ok


def send(entry, feed_title):
    cap = caption(entry, feed_title)
    img = find_image(entry)

    if img and tg("sendPhoto", {"chat_id": TG_CHAT, "photo": img,
                                 "caption": cap, "parse_mode": "HTML"}):
        return
    tg("sendMessage", {"chat_id": TG_CHAT, "text": cap,
                        "parse_mode": "HTML", "disable_web_page_preview": False})


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    seen = list(state.get("seen", []))
    seen_set = set(seen)
    first_run = not seen

    if first_run:
        print("Перший запуск: нічого не шлю, тільки запам'ятовую поточні записи.")

    for url in FEEDS:
        parsed = feedparser.parse(url)
        if parsed.bozo and not parsed.entries:
            print(f"{url}: помилка парсингу — {parsed.bozo_exception}")
            continue

        feed_title = parsed.feed.get("title", url)
        entries = parsed.entries[:MAX_PER_FEED]
        sent = 0

        for entry in reversed(entries):
            eid = entry_id(url, entry)
            if eid in seen_set:
                continue

            seen_set.add(eid)
            seen.append(eid)

            if first_run:
                continue

            send(entry, feed_title)
            sent += 1
            time.sleep(3)

        print(f"{feed_title}: надіслано {sent}")

    STATE_FILE.write_text(json.dumps({"seen": seen[-3000:]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
