"""
thisday_bot.py

Рубрика "Цього дня" для каналу "Синдром Гравця".

Розрахований на РІДКІ запуски (1 числа кожного місяця) - за один запуск генерує пости
для КОЖНОГО дня місяця, що починається (1-28/30/31), про легендарні/культові ігри, які
офіційно вийшли (або ще вийдуть, якщо реліз вже заплановано саме на цей день цього ж
місяця/року - напр. наперед відомий реліз AAA-гри пізніше цього місяця) саме в цей день
місяця (в будь-якому році). Дні без по-справжньому гучних релізів просто пропускаються -
без порожніх постів. Публікація в реальний канал - вручну, в потрібний день, тож фрази на
кшталт "відбувся реліз" встигають стати правдою до моменту публікації.

Формат поста (рівно одне речення, без нічого зайвого):
🎉Цього дня, 26 січня 2010 року, відбувся реліз Mass Effect 2.

Якщо в один день вийшло кілька культових ігор (в різні роки) - усі йдуть ОДНИМ постом:
речення про кожну гру - окремим абзацом у тому самому підписі, а картинки - одним альбомом
(до 10 фото в одному пості - обмеження самого Telegram; якщо ігор більше 10, зайві підуть
наступним постом).

Що робить:
1. Отримує токен доступу до IGDB API (Twitch Client Credentials flow) - живе типово ~60 днів,
   тому просто отримуємо новий на кожен запуск, без кешування.
2. Для місяця, що починається, проходить по роках від START_YEAR до поточного і для кожного
   року запитує в IGDB (games endpoint) усі ОСНОВНІ ігри (не DLC/порт/ремастер/пак і т.д.) з
   датою релізу в цьому місяці цього року - включно з поточним роком/місяцем ЦІЛКОМ (навіть
   дні, які ще не настали, якщо реліз уже офіційно анонсовано на конкретну дату).
3. Групує знайдені ігри по дню місяця (день релізу).
4. Попередньо відсіює зовсім невідомі/нішеві ігри по метриках IGDB (рейтинг критиків/
   користувачів, кількість "фоловерів"/антісипації) - це лише грубий перший фільтр, щоб не
   ганяти Claude на явне сміття.
5. Крос-перевіряє дату релізу на gamefaqs.gamespot.com ("This Day in Gaming") - незалежна
   перевірка того, що гра справді вийшла саме в цей день/місяць (не помилка в IGDB). Якщо
   сайт недоступний чи парсинг зламався - перевірку просто пропускає, не блокуючи публікацію.
6. Питає Claude (два окремих питання): (а) чи це основна гра, а не DLC/доповнення/
   перевидання/збірка - цікавить лише ПЕРШИЙ оригінальний реліз; (б) чи це ДІЙСНО
   легендарна гра рівня номінантів The Game Awards / гри року свого випуску - високий
   рейтинг сам по собі НЕ доказ (той самий урок, що і з рубрикою знижок). Поріг навмисно
   дуже високий.
7. Для тих, хто пройшов усе - формує текст поста за жорстким шаблоном (без участі Claude,
   щоб формат ніколи не "поплив") і бере офіційний бокс-арт з IGDB у максимальній якості.
8. Групує ігри одного дня в один пост (альбом фото + один спільний підпис із реченням про
   кожну гру) і публікує в приватний Telegram-канал-чернетку, з паузою між постами, щоб не
   впертись у ліміти Telegram.
9. Дедуп (thisday_state.json, комітиться в репозиторій, як і в інших ботах): posted_ids -
   вже опубліковані ігри (ніколи не повторюються), skipped_ids - остаточно визнані
   "недостатньо легендарними"/DLC (якщо пізніше зміниш поріг - можна вручну очистити
   skipped_ids в файлі на GitHub, щоб їх переоцінили за новою логікою).

Секрети, які потрібні в GitHub Actions (Settings -> Secrets and variables -> Actions):
- TG_TOKEN             (той самий токен бота, що і в rss_bot.py/discount_bot.py)
- THISDAY_TG_CHAT      (chat_id каналу-чернетки для рубрики "цього дня")
- IGDB_CLIENT_ID       (з Twitch Developer Console)
- IGDB_CLIENT_SECRET   (з Twitch Developer Console)
- ANTHROPIC_API_KEY    (той самий ключ, що і в інших ботах)
"""

