import html
import json
import os
import pathlib
import time

import requests

# ────────────────────────────────────────────────
#  НАЛАШТУВАННЯ — міняй тільки цей блок
# ────────────────────────────────────────────────

SUBREDDITS = [
    "PS5",
    "gaming",
    "GamingLeaksAndRumours",
]

LIMIT = 25          # скільки останніх постів перевіряти в кожному сабі
MIN_SCORE = 0       # мінімум апвоутів (0 = без фільтра)
SEND_VIDEO_LINKS = True   # відео слати посиланням (True) чи пропускати (False)

REDDIT_LOGIN = "твій_нік_на_реддіті"   # тільки для User-Agent, реддіт цього вимагає

# ────────────────────────────────────────────────

TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT = os.environ["TG_CHAT"]
REDDIT_ID = os.environ["REDDIT_ID"]
REDDIT_SECRET = os.environ["REDDIT_SECRET"]

UA = f"github-actions:tg-reddit-feed:1.0 (by /u/{REDDIT_LOGIN})"
STATE_FILE = pathlib.Path("state.json")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
VID_EXT = (".gif", ".gifv", ".mp4")


def get_token():
    r = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(REDDIT_ID, REDDIT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": UA},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch(sub, token):
    r = requests.get(
        f"https://oauth.reddit.com/r/{sub}/new",
        params={"limit": LIMIT},
        headers={"User-Agent": UA, "Authorization": f"bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return [c["data"] for c in r.json()["data"]["children"]]


def detect_media(p):
    """Повертає ('photo', url) / ('gallery', [urls]) / ('video', None) / (None, None)"""
    if p.get("is_gallery"):
        urls = []
        meta = p.get("media_metadata") or {}
        for item in (p.get("gallery_data") or {}).get("items", [])[:10]:
            m = meta.get(item.get("media_id"), {})
            u = (m.get("s") or {}).get("u")
            if u:
                urls.append(html.unescape(u))
        if urls:
            return "gallery", urls

    url = p.get("url_overridden_by_dest") or p.get("url") or ""
    clean = url.lower().split("?")[0]

    if p.get("is_video") or "v.redd.it" in url or clean.endswith(VID_EXT):
        return "video", None
    if clean.endswith(IMG_EXT) or p.get("post_hint") == "image":
        return "photo", url
    return None, None


def caption(p):
    title = html.escape(html.unescape(p.get("title", "")))[:700]
    link = "https://reddit.com" + p.get("permalink", "")
    return f"<b>{title}</b>\n\nr/{p['subreddit']} · <a href=\"{link}\">джерело</a>"


def tg(method, payload):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
        json=payload,
        timeout=60,
    )
    if not r.ok:
        print(f"  ! Telegram {method}: {r.text[:200]}")
    return r.ok


def send(p, kind, media):
    cap = caption(p)

    if kind == "photo":
        if tg("sendPhoto", {"chat_id": TG_CHAT, "photo": media,
                            "caption": cap, "parse_mode": "HTML"}):
            return
    elif kind == "gallery":
        group = [{"type": "photo", "media": u} for u in media]
        group[0].update(caption=cap, parse_mode="HTML")
        if tg("sendMediaGroup", {"chat_id": TG_CHAT, "media": group}):
            return
    elif kind == "video" and not SEND_VIDEO_LINKS:
        return

    # фолбек: якщо медіа не віддалося — просто текст із посиланням
    tg("sendMessage", {"chat_id": TG_CHAT, "text": cap, "parse_mode": "HTML"})


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    seen = list(state.get("seen", []))
    seen_set = set(seen)
    first_run = not seen

    if first_run:
        print("Перший запуск: нічого не шлю, тільки запам'ятовую поточні пости.")

    token = get_token()

    for sub in SUBREDDITS:
        try:
            posts = fetch(sub, token)
        except Exception as e:
            print(f"r/{sub}: помилка — {e}")
            continue

        sent = 0
        for p in reversed(posts):          # від старих до нових
            pid = p["id"]
            if pid in seen_set:
                continue

            seen_set.add(pid)
            seen.append(pid)

            if first_run:
                continue
            if p.get("score", 0) < MIN_SCORE:
                continue

            kind, media = detect_media(p)
            if not kind:
                continue

            send(p, kind, media)
            sent += 1
            time.sleep(3)                  # щоб не впертись у ліміти Telegram

        print(f"r/{sub}: надіслано {sent}")

    STATE_FILE.write_text(json.dumps({"seen": seen[-3000:]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
