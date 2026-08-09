"""
discount_bot.py

Рубрика "Найнижча ціна за увесь час" для каналу "Синдром Гравця".

Розрахований на РІДКІ запуски (раз на ~14-16 днів, коли в PS Store оновлюється хвиля
акцій) - за один запуск бере ВСІ нові ігри, що проходять фільтр (до MAX_POSTS_PER_RUN
штук), і публікує їх одним заходом. Наступного разу вже опубліковані/перевірені ігри
пропускаються.

Що робить:
1. Бере список ігор зі знижками на https://psprices.com/region-ua/collection/lowest-prices-ever
   (кожна гра в цій добірці зараз коштує стільки, скільки НІКОЛИ раніше не коштувала).
2. Пропускає ігри, які вже публікувались або вже точно визнані "непопулярними" (дедуп
   у discount_state.json). Технічні збої парсингу НЕ позначають гру назавжди пропущеною -
   такі ігри просто спробуються ще раз наступного запуску.
3. Для кожної нової гри йде на її сторінку psprices.com і бере:
   - офіційну обкладинку гри (пряме посилання на CDN PlayStation),
   - оцінки Metacritic / OpenCritic (показник того, наскільки гра відома),
   - жанр (щоб відсіяти Adult-контент), видавця, ціни, знижку, дату закінчення акції,
   - список вмісту (якщо це бандл/видання з кількох ігор чи сюжетних DLC).
4. Залишає тільки більш-менш популярні / хайпові ігри:
   - або Metacritic, або OpenCritic оцінка є і становить 70+,
   - або (якщо оцінок немає чи вони нижчі) питає Claude, чи це все одно відома/хайпова гра.
5. Для гри, що пройшла фільтр, додатково заходить на офіційну сторінку store.playstation.com
   (посилання з psprices.com веде саме туди) і бере звідти офіційну англійську/локалізовану
   назву та канонічне посилання - бо назви на psprices.com часто перекладені українською
   не так, як у самому PS Store.
6. Просить Claude написати текст поста в стилі каналу (з готовими фактами, включно з
   відфільтрованим списком вмісту бандла).
7. Публікує пост (фото + підпис) у приватний Telegram-канал-чернетку для знижок.
8. Зупиняється, коли опубліковано MAX_POSTS_PER_RUN постів за цей запуск.

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

UKRAINIAN_MONTHS_GENITIVE = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня", 5: "травня", 6: "червня",
    7: "липня", 8: "серпня", 9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}

STYLE_GUIDE = """
Ти пишеш пост для рубрики "Найнижча ціна за увесь час" у Telegram-каналі "Синдром Гравця"
(PS Store знижки, PS Plus, Sony/PlayStation новини).

ОБОВ'ЯЗКОВИЙ формат, три рядки (порожній рядок між ними), більше нічого не додавай:

🔥Найнижча ціна в PlayStation Store за увесь час!

[Назва](ПОСИЛАННЯ) *(вміст, якщо це бандл/видання з кількох ігор)* за X UAH замість Y UAH.

Це найбільша знижка Z% з моменту релізу [гри/бандлу], дійсна до ДАТА.

Приклади (наслідуй буквально цю структуру і тон):

🔥Найнижча ціна в PlayStation Store за увесь час!