import calendar
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from anthropic import Anthropic

# ---------- Налаштування ----------

TG_TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["THISDAY_TG_CHAT"]
IGDB_CLIENT_ID = os.environ["IGDB_CLIENT_ID"]
IGDB_CLIENT_SECRET = os.environ["IGDB_CLIENT_SECRET"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

CLAUDE_MODEL = "claude-sonnet-5"

STATE_FILE = "thisday_state.json"

START_YEAR = 1980            # з якого року шукати релізи
MAX_POSTS_PER_RUN = 40        # запобіжник (орієнтовно ~30-31 пост за місяць, з запасом)
IGDB_MAIN_GAME_CATEGORY = 0   # у IGDB: 0 = основна гра (не DLC/порт/ремастер/пак тощо)

# грубий перший фільтр по метриках IGDB - лише щоб не ганяти Claude на явне ноунейм сміття,
# НЕ підстава для автоматичного схвалення (фінальне рішення завжди за Claude)
MIN_TOTAL_RATING = 65
MIN_AGGREGATED_RATING = 65
MIN_FOLLOWS = 150
MIN_HYPES = 30

HEADERS_HTML = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

UKRAINIAN_MONTHS_GENITIVE = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня", 5: "травня", 6: "червня",
    7: "липня", 8: "серпня", 9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}

GAMEFAQS_URL = "https://gamefaqs.gamespot.com/games/thisdayingaming"

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


# ---------- Крок 1: авторизація в IGDB (через Twitch) ----------

