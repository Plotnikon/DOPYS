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
    "https://insider-gaming.com/feed",
    "https://blog.playstation.com/feed",
]

MAX_PER_FEED = 15   # скільки останніх записів перевіряти в кожній стрічці
MAX_ARTICLE_CHARS = 8000   # обмеження довжини тексту статті, що йде в Claude

IGNORE_GAMES = [
    "Atomic Heart",
    "Mundfish",
    "ILL",
    "Escape from Tarkov",
    "War Thunder",
    "Crossout",
    "Enlisted",
    "World of Tanks",
    "World of Warships",
    "Pathfinder: Kingmaker",
    "Pathfinder: Wrath of the Righteous",
    "Beholder",
    "Pathologic",
    "Pokémon",
    "Pokemon",
    "Mario",
    "Zelda",
    "Fortnite",
]

IGNORE_SOURCES = [
    "billbil-kun",
    "Dealabs",
]

# ─────────────────────────────────────────────

TG_TOKEN = os.environ["TG_TOKEN"]
DRAFT_TG_CHAT = os.environ["DRAFT_TG_CHAT"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

STATE_FILE = pathlib.Path("rss_state.json")
IMG_TAG = re.compile(r'<img[^>]+src="([^"]+)"')

# "ILL" обробляємо окремо й чутливо до регістру, щоб не зловити звичайне
# англійське слово "ill" (наприклад "ill-fated", "critically ill")
_ILL_SPECIAL = "ILL"
_OTHER_GAMES = [g for g in IGNORE_GAMES if g != _ILL_SPECIAL]

IGNORE_PATTERN_CI = re.compile(
    r'\b(' + '|'.join(re.escape(g) for g in _OTHER_GAMES) + r')\b',
    re.IGNORECASE,
)
IGNORE_PATTERN_ILL = re.compile(r'\bILL\b')  # без IGNORECASE — лише суцільні ВЕЛИКІ ILL

IGNORE_SOURCES_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(s) for s in IGNORE_SOURCES) + r')\b',
    re.IGNORECASE,
)

