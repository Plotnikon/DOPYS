"""
discount_bot.py

Рубрика "Найнижча ціна за увесь час" для каналу "Синдром Гравця".

Розрахований на РІДКІ запуски (раз на ~14-16 днів, коли в PS Store оновлюється хвиля
акцій) - за один запуск бере ВСІ нові ігри, що проходять фільтр (до MAX_POSTS_PER_RUN
штук), і публікує їх одним заходом. Наступного разу вже опубліковані/перевірені ігри
пропускаються.

Що робить:
1. Бере список ігор зі знижками на https://psprices.com/region-ua/collection/lowest-prices-ever
   (кожна гра в цій добірці зараз коштує стільки, скільки НІКОЛИ раніше не коштувала -
   тобто це вже готовий список "найнижча ціна за увесь час", нічого додатково перевіряти не треба).
2. Пропускає ігри, які вже публікувались або вже перевірялись раніше (дедуп у discount_state.json).
3. Для кожної нової гри йде на її сторінку і бере:
   - офіційну обкладинку гри (те саме зображення, що і в PlayStation Store),
   - посилання "купити" (веде на PlayStation Store),
   - оцінки Metacritic / OpenCritic (показник того, наскільки гра відома),
   - жанр (щоб відсіяти Adult-контент),
   - видавця, ціни, знижку, дату закінчення акції.
4. Залишає тільки більш-менш популярні / хайпові ігри:
   - або Metacritic, або OpenCritic оцінка є і становить 70+,
   - або (якщо оцінок немає чи вони нижчі - можливо, це інді без критичних оглядів)
     питає Claude, чи це все одно відома/хайпова гра.
5. Просить Claude написати текст поста в стилі каналу.
6. Публікує пост (фото + підпис) у приватний Telegram-канал-чернетку для знижок.
7. Зупиняється, коли опубліковано MAX_POSTS_PER_RUN постів за цей запуск.

Секрети, які потрібні в GitHub Actions (Settings -> Secrets and variables -> Actions):
- TG_TOKEN            (той самий токен бота, що і в rss_bot.py)
- DISCOUNT_TG_CHAT    (chat_id каналу-чернетки для знижок, напр. -1003704195755)
- ANTHROPIC_API_KEY   (той самий ключ, що і в rss_bot.py)

Стан (discount_state.json) комітиться назад у репозиторій, як і rss_state.json.
"""

import json
import os
import re
import sys

import requests
from anthropic import Anthropic

# ---------- Налаштування ----------