def get_igdb_token():
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": IGDB_CLIENT_ID,
            "client_secret": IGDB_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def igdb_query(token, endpoint, body):
    """Запит до IGDB API (Apicalypse-синтаксис у тілі запиту)."""
    resp = requests.post(
        f"https://api.igdb.com/v4/{endpoint}",
        headers={
            "Client-ID": IGDB_CLIENT_ID,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "text/plain",
        },
        data=body.encode("utf-8"),
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"[igdb_query] {endpoint}: HTTP {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        return []
    return resp.json()


# ---------- Крок 2: збір ігор для місяця, що починається ----------

def fetch_games_for_month(token, target_month):
    """Повертає {день_місяця: [ігри]} - для кожного року від START_YEAR до поточного
    запитує в IGDB усі ОСНОВНІ ігри (не DLC/порти/ремастери) з датою релізу в цьому місяці
    цього року, і групує їх по дню релізу. Включає й ще не вийшлі, але вже заплановані
    релізи поточного місяця/року (див. примітку нижче)."""
    now = datetime.now(timezone.utc)
    current_year = now.year

    games_by_day = {}
    seen_game_ids = set()

    for year in range(START_YEAR, current_year + 1):
        month_start = datetime(year, target_month, 1, tzinfo=timezone.utc)
        if target_month == 12:
            month_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            month_end = datetime(year, target_month + 1, 1, tzinfo=timezone.utc)

        # ПРИМІТКА: current_year НЕ обрізається до "зараз" - навмисно. Пости готуються
        # заздалегідь пачкою на весь місяць (і вже вийшли, і ще заплановані релізи цього ж
        # місяця, напр. "15 вересня вийде Marvel's Wolverine") - Денис публікує їх вручну
        # у відповідний день, тож до моменту публікації дата вже настане.
        if month_start >= month_end:
            continue

        offset = 0
        while True:
            body = f"""
                fields name, first_release_date, cover.image_id, aggregated_rating,
                       total_rating, follows, hypes,
                       involved_companies.company.name, involved_companies.developer,
                       involved_companies.publisher, category, parent_game;
                where first_release_date >= {int(month_start.timestamp())}
                    & first_release_date < {int(month_end.timestamp())};
                limit 500;
                offset {offset};
            """
            # ПРИМІТКА: фільтр "category = 0" (основна гра) навмисно НЕ в самому запиті -
            # у IGDB є відомий баг/особливість: 0 - це "нульове" значення enum, і
            # "where category = 0" на боці сервера мовчки не знаходить нічого (перевірено:
            # інші фільтри працюють, саме "category = 0" повертав 0 рядків завжди).
            # Тому фільтруємо по category локально, вже після отримання даних.
            results = igdb_query(token, "games", body)
            for g in results:
                game_id = g.get("id")
                if game_id is None or game_id in seen_game_ids:
                    continue
                seen_game_ids.add(game_id)
                if g.get("category") not in (None, IGDB_MAIN_GAME_CATEGORY):
                    continue  # DLC/порт/ремастер/пак тощо - не основна гра
                if g.get("parent_game") is not None:
                    # у DLC/доповнень/експаншенів в IGDB майже завжди заповнене parent_game
                    # (посилання на базову гру) - НАВІТЬ якщо їхнє власне category чомусь
                    # не проставлене правильно (саме так пройшло Fallout 3: Mothership Zeta)
                    continue
                ts = g.get("first_release_date")
                if ts is None:
                    continue
                release_day = datetime.fromtimestamp(ts, tz=timezone.utc).day
                g["_year"] = datetime.fromtimestamp(ts, tz=timezone.utc).year
                games_by_day.setdefault(release_day, []).append(g)

            if len(results) < 500:
                break
            offset += 500

        time.sleep(0.3)  # не перевищувати ліміт запитів IGDB

    print(f"[fetch_games_for_month] місяць {target_month}: зібрано {len(seen_game_ids)} ігор за {current_year - START_YEAR + 1} років")
    return games_by_day


# деякі "Deluxe/Ultimate/Digital Deluxe Edition" виходять ТОГО Ж дня, що й основна гра
# (напр. Baldur's Gate III + Baldur's Gate 3: Digital Deluxe Edition) - Claude-перевірка на
# "перший оригінальний реліз" не завжди ловить це надійно (для видання, випущеного день-в-
# день з основною грою, це формально теж "перший реліз"). Тому - жорсткий, детерміністичний
# фільтр по назві: якщо в назві є "Edition"/"видання" - ніколи не постимо, постимо ЛИШЕ
# голу назву основної гри. Денис прямо попросив: "непотрібно писати про видання, тільки
# саму гру основну".
EDITION_TITLE_RE = re.compile(r"\bedition\b", re.IGNORECASE)


def looks_like_edition_variant(title):
    return bool(EDITION_TITLE_RE.search(title))


def passes_prefilter(game):
    """Грубий перший фільтр по метриках IGDB - лише щоб не витрачати виклики Claude на
    явно нішеві/невідомі ігри. НЕ підстава для автоматичного схвалення."""
    total_rating = game.get("total_rating")
    aggregated_rating = game.get("aggregated_rating")
    follows = game.get("follows")
    hypes = game.get("hypes")
    return (
        (total_rating is not None and total_rating >= MIN_TOTAL_RATING)
        or (aggregated_rating is not None and aggregated_rating >= MIN_AGGREGATED_RATING)
        or (follows is not None and follows >= MIN_FOLLOWS)
        or (hypes is not None and hypes >= MIN_HYPES)
    )


def extract_companies(game):
    names = []
    for ic in game.get("involved_companies") or []:
        company = (ic.get("company") or {}).get("name")
        if company and (ic.get("publisher") or ic.get("developer")):
            names.append(company)
    # унікалізуємо, зберігаючи порядок
    seen = set()
    result = []
    for n in names:
        if n not in seen:
            seen.add(n)
            result.append(n)
    return ", ".join(result) if result else None


# ---------- Крок 2.5: крос-перевірка дати релізу на gamefaqs.gamespot.com ----------

GF_LINK_RE = re.compile(r'<a href="/[a-z0-9\-]+/\d+-[^"]+">([^<]+)</a>')


def _normalize_title(title):
    t = title.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return t.strip()


def fetch_gamefaqs_titles_for_day(month, day):
    """Повертає множину нормалізованих назв ігор, які gamefaqs.gamespot.com показує в
    рубриці 'This Day in Gaming' саме для цього дня/місяця (за всі роки одразу - рік у
    URL-параметрі не впливає на це, лише прив'язує день/місяць). Використовується як
    незалежна крос-перевірка дати релізу перед публікацією.

    Якщо сайт недоступний чи парсинг не спрацював (напр. сайт змінив розмітку) - повертає
    None, і крос-перевірку тоді просто ПРОПУСКАЄМО (не блокуємо публікацію через це - краще
    пропустити зайву перевірку, ніж помилково відсіяти реальні релізи через нестабільний
    парсинг стороннього сайту)."""
    year_for_url = 2024  # довільний невисокосний... точніше високосний рік (є 29 лютого) -
    # аби дата в URL була завжди валідною незалежно від місяця/дня
    date_str = f"{year_for_url}-{month:02d}-{day:02d}"
    try:
        resp = requests.get(f"{GAMEFAQS_URL}?date={date_str}", headers=HEADERS_HTML, timeout=20)
    except requests.RequestException as e:
        print(f"[fetch_gamefaqs_titles_for_day] помилка запиту: {e}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"[fetch_gamefaqs_titles_for_day] HTTP {resp.status_code}", file=sys.stderr)
        return None

    titles = GF_LINK_RE.findall(resp.text)
    normalized = {_normalize_title(t) for t in titles if t.strip()}
    if not normalized:
        print(
            "[fetch_gamefaqs_titles_for_day] 0 назв розпізнано - можливо, змінилась "
            "розмітка сторінки, пропускаю крос-перевірку для цього дня",
            file=sys.stderr,
        )
        return None
    return normalized


def is_confirmed_by_gamefaqs(title, gamefaqs_titles):
    """gamefaqs_titles=None означає, що крос-перевірку не вдалось виконати - тоді НЕ
    блокуємо публікацію через це (fail-open)."""
    if gamefaqs_titles is None:
        return True
    norm = _normalize_title(title)
    if not norm:
        return True
    return any(norm in gf or gf in norm for gf in gamefaqs_titles)


# ---------- Крок 3: перевірка "чи це дійсно легендарна гра" ----------

def _extract_text(resp):
    """claude-sonnet-5 інколи повертає блок 'роздумів' (thinking) першим у відповіді,
    тому не можна покладатись на resp.content[0]."""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def ask_claude_is_main_release(title, companies):
    """Друга лінія захисту від DLC/доповнень (окрім category+parent_game з IGDB, які
    іноді неправильно заповнені - саме так проскочив Fallout 3: Mothership Zeta). Питаємо
    Claude окремо й прямо, ще до перевірки на "легендарність" франшизи - бо приналежність
    до відомої франшизи НЕ означає, що конкретний товар є основною грою.

    Денис уточнив: цікавить СУТО перший, оригінальний реліз гри - НЕ колекції, збірки,
    remaster/remake, Definitive/Complete/Anniversary/Super Collection видання, і НЕ
    розширення для вже існуючої гри (навіть для великої франшизи)."""
    prompt = (
        f'Товар в базі ігор називається "{title}"'
        + (f" (розробник/видавець: {companies})" if companies else "")
        + ".\n\n"
        "Чи це ПЕРШИЙ, ОРИГІНАЛЬНИЙ реліз самостійної гри (не пізніше перевидання чи "
        "додаток до чогось іншого)?\n\n"
        "Відповідай \"ні\", якщо це:\n"
        "- DLC, доповнення/експаншен, сезонний пропуск, набір місій/косметики чи будь-який "
        "платний або безкоштовний додаток ДО іншої гри (напр. \"Fallout 3: Mothership Zeta\", "
        "\"Hearthstone: The Boomsday Project\");\n"
        "- пізніше перевидання/збірка/колекція/порт вже випущеної гри - Remaster, Remake, "
        "Definitive Edition, Complete Edition, Anniversary Edition, Super Collection, "
        "Trilogy тощо (напр. \"The Sims 2: Super Collection\") - цікавить ЛИШЕ дата "
        "ПЕРШОГО оригінального релізу самої гри, не пізніших перевидань.\n\n"
        "Якщо сумніваєшся - відповідай \"ні\". "
        'Відповідай ЛИШЕ одним словом: "так" або "ні".'
    )
    resp = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    answer = _extract_text(resp).strip().lower()
    return answer.startswith("так")


def ask_claude_is_legendary(title, year, companies):
    """Висока оцінка в IGDB - показник ЯКОСТІ, не впізнаваності. Багато нішевих ігор мають
    хороші оцінки, лишаючись невідомими широкій аудиторії (той самий урок, що і з рубрикою
    знижок). Тому фінальне рішення - завжди за цією перевіркою, незалежно від метрик IGDB.

    Поріг навмисно ДУЖЕ високий (за явним запитом Дениса): конкретний орієнтир - ігри рівня
    номінантів The Game Awards / визнані грою року свого випуску (для ігор до 2014-го, коли
    TGA ще не існувало - еквівалент: GOTY від великих видань типу IGN/GameSpot чи явний
    культовий консенсус). Приклади рівня: God of War, The Last of Us, ексклюзиви Sony,
    Fable. НЕ "популярна в своїй ніші" франшиза (Five Nights at Freddy's, Hearthstone) і НЕ
    одноразовий культовий інді-хіт без продовжень (The Stanley Parable, Undertale)."""
    prompt = (
        f'Гра називається "{title}", вийшла у {year} році'
        + (f" (розробник/видавець: {companies})" if companies else "")
        + ".\n\n"
        "Головний тест: чи ця гра номінувалась (в будь-якій категорії) або перемагала на "
        "The Game Awards, чи вважається грою року свого випуску (Game of the Year від "
        "провідних видань - IGN, GameSpot, Metacritic Users' Choice тощо)? Якщо гра вийшла "
        "до 2014 року (до заснування The Game Awards) - застосуй той самий стандарт до "
        "тодішніх церемоній/премій (напр. Spike Video Game Awards) або широкого визнання "
        "критиків як однієї з найкращих ігор того року.\n\n"
        "Додатковий орієнтир рівня (якщо не впевнений щодо номінацій, але гра явно цього "
        "калібру): God of War, The Last of Us, Uncharted, Horizon, Spider-Man, Gran Turismo "
        "(великі ексклюзиви Sony/PlayStation), Fable, Halo, Gears of War, Call of Duty, "
        "Grand Theft Auto, Red Dead Redemption, Assassin's Creed, Final Fantasy, Resident "
        "Evil, The Elder Scrolls, The Witcher, Zelda, Mario, Diablo, Elden Ring/Dark Souls, "
        "Cyberpunk 2077, Baldur's Gate 3, Minecraft, Fortnite.\n\n"
        "Відповідай \"ні\", якщо це:\n"
        "- гра, що НЕ номінувалась на головні нагороди року і не вважалась однією з "
        "найкращих ігор свого року випуску, навіть якщо комерційно дуже успішна чи відома "
        "у своїй ніші (напр. Five Nights at Freddy's, Hearthstone, FIFA/EA Sports FC, "
        "класичний Castlevania чи Mega Man);\n"
        "- одноразова культова чи навіть дуже відома інді-гра БЕЗ активної франшизи/сиквелів "
        "(напр. The Stanley Parable, Undertale, Braid, Journey) - якщо вона САМА не "
        "номінувалась на головні нагороди року;\n"
        "- гра середнього рівня відомості, нішева, чи відома тільки фанатам жанру, навіть "
        "з хорошими оцінками критиків;\n"
        "- франшиза, яка вже давно закрита/забута і нових ігор не випускає.\n\n"
        "Поріг НАВМИСНО дуже високий і вузький - беремо лише справжній найтоповіший рівень. "
        "Якщо сумніваєшся - відповідай \"ні\". "
        'Відповідай ЛИШЕ одним словом: "так" або "ні".'
    )
    resp = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    answer = _extract_text(resp).strip().lower()
    return answer.startswith("так")


# ---------- Крок 4: текст поста і картинка ----------

def format_ukrainian_date_full(day, month, year):
    month_name = UKRAINIAN_MONTHS_GENITIVE.get(month)
    return f"{day} {month_name} {year} року"


def build_post_text(title, day, month, year):
    # формат жорстко зашитий у коді (без участі Claude), щоб ніколи не "поплив"
    formatted_date = format_ukrainian_date_full(day, month, year)
    return f"🎉Цього дня, {formatted_date}, відбувся реліз {title}."


def get_cover_url(image_id):
    if not image_id:
        return None
    # t_original - максимальна доступна якість бокс-арту в IGDB
    return f"https://images.igdb.com/igdb/image/upload/t_original/{image_id}.jpg"


# ---------- Крок 5: публікація в Telegram ----------

MAX_MEDIA_GROUP_SIZE = 10  # ліміт Telegram на кількість фото в одному "альбомі"-пості


def send_to_telegram(caption, image_url):
    """Один пост - одна гра (фото + підпис). Використовується, якщо на день випала
    рівно 1 гра - у Telegram sendMediaGroup вимагає МІНІМУМ 2 елементи."""
    api_url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
    payload = {"chat_id": CHAT_ID, "photo": image_url, "caption": caption}
    resp = requests.post(api_url, json=payload, timeout=30)

    if resp.status_code == 429:
        retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
        print(f"[send_to_telegram] flood control, чекаю {retry_after}с")
        time.sleep(retry_after + 1)
        resp = requests.post(api_url, json=payload, timeout=30)

    if resp.status_code != 200 or not resp.json().get("ok"):
        fallback_url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        resp = requests.post(
            fallback_url,
            json={"chat_id": CHAT_ID, "text": caption},
            timeout=30,
        )
    return resp.status_code == 200 and resp.json().get("ok")


def send_media_group_to_telegram(caption, image_urls):
    """Кілька ігор одного дня - ОДИН пост з альбомом фото (до 10 штук), підпис із усіма
    реченнями йде під альбомом одним блоком (Telegram показує caption першого елемента
    як підпис усього альбому - тому підпис ставимо лише на перше фото)."""
    media = [{"type": "photo", "media": url} for url in image_urls]
    media[0]["caption"] = caption

    api_url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMediaGroup"
    payload = {"chat_id": CHAT_ID, "media": media}
    resp = requests.post(api_url, json=payload, timeout=30)

    if resp.status_code == 429:
        retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
        print(f"[send_media_group_to_telegram] flood control, чекаю {retry_after}с")
        time.sleep(retry_after + 1)
        resp = requests.post(api_url, json=payload, timeout=30)

    if resp.status_code != 200 or not resp.json().get("ok"):
        print(f"[send_media_group_to_telegram] не вдалось: HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        # запасний варіант - хоча б текстом, без фото (краще ніж нічого)
        fallback_url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        resp = requests.post(
            fallback_url,
            json={"chat_id": CHAT_ID, "text": caption},
            timeout=30,
        )
    return resp.status_code == 200 and resp.json().get("ok")


# ---------- main ----------

def main():
    state = load_state()
    posted_ids = set(state["posted_ids"])
    skipped_ids = set(state["skipped_ids"])
    save_state(state)  # гарантуємо, що файл стану існує з першого запуску

    target_month = datetime.now(timezone.utc).month
    days_in_month = calendar.monthrange(datetime.now(timezone.utc).year, target_month)[1]

    token = get_igdb_token()
    games_by_day = fetch_games_for_month(token, target_month)

    posted_this_run = 0  # рахуємо ІГРИ (не пости) - один пост може містити до 10 ігор

    for day in range(1, days_in_month + 1):
        if posted_this_run >= MAX_POSTS_PER_RUN:
            print(f"Досягнуто ліміту {MAX_POSTS_PER_RUN} ігор за прогін, зупиняюсь")
            break

        candidates = games_by_day.get(day, [])
        # у межах дня - в хронологічному порядку років
        candidates.sort(key=lambda g: g["_year"])

        if not candidates:
            continue

        # крос-перевірка дати релізу на gamefaqs.gamespot.com - один запит на весь день
        gamefaqs_titles = fetch_gamefaqs_titles_for_day(target_month, day)

        # спочатку відбираємо ВСІХ гідних кандидатів цього дня, і лише потім постимо -
        # щоб зібрати їх усіх в один пост (до MAX_MEDIA_GROUP_SIZE)
        approved = []  # [{"game_id", "title", "year", "image_url"}]

        for game in candidates:
            game_id = str(game["id"])
            if game_id in posted_ids or game_id in skipped_ids:
                continue

            if not passes_prefilter(game):
                continue  # технічно/метрично не пройшло - не позначаємо остаточно, спробуємо ще раз

            title = game.get("name")
            image_id = (game.get("cover") or {}).get("image_id")
            if not title or not image_id:
                continue  # без назви чи картинки постити нічого - спробуємо ще раз наступного разу

            if looks_like_edition_variant(title):
                print(f"[main] {title}: це видання/Edition, а не гола основна гра, пропускаю остаточно")
                skipped_ids.add(game_id)
                state["skipped_ids"] = list(skipped_ids)
                save_state(state)
                continue

            if not is_confirmed_by_gamefaqs(title, gamefaqs_titles):
                print(f"[main] {title}: не підтверджено на gamefaqs.gamespot.com за {day} число, пропускаю (спробую ще раз пізніше)")
                continue  # НЕ позначаємо остаточно - могло бути неточне співставлення назв

            year = game["_year"]
            companies = extract_companies(game)

            if not ask_claude_is_main_release(title, companies):
                print(f"[main] {title} ({year}): це DLC/доповнення, а не основна гра, пропускаю остаточно")
                skipped_ids.add(game_id)
                state["skipped_ids"] = list(skipped_ids)
                save_state(state)
                continue

            if not ask_claude_is_legendary(title, year, companies):
                print(f"[main] {title} ({year}): недостатньо легендарна гра, пропускаю остаточно")
                skipped_ids.add(game_id)
                state["skipped_ids"] = list(skipped_ids)
                save_state(state)
                continue

            approved.append({
                "game_id": game_id,
                "title": title,
                "year": year,
                "image_url": get_cover_url(image_id),
            })

        if not approved:
            continue

        # ділимо на пачки по MAX_MEDIA_GROUP_SIZE (запас на дуже гучні дні з >10 релізами)
        for i in range(0, len(approved), MAX_MEDIA_GROUP_SIZE):
            if posted_this_run >= MAX_POSTS_PER_RUN:
                break
            chunk = approved[i:i + MAX_MEDIA_GROUP_SIZE]

            caption = "\n\n".join(
                build_post_text(g["title"], day, target_month, g["year"]) for g in chunk
            )

            if len(chunk) == 1:
                ok = send_to_telegram(caption, chunk[0]["image_url"])
            else:
                ok = send_media_group_to_telegram(caption, [g["image_url"] for g in chunk])

            if ok:
                titles = ", ".join(f'{g["title"]} ({g["year"]})' for g in chunk)
                print(f"Опубліковано ({day} число, {len(chunk)} ігор): {titles}")
                for g in chunk:
                    posted_ids.add(g["game_id"])
                posted_this_run += len(chunk)
            else:
                print(f"Не вдалось опублікувати пост за {day} число", file=sys.stderr)
                # тимчасова помилка Telegram - не позначаємо остаточно, спробуємо ще раз

            state["posted_ids"] = list(posted_ids)
            state["skipped_ids"] = list(skipped_ids)
            save_state(state)

            time.sleep(1.5)  # пауза між постами, щоб не впертись у ліміти Telegram

    print(f"Готово. Опубліковано нових ігор: {posted_this_run}")


if __name__ == "__main__":
    main()