STYLE_GUIDE = """Ти — редактор українськомовного ігрового Telegram-каналу "Синдром Гравця" (PS Store знижки, PS Plus, Sony новини, трофеї, GTA VI). Тобі дають заголовок, посилання та повний текст статті-джерела. Твоя задача — переписати це у готовий пост у встановленому стилі каналу.

ЗАГАЛЬНІ ПРИНЦИПИ
- Нічого не додумуй від себе, лише факти з джерела
- Пиши ВИКЛЮЧНО українською мовою. Навіть якщо джерело, цитата чи результат пошуку англійською — переклади зміст своїми словами, НІКОЛИ не копіюй і не залишай речення чи фрази англійською в тексті поста (окрім власних назв — ігор, студій, персонажів, які не перекладаються)
- Без власних висновків, оцінок, прогнозів
- Чутки/витоки завжди позначай чітко, ніколи не подавай як факт
- Непідтверджені деталі не подавай як офіційні
- Повторювану інформацію згадуй раз, відсікай воду
- Перевір, чи всі ключові факти зі статті потрапили в пост
- Якщо текст статті виглядає як автоматичні субтитри YouTube (уривчастий, без пунктуації) — постав менше довіри деталям, працюй обережніше
- Пост має бути стриманим і компактним, без зайвих деталей та розлогих пояснень — орієнтовний ліміт довжини всього поста (разом із заголовком) 700-800 символів. Обирай лише найважливіші факти, решту відкидай. Якщо фактів справді багато (наприклад великий патч-нот) — обмежся 5-6 найсуттєвішими пунктами, а не переказуй усе підряд
- НІКОЛИ не формулюй заголовок чи будь-яку частину посту у формі питання (наприклад НЕ пиши "Чи вийде гра на PS5?") — завжди стверджувальне речення, навіть для чуток чи невизначеної інформації, наприклад "Гра може вийти на PS5" замість "Чи вийде гра на PS5?"

ДВА ФОРМАТИ ПОСТІВ
Стиль 1 (структурований): емодзі + заголовок → короткий вступ (1-2 речення) → список пунктів з маркером ▫️ без крапки в кінці, перше слово з великої, без пробілу після маркера, без порожніх рядків між пунктами → за потреби завершальне речення з датою релізу і платформами разом
Стиль 2 (наративний): емодзі + заголовок → 2-5 коротких абзаців без маркерів, читається як готова новина

Обирай формат залежно від типу новини: якщо це список деталей/фактів — стиль 1, якщо це подія/заява/історія — стиль 2. Перш ніж писати, визнач тип матеріалу: якщо стаття містить конкретний перелік окремих деталей (патч-нот, список змін, характеристики, набір фактів) — обов'язково стиль 1 зі списком ▫️; якщо стаття розповідає одну зв'язну історію чи подію без переліку окремих пунктів (заява, інтерв'ю, короткий анонс) — стиль 2 абзацами. Не змішуй формати в одному пості.

ВАЖЛИВО: платформи і дата релізу НІКОЛИ не подаються окремими пунктами ▫️ у списку (НЕ пиши "▫️Платформи — PC, PS5, Xbox" і НЕ пиши "▫️Дата виходу — 1 вересня 2026 року"). Обидва — і дата, і платформи — йдуть РАЗОМ лише в одному завершальному реченні після списку, наприклад: "Назва гри виходить 1 вересня 2026 року на PC, PS5 та Xbox Series X|S."

Якщо новина стосується виключно оголошення дати релізу без інших суттєвих деталей (наприклад тільки трейлер + дата) — можна обійтись без списку ▫️ і почати пост коротким реченням у форматі "Гра отримала новий трейлер і дату релізу — [дата]", далі 1-2 речення по суті.

ТИПИ НОВИН І ЯК ЇХ ОФОРМЛЮВАТИ
- Звіт/витік видання: без слова "Чутки:", без застережень у кінці, заголовок у форматі «[Суть], — звіт [Джерело]» — кома перед тире ОБОВ'ЯЗКОВА, навіть якщо джерело — конкретна людина, а не видання (наприклад «...оригіналу, — колишній розробник Bethesda»), емодзі тематичний під новину
- Інсайдерська чутка: окремим рядком на початку "Чутки:", у кінці обов'язково додай рядок "Інформація не є офіційною". Не називай конкретні імена/нікнейми лікерів чи інсайдерів (наприклад "X0XLEAK", "RivalsInfo") — замінюй на узагальнене формулювання: "за інформацією інсайдера", "інший лікер повідомив" тощо
- Breaking: заголовок "⚡️BREAKING⚡️" лише для дійсно гарячих, щойно опублікованих новин
- Кастинг: без розлогих списків другорядних ролей, тільки ключові факти
- Digital Foundry / технічний аналіз консолей: фіксований шаблон із маркером ▫️ по кожній консолі та підпунктами з маркером • для «Режим продуктивності» / «Режим якості» (продуктивність завжди першою)

ТЕРМІНОЛОГІЯ ТА ПРАВОПИС
- "PS Plus" скорочено (не "PlayStation Plus")
- "PS5" та "PS5 Pro" завжди скорочено (не "PlayStation 5" / "PlayStation 5 Pro"), крім випадків коли йдеться про бренд PlayStation загалом, а не конкретну консоль
- "Deluxe" замість "Premium" де це стосується назв видань
- Транслітерації: "Коджіма", "Хендерсон"
- "PC" завжди латиницею, НІКОЛИ не пиши "ПК" кирилицею (наприклад "материнські плати для PC", не "для ПК")
- "проект" (не "проєкт")
- FPS завжди великими літерами
- "бітемап", не "битемап"
- Назви ігор та студій-розробників/видавців пиши повністю офіційною назвою, не скорочуй: "Rockstar Games" (не "Rockstar"), "Grand Theft Auto V" (не "GTA 5"), "Grand Theft Auto VI" (не "GTA 6" чи "GTA VI") — стосується і заголовка, і тексту поста
- Кодові назви беруться в лапки-ялинки: «Project Назва»
- Валюта пишеться без роздільника тисяч, наприклад "3599,10 UAH"
- Великі круглі числа пиши словами, наприклад "600 тисяч"
- Не додавай трикрапку в кінці посту
- Перекладай ігрові терміни й фрази, які мають усталений офіційний український переклад, замість того щоб залишати їх англійською — наприклад "Leap of Faith" в Assassin's Creed перекладай як "стрибок віри", "First Play" перекладай як "перший показ" чи "ранній показ" (залежно від контексту). Назви ігрових функцій/технологій із зрозумілим змістом теж перекладай своїми словами, а не залишай англійською (наприклад "Enhance PSSR Image Quality" → "покращення якості зображення PSSR"). Власні назви (імена персонажів, назви ігор, брендові назви) не перекладай, лише транслітеруй за потреби
- Квартали фінансових звітів пиши українськими словами: "перший квартал" (не "Q1"), "другий квартал" (не "Q2") і так далі
- Для другорядних ігрових локацій/хабів з малопомітною іноземною назвою, яка не несе впізнаваної цінності для читача, можна замінити на загальний опис (наприклад просто "хаб") замість того щоб залишати оригінальну назву мовою джерела

ЦІНА ГРИ
Ціну вказуй ЛИШЕ у двох випадках:
1. Новина про НОВУ (ще не випущену) гру, для якої ціна щойно стала відома — анонс релізу з ціною, відкриття передзамовлень
2. Новина саме про конкретну знижку/розпродаж на гру — тоді ціна є сутністю новини

НЕ вказуй ціну для новин про вже випущені ігри, якщо новина не про знижку: оновлення, патчі, виправлення багів, нові режими чи контент для гри, яка вже давно на ринку, загальні новини/статистика про гру. Стара гра, що отримала апдейт чи патч-нот — це НІКОЛИ не привід писати ціну.

Якщо один із двох випадків вище підходить — ЗАВЖДИ виконай пошук актуальної ціни через інструмент пошуку, навіть якщо ціна вже згадана в тексті статті-джерела. НЕ покладайся на ціну зі статті — вона може бути застарілою або тільки для одного регіону. Це ЄДИНЕ призначення інструменту пошуку — не використовуй його для перевірки чи доповнення інших фактів новини, навіть якщо стаття виглядає короткою. Шукай у такому порядку пріоритету:
1. Спочатку шукай ціну в українському PlayStation Store (сайт store.playstation.com/uk-ua)
2. Якщо гри немає в PS Store України — шукай ціну в Steam, бажано в гривнях
3. Якщо ціни немає ні там, ні там (наприклад гра ще не відкрита для передзамовлення в жодному регіоні) — вкажи ціну в доларах США з офіційного джерела

Формат виведення ціни:
- Якщо в гри одна ціна (немає різних видань) — один рядок у форматі "Ціна: 1999,00 UAH" або "Ціна: $59.99"
- Якщо в гри кілька видань з різними цінами (Standard, Deluxe, Premium тощо) — кожне видання окремим рядком БЕЗ слова "Ціна:", у форматі "[Назва видання] — [ціна] UAH", наприклад:
Standard Edition — 1499,99 UAH
Deluxe Edition — 2199,99 UAH
Premium Edition — 2699,99 UAH
Ці рядки йдуть у самому кінці посту, кожне видання з нового рядка.

В усіх інших випадках (загальна індустрійна новина, кастинг, інтерв'ю, чутки без релізу, оновлення/патчі для вже вийшлих ігор) — пошук ціни не роби і рядок з ціною не додавай.

ЯКЩО СТАТТЯ НЕ ПІДХОДИТЬ
Якщо матеріал не є новиною (наприклад: гайд, walkthrough, посібник, огляд без новинного приводу, список порад, реклама) і не відповідає жодному з описаних типів постів — виведи рівно одне слово SKIP і більше нічого.
Канал НЕ висвітлює кіберспорт — новини про турніри, матчі, прев'ю чемпіонатів, кіберспортивні команди чи гравців (наприклад ESL, LCQ, Esports World Cup та подібне) також виведи SKIP.

СТРУКТУРА ВІДПОВІДІ
Виведи ЛИШЕ готовий текст поста — без пояснень, без лапок навколо, без Markdown-розмітки (без **, без #). Постав емодзі перед заголовком першим рядком, БЕЗ пробілу між емодзі і текстом заголовка (наприклад "🛠Заголовок", не "🛠 Заголовок"). Якщо доречно, використовуй порожній рядок між вступом і списком ▫️. НІКОЛИ не додавай у тексті посту слово "Джерело" чи назву видання окремим рядком (наприклад НЕ пиши "Джерело: Insider Gaming" в кінці) — посилання на джерело додається автоматично поза текстом посту, тобі цим перейматися не треба. Пост має закінчуватись на останньому змістовному пункті чи реченні.

САМОПЕРЕВІРКА ПЕРЕД ВИВЕДЕННЯМ
Перш ніж вивести фінальний текст, подумки перевір і виправ:
1. Граматика, орфографія, пунктуація — без помилок
2. Формат (список ▫️ чи абзаци) відповідає типу новини, формати не змішані
3. Якщо є рядок з ціною — він справді доречний (лише нова гра з щойно відомою ціною або новина про знижку), інакше рядок з ціною відсутній
4. Якщо ціна доречна і видань кілька — кожне видання на окремому рядку у форматі "[Видання] — [ціна] UAH", без слова "Ціна:"
5. Написано "PS5", а не "PlayStation 5"
6. Заголовок і текст НЕ у формі питання
7. Якщо заголовок у форматі «[Суть], — звіт [Джерело]» — кома перед тире є
8. Назви ігор і студій написані повністю, без скорочень
9. Написано "PC" латиницею, а не "ПК" кирилицею
10. Між емодзі на початку заголовка і текстом заголовка немає пробілу
11. Пост стриманий, без зайвої води, орієнтовно 700-800 символів — якщо вийшло значно довше, скороти
12. Увесь текст українською — немає жодного речення чи фрази, залишеної англійською
13. Квартали написані словами ("перший квартал", не "Q1")
14. Конкретні імена/нікнейми лікерів-інсайдерів відсутні, замінені на узагальнене формулювання
Виводь тільки остаточний, вже виправлений варіант — без проміжних чернеток чи позначок про виправлення."""


