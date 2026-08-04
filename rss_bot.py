import hashlib
import html
import json
import os
import pathlib
import re
import time

import feedparser
import requests
import trafilatura

# ─────────────────────────────────────────────
#  НАЛАШТУВАННЯ — міняй тільки цей блок
# ─────────────────────────────────────────────

FEEDS = [
    "https://wccftech.com/topic/games/feed/",
    "https://www.polygon.com/rss/gaming/index.xml",
    "https://www.playstationlifestyle.net/feed/",
]

MAX_PER_FEED = 15   # скільки останніх записів перевіряти в кожній стрічці
MAX_ARTICLE_CHARS = 8000   # обмеження довжини тексту статті, що йде в Claude

# ─────────────────────────────────────────────

TG_TOKEN = os.environ["TG_TOKEN"]
DRAFT_TG_CHAT = os.environ["DRAFT_TG_CHAT"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = "claude-sonnet-5"

STATE_FILE = pathlib.Path("rss_state.json")
IMG_TAG = re.compile(r'<img[^>]+src="([^"]+)"')

STYLE_GUIDE = """Ти — редактор українськомовного ігрового Telegram-каналу "Синдром Гравця" (PS Store знижки, PS Plus, Sony новини, трофеї, GTA VI). Тобі дають заголовок, посилання та повний текст статті-джерела. Твоя задача — переписати це у готовий пост у встановленому стилі каналу.

ЗАГАЛЬНІ ПРИНЦИПИ
- Нічого не додумуй від себе, лише факти з джерела
- Без власних висновків, оцінок, прогнозів
- Чутки/витоки завжди позначай чітко, ніколи не подавай як факт
- Непідтверджені деталі не подавай як офіційні
- Повторювану інформацію згадуй раз, відсікай воду
- Перевір, чи всі ключові факти зі статті потрапили в пост
- Якщо текст статті виглядає як автоматичні субтитри YouTube (уривчастий, без пунктуації) — постав менше довіри деталям, працюй обережніше

ДВА ФОРМАТИ ПОСТІВ
Стиль 1 (структурований): емодзі + заголовок → короткий вступ (1-2 речення) → список пунктів з маркером ▫️ без крапки в кінці, перше слово з великої, без пробілу після маркера, без порожніх рядків між пунктами → за потреби рядок з датою релізу і платформами формату «Реліз [дата] року на [платформи]»
Стиль 2 (наративний): емодзі + заголовок → 2-5 коротких абзаців без маркерів, читається як готова новина

Обирай формат залежно від типу новини: якщо це список деталей/фактів — стиль 1, якщо це подія/заява/історія — стиль 2.

ТИПИ НОВИН І ЯК ЇХ ОФОРМЛЮВАТИ
- Звіт/витік видання: без слова "Чутки:", без застережень у кінці, заголовок у форматі «[Суть], — звіт [Джерело]», емодзі тематичний під новину
- Інсайдерська чутка: окремим рядком на початку "Чутки:", у кінці обов'язково додай рядок "Інформація не є офіційною"
- Breaking: заголовок "⚡️BREAKING⚡️" лише для дійсно гарячих, щойно опублікованих новин
- Кастинг: без розлогих списків другорядних ролей, тільки ключові факти
- Digital Foundry / технічний аналіз консолей: фіксований шаблон із маркером ▫️ по кожній консолі та підпунктами з маркером • для «Режим продуктивності» / «Режим якості» (продуктивність завжди першою)

ТЕРМІНОЛОГІЯ ТА ПРАВОПИС
- "PS Plus" скорочено (не "PlayStation Plus")
- "Deluxe" замість "Premium" де це стосується назв видань
- "Grand Theft Auto VI" пиши повністю з римською цифрою
- Транслітерації: "Коджіма", "Хендерсон"
- "PC" завжди латиницею
- "проект" (не "проєкт")
- FPS завжди великими літерами
- "бітемап", не "битемап"
- Кодові назви беруться в лапки-ялинки: «Project Назва»
- Валюта пишеться без роздільника тисяч, наприклад "3599,10 UAH"
- Великі круглі числа пиши словами, наприклад "600 тисяч"
- Не додавай трикрапку в кінці посту

СТРУКТУРА ВІДПОВІДІ
Виведи ЛИШЕ готовий текст поста — без пояснень, без лапок навколо, без Markdown-розмітки (без **, без #). Постав емодзі перед заголовком першим рядком. Якщо доречно, використовуй порожній рядок між вступом і списком ▫️."""


def entry_id(feed_url, entry):
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(f"{feed_url}|{raw}".encode()).hexdigest()[:16]


def clean_text(raw, limit=600):
    text = re.sub("<[^<]+?>", "", raw or "")
    text = html.unescape(text).strip()
    return text[:limit]


def fetch_full_article(url):
    """Тягне повний текст статті зі сторінки. Повертає None, якщо не вдалось."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        if text:
            return text[:MAX_ARTICLE_CHARS]
    except Exception as e:
        print(f"  ! Не вдалось витягти статтю {url}: {e}")
    return None


def format_with_claude(title, article_text, feed_title, link):
    user_content = (
        f"Джерело: {feed_title}\n"
        f"Посилання: {link}\n"
        f"Заголовок: {title}\n\n"
        f"Текст статті:\n{article_text}"
    )

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 1200,
            "system": STYLE_GUIDE,
            "messages": [{"role": "user", "content": user_content}],
        },
        timeout=90,
    )
    r.raise_for_status()
    data = r.json()
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()


def tg(method, payload):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
        json=payload,
        timeout=60,
    )
    if not r.ok:
        print(f"  ! Telegram {method}: {r.text[:200]}")
    return r.ok


def send_draft(entry, feed_title):
    link = entry.get("link", "")
    title = entry.get("title", "").strip()

    article_text = fetch_full_article(link)
    if not article_text:
        article_text = clean_text(entry.get("summary", "") or entry.get("description", ""), limit=2000)

    if not article_text:
        print(f"  ! Пропущено (немає тексту): {title}")
        return

    try:
        post = format_with_claude(title, article_text, feed_title, link)
    except Exception as e:
        print(f"  ! Помилка Claude API: {e}")
        return

    message = f"{post}\n\n—\nДжерело: {link}"
    tg("sendMessage", {
        "chat_id": DRAFT_TG_CHAT,
        "text": message,
        "disable_web_page_preview": False,
    })


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

            send_draft(entry, feed_title)
            sent += 1
            time.sleep(3)

        print(f"{feed_title}: надіслано {sent}")

    STATE_FILE.write_text(json.dumps({"seen": seen[-3000:]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
