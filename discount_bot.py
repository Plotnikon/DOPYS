"""
discount_bot.py

Рубрика "Найнижча ціна за увесь час" для каналу "Синдром Гравця".

Розрахований на РІДКІ запуски (раз на ~14-16 днів, коли в PS Store оновлюється хвиля
акцій) - за один запуск бере ВСІ нові ігри, що проходять фільтр (до MAX_POSTS_PER_RUN
штук), і публікує їх одним заходом. Наступного разу вже опубліковані ігри пропускаються.

Що робить:
1. Бере список ігор зі знижками на https://psdeals.net/ua-store/collection/cheaper_than_ever
   (ці ігри зараз мають найнижчу ціну в PlayStation Store за весь час спостережень сайту).
2. Пропускає ігри, які вже публікувались або вже перевірялись раніше (дедуп у discount_state.json).
3. Залишає тільки більш-менш популярні / хайпові ігри:
   - або має 2000+ відгуків у PlayStation Store,
   - або (для менш зареитингованих, у т.ч. інді) питає Claude, чи це відома/хайпова гра.
4. Для кожної відібраної гри йде на її сторінку на psdeals.net і бере:
   - офіційну обкладинку гри (те саме зображення, що і в PlayStation Store),
   - пряме посилання "Buy at PlayStation Store",
   - жанр (щоб відсіяти Adult-контент),
   - ціни, знижку, дату закінчення акції.
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

LIST_URL = "https://psdeals.net/ua-store/collection/cheaper_than_ever"
PAGES_TO_CHECK = 15          # запуски рідкісні (раз на ~14-16 днів) - переглядаємо список глибше
POPULARITY_THRESHOLD = 2000  # мін. к-сть відгуків PS Store, щоб вважати гру популярною без питання Claude
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

CARD_RE = re.compile(
    r'href="(/ua-store/game/(\d+)/[^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)


def fetch_candidates():
    """Повертає список {id, url, raw_text} з перших кількох сторінок списку знижок."""
    candidates = []
    seen_ids = set()
    for page in range(1, PAGES_TO_CHECK + 1):
        url = LIST_URL if page == 1 else f"{LIST_URL}/{page}"
        resp = requests.get(url, headers=HEADERS, params={"sort": "best-new-deals"}, timeout=30)
        print(f"[fetch_candidates] сторінка {page}: HTTP {resp.status_code}, {len(resp.text)} байт")
        if resp.status_code != 200:
            print(f"[fetch_candidates] сторінка {page} не завантажилась, зупиняюсь")
            break
        page_matches = 0
        blocked_markers = ("Just a moment", "cf-browser-verification", "Enable JavaScript", "Access denied")
        if any(marker in resp.text for marker in blocked_markers):
            print(f"[fetch_candidates] схоже на блокування/захист від ботів на сторінці {page}, пропускаю")
            continue
        for match in CARD_RE.finditer(resp.text):
            path, game_id, inner_html = match.groups()
            if game_id in seen_ids:
                continue
            seen_ids.add(game_id)
            page_matches += 1
            raw_text = re.sub(r"<[^>]+>", " ", inner_html)
            raw_text = re.sub(r"\s+", " ", raw_text).strip()
            candidates.append(
                {
                    "id": game_id,
                    "url": "https://psdeals.net" + path,
                    "raw_text": raw_text,
                }
            )
        print(f"[fetch_candidates] сторінка {page}: знайдено карток ігор: {page_matches}")
        if page_matches == 0:
            break  # далі, ймовірно, порожні сторінки - список скінчився
    return candidates


def parse_candidate_stats(raw_text):
    """Витягує зі зведеного тексту картки: знижку %, поточну ціну (UAH, схема, без роздільників),
    рейтинг та к-сть відгуків (можуть бути відсутні)."""
    discount_match = re.search(r"-(\d+)%", raw_text)
    price_match = re.search(r"([\d.]+)\s*UAH\b", raw_text)
    rating_count_match = re.search(r"(\d\.\d{1,2})\s+(\d+)\s*$", raw_text)

    discount_pct = int(discount_match.group(1)) if discount_match else None
    price_now = float(price_match.group(1)) if price_match else None
    rating_count = int(rating_count_match.group(2)) if rating_count_match else None

    return {
        "discount_pct": discount_pct,
        "price_now": price_now,
        "rating_count": rating_count,
    }


# ---------- Крок 2: фільтр популярності ----------

def looks_popular_enough(rating_count):
    return rating_count is not None and rating_count >= POPULARITY_THRESHOLD


def ask_claude_is_popular(title):
    """Для ігор без достатньої к-сті відгуків - питаємо Claude, чи це відома/хайпова гра
    (в т.ч. інді-хіти), навіть якщо офіційних відгуків мало."""
    prompt = (
        f'Гра називається "{title}". Чи є ця гра достатньо відомою/популярною/хайповою '
        "серед геймерів PlayStation, щоб про знижку на неї варто було написати пост "
        "у геймерському Telegram-каналі? Враховуй і AAA-тайтли, і відомі інді-хіти. "
        'Відповідай ЛИШЕ одним словом: "так" або "ні".'
    )
    resp = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    answer = resp.content[0].text.strip().lower()
    return answer.startswith("так")


# ---------- Крок 3: деталі гри ----------

def fetch_game_detail(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return None
    html = resp.text

    def meta(name):
        m = re.search(rf'property="{name}" content="([^"]*)"', html)
        return m.group(1) if m else None

    title = meta("og:title") or ""
    title = re.sub(r"\s*—\s*buy online\s*—\s*PS Deals.*$", "", title)
    title = re.sub(r"^\d+%\s*discount on\s*", "", title, flags=re.IGNORECASE)
    title = title.strip()

    image_url = meta("og:image")

    buy_match = re.search(r'href="(https://store\.playstation\.com/[^"]+)"', html)
    buy_url = buy_match.group(1) if buy_match else url

    genre_match = re.search(r"Genre:.*?</strong>\s*(.*?)</p", html, re.DOTALL)
    genre_text = re.sub(r"<[^>]+>", " ", genre_match.group(1)) if genre_match else ""

    lowest_price_match = re.search(r"Lowest price.*?([\d.,]+)\s*₴", html, re.DOTALL)
    lowest_price = lowest_price_match.group(1) if lowest_price_match else None

    ends_match = re.search(r"Ends:\s*([A-Za-z]+ \d{1,2}, \d{4})", html)
    ends_text = ends_match.group(1) if ends_match else None

    save_match = re.search(r"SAVE:\s*(\d+)%", html)
    discount_pct = int(save_match.group(1)) if save_match else None

    return {
        "title": title,
        "image_url": image_url,
        "buy_url": buy_url,
        "genre_text": genre_text.lower(),
        "lowest_price": lowest_price,
        "ends_text": ends_text,
        "discount_pct": discount_pct,
    }


def is_adult(genre_text):
    return any(g in genre_text for g in IGNORE_GENRES)


# ---------- Крок 4: генерація тексту поста ----------

def generate_post_text(detail, price_now, price_old):
    facts = (
        f"Назва: {detail['title']}\n"
        f"Посилання на PlayStation Store: {detail['buy_url']}\n"
        f"Поточна ціна: {price_now}\n"
        f"Ціна без знижки: {price_old}\n"
        f"Знижка: {detail['discount_pct']}%\n"
        f"Найнижча ціна за весь час (за даними трекера): {detail['lowest_price']}\n"
        f"Діє до: {detail['ends_text']}\n"
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

        stats = parse_candidate_stats(c["raw_text"])

        if stats["price_now"] is not None and stats["price_now"] <= 0:
            continue  # 100% знижка / включено в PS+ - це не "купівля зі знижкою"
        if stats["discount_pct"] is not None and stats["discount_pct"] >= 100:
            continue

        popular = looks_popular_enough(stats["rating_count"])
        if not popular:
            # для оцінки популярності беремо чорнову назву з тексту картки
            rough_title = re.sub(r"^UA\s+", "", c["raw_text"]).split(" -")[0][:120]
            popular = ask_claude_is_popular(rough_title)

        if not popular:
            skipped_ids.add(game_id)
            continue

        detail = fetch_game_detail(c["url"])
        if not detail or not detail["image_url"] or not detail["buy_url"]:
            skipped_ids.add(game_id)
            continue

        if is_adult(detail["genre_text"]):
            skipped_ids.add(game_id)
            continue

        price_now_text = f"{stats['price_now']:.2f}".replace(".", ",") if stats["price_now"] else "?"
        # стара ціна не завжди легко дістати окремо зі списку - рахуємо з відсотка знижки, якщо можливо
        price_old_text = "?"
        if stats["price_now"] and detail["discount_pct"]:
            price_old = stats["price_now"] / (1 - detail["discount_pct"] / 100)
            price_old_text = f"{price_old:.2f}".replace(".", ",")

        post_text = generate_post_text(detail, f"{price_now_text} UAH", f"{price_old_text} UAH")

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