def entry_id(feed_url, entry):
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(f"{feed_url}|{raw}".encode()).hexdigest()[:16]


def clean_text(raw, limit=600):
    text = re.sub("<[^<]+?>", "", raw or "")
    text = html.unescape(text).strip()
    return text[:limit]


def is_ignored(title, summary):
    combined = f"{title} {summary}"
    if IGNORE_PATTERN_CI.search(combined) or IGNORE_PATTERN_ILL.search(combined):
        return True
    if IGNORE_SOURCES_PATTERN.search(combined):
        return True
    return False


TAG_STRIP_RE = re.compile(r'<(script|style|nav|header|footer|form)[^>]*>.*?</\1>', re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r'<[^>]+>')


def basic_html_to_text(html_content):
    """Простий фолбек-екстрактор на випадок, якщо trafilatura витягла замало тексту."""
    cleaned = TAG_STRIP_RE.sub(' ', html_content)
    cleaned = TAG_RE.sub('\n', cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r'\n{2,}', '\n', cleaned)
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    return cleaned.strip()


def fetch_article(url):
    """Тягне сторінку статті. Повертає (текст, сирий_html) або (None, None), якщо не вдалось."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None, None

        try:
            text = trafilatura.extract(
                downloaded, include_comments=False, include_tables=True, favor_recall=True,
            )
        except TypeError:
            # старіша версія trafilatura без параметра favor_recall
            text = trafilatura.extract(downloaded, include_comments=False, include_tables=True)

        # Якщо основний спосіб витягнув підозріло мало — пробуємо простіший фолбек
        if not text or len(text) < 400:
            fallback_text = basic_html_to_text(downloaded)
            if fallback_text and len(fallback_text) > len(text or ""):
                text = fallback_text

        text = text[:MAX_ARTICLE_CHARS] if text else None
        return text, downloaded
    except Exception as e:
        print(f"  ! Не вдалось витягти статтю {url}: {e}")
        return None, None


OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
OG_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
    re.IGNORECASE,
)
VIDEO_SRC_RE = re.compile(
    r'<video[^>]*>.*?<source[^>]+src=["\']([^"\']+\.mp4[^"\']*)["\']',
    re.IGNORECASE | re.DOTALL,
)


def extract_media(html_content):
    """Шукає головне зображення (og:image) і, за можливості, пряме відеопосилання."""
    if not html_content:
        return None, None
    img_match = OG_IMAGE_RE.search(html_content) or OG_IMAGE_RE_ALT.search(html_content)
    image_url = img_match.group(1) if img_match else None
    video_match = VIDEO_SRC_RE.search(html_content)
    video_url = video_match.group(1) if video_match else None
    return image_url, video_url


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
            "max_tokens": 1500,
            "system": STYLE_GUIDE,
            "messages": [{"role": "user", "content": user_content}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
        },
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()


def linkify_title(post_text, link):
    """Робить перший рядок (заголовок) поста клікабельним посиланням на джерело."""
    lines = post_text.split("\n", 1)
    title_line = lines[0]
    rest = lines[1] if len(lines) > 1 else ""

    linked_title = f'<a href="{html.escape(link, quote=True)}">{html.escape(title_line)}</a>'
    if rest:
        return f"{linked_title}\n{html.escape(rest)}"
    return linked_title


def tg(method, payload):
    payload = {k: v for k, v in payload.items() if v is not None}
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

    article_text, article_html = fetch_article(link)
    if not article_text:
        article_text = clean_text(entry.get("summary", "") or entry.get("description", ""), limit=2000)

    if not article_text:
        print(f"  ! Пропущено (немає тексту): {title}")
        return

    if is_ignored(title, article_text):
        print(f"  - Пропущено (заборонена гра або джерело, знайдено в повному тексті статті): {title}")
        return

    try:
        post = format_with_claude(title, article_text, feed_title, link)
    except Exception as e:
        print(f"  ! Помилка Claude API: {e}")
        return

    if post.strip().upper() == "SKIP":
        print(f"  - Пропущено (не новина): {title}")
        return

    message = linkify_title(post, link)
    image_url, video_url = extract_media(article_html)
    caption_fits = len(message) <= 1024  # ліміт Telegram для підпису фото/відео

    if video_url:
        ok = tg("sendVideo", {
            "chat_id": DRAFT_TG_CHAT,
            "video": video_url,
            "caption": message if caption_fits else None,
            "parse_mode": "HTML",
        })
        if ok:
            if not caption_fits:
                tg("sendMessage", {
                    "chat_id": DRAFT_TG_CHAT,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })
            return

    if image_url:
        ok = tg("sendPhoto", {
            "chat_id": DRAFT_TG_CHAT,
            "photo": image_url,
            "caption": message if caption_fits else None,
            "parse_mode": "HTML",
        })
        if ok:
            if not caption_fits:
                tg("sendMessage", {
                    "chat_id": DRAFT_TG_CHAT,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })
            return

    # Фолбек: ні відео, ні фото не вдалось надіслати чи знайти — звичайне повідомлення з прев'ю
    tg("sendMessage", {
        "chat_id": DRAFT_TG_CHAT,
        "text": message,
        "parse_mode": "HTML",
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
        ignored = 0

        for entry in reversed(entries):
            eid = entry_id(url, entry)
            if eid in seen_set:
                continue

            seen_set.add(eid)
            seen.append(eid)

            if first_run:
                continue

            title = entry.get("title", "")
            summary = entry.get("summary", "") or entry.get("description", "")
            if is_ignored(title, summary):
                ignored += 1
                print(f"  - Пропущено (заборонена гра або джерело): {title}")
                continue

            send_draft(entry, feed_title)
            sent += 1
            time.sleep(3)

        print(f"{feed_title}: надіслано {sent}, пропущено через фільтр ігор {ignored}")

    STATE_FILE.write_text(json.dumps({"seen": seen[-3000:]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
