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
    "https://insider-gaming.com/feed",
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
]

# ─────────────────────────────────────────────

TG_TOKEN = os.environ["TG_TOKEN"]
DRAFT_TG_CHAT = os.environ["DRAFT_TG_CHAT"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = "claude-sonnet-5"

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
Стиль 2 (наративний): емодзі