TG_TOKEN = os.environ["TG_TOKEN"]
DRAFT_CHAT_ID = os.environ["DISCOUNT_TG_CHAT"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

CLAUDE_MODEL = "claude-sonnet-5"

STATE_FILE = "discount_state.json"

LIST_URL = "https://psprices.com/region-ua/collection/lowest-prices-ever"
PAGES_TO_CHECK = 5           # список відсортований за останнім оновленням - нові ігри зверху
CRITIC_SCORE_THRESHOLD = 70  # мін. Metacritic/OpenCritic, щоб вважати гру популярною без питання Claude
MAX_POSTS_PER_RUN = 16       # запобіжник від надмірної кількості постів за один прогін
IGNORE_GENRES = {"adult"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
}

STYLE_GUIDE = """
Ти пишеш пост для щоденної рубрики "Найнижча ціна за увесь час" у Telegram-каналі
"Синдром Гравця" (PS Store знижки, PS Plus, Sony/PlayStation новини).

Формат (наслідуй цей приклад один в один за структурою):

🔥 Найнижча ціна в PlayStation Store за увесь час!

<a href="ПОСИЛАННЯ">Назва гри</a> *(перелік вмісту бандла курсивом, лише якщо це бандл з кількох ігор/DLC)* за X UAH замість Y UAH.

Це найбільша знижка Z% [з моменту релізу, якщо доречно], дійсна до ДАТА.

Правила:
- Все українською.
- Заголовок "🔥 Найнижча ціна в PlayStation Store за увесь час!" — з пробілом після емодзі, без крапки в кінці.
- Назва гри — клікабельне HTML-посилання <a href="..."> на офіційну сторінку PlayStation Store.
- Ціни: без розділювача тисяч, кома замість крапки (напр. 599,80 UAH, а не 599.80 UAH чи 599,800 UAH).
- PS5/PS4/PS5 Pro завжди скорочено (ніколи "PlayStation 5"), PS Plus не повною назвою.
- Назви ігор і студій — повністю, без скорочень (Rockstar Games, а не Rockstar).
- Ніяких запитань у тексті — тільки констатація факту.
- Ніяких власних висновків чи прогнозів — тільки факти з наданих даних.
- Без трикрапки в кінці.
- Компактно: 2-4 короткі речення, без зайвої води.
- Якщо це бандл (кілька ігор/видань в одному лоті) — додай перелік вмісту курсивом у дужках одразу після назви.
- Виведи ЛИШЕ готовий текст поста (HTML, придатний для Telegram parse_mode=HTML), без пояснень і без markdown-обгортки.
"""

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------- Стан / дедуп ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"posted_ids": [], "skipped_ids": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- Крок 1: список кандидатів ----------

CARD_RE = re.compile(r'href="(/region-ua/game/(\d+)/[^"]+)"', re.DOTALL)


def fetch_candidates():
    """Повертає список {id, url} з перших кількох сторінок добірки 'найнижча ціна'."""
    candidates = []
    seen_ids = set()
    for page in range(1, PAGES_TO_CHECK + 1):
        url = LIST_URL if page == 1 else f"{LIST_URL}?page={page}"
        resp = requests.get(url, headers=HEADERS, timeout=30)
        print(f"[fetch_candidates] сторінка {page}: HTTP {resp.status_code}, {len(resp.text)} байт")
        if resp.status_code != 200:
            print(f"[fetch_candidates] сторінка {page} не завантажилась, зупиняюсь. Фрагмент: {resp.text[:300]!r}")
            break
        page_matches = 0
        for path, game_id in CARD_RE.findall(resp.text):
            if game_id in seen_ids:
                continue
            seen_ids.add(game_id)
            page_matches += 1
            candidates.append({"id": game_id, "url": "https://psprices.com" + path})
        print(f"[fetch_candidates] сторінка {page}: знайдено карток ігор: {page_matches}")
        if page_matches == 0:
            break  # далі, ймовірно, порожні сторінки - список скінчився
    return candidates


# ---------- Крок 2: деталі гри ----------

BUY_BLOCK_RE = re.compile(r'href="([^"]*?/game/buy/\d+)"[^>]*>(.*?)</a>', re.DOTALL)
PRICE_BLOCK_RE = re.compile(
    r"([\d][\d\s]*(?:,\d{2})?)\s*₴.*?trending_down\s*(\d+)\s*%\s*([\d][\d\s]*(?:,\d{2})?)\s*₴"
)
UNTIL_RE = re.compile(r"until\s*(\d{2}/\d{2}/\d{4})")
OPENCRITIC_RE = re.compile(r'href="https://opencritic\.com/game/[^"]*"[^>]*>(.*?)</a>', re.DOTALL)
METACRITIC_RE = re.compile(r'href="https://www\.metacritic\.com/game/[^"]*"[^>]*>(.*?)</a>', re.DOTALL)
# наступні регулярки застосовуються НЕ до сирого HTML, а до тексту з уже прибраними тегами
RELEASE_RE = re.compile(r"Release date\s+([A-Za-z]{3,4}\.?\s\d{1,2},\s\d{4})")
PUBLISHER_RE = re.compile(
    r"Publisher\s+(.+?)\s+(?:Download size|What's included|Optimization|Ratings|Audio|Genres|Trophies)"
)
GENRES_RE = re.compile(r"Genres\s+(.+?)\s+(?:Local co-op|Playtime|Trophies|Also known as)")


def _clean_number(text):
    return text.replace("\xa0", " ").replace(" ", "").replace(",", ".")


def _extract_score(pattern, html):
    m = pattern.search(html)
    if not m:
        return None
    digits = re.sub(r"<[^>]+>", "", m.group(1))
    digits = re.search(r"\d+", digits)
    return int(digits.group(0)) if digits else None


def fetch_game_detail(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        print(f"[fetch_game_detail] {url}: HTTP {resp.status_code}")
        return None
    html = resp.text

    def meta(name):
        m = re.search(rf'property="{name}" content="([^"]*)"', html)
        return m.group(1) if m else None

    title = meta("og:title") or ""
    title = re.sub(r"^-?\d+%\s*", "", title).strip()

    image_url = meta("og:image")

    buy_match = BUY_BLOCK_RE.search(html)
    price_now = price_old = discount_pct = ends_text = None
    buy_url = None
    if buy_match:
        raw_buy_path = buy_match.group(1)
        buy_url = raw_buy_path if raw_buy_path.startswith("http") else "https://psprices.com" + raw_buy_path
        block_text = re.sub(r"<[^>]+>", " ", buy_match.group(2))
        price_match = PRICE_BLOCK_RE.search(block_text)
        if price_match:
            price_now = float(_clean_number(price_match.group(1)))
            discount_pct = int(price_match.group(2))
            price_old = float(_clean_number(price_match.group(3)))
        until_match = UNTIL_RE.search(block_text)
        ends_text = until_match.group(1) if until_match else None

    # для міток типу "Release date"/"Publisher"/"Genres" надійніше спочатку прибрати
    # всі теги в звичайний текст, ніж покладатись на конкретну вкладеність HTML
    page_text = re.sub(r"<[^>]+>", " ", html)
    page_text = re.sub(r"\s+", " ", page_text).strip()

    genre_match = GENRES_RE.search(page_text)
    genre_text = genre_match.group(1).strip().lower() if genre_match else ""

    metacritic = _extract_score(METACRITIC_RE, html)
    opencritic = _extract_score(OPENCRITIC_RE, html)

    release_match = RELEASE_RE.search(page_text)
    release_text = release_match.group(1) if release_match else None

    publisher_match = PUBLISHER_RE.search(page_text)
    publisher = publisher_match.group(1).strip() if publisher_match else None

    return {
        "title": title,
        "image_url": image_url,
        "buy_url": buy_url,
        "genre_text": genre_text,
        "metacritic": metacritic,
        "opencritic": opencritic,
        "release_text": release_text,
        "publisher": publisher,
        "price_now": price_now,
        "price_old": price_old,
        "discount_pct": discount_pct,
        "ends_text": ends_text,
    }


def is_adult(genre_text):
    return any(g in genre_text for g in IGNORE_GENRES)


# ---------- Крок 3: фільтр популярності ----------

def looks_popular_enough(detail):
    for score in (detail["metacritic"], detail["opencritic"]):
        if score is not None and score >= CRITIC_SCORE_THRESHOLD:
            return True
    return False


def ask_claude_is_popular(title, publisher):
    """Для ігор без достатньо високої критичної оцінки - питаємо Claude, чи це все одно
    відома/хайпова гра (в т.ч. інді-хіти)."""
    prompt = (
        f'Гра називається "{title}"'
        + (f' (видавець: {publisher})' if publisher else "")
        + ". Чи є ця гра достатньо відомою/популярною/хайповою серед геймерів PlayStation, "
        "щоб про знижку на неї варто було написати пост у геймерському Telegram-каналі? "
        "Враховуй і AAA-тайтли, і відомі інді-хіти. "
        'Відповідай ЛИШЕ одним словом: "так" або "ні".'
    )
    resp = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    answer = resp.content[0].text.strip().lower()
    return answer.startswith("так")


# ---------- Крок 4: генерація тексту поста ----------

def format_price(value):
    if value is None:
        return "?"
    return f"{value:.2f}".replace(".", ",")


def generate_post_text(detail):
    facts = (
        f"Назва: {detail['title']}\n"
        f"Посилання на PlayStation Store: {detail['buy_url']}\n"
        f"Поточна ціна: {format_price(detail['price_now'])} UAH\n"
        f"Ціна без знижки: {format_price(detail['price_old'])} UAH\n"
        f"Знижка: {detail['discount_pct']}%\n"
        f"Це офіційно найнижча ціна за весь час спостережень.\n"
        f"Видавець: {detail['publisher'] or 'невідомо'}\n"
        f"Дата релізу: {detail['release_text'] or 'невідомо'}\n"
        f"Діє до: {detail['ends_text'] or 'невідомо'}\n"
    )
    resp = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        system=STYLE_GUIDE,
        messages=[{"role": "user", "content": facts}],
    )
    return resp.content[0].text.strip()


# ---------- Крок 5: публікація в Telegram ----------

def send_to_telegram(caption, image_url):
    api_url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
    payload = {
        "chat_id": DRAFT_CHAT_ID,
        "photo": image_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    resp = requests.post(api_url, json=payload, timeout=30)
    if resp.status_code != 200 or not resp.json().get("ok"):
        # якщо фото не приймається (напр. недоступне посилання) - шлемо просто текст
        fallback_url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        resp = requests.post(
            fallback_url,
            json={
                "chat_id": DRAFT_CHAT_ID,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
    return resp.status_code == 200 and resp.json().get("ok")


# ---------- main ----------

def main():
    state = load_state()
    posted_ids = set(state["posted_ids"])
    skipped_ids = set(state["skipped_ids"])

    # гарантуємо, що файл стану існує з першого ж запуску,
    # навіть якщо кандидатів не знайдеться - інакше крок коміту в Actions впаде
    save_state(state)

    candidates = fetch_candidates()
    print(f"Знайдено кандидатів на сторінках списку: {len(candidates)}")

    posted_this_run = 0

    for c in candidates:
        if posted_this_run >= MAX_POSTS_PER_RUN:
            print(f"Досягнуто ліміту {MAX_POSTS_PER_RUN} постів за прогін, зупиняюсь")
            break

        game_id = c["id"]
        if game_id in posted_ids or game_id in skipped_ids:
            continue

        detail = fetch_game_detail(c["url"])
        if not detail or not detail["image_url"] or not detail["buy_url"]:
            skipped_ids.add(game_id)
            continue

        if detail["price_now"] is None or detail["discount_pct"] is None:
            print(f"[main] не вдалось розпарсити ціну для {detail['title']}, пропускаю")
            skipped_ids.add(game_id)
            continue

        if detail["price_now"] <= 0 or detail["discount_pct"] >= 100:
            skipped_ids.add(game_id)  # 100% знижка / включено в підписку - не "купівля зі знижкою"
            continue

        if is_adult(detail["genre_text"]):
            skipped_ids.add(game_id)
            continue

        popular = looks_popular_enough(detail)
        if not popular:
            popular = ask_claude_is_popular(detail["title"], detail["publisher"])

        if not popular:
            skipped_ids.add(game_id)
            continue

        post_text = generate_post_text(detail)

        ok = send_to_telegram(post_text, detail["image_url"])
        if ok:
            print(f"Опубліковано: {detail['title']}")
            posted_ids.add(game_id)
            posted_this_run += 1
        else:
            print(f"Не вдалось опублікувати: {detail['title']}", file=sys.stderr)
            skipped_ids.add(game_id)

        state["posted_ids"] = list(posted_ids)
        state["skipped_ids"] = list(skipped_ids)
        save_state(state)

    print(f"Готово. Опубліковано нових постів: {posted_this_run}")


if __name__ == "__main__":
    main()