[NieR: Automata Game of the YoRHa Edition](https://store.playstation.com/uk-ua/product/EP0082-CUSA04480_00-GOTYORHADIGITAL0) за 479,60 UAH замість 1199,00 UAH.

Це найбільша знижка 60% з моменту релізу гри, дійсна до 1 липня.

---

🔥Найнижча ціна в PlayStation Store за увесь час!

[The Yakuza Complete Series](https://store.playstation.com/uk-ua/product/JP0177-PPSA31334_00-YAKUZACOMPLETE00) *(Yakuza 0 Director's Cut, Yakuza Kiwami, Yakuza Kiwami 2, Yakuza 3 Remastered, Yakuza 4 Remastered, Yakuza 5 Remastered, Yakuza 6: The Song of Life)* за 1599,50 UAH замість 3199,00 UAH.

Це найбільша знижка 50% з моменту релізу бандлу, дійсна до 15 липня.

---

🔥Найнижча ціна в PlayStation Store за увесь час!

[Indiana Jones And The Great Circle Premium Edition](https://store.playstation.com/uk-ua/product/UP1003-PPSA26786_00-PREMIUMEDITION00) *(основна гра + DLC)* за 1499,50 UAH замість 2999,00 UAH.

Це найбільша знижка 50% з моменту релізу гри, дійсна до 12 серпня.

Правила:
- Все українською, крім власних назв ігор/видань - вони завжди англійською, повністю,
  так само як офіційно називаються в PlayStation Store (без скорочень: "Grand Theft Auto V",
  а не "GTA 5"; "Gold Edition"/"Complete Edition"/"Deluxe Edition" пишуться повністю англійською
  як частина назви, а не перекладаються).
- Заголовок "🔥Найнижча ціна в PlayStation Store за увесь час!" - БЕЗ пробілу між емодзі і
  текстом, з знаком оклику, без крапки в кінці.
- Назва - посилання у форматі Markdown [Назва](ПОСИЛАННЯ) на офіційну сторінку PlayStation
  Store. НЕ використовуй HTML-теги на кшталт <a href="...">.
- Якщо до складу видання входить щось більше за просто саму гру - одразу після назви додай
  курсивом у дужках короткий опис вмісту:
  * Якщо це колекція/бандл з кількох ОКРЕМИХ ігор (кожна має свою назву) - перелічуй їх
    поіменно: *(Гра 1, Гра 2, Гра 3)*.
  * Якщо це Premium/Deluxe/Gold/Complete/Ultimate/Digital Deluxe Edition ОДНІЄЇ гри, куди
    входить сама гра плюс сюжетне DLC/season pass/доповнення - пиши узагальнено: якщо з
    наданих фактів зрозуміла ТОЧНА кількість DLC - вкажи число, напр. *(основна гра + 2 DLC)*;
    якщо кількість невідома чи неточна - просто *(основна гра + DLC)* без числа. Точні назви
    самих DLC тут не потрібні, на відміну від колекції з кількох ігор вище.
  * НІКОЛИ не згадуй косметичні набори, скіни, аватари, звукові доріжки, артбуки, ігрову
    валюту чи інший непринциповий бонусний контент. Якщо єдиний додатковий вміст - щось
    подібне (напр. лише аватари) - взагалі не додавай дужки, ніби це звичайна одна гра.
- Ціни: без розділювача тисяч, кома замість крапки (напр. 599,80 UAH, а не 599.80 UAH).
- PS5/PS4/PS5 Pro завжди скорочено (ніколи "PlayStation 5"), PS Plus не повною назвою.
- Ніяких запитань у тексті - тільки констатація факту.
- Ніяких власних висновків чи прогнозів - тільки факти з наданих даних.
- Без трикрапки в кінці.
- Останнє речення ЗАВЖДИ має вигляд: "Це найбільша знижка Z% з моменту релізу гри/бандлу,
  дійсна до ДАТА." - дата вже буде надана в готовому українському форматі (напр. "1 липня"),
  просто встав її як є. Це речення ніколи не можна пропускати чи скорочувати.
- Виведи ЛИШЕ готовий текст поста у форматі Telegram Markdown (parse_mode=Markdown):
  [текст](посилання) для лінків, *текст* для виділення дужок з вмістом бандла. Без
  пояснень від себе, без обгортки у потрійні лапки ```.
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
            print(f"[fetch_candidates] сторінка {page} не завантажилась, зупиняюсь")
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


# ---------- Крок 2: деталі гри (psprices.com) ----------

BUY_BLOCK_RE = re.compile(r'href="([^"]*?/game/buy/\d+)"[^>]*>(.*?)</a>', re.DOTALL)
PRICE_BLOCK_RE = re.compile(
    r"([\d][\d\s]*(?:,\d{2})?)\s*₴.*?trending_down\s*(\d+)\s*%\s*([\d][\d\s]*(?:,\d{2})?)\s*₴"
)
OPENCRITIC_RE = re.compile(r'href="https://opencritic\.com/game/[^"]*"[^>]*>(.*?)</a>', re.DOTALL)
METACRITIC_RE = re.compile(r'href="https://www\.metacritic\.com/game/[^"]*"[^>]*>(.*?)</a>', re.DOTALL)
# наступні регулярки застосовуються НЕ до сирого HTML, а до тексту з уже прибраними тегами
RELEASE_RE = re.compile(r"Release date\s+([A-Za-z]{3,4}\.?\s\d{1,2},\s\d{4})")
PUBLISHER_RE = re.compile(
    r"Publisher\s+(.+?)\s+(?:Download size|What's included|Optimization|Ratings|Audio|Genres|Trophies)"
)
GENRES_RE = re.compile(r"Genres\s+(.+?)\s+(?:Local co-op|Playtime|Trophies|Also known as)")
WHATS_INCLUDED_RE = re.compile(
    r"What's included\s+(.+?)\s+(?:Optimization|Ratings|Audio|Genres|Trophies|Video|Subtitles|Download size)"
)


def _clean_number(text):
    return text.replace("\xa0", " ").replace(" ", "").replace(",", ".")


def _extract_score(pattern, html):
    m = pattern.search(html)
    if not m:
        return None
    digits = re.sub(r"<[^>]+>", "", m.group(1))
    digits = re.search(r"\d+", digits)
    return int(digits.group(0)) if digits else None


def _parse_expiry(html, block_text):
    """Пробує кілька форматів дати закінчення акції, включно зі структурованими
    даними schema.org (JSON-LD), якщо видимий текст не збігся."""
    for pattern in (
        r"until\s*(\d{1,2})/(\d{1,2})/(\d{4})",   # MM/DD/YYYY
        r"until\s*(\d{4})-(\d{1,2})-(\d{1,2})",   # YYYY-MM-DD
    ):
        m = re.search(pattern, block_text)
        if m:
            g = m.groups()
            if len(g[0]) == 4:  # YYYY-MM-DD
                year, month, day = int(g[0]), int(g[1]), int(g[2])
            else:  # MM/DD/YYYY
                month, day, year = int(g[0]), int(g[1]), int(g[2])
            return day, month, year

    m = re.search(r'"priceValidUntil"\s*:\s*"(\d{4})-(\d{2})-(\d{2})', html)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return day, month, year

    return None


def format_ukrainian_date(parsed):
    if not parsed:
        return None
    day, month, _year = parsed
    month_name = UKRAINIAN_MONTHS_GENITIVE.get(month)
    if not month_name:
        return None
    return f"{day} {month_name}"


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
    price_now = price_old = discount_pct = ends_date = None
    buy_url = None
    if buy_match:
        raw_buy_path = buy_match.group(1)
        buy_url = raw_buy_path if raw_buy_path.startswith("http") else "https://psprices.com" + raw_buy_path
        block_text = re.sub(r"<[^>]+>", " ", buy_match.group(2))
        block_text = re.sub(r"\s+", " ", block_text).strip()
        price_match = PRICE_BLOCK_RE.search(block_text)
        if price_match:
            price_now = float(_clean_number(price_match.group(1)))
            discount_pct = int(price_match.group(2))
            price_old = float(_clean_number(price_match.group(3)))
        ends_date = _parse_expiry(html, block_text)

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

    included_match = WHATS_INCLUDED_RE.search(page_text)
    included_text = included_match.group(1).strip() if included_match else None

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
        "ends_date": ends_date,
        "included_text": included_text,
    }


def is_adult(genre_text):
    return any(g in genre_text for g in IGNORE_GENRES)


# ---------- Крок 3: офіційна сторінка PlayStation Store (англійська назва) ----------

def _fetch_og_title(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    except requests.RequestException as e:
        print(f"[_fetch_og_title] помилка запиту {url}: {e}")
        return None, None
    if resp.status_code != 200:
        print(f"[_fetch_og_title] HTTP {resp.status_code} для {url}")
        return None, None
    title_match = re.search(r'property="og:title" content="([^"]*)"', resp.text)
    title = title_match.group(1) if title_match else None
    if title:
        title = re.split(r"\s*\|\s*PlayStation", title)[0].strip()
    return title, resp.url


def fetch_official_store_info(psprices_buy_url):
    """psprices_buy_url веде через редирект на справжню store.playstation.com сторінку.
    Посилання для поста лишаємо українське (той регіон, де купують читачі), а от назву
    беремо з АНГЛІЙСЬКОЇ версії тієї ж сторінки товару - бо український стор Sony теж
    часто показує локалізовану назву (напр. "Брама Балдура 3"), а нам треба офіційну
    англійську, як у прикладах."""
    _ignored_title, uk_url = _fetch_og_title(psprices_buy_url)
    if not uk_url:
        return None, None

    en_url = re.sub(r"/uk-ua/", "/en-us/", uk_url, count=1)
    en_title, _ = _fetch_og_title(en_url) if en_url != uk_url else (None, None)

    return en_title, uk_url


# ---------- Крок 4: фільтр популярності ----------

def _extract_text(resp):
    """claude-sonnet-5 інколи повертає блок 'роздумів' (thinking) першим у відповіді,
    тому не можна покладатись на resp.content[0] - треба знайти саме текстовий блок."""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


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
        + (f" (видавець: {publisher})" if publisher else "")
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
    answer = _extract_text(resp).strip().lower()
    return answer.startswith("так")


# ---------- Крок 5: генерація тексту поста ----------

def format_price(value):
    if value is None:
        return "?"
    return f"{value:.2f}".replace(".", ",")


def generate_post_text(detail, final_title, final_url, formatted_date):
    facts = (
        f"Назва: {final_title}\n"
        f"Посилання на PlayStation Store: {final_url}\n"
        f"Поточна ціна: {format_price(detail['price_now'])} UAH\n"
        f"Ціна без знижки: {format_price(detail['price_old'])} UAH\n"
        f"Знижка: {detail['discount_pct']}%\n"
        f"Це офіційно найнижча ціна за весь час спостережень.\n"
        f"Видавець: {detail['publisher'] or 'невідомо'}\n"
        f"Дійсна до (вже у форматі для вставки): {formatted_date or 'дата невідома - не вигадуй, просто опусти цю частину, якщо справді невідома'}\n"
        f"Сирий список вмісту (якщо є) - сам вирішуй, що з цього справжній вміст бандла "
        f"(ігри/сюжетні DLC), а що просто бонуси, які не варто перелічувати: "
        f"{detail['included_text'] or '(даних немає, це не бандл)'}\n"
    )
    resp = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        system=STYLE_GUIDE,
        messages=[{"role": "user", "content": facts}],
    )
    return _extract_text(resp).strip()


# ---------- Крок 6: публікація в Telegram ----------

def send_to_telegram(caption, image_url):
    api_url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
    payload = {
        "chat_id": DRAFT_CHAT_ID,
        "photo": image_url,
        "caption": caption,
        "parse_mode": "Markdown",
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
                "parse_mode": "Markdown",
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

        # технічні збої НЕ позначають гру назавжди пропущеною - просто спробуємо ще раз
        # наступного запуску (могло бути тимчасове мережеве глюкало, або баг, який ми
        # ще виправимо)
        if not detail or not detail["image_url"] or not detail["buy_url"]:
            print(f"[main] неповні дані для {c['url']}, спробую ще раз наступного разу")
            continue

        if detail["price_now"] is None or detail["discount_pct"] is None:
            print(f"[main] не вдалось розпарсити ціну для {detail['title']}, спробую ще раз наступного разу")
            continue

        # а ось ці причини - вже остаточні, гру більше перевіряти не треба
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

        # для фінальних кандидатів беремо офіційну назву й посилання зі store.playstation.com
        official_title, official_url = fetch_official_store_info(detail["buy_url"])
        final_title = official_title or detail["title"]
        final_url = official_url or detail["buy_url"]
        formatted_date = format_ukrainian_date(detail["ends_date"])

        post_text = generate_post_text(detail, final_title, final_url, formatted_date)

        ok = send_to_telegram(post_text, detail["image_url"])
        if ok:
            print(f"Опубліковано: {final_title}")
            posted_ids.add(game_id)
            posted_this_run += 1
        else:
            print(f"Не вдалось опублікувати: {final_title}", file=sys.stderr)
            # тут теж не позначаємо остаточно пропущеною - могла бути тимчасова помилка Telegram

        state["posted_ids"] = list(posted_ids)
        state["skipped_ids"] = list(skipped_ids)
        save_state(state)

    print(f"Готово. Опубліковано нових постів: {posted_this_run}")


if __name__ == "__main__":
    main()
