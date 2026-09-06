#!/usr/bin/env python3
# 2026-08-04, решение автора: конвейер вернулся НА 5090 (скачивание,
# расшифровка на GPU 5090, озвучка Fish 5090, отправка). На Спарке
# остаётся ТОЛЬКО пересказ моделью T-Pro. Причина: Спарк в одиночку
# делал всё, прогоны шли по 4-5 часов.
"""Утренний дайджест: новости RT (английская + испанская + арабская редакции —
повестка у них разная) + посты Стрелкова и Джексона Хинкла.

Что делает (по порядку, решения автора 2026-07-16/17):
  1. Скачивает заголовки; УЖЕ виденные вчера пропускает (память в ~/.rt-news-seen.json).
  2. Новые пересказывает по-русски через T-pro (на DGX Spark), числа — словами.
  3. Сторож чинит смесь букв («Кирill») и оставшиеся цифры.
  4. Перед каждым источником — вступление (RT / Гиркин / Хинкл).
  5. В конце — пожелание хорошего дня.
  6. Сначала озвучивает ВСЁ в файлы (голос Адама), потом шлёт голосовые ОДНИМ пакетом.

Usage: digest-news-en-ru.py [max_articles]   (max_articles — только для теста)
"""
import sys
import os
import re
import ssl
import html
import json
import hashlib
import collections
import urllib.request
import subprocess
import time
from datetime import date, timedelta, datetime, timezone

LIST_URL = "https://www.rt.com/news/"
STRELKOV_URL = "https://t.me/s/strelkovii"
STRELKOV_N = 6   # сколько последних постов канала СМОТРЕТЬ (новые из них отберём)
MIN_POST_CHARS = 140  # короче — это промо/анонсы, а не новость; пропускаем
# «Только свежие»: статьи RT старше стольких часов пропускаем (RT держит аналитику
# на ленте по нескольку дней — так она не пойдёт повторно как «новая»). Решение автора 2026-07-18.
MAX_AGE_HOURS = 36

# Доп. редакции RT — контент ОТЛИЧАЕТСЯ от английской (своя повестка под аудиторию).
# Испанская отдаёт полный текст статьи; арабская — только выжимку (meta description).
# T-pro переводит и то, и другое на русский. limit — сколько свежих статей брать.
RT_EXTRA = [
    {
        "name": "RT-ES", "prefix": "rtes", "limit": 12,
        "lists": ["https://actualidad.rt.com/actualidad"],
        "base": "https://actualidad.rt.com",
        "link_re": r'href="(/actualidad/(\d+)-[^"]+)"',
        "body": "itemprop",
        "intro": "Новости из испанской редакции Эр-Тэ.",
    },
    {
        "name": "RT-AR", "prefix": "rtar", "limit": 12,
        "lists": ["https://arabic.rt.com/world/", "https://arabic.rt.com/middle_east/"],
        "base": "https://arabic.rt.com",
        "link_re": r'href="(/(?:world|middle_east)/(\d+)-[^"]+)"',
        "body": "meta",
        "intro": "Новости из арабской редакции Эр-Тэ.",
    },
]

# Иранские агентства (автор одобрил Tehran Times и Mehr 2026-07-23; Press TV отклонил).
IRAN_AGENCIES = [
    {
        "name": "Tehran Times", "prefix": "tehrantimes", "limit": 8,
        "list": "https://www.tehrantimes.com/",
        "base": "https://www.tehrantimes.com",
        "link_re": r'href="(/news/(\d+)/[^"?#]+)"',
        "intro": "Новости от иранского агентства Тегеран Таймс.",
    },
    {
        "name": "Mehr", "prefix": "mehr", "limit": 8,
        "list": "https://en.mehrnews.com/",
        "base": "https://en.mehrnews.com",
        "link_re": r'href="(/news/(\d+)/[^"?#]+)"',
        "intro": "Новости от иранского агентства Мехр.",
    },
]
IRAN_SKIP = re.compile(
    r"about-us|contact-us|privacy|terms|advertise|sitemap|rss|/tags?/", re.I
)

# «Аль-Масира» (англ.), рупор Ансар Алла (автор одобрил 2026-07-24). Их сервер
# требует обхода SSL, а в тексте статьи первый абзац — служебная «шапка».
ALMASIRAH_LIST = "https://english.almasirah.net.ye/"
ALMASIRAH_LINK_RE = (
    r'href="(https://english\.masirahtv\.net/(?:post|news)/(\d+)/[^"?#]+)"'
)
ALMASIRAH_LIMIT = 6
ALMASIRAH_INTRO = (
    "Новости от йеменского агентства Аль-Масира, рупора движения Ансар Алла."
)
ALMASIRAH_BOILER = re.compile(
    r"Almasirah Media Network|English version of Almasirah|"
    r"misinformation imposed by the main stream",
    re.I,
)
_INSECURE_CTX = ssl.create_default_context()
_INSECURE_CTX.check_hostname = False
_INSECURE_CTX.verify_mode = ssl.CERT_NONE

# The Electronic Intifada. автор 2026-09-04: «все израильские источники
# вычисти, замени на этот». Пропалестинская лента про Газу; статьи все
# по делу, поэтому берём просто самые свежие, без отбора по словам.
EI_FEED = "https://electronicintifada.net/rss.xml"
EI_MAX = 8            # сколько свежих статей брать за выпуск
EI_INTRO = "Новости из Газы от The Electronic Intifada."

# Как назвать источник ВСЛУХ (заказ автора 2026-09-04: «добавь источники,
# чтобы я понимал, откуда новость»). Английские имена — русским
# произношением, иначе голос читает по буквам. Русские метки (Хинкл,
# Аль-Масира, Стрелков и т.д.) идут как есть, их тут перечислять не надо.
ГОЛОС_ИМЯ = {
    "The Electronic Intifada": "Электроник Интифада",
    "Tehran Times": "Тегеран Таймс",
    "Mehr": "Мехр",
    "RT-ES": "испанская редакция Эр-Ти",
    "RT-AR": "арабская редакция Эр-Ти",
}

# Ютуб-обозреватели (автор 2026-07-31: «ставь обоих, а контору Григорян — стереть»).
# Звук ролика -> распознавание (faster-whisper, CPU) -> пересказ T-pro с запалом.
# limit = ОКНО просмотра списка (15 последних), а не лимит: берутся ВСЕ новые
# из окна, старое отсекает дедуп (решение автора 2026-08-01: без ограничений).
YT_CHANNELS = [
    {"name": "Хинкл", "prefix": "hinkle", "limit": 15,
     "url": "https://www.youtube.com/@JacksonHinkleOfficial/videos",
     "intro": "Ролики Джексона Хинкла с ютуба."},
    {"name": "Хайфонг", "prefix": "haiphong", "limit": 15,
     "url": "https://www.youtube.com/@DannyHaiphongYT/videos",
     "intro": "Ролики Дэнни Хайфонга с ютуба."},
    {"name": "Такер", "prefix": "tucker", "limit": 15,
     "url": "https://www.youtube.com/@TuckerCarlson/videos",
     "intro": "Ролики Такера Карлсона с ютуба."},
    {"name": "Транзишн", "prefix": "transition", "limit": 15,
     "url": "https://www.youtube.com/@Transition_Protocol/videos",
     "intro": "Ролики канала Транзишн Протокол с ютуба."},
    {"name": "Вячеслав", "prefix": "vyacheslav", "limit": 15,
     "url": "https://www.youtube.com/@1Вячеслав/videos",
     "intro": "Ролики Вячеслава с ютуба.", "lang": "russian"},
    # добавлены 2026-09-02 по просьбе автора
    {"name": "Ратбон", "prefix": "rathbone", "limit": 15,
     "url": "https://www.youtube.com/@Rathbonee/videos",
     "intro": "Ролики Ратбона с ютуба."},
    # берём = сколько САМЫХ СВЕЖИХ новых роликов брать за прогон.
    # Заказ автора 2026-09-02: «из множества длинных роликов бери только
    # последние, не тяни всё». Тёркс сыплет по 7 штук в сутки (три из них
    # — часовые шоу), Кэндис — интервью по два часа. Остальное не копится
    # в очередь, а помечается прослушанным и пропадает.
    {"name": "Янг Тёркс", "prefix": "tyt", "limit": 15, "берём": 3,
     "url": "https://www.youtube.com/@TheYoungTurks/videos",
     "intro": "Ролики Янг Тёркс с ютуба."},
    {"name": "Кэндис Оуэнс", "prefix": "candace", "limit": 15, "берём": 1,
     "url": "https://www.youtube.com/@RealCandaceO/videos",
     "intro": "Ролики Кэндис Оуэнс с ютуба."},
]
YTDLP = "/home/user/.local/bin/yt-dlp"
WHISPER_PY = "/home/user/tts_test/venv_fish/bin/python"
YT_TRANSCRIBE = "/home/user/bin/yt-transcribe.py"
YT_WORKDIR = "/tmp/yt_news"

# Пульт новостей (автор 2026-09-04): каналы и вкл/выкл лент живут в
# ~/news-config.json, чтобы их можно было править с телефона, не трогая
# код. Если файла нет или он битый — работаем на списках выше (YT_CHANNELS
# по умолчанию, все ленты включены).
NEWS_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
ЛЕНТЫ_ВКЛ = {"iran-tehrantimes": True, "iran-mehr": True,
             "almasirah": True, "ei": True, "strelkov": True}


def _применить_конфиг():
    """Прочитать пультовый конфиг и перекрыть им YT_CHANNELS и флаги лент."""
    global YT_CHANNELS, ЛЕНТЫ_ВКЛ
    try:
        cfg = json.load(open(NEWS_CONFIG, encoding="utf-8"))
    except Exception:
        return
    каналы = []
    for c in cfg.get("ютуб", []):
        if not c.get("вкл", True):
            continue
        d = {"name": c["имя"], "prefix": c["prefix"],
             "limit": c.get("лимит", 15),
             "url": c["url"], "intro": c.get("intro", "")}
        if c.get("берём") is not None:
            d["берём"] = c["берём"]
        if c.get("lang"):
            d["lang"] = c["lang"]
        каналы.append(d)
    if каналы:
        YT_CHANNELS = каналы
    ЛЕНТЫ_ВКЛ.update(cfg.get("ленты_вкл", {}))

# Пересказ делает T-pro (Т-Банк, 32B) на DGX Spark — чистый русский без чужих языков.
# Пересказ — T-Pro, сжатая в 4 бита (int4), на 5090. Переехала со Спарка
# 2026-08-04: в bf16 62 ГБ в 32 ГБ видеопамяти не влезали, отсюда сжатие
# (18 ГБ). Старый адрес Спарка был http://127.0.0.1:8000.
VLLM_URL = "http://127.0.0.1:8002/v1/chat/completions"
MODEL = "tpro"
VOICE_PLAIN = "adam"  # решение автора 2026-08-29: озвучка новостей голосом Адама (был puffin)

# Озвучка-в-файл и пакетная отправка в телеграм.
SAY_OGG = "/home/user/bin/say-to-ogg.sh"
OGG_DIR = "/tmp/rt_news_ogg"
TG_CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram.json")

# ── Прогресс для Пульта новостей (заказ автора 2026-09-04: монитор с
# прогресс-баром на каждом этапе и прогнозом времени). Пишем в файл, его
# читает remote-news.py. История длительностей — для прогноза.
PROGRESS_FILE = "/home/user/news-progress.json"
PROGRESS_HIST = "/home/user/news-progress-hist.json"
_прогон_старт = None


def прогресс(этап, сделано=0, всего=0):
    """Отметить этап прогона. сделано/всего — для прогресс-бара этапа."""
    global _прогон_старт
    if _прогон_старт is None:
        _прогон_старт = time.time()
    try:
        json.dump({"этап": этап, "сделано": сделано, "всего": всего,
                   "старт": _прогон_старт, "время": time.time(),
                   "работает": True},
                  open(PROGRESS_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False)
    except Exception:
        pass


def прогресс_конец(итог="готово"):
    """Пометить прогон завершённым и запомнить его длительность."""
    global _прогон_старт
    старт = _прогон_старт or time.time()
    try:
        json.dump({"этап": итог, "сделано": 0, "всего": 0,
                   "старт": старт, "время": time.time(), "работает": False},
                  open(PROGRESS_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False)
    except Exception:
        pass
    # копим длительности удачных прогонов (для прогноза на пульте)
    if итог == "готово":
        try:
            ист = json.load(open(PROGRESS_HIST))
        except Exception:
            ист = []
        ист.append(round(time.time() - старт))
        json.dump(ист[-10:], open(PROGRESS_HIST, "w"))

# Память виденных заголовков (id -> дата), чтобы завтра не повторять.
SEEN_FILE = os.path.expanduser("~/.rt-news-seen.json")
SEEN_KEEP_DAYS = 21

# Вступления по источникам (дословно, как просил автор) и финал.
INTRO_RT = "Новости от конторы Григорян."
INTRO_STRELKOV = "Новости от сидельца Гиркина и от сидящих с ним аналитиков."
CLOSING = "На этом новости всё. Хорошего дня и весёлого настроения!"

MAX_ARTICLES = int(sys.argv[1]) if len(sys.argv) > 1 else None
UA = "Mozilla/5.0 (X11; Linux x86_64) digest-news-en-ru/1.0"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(chunk):
    text = re.sub(r"<[^>]+>", " ", chunk)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def is_fresh(article_html):
    """True, если статья опубликована не позже MAX_AGE_HOURS назад.
    Дата берётся из JSON-LD datePublished. Нет даты — считаем свежей (не рискуем)."""
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', article_html)
    if not m:
        return True
    try:
        dt = datetime.fromisoformat(m.group(1))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt <= timedelta(hours=MAX_AGE_HOURS)


def list_articles(list_html):
    """[(url, title, aid), ...]  aid — числовой id статьи RT (для памяти виденного)."""
    seen = set()
    articles = []
    for m in re.finditer(
        r'<a[^>]+href="(/news/(\d+)-[^"]+/)"[^>]*>(.*?)</a>', list_html, re.S
    ):
        url, aid, inner = m.group(1), m.group(2), m.group(3)
        title = strip_tags(inner)
        if not title or len(title) < 8:
            continue
        if aid in seen:
            continue
        seen.add(aid)
        articles.append(("https://www.rt.com" + url, title, aid))
        if MAX_ARTICLES is not None and len(articles) >= MAX_ARTICLES:
            break
    return articles


def clean_title(title):
    """Убрать пометки вроде (PHOTOS, VIDEO), (VIDEO), Opinion и т.п."""
    title = re.sub(
        r"\([^()]*(?:PHOTO|VIDEO|AUDIO|IMAGE|GRAPHIC)[^()]*\)", "", title, flags=re.I
    )
    title = re.sub(r"\bOpinion\b", "", title)
    return re.sub(r"\s+", " ", title).strip(" -–—")


def article_text(article_html):
    paras = re.findall(r"<p[^>]*>(.*?)</p>", article_html, re.S)
    text = " ".join(strip_tags(p) for p in paras)
    return text[:4000]


def article_title(article_html):
    """Заголовок статьи из og:title (или <h1>) — для иранских агентств."""
    m = re.search(
        r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', article_html
    )
    if m:
        return strip_tags(m.group(1))
    m = re.search(r"<h1[^>]*>(.*?)</h1>", article_html, re.S)
    return strip_tags(m.group(1)) if m else ""


def iran_agency_links(ag):
    """[(url, aid), ...] свежих статей иранского агентства, не более ag['limit']."""
    try:
        h = fetch(ag["list"])
    except Exception as e:
        print(f"{ag['name']}: список не скачался ({e})", file=sys.stderr)
        return []
    out, ids = [], set()
    for m in re.finditer(ag["link_re"], h):
        href, aid = m.group(1), m.group(2)
        if aid in ids or IRAN_SKIP.search(href):
            continue
        ids.add(aid)
        out.append((ag["base"] + href, aid))
        if len(out) >= ag["limit"]:
            break
    return out


def fetch_insecure(url):
    """Скачать с обходом проверки SSL (для серверов с кривым сертификатом)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(url=req, timeout=20, context=_INSECURE_CTX) as r:
        return r.read().decode("utf-8", "replace")


def almasirah_links():
    """[(url, aid), ...] свежих статей «Аль-Масиры», не более ALMASIRAH_LIMIT."""
    try:
        h = fetch_insecure(ALMASIRAH_LIST)
    except Exception as e:
        print(f"Аль-Масира: список не скачался ({e})", file=sys.stderr)
        return []
    out, ids = [], set()
    for m in re.finditer(ALMASIRAH_LINK_RE, h):
        url, aid = m.group(1), m.group(2)
        if aid in ids:
            continue
        ids.add(aid)
        out.append((url, aid))
        if len(out) >= ALMASIRAH_LIMIT:
            break
    return out


def almasirah_body(article_html):
    """Текст статьи «Аль-Масиры» без служебной шапки (первый абзац-дисклеймер)."""
    clean = []
    for p in re.findall(r"<p[^>]*>(.*?)</p>", article_html, re.S):
        t = strip_tags(p)
        if t and not ALMASIRAH_BOILER.search(t):
            clean.append(t)
    return " ".join(clean)[:4000]


def ei_links():
    """[(url, метка), ...] — свежие статьи The Electronic Intifada."""
    out, метки = [], set()
    try:
        x = fetch(EI_FEED)
    except Exception as e:
        print(f"Electronic Intifada: лента не скачалась ({e})",
              file=sys.stderr)
        return out
    for кусок in re.findall(r"<item>(.*?)</item>", x, re.S):
        m = re.search(r"<link>\s*(?:<!\[CDATA\[)?\s*(https?://[^<\]\s]+)",
                      кусок)
        if not m:
            continue
        url = m.group(1).strip()
        метка = re.sub(r"[^a-z0-9]+", "-", url.lower())[-60:]
        if метка in метки:
            continue
        метки.add(метка)
        out.append((url, метка))
        if len(out) >= EI_MAX:
            break
    return out


def ei_body(h):
    """Тело статьи. Чистый текст часто лежит в служебных данных страницы
    (articleBody) — берём его, иначе как обычно."""
    m = re.search(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', h)
    if m:
        try:
            текст = json.loads('"' + m.group(1) + '"').strip()
            if len(текст) > 200:
                return текст
        except Exception:
            pass
    return article_text(h)


def clean_post(text):
    """Убрать рекламный хвост канала, адреса аккаунтов и значки."""
    text = re.split(r"={3,}|ПОДПИСЫВАЙТЕСЬ|ПОДПИШИСЬ|ПОДПИШИТЕСЬ", text)[0]
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b(?:t\.me|telegram\.me|telega\.at)/\S+", " ", text, flags=re.I)
    text = re.sub(r"@[A-Za-z0-9_]{3,}", " ", text)
    text = re.sub(r"[#★☆▶►●•]", " ", text)
    text = re.sub(
        r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
        r"\U00002B00-\U00002BFF\U00002190-\U000021FF️]",
        " ",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


def channel_posts(url, n):
    """Тексты n последних постов телеграм-канала (страница t.me/s/...)."""
    page = fetch(url)
    blocks = re.findall(
        r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', page, re.S
    )
    posts = []
    for b in blocks[-n:]:
        chunk = re.sub(r"<br\s*/?>", " ", b)
        posts.append(clean_post(strip_tags(chunk)))
    return [p for p in posts if p]


def _chat(prompt, temperature=0.3):
    """Один запрос к модели пересказа (T-pro), вернуть текст ответа.

    Защита (2026-09-04): если T-pro отвалилась посреди прогона (её выбило
    из видеопамяти — «Connection refused»), НЕ бросаем ролик, а
    переподнимаем модель и повторяем запрос. Иначе после первого падения
    все оставшиеся ролики шли в пропуск (разбор выпуска 6:00)."""
    payload = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ).encode()
    for попытка in (1, 2):
        req = urllib.request.Request(
            VLLM_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                data = json.load(r)
            return (data["choices"][0]["message"].get("content") or "").strip()
        except (urllib.error.URLError, ConnectionError) as e:
            if попытка == 2:
                raise
            print(f"  T-pro не ответила ({e}) — переподнимаю и повторяю",
                  file=sys.stderr)
            прогресс("модель отвалилась — переподнимаю")
            subprocess.run(["/home/user/.local/bin/tpro-5090-start.sh"],
                           check=False)
            time.sleep(3)


def ask_plain(title, text):
    """Полный пересказ новости по-русски, числа — словами."""
    prompt = (
        "Перескажи эту новость подробно, СВОИМИ СЛОВАМИ, живым и чистым русским "
        "языком. Без сильных сокращений — передай все важные факты и детали. "
        "Ничего не выдумывай от себя. Пиши ТОЛЬКО по-русски, без иностранных слов "
        "и вставок на других языках. ВСЕ числа пиши СЛОВАМИ в правильном склонении, "
        "не цифрами (например: «двадцать три человека», «в две тысячи двадцать "
        "шестом году», «пятого мая», «около сорока процентов»). "
        "Не упоминай автора, источник, издание, фото и видео. Не вставляй ссылки, "
        "адреса и названия телеграм-каналов, @-имена. "
        "В ответе — ТОЛЬКО пересказ, без заголовка и пояснений.\n\n"
        + (f"Заголовок: {title}\n\n" if title else "")
        + f"Текст:\n{text}"
    )
    content = _chat(prompt, temperature=0.4)
    content = re.sub(
        r"^\s*(?:Заголовок:.*|[^\n]*(?:пересказ|кратк|суть)[^\n]*:)\s*\n+",
        "", content, flags=re.I,
    ).strip()
    return content


def ask_channel(post):
    """Пересказ поста блогера (Гиркин) С СОХРАНЕНИЕМ сарказма и иронии —
    НЕ сглаживать в сухое повествование (претензия автора 2026-07-24)."""
    prompt = (
        "Перескажи этот пост по-русски своими словами, но ОБЯЗАТЕЛЬНО СОХРАНИ "
        "авторский тон: сарказм, иронию, язвительность, острые и колкие "
        "формулировки, злость и подначку. НЕ превращай в сухое нейтральное "
        "изложение — пиши так же живо и ядовито, как автор. Ничего не добавляй "
        "от себя сверх сказанного. ВСЕ числа пиши словами в правильном склонении. "
        "Пиши ТОЛЬКО по-русски. Не вставляй ссылки, @-имена и названия каналов. "
        "В ответе — только пересказ, без заголовка и пояснений.\n\n"
        f"Пост:\n{post}"
    )
    content = _chat(prompt, temperature=0.6)
    content = re.sub(
        r"^\s*(?:Заголовок:.*|[^\n]*(?:пересказ|кратк|суть)[^\n]*:)\s*\n+",
        "", content, flags=re.I,
    ).strip()
    return content


def _mixed_words(text):
    """Слова со смесью кириллицы и латиницы (глюк «Кирill»). Дефис/пробел разбивают."""
    return [
        w
        for w in re.findall(r"[A-Za-zА-Яа-яЁё]+", text)
        if re.search(r"[А-Яа-яЁё]", w) and re.search(r"[A-Za-z]", w)
    ]


def _latin_words(text):
    """Слова целиком латиницей (THAAD, Patriot). Голос читает их как попало —
    автор 2026-09-03 услышал «Тейчтад» вместо THAAD."""
    видел, out = set(), []
    for w in re.findall(r"[A-Za-z]{2,}", text):
        if w.lower() not in видел:
            видел.add(w.lower())
            out.append(w)
    return out


ЧИСЛОСЛОВА = set("""
ноль один одна одно два две три четыре пять шесть семь восемь девять десять
одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать
семнадцать восемнадцать девятнадцать двадцать тридцать сорок пятьдесят
шестьдесят семьдесят восемьдесят девяносто сто двести триста четыреста
пятьсот шестьсот семьсот восемьсот девятьсот тысяча тысячи тысяч тысячу
миллион миллиона миллионов миллиард миллиарда миллиардов процент процента
процентов первого второго третьего четвёртого пятого шестого седьмого
восьмого девятого десятого января февраля марта апреля мая июня июля
августа сентября октября ноября декабря год года году годов лет целых
десятых сотых
первом втором третьем четвёртом пятом шестом седьмом восьмом
девятом десятом двадцатом тридцатом первый второй третий
""".split())


def _повторы(текст):
    """Повторяющиеся цепочки слов. Заказ автора 2026-09-03: ловить повторы
    в САМОМ тексте счётом, а не пожеланием модели «не повторяйся».
    Ищем два случая: цепочку из шести слов, сказанную дважды, и цепочку
    из трёх слов, сказанную трижды (так звучит «сократилась на лю,
    чем эти сократилась на лю»)."""
    # пометки источника («Источник — …») повторяются законно — не считаем
    текст = re.sub(r"Источник[и]? — [^.]+\.\s*", " ", текст)
    слова = re.findall(r"[А-Яа-яЁёA-Za-z]+", текст.lower())
    найдено = []
    for длина, сколько in ((6, 2), (4, 2)):
        счёт = collections.Counter(
            tuple(слова[i:i + длина]) for i in range(len(слова) - длина + 1))
        for цепочка, раз in счёт.items():
            # цепочка из одних коротких служебных слов («и в том же»)
            # повторяется в живой речи и сама по себе не беда
            # даты и числа словами повторяются сами собой — это не беда
            if раз >= сколько and any(
                    len(w) >= 5 and w not in ЧИСЛОСЛОВА for w in цепочка):
                найдено.append((" ".join(цепочка), раз))
    # убираем короткие куски, целиком сидящие внутри длинных находок
    найдено.sort(key=lambda п: -len(п[0]))
    итог = []
    for фраза, раз in найдено:
        if not any(фраза in б for б, _ in итог):
            итог.append((фраза, раз))
    return итог[:8]


def _одинаковые_зачины(текст):
    """Соседние предложения, начатые одними и теми же четырьмя словами."""
    # пометки источника («Источник — …») начинаются одинаково законно
    текст = re.sub(r"Источник[и]? — [^.]+\.\s*", " ", текст)
    предложения = [п.strip() for п in re.split(r"(?<=[.!?…])\s+", текст) if п.strip()]
    зачины = []
    for а, б in zip(предложения, предложения[1:]):
        на = re.findall(r"[А-Яа-яЁёA-Za-z]+", а.lower())[:3]
        нб = re.findall(r"[А-Яа-яЁёA-Za-z]+", б.lower())[:3]
        if len(на) == 3 and на == нб:
            зачины.append(" ".join(на))
    return зачины


def guard_text(text):
    """Сторож: чинит (а) смесь русских+латинских букв, (б) слова латиницей,
    (в) оставшиеся цифры — просим модель одним запросом переписать."""
    problems = []
    mixed = _mixed_words(text)
    if mixed:
        problems.append(
            "слова со смешанными русскими и латинскими буквами (" + ", ".join(mixed)
            + ") — замени латинские буквы русскими, чтобы слово стало правильным русским"
        )
    latin = [w for w in _latin_words(text) if w not in mixed]
    if latin:
        problems.append(
            "слова латинскими буквами (" + ", ".join(latin[:20])
            + ") — запиши их русскими буквами так, как они звучат по-русски "
            "(THAAD — «Тэд», Patriot — «Пэтриот»), или замени русским названием"
        )
    повторы = _повторы(text)
    if повторы:
        problems.append(
            "одно и то же сказано дважды ("
            + "; ".join(f"«{ф}» — {р} раза" for ф, р in повторы)
            + ") — оставь это один раз, повторные упоминания убери, "
            "а если они несут новое, перепиши другими словами"
        )
    зачины = _одинаковые_зачины(text)
    if зачины:
        problems.append(
            "соседние предложения начинаются одинаково ("
            + "; ".join(f"«{з}»" for з in зачины)
            + ") — начни их по-разному"
        )
    if re.search(r"\d", text):
        problems.append(
            "числа записаны цифрами — перепиши ВСЕ числа словами в правильном "
            "склонении (например «двадцать три», «в две тысячи двадцать шестом году»)"
        )
    if not problems:
        return text
    print(f"  ⚠ правки сторожа: {len(problems)}", file=sys.stderr)
    try:
        fixed = _chat(
            "Исправь в тексте следующее: " + "; ".join(problems) + ". "
            "Остальной текст оставь дословно без изменений. Верни ВЕСЬ текст "
            "целиком, без пояснений и заголовка.\n\n" + text,
            temperature=0.2,
        )
        fixed = re.sub(
            r"^\s*[^\n]*(?:исправл|текст|вот)[^\n]*:\s*\n+", "", fixed, flags=re.I
        ).strip()
        return fixed or text
    except Exception as e:
        print(f"  сторож не сработал ({e})", file=sys.stderr)
        return text


def _tg_creds():
    with open(TG_CONF) as f:
        d = json.load(f)
    return d["token"], d["chat_id"]


def make_ogg(text, path, voice=VOICE_PLAIN):
    """Озвучить текст в ogg-файл (без отправки). True — успех."""
    env = {**os.environ, "SAY_TG_VOICE": voice}
    rc = subprocess.run([SAY_OGG, text, path], check=False, env=env).returncode
    return rc == 0 and os.path.exists(path) and os.path.getsize(path) > 0


def send_voice_file(path):
    """Отправить готовый ogg как голосовое в телеграм. True — успех.

    2026-08-05: раньше при отказе голосовое просто терялось — в прогоне
    02:01 телеграм ответил 429 («слишком часто»), и одна новость до
    автора не доехала. Теперь ждём и пробуем снова: 429 — это не «нельзя»,
    а «погоди». Столько же смысла в повторе при 5xx — это сбой на их
    стороне. При прочих кодах (403, 400) повторять бесполезно.
    """
    token, chat = _tg_creds()
    url = f"https://api.telegram.org/bot{token}/sendVoice"
    ЗАДЕРЖКИ = (5, 15, 30)          # сколько ждать перед каждой попыткой
    for попытка, пауза in enumerate((0,) + ЗАДЕРЖКИ):
        if пауза:
            time.sleep(пауза)
        r = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", url,
             "-F", f"chat_id={chat}", "-F", f"voice=@{path}"],
            check=False, capture_output=True, text=True,
        )
        code = (r.stdout or "").strip()
        if code == "200":
            if попытка:
                print(f"  ушло с {попытка + 1}-й попытки", file=sys.stderr)
            return True
        повторимо = code == "429" or code.startswith("5")
        print(f"  sendVoice http={code}"
              f"{' — жду и пробую снова' if повторимо else ''}",
              file=sys.stderr)
        if not повторимо:
            return False
    print("  не отправилось после всех попыток", file=sys.stderr)
    return False


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen):
    """Сохранить память виденного, выкинув записи старше SEEN_KEEP_DAYS."""
    cutoff = (date.today() - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seen, f, ensure_ascii=False)
    os.replace(tmp, SEEN_FILE)


def rt_edition_articles(ed):
    """[(url, aid), ...] для доп. редакции RT, не более ed['limit'] свежих."""
    seen = set()
    out = []
    for lu in ed["lists"]:
        try:
            h = fetch(lu)
        except Exception as e:
            print(f"{ed['name']}: список {lu} не скачался ({e})", file=sys.stderr)
            continue
        for mm in re.finditer(ed["link_re"], h):
            rel, aid = mm.group(1), mm.group(2)
            if aid in seen:
                continue
            seen.add(aid)
            out.append((ed["base"] + rel, aid))
            if len(out) >= ed["limit"]:
                return out
    return out


def rt_edition_body(ed, h):
    """Тело статьи для доп. редакции: испанская — itemprop, арабская — meta."""
    if ed["body"] == "itemprop":
        i = h.find('itemprop="articleBody"')
        chunk = h[i:i + 20000] if i >= 0 else h
        paras = re.findall(r"<p[^>]*>(.*?)</p>", chunk, re.S)
        return " ".join(strip_tags(p) for p in paras)[:4000]
    if ed["body"] == "meta":
        mm = re.search(
            r'<meta[^>]+name="description"[^>]+content="([^"]+)"', h
        )
        return strip_tags(mm.group(1)) if mm else ""
    return article_text(h)


def yt_latest(ch):
    """[(video_id, title), ...] свежих роликов канала, не более ch['limit']."""
    try:
        out = subprocess.run(
            [YTDLP, "--flat-playlist", "--playlist-end", str(ch["limit"]),
             "--print", "%(id)s\t%(title)s", ch["url"]],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except Exception as e:
        print(f"{ch['name']}: список роликов не скачался ({e})", file=sys.stderr)
        return []
    vids = []
    for line in (out or "").strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            vids.append((parts[0], parts[1]))
    return vids


ЧАСТЬ_РАСШИФРОВКИ = 10000  # знаков за один заход к T-pro (её окно ~8192 токена)


def _чисто(текст):
    """Срезать вводную преамбулу, которую иногда лепит модель."""
    return re.sub(
        r"^\s*(?:Заголовок:.*|[^\n]*(?:пересказ|кратк|суть)[^\n]*:)\s*\n+",
        "", текст, flags=re.I,
    ).strip()


def _куски(текст, размер=ЧАСТЬ_РАСШИФРОВКИ):
    """Порезать расшифровку по границам предложений, не рвя мысль посередине."""
    предложения = re.split(r"(?<=[.!?…])\s+", текст)
    куски, текущий = [], ""
    for п in предложения:
        if текущий and len(текущий) + 1 + len(п) > размер:
            куски.append(текущий)
            текущий = п
        else:
            текущий = (текущий + " " + п).strip()
    if текущий:
        куски.append(текущий)
    return куски


ТРЕБОВАНИЯ_К_ПЕРЕСКАЗУ = (
    "Перескажи по-русски, О ЧЁМ он говорит и ЧТО утверждает, сохраняя его "
    "запал, напор и острые формулировки — не превращай в сухую сводку. "
    "ПОЛНОСТЬЮ ПРОПУСКАЙ РЕКЛАМУ: расхваливание товаров и услуг, названия "
    "спонсоров, скидки, промокоды, призывы что-то купить, заказать, "
    "подписаться или перейти на сайт — этого в пересказе быть не должно, "
    "даже упоминанием. Пересказывай только новости и рассуждения по делу. "
    "Ничего не добавляй от себя. ВСЕ числа пиши словами в правильном "
    "склонении. Пиши ТОЛЬКО по-русски. Не вставляй ссылки и @-имена. "
    "В ответе — только пересказ, без заголовка и пояснений."
)


def ask_video(title, text):
    """Пересказ ролика обозревателя — сохраняя запал и напор автора.

    2026-08-08, решение автора («после всех этих обрезок смысл сказанного
    Такером вообще теряется — не делай никаких обрезок»): расшифровку
    больше НЕ режем. Длинный ролик пересказываем по частям, потом сводим
    части в один связный рассказ — так ничего из сказанного не пропадает.
    """
    куски = _куски(text)

    if len(куски) <= 1:
        return _чисто(_chat(
            "Это расшифровка видеоролика обозревателя. "
            + ТРЕБОВАНИЯ_К_ПЕРЕСКАЗУ
            + f"\n\nНазвание ролика: {title}\n\nРасшифровка:\n{text}",
            temperature=0.5,
        ))

    print(f"  длинный ролик: пересказываю по частям ({len(куски)})",
          file=sys.stderr)
    части = []
    for i, кусок in enumerate(куски, 1):
        части.append(_чисто(_chat(
            f"Это ЧАСТЬ {i} из {len(куски)} расшифровки видеоролика "
            "обозревателя. " + ТРЕБОВАНИЯ_К_ПЕРЕСКАЗУ
            + " Если эта часть целиком рекламная и новостей в ней нет — "
            "ответь одним словом: РЕКЛАМА."
            + f"\n\nНазвание ролика: {title}\n\nЧасть расшифровки:\n{кусок}",
            temperature=0.5,
        )))
    # выкидываем куски, оказавшиеся сплошной рекламой (у Такера так бывает)
    было = len(части)
    части = [ч for ч in части
             if ч and not re.fullmatch(r"\W*РЕКЛАМА\W*", ч, flags=re.I)]
    if было - len(части):
        print(f"  выброшено рекламных частей: {было - len(части)}",
              file=sys.stderr)
    if not части:
        return ""
    if len(части) == 1:
        return части[0]

    # свести части в один рассказ; если своды не влезают — отдаём подряд
    свод = "\n\n".join(f"Часть {i}:\n{ч}" for i, ч in enumerate(части, 1))
    if len(свод) > ЧАСТЬ_РАСШИФРОВКИ:
        return "\n\n".join(части)
    сведённое = _чисто(_chat(
        "Ниже — пересказы частей ОДНОГО видеоролика по порядку. Сшей их в "
        "один связный рассказ: без повторов, без слов «часть первая», "
        "«в этой части» и подобных. Сохрани запал и все важные утверждения "
        "автора. Ничего не добавляй от себя. ВСЕ числа словами. Пиши ТОЛЬКО "
        "по-русски. В ответе — только готовый рассказ.\n\n"
        f"Название ролика: {title}\n\n{свод}",
        temperature=0.4,
    ))
    return сведённое or "\n\n".join(части)


СВОДКА_ПОРЦИЯ = 9000        # знаков пересказов за один заход к T-pro
СВОДКА_ВСТУПЛЕНИЕ = "Сводка новостей за сегодня."


def _свести_порцию(куски_текста, всего_порций, номер):
    """Из нескольких пересказов — тематические заметки без повторов."""
    return _чисто(_chat(
        "Ниже — пересказы новостей за сегодня: ролики обозревателей и статьи информагентств "
        f"(порция {номер} из {всего_порций}). Разложи их ПО ТЕМАМ, ПОДРОБНО: "
        "что произошло, где, кто и что именно заявил, какие названы цифры, "
        "имена и последствия. НЕ СОКРАЩАЙ до сухих пунктов — сохраняй "
        "подробности и доводы, пиши развёрнуто. ОДНО И ТО ЖЕ событие, о котором "
        "говорят разные обозреватели, своди вместе, но НЕ теряй деталей, "
        "которые есть у одного и нет у другого — дополняй ими картину. "
        "Выбрасывай рекламу, призывы подписаться и разговоры о самих каналах. "
        "Ничего не добавляй от себя. Пиши ТОЛЬКО по-русски, числа словами.\n\n"
        + "\n\n".join(куски_текста),
        temperature=0.4,
    ))


ЧАСТЬ_ВЫПУСКА = 7000        # знаков в одном голосовом дайджеста


def _разметить_темы(элементы):
    """Каждому сообщению — короткая тема. [(номер, тема), ...]"""
    порции, текущая, длина = [], [], 0
    for н, (источник, текст) in enumerate(элементы, 1):
        кусок = f"=== {н} ===\n{текст[:2500]}"
        if текущая and длина + len(кусок) > СВОДКА_ПОРЦИЯ:
            порции.append(текущая)
            текущая, длина = [], 0
        текущая.append(кусок)
        длина += len(кусок)
    if текущая:
        порции.append(текущая)

    темы = []
    for i, порция in enumerate(порции, 1):
        try:
            ответ = _chat(
                "Ниже — сообщения новостей, каждое под своим номером. "
                "Для КАЖДОГО номера напиши ровно одну строку вида\n"
                "номер | тема\n"
                "где тема — о чём это сообщение, тремя-шестью словами "
                "(например: «удар по порту Ходейда» или «спор о бюджете США»). "
                "Никаких пояснений, заголовков и пустых строк — только строки "
                "«номер | тема», по-русски.\n\n" + "\n\n".join(порция),
                temperature=0.2,
            ) or ""
        except Exception as e:
            print(f"  разметка тем, порция {i}: пропуск ({e})", file=sys.stderr)
            continue
        for строка in ответ.splitlines():
            m = re.match(r"\s*(\d+)\s*[|—-]\s*(.+)", строка)
            if m:
                номер = int(m.group(1))
                if 1 <= номер <= len(элементы):
                    темы.append((номер, m.group(2).strip()))
    return темы


def _слить_темы(темы, всего):
    """Из списка тем — группы «одно событие»: [(название, [номера]), ...]
    Порядок — от важного к мелкому (это и есть выборка новостей)."""
    строки = "\n".join(f"{н} | {т}" for н, т in темы)
    try:
        ответ = _chat(
            "Ниже — список сообщений новостей: номер и тема. Разные источники "
            "часто говорят ОБ ОДНОМ И ТОМ ЖЕ — собери такие номера вместе.\n"
            "Выведи группы, каждую двумя строками:\n"
            "Тема: название события\n"
            "Номера: 3, 7, 12\n"
            "Правила: каждый номер попадает РОВНО В ОДНУ группу; ни один "
            "номер не потерять; в одной группе — ТОЛЬКО ОДНО событие, "
            "разные события вместе не сваливай (если о событии сказал один "
            "источник — пусть будет группа из одного номера); "
            "сначала самые важные и самые обсуждаемые "
            "события, мелочи в конец. Больше ничего не пиши. По-русски.\n\n"
            + строки,
            temperature=0.2,
        ) or ""
    except Exception as e:
        print(f"  слияние тем не удалось ({e})", file=sys.stderr)
        return []

    группы, название = [], None
    for строка in ответ.splitlines():
        # модель любит обрамлять строки звёздочками — снимаем их
        m = re.match(r"\s*[*#>\-\s]*Тема\s*:\s*(.+)", строка, re.I)
        if m:
            название = m.group(1).strip().strip("*# ").strip()
            continue
        m = re.match(r"\s*[*#>\-\s]*Номера\s*:\s*(.+)", строка, re.I)
        if m and название:
            номера = [int(x) for x in re.findall(r"\d+", m.group(1))
                      if 1 <= int(x) <= всего]
            if номера:
                группы.append((название, номера))
            название = None

    # ни одну новость не терять: что модель забыла — своей группой в конец
    розданы = {н for _, номера in группы for н in номера}
    for н in range(1, всего + 1):
        if н not in розданы:
            группы.append((f"новость {н}", [н]))
    return группы


def _рассказ_по_теме(название, тексты):
    """Один связный рассказ о событии из всех сообщений о нём."""
    сырьё = "\n\n".join(тексты)
    if len(сырьё) > СВОДКА_ПОРЦИЯ:      # редко: об одном событии говорят все
        сырьё = сырьё[:СВОДКА_ПОРЦИЯ]
    один = len(тексты) == 1
    return _чисто(_chat(
        (f"Ниже — сообщения РАЗНЫХ источников об одном событии: {название}. "
         "Слей их в ОДИН связный рассказ. Что говорят все — скажи один раз; "
         "то, что есть только у одного, добавь к общей картине. "
         if not один else
         f"Ниже — сообщение о событии: {название}. Перескажи его связно. ")
        + "Пиши ТОЛЬКО об этом событии — соседних новостей не касайся, "
        "события не пересчитывай («произошло два события» — так не надо). "
        "НЕ называй источники, каналы, обозревателей и сайты: ни «по данным "
        "такого-то», ни «как сообщает такой-то» — просто рассказывай, что "
        "произошло. "
        "Пиши подробно и живым языком: где, когда, кто именно что заявил, "
        "какие названы цифры и чем это грозит ПО СЛОВАМ ИСТОЧНИКОВ. Своих "
        "рассуждений и выводов не добавляй. Не ужимай до пары предложений "
        "и не выбрасывай важного. ОДНО И ТО ЖЕ не повторяй дважды. "
        "Рекламу, призывы подписаться и разговоры о самих каналах выброси. "
        "Ничего не выдумывай. Начни сразу с сути, без вступлений вроде "
        "«в этом сообщении». Пиши ТОЛЬКО по-русски, все числа словами.\n\n"
        + сырьё,
        temperature=0.4,
    ))


def сводка_роликов(ролики):
    """Из всего собранного за день — выпуск, разложенный ПО СОБЫТИЯМ.

    2026-09-02, заказ автора: «выборка новостей со всех источников, а не
    пересказ с повторами». Раньше сырьё резалось по ОБЪЁМУ, и одно
    событие попадало в разные порции — каждая пересказывала его заново.
    Теперь три шага: (1) каждому сообщению — тема; (2) темы об одном
    событии сливаются в группы, важное вперёд; (3) на каждую группу —
    ОДИН рассказ из всех источников сразу. Событие звучит один раз.
    Если разметка не удалась — откат на старый способ (порциями).
    """
    if not ролики:
        return []

    темы = _разметить_темы(ролики)
    группы = _слить_темы(темы, len(ролики)) if темы else []
    if not группы:
        print("  темы не разметились — свожу по-старому, порциями",
              file=sys.stderr)
        return _сводка_порциями(ролики)

    print(f"  {len(ролики)} сообщений сложились в {len(группы)} событий",
          file=sys.stderr)
    рассказы = []
    for название, номера in группы:
        тексты = [f"[{ролики[н - 1][0]}] {ролики[н - 1][1]}" for н in номера]
        try:
            # сторож и здесь: дайджест пишется заново, и модель возвращает
            # в него цифры и латиницу, которые голос читает как попало
            # (автор 2026-09-03: «сократилась на лю», «Тейчтад»)
            р = guard_text(_рассказ_по_теме(название, тексты))
        except Exception as e:
            print(f"  «{название[:40]}»: пропуск ({e})", file=sys.stderr)
            continue
        if р:
            # пометка источника — ПОСЛЕ сторожа, чтобы он не портил имена
            # (заказ автора 2026-09-04: понимать, откуда новость). Источники
            # события — по порядку, без повторов; английские произносим
            # по-русски (ГОЛОС_ИМЯ).
            видели, имена = set(), []
            for н in номера:
                метка = ролики[н - 1][0]
                имя = ГОЛОС_ИМЯ.get(метка, метка)
                if имя not in видели:
                    видели.add(имя)
                    имена.append(имя)
            слово = "Источник" if len(имена) == 1 else "Источники"
            рассказы.append(f"{слово} — {', '.join(имена)}. {р}")
    if not рассказы:
        return _сводка_порциями(ролики)

    # склеиваем рассказы в голосовые части — по объёму, но НЕ разрывая событие
    части, текущая, длина = [], [], 0
    for р in рассказы:
        if текущая and длина + len(р) > ЧАСТЬ_ВЫПУСКА:
            части.append("\n\n".join(текущая))
            текущая, длина = [], 0
        текущая.append(р)
        длина += len(р)
    if текущая:
        части.append("\n\n".join(текущая))
    # последняя проверка — уже по готовой части: повтор мог оказаться
    # НЕ внутри рассказа, а между двумя разными событиями
    очищенные = []
    for часть in части:
        if _повторы(часть) or _одинаковые_зачины(часть):
            часть = guard_text(часть)
        очищенные.append(часть)
    return очищенные


def _сводка_порциями(ролики):
    """Старый способ (запасной): порции по объёму -> заметки -> дайджест."""
    порции, текущая, длина = [], [], 0
    for канал, текст in ролики:
        кусок = f"[{канал}] {текст}"
        if текущая and длина + len(кусок) > СВОДКА_ПОРЦИЯ:
            порции.append(текущая)
            текущая, длина = [], 0
        текущая.append(кусок)
        длина += len(кусок)
    if текущая:
        порции.append(текущая)

    заметки = []
    for i, порция in enumerate(порции, 1):
        try:
            з = _свести_порцию(порция, len(порции), i)
            if з:
                заметки.append(з)
        except Exception as e:
            print(f"  порция {i}: пропуск ({e})", file=sys.stderr)
    if not заметки:
        return []

    группы, текущая, длина = [], [], 0
    for з in заметки:
        if текущая and длина + len(з) > СВОДКА_ПОРЦИЯ:
            группы.append(текущая)
            текущая, длина = [], 0
        текущая.append(з)
        длина += len(з)
    if текущая:
        группы.append(текущая)

    части = []
    for н, группа in enumerate(группы, 1):
        часть = _итоговый_дайджест(группа, н, len(группы))
        if часть:
            части.append(часть)
    return части


def _итоговый_дайджест(заметки, номер=1, всего=1):
    """Из заметок — связный подробный рассказ (одна часть дайджеста)."""
    приставка = ("" if всего == 1
                 else f"Это ЧАСТЬ {номер} из {всего} сегодняшнего выпуска — "
                      "не подводи итогов и не прощайся, дальше будут ещё. ")
    return _чисто(_chat(
        приставка
        + "Ниже — заметки по темам, собранные из роликов обозревателей и статей "
        "информагентств за "
        "сегодня. Составь из них связный ПОДРОБНЫЙ рассказ, который "
        "человек будет слушать: по темам, на каждую тему — полноценный "
        "рассказ живым языком, с подробностями: где, когда, кто именно что "
        "заявил, какие названы цифры и чем дело грозит. НЕ ужимай до пары "
        "предложений и ничего важного не выбрасывай — пусть выйдет длинно, "
        "это нормально. Повторы объединяй, ничего не выдумывай, рекламу "
        "и разговоры о каналах выбрасывай. Начни сразу с новостей, без вступлений вроде "
        "«вот дайджест». Пиши ТОЛЬКО по-русски, все числа словами.\n\n"
        + "\n\n".join(заметки),
        temperature=0.4,
    ))


def yt_channel_items(ch, seen):
    """Пересказы НОВЫХ роликов канала: звук -> распознавание -> T-pro."""
    os.makedirs(YT_WORKDIR, exist_ok=True)
    new = [
        (vid, title, ch["prefix"] + ":" + vid)
        for vid, title in yt_latest(ch)
        if ch["prefix"] + ":" + vid not in seen
    ]
    if not new:
        return []
    # отсечка «берём только последние»: список идёт от свежих к старым,
    # хвост сразу метим прослушанным, чтобы он не всплыл завтра
    берём = ch.get("берём")
    if берём and len(new) > берём:
        сегодня = date.today().isoformat()
        for vid, title, iid in new[берём:]:
            seen[iid] = сегодня
        print(f"{ch['name']}: новых {len(new)}, беру {берём} свежих, "
              f"остальные {len(new) - берём} пропускаю", file=sys.stderr)
        new = new[:берём]
    # 1) качаем звук всех новых роликов
    audios = []
    for vid, title, iid in new:
        print(f"[{ch['name']} новый ролик] {title[:60]}", file=sys.stderr)
        path = os.path.join(YT_WORKDIR, f"{vid}.m4a")
        if not os.path.exists(path):
            subprocess.run(
                # 2026-08-25: ютуб перекрыл прямую отдачу формата 140 —
                # без движка JavaScript он даёт только потоковые форматы,
                # и качалка падала с «403 Forbidden» (все 65 роликов мимо).
                # bestaudio берёт что дают; ffmpeg потом читает по содержимому,
                # а не по расширению файла.
                [YTDLP, "-f", "bestaudio[ext=m4a]/bestaudio", "-o", path,
                 f"https://www.youtube.com/watch?v={vid}"],
                capture_output=True, text=True, timeout=900,
            )
        if os.path.exists(path):
            # 2026-08-08, решение автора: НИКАКИХ обрезок. Раньше длинные
            # ролики резались до 20 минут — «смысл сказанного Такером
            # вообще теряется». Берём ролик целиком, а длинную расшифровку
            # ask_video пересказывает по частям и сводит воедино.
            audios.append((path, vid, title, iid))
        else:
            print("  звук не скачался — пропуск", file=sys.stderr)
    if not audios:
        return []
    # 2) распознаём одним заходом (модель загружается один раз).
    #
    # 2026-08-05: на карте тесно — T-pro держит ~23 ГБ из 32, Fish ещё
    # 2.3, и whisper-large-v3 (нужно ~5) не влезает: первый же ночной
    # прогон дал «CUDA out of memory», два ролика пропали.
    #
    # Решение автора: «когда работает T-Pro ты можешь выгрузить Fish, а
    # когда T-Pro закончит, загрузить его заново». Гасим именно Fish —
    # он нужен только в самом конце, при озвучке, а T-pro требуется
    # сразу после расшифровки, для пересказа. Проверено 2026-08-05:
    # без Fish свободно ~7 ГБ, ролик расшифровывается за 52 с.
    subprocess.run(["pkill", "-f", "avatar/fish_server.py"],
                   check=False, capture_output=True, timeout=60)
    time.sleep(5)          # дать карте освободиться
    try:
        subprocess.run(
            [WHISPER_PY, YT_TRANSCRIBE] + [a[0] for a in audios],
            check=False, timeout=7200,
            env={**os.environ, "WHISPER_LANG": ch.get("lang", "english")},
        )
    finally:
        # поднять обязательно, даже если расшифровка упала —
        # иначе озвучивать будет нечем
        subprocess.run(["/home/user/bin/ensure-fish.sh"],
                       check=False, capture_output=True, timeout=300)
    # 3) пересказ
    items = []
    for path, vid, title, iid in audios:
        try:
            with open(path + ".txt") as f:
                text = f.read()
        except OSError:
            print(f"  {vid}: расшифровки нет — пропуск", file=sys.stderr)
            continue
        try:
            if len(text) >= 300:
                plain = ask_video(title, text)
                if plain:
                    items.append((guard_text(plain), iid))
        except Exception as e:
            print(f"  {vid}: пропуск ({e})", file=sys.stderr)
        finally:
            for p in (path, path + ".txt"):
                try:
                    os.remove(p)
                except OSError:
                    pass
    return items


def collect_new():
    """Собрать НОВЫЕ (не виденные) пересказы, сгруппированные с вступлениями.
    Возвращает (segments, seen): segments = [(текст, item_id или None), ...]."""
    seen = load_seen()
    segments = []
    # Свежесть = что автор ещё НЕ слышал (дедуп по seen). Часовой фильтр MAX_AGE_HOURS
    # включаем ТОЛЬКО на «первом разе» — когда память почти пуста, чтобы не свалить
    # гору старья. Дальше решает только дедуп (иначе выкинули бы непрослушанное за
    # выходные). Решение автора 2026-07-18.
    first_run = len(seen) < 5

    # --- RT УДАЛЁН (автор 2026-07-31: «контору Григорян — стереть»),
    #     вместо него — ютуб-обозреватели (Хинкл, Хайфонг) ---
    # 2026-08-26, заказ автора: в сводку идут И ролики, И агентства —
    # весь выпуск одним дайджестом. Гиркин остаётся отдельно (у него
    # ценен сам ядовитый слог, в сухой сводке он теряется).
    в_сводку, ид_сводки = [], []
    for нч, ch in enumerate(YT_CHANNELS, 1):
        прогресс(f"смотрю ютуб: {ch['name']}", нч, len(YT_CHANNELS))
        for текст, iid in yt_channel_items(ch, seen):
            в_сводку.append((ch["name"], текст))
            ид_сводки.append(iid)

    # --- иранские агентства (Tehran Times, Mehr — одобрены автором 2026-07-23) ---
    прогресс("текстовые ленты")
    for ag in IRAN_AGENCIES:
        if not ЛЕНТЫ_ВКЛ.get("iran-" + ag["prefix"], True):
            continue  # лента выключена на пульте
        ag_items = []
        for url, aid in iran_agency_links(ag):
            iid = ag["prefix"] + ":" + aid
            if iid in seen:
                continue
            print(f"[{ag['name']} новая] {url[:70]}", file=sys.stderr)
            try:
                h = fetch(url)
                if first_run and not is_fresh(h):
                    print("  пропуск (старая, первый прогон)", file=sys.stderr)
                    continue
                body = article_text(h)
                if len(body) < 200:
                    continue
                plain = ask_plain(article_title(h), body)
                if not plain:
                    continue
                ag_items.append((guard_text(plain), iid))
            except Exception as e:
                print(f"  пропуск ({e})", file=sys.stderr)
        for текст, iid in ag_items:
            в_сводку.append((ag["name"], текст))
            ид_сводки.append(iid)

    # --- «Аль-Масира» (Ансар Алла) — одобрена автором 2026-07-24 ---
    alm_items = []
    for url, aid in (almasirah_links() if ЛЕНТЫ_ВКЛ.get("almasirah", True)
                     else []):
        iid = "almasirah:" + aid
        if iid in seen:
            continue
        print(f"[Аль-Масира новая] {url[:70]}", file=sys.stderr)
        try:
            h = fetch_insecure(url)
            body = almasirah_body(h)
            if len(body) < 200:
                continue
            plain = ask_plain(article_title(h), body)
            if not plain:
                continue
            alm_items.append((guard_text(plain), iid))
        except Exception as e:
            print(f"  пропуск ({e})", file=sys.stderr)
    for текст, iid in alm_items:
        в_сводку.append(("Аль-Масира", текст))
        ид_сводки.append(iid)

    # --- The Electronic Intifada (Газа) — автор 2026-09-04 ---
    ei_itms = []
    for url, slug in (ei_links() if ЛЕНТЫ_ВКЛ.get("ei", True) else []):
        iid = "ei:" + slug
        if iid in seen:
            continue
        print(f"[Electronic Intifada новая] {slug[:50]}", file=sys.stderr)
        try:
            h = fetch_insecure(url)
            body = ei_body(h)
            if len(body) < 200:
                continue
            plain = ask_plain(article_title(h), body)
            if not plain:
                continue
            ei_itms.append((guard_text(plain), iid))
        except Exception as e:
            print(f"  пропуск ({e})", file=sys.stderr)
    for текст, iid in ei_itms:
        в_сводку.append(("The Electronic Intifada", текст))
        ид_сводки.append(iid)

    # --- всё собранное сводим в один дайджест ---
    if в_сводку:
        прогресс("свожу выпуск по событиям")
        части = сводка_роликов(в_сводку)
        if части:
            segments.append((СВОДКА_ВСТУПЛЕНИЕ, None))
            for н, часть in enumerate(части, 1):
                # ярлыки «услышано» вешаем на ПОСЛЕДНЮЮ часть: пометим
                # новости виденными, только когда дайджест дошёл целиком
                segments.append((часть, ид_сводки if н == len(части) else None))
        else:
            print("сводка не собралась — шлю пересказы по одному",
                  file=sys.stderr)
            segments.append((СВОДКА_ВСТУПЛЕНИЕ, None))
            for (источник, текст), iid in zip(в_сводку, ид_сводки):
                segments.append((текст, iid))

    # --- телеграм-каналы ---
    for name, url, n, intro, prefix in (
        [("Стрелков", STRELKOV_URL, STRELKOV_N, INTRO_STRELKOV, "strelkov")]
        if ЛЕНТЫ_ВКЛ.get("strelkov", True) else []
    ):
        try:
            posts = channel_posts(url, n)
        except Exception as e:
            print(f"{name}: не скачался ({e})", file=sys.stderr)
            posts = []
        ch_items = []
        for post in posts:
            if len(post) < MIN_POST_CHARS:
                continue  # короткие промо/анонсы стримов — не новости
            iid = prefix + ":" + hashlib.md5(post.encode("utf-8")).hexdigest()[:12]
            if iid in seen:
                continue
            print(f"[{name} новый пост]", file=sys.stderr)
            try:
                plain = ask_channel(post)  # сохраняем сарказм/иронию автора
                if not plain:
                    continue
                ch_items.append((guard_text(plain), iid))
            except Exception as e:
                print(f"  пропуск ({e})", file=sys.stderr)
        if ch_items:
            segments.append((intro, None))
            segments.extend(ch_items)

    return segments, seen


# Сколько ждать свободной видеокарты и как часто пробовать (2026-08-06)
ЖДАТЬ_КАРТУ = 6 * 3600
ПРОБОВАТЬ_РАЗ_В = 20 * 60


КВИН_МЕТРИКИ = "http://127.0.0.1:8000/metrics"
КВИН_СТОП = "/home/user/.local/bin/qwen35-vllm-stop.sh"


def _квин_счётчики():
    """Сколько Квин сейчас считает и сколько наработал всего.

    Возвращает (запросов_в_работе, всего_токенов) или None, если Квин
    не отвечает (значит, карту держит не он — трогать нечего).
    """
    try:
        вывод = subprocess.run(
            ["curl", "-s", "-m", "5", КВИН_МЕТРИКИ],
            capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    if "vllm:" not in (вывод or ""):
        return None
    в_работе = токенов = 0.0
    for строка in вывод.splitlines():
        if строка.startswith("#") or not строка.startswith("vllm:"):
            continue
        try:
            имя, значение = строка.rsplit(" ", 1)
            число = float(значение)
        except ValueError:
            continue
        имя = имя.split("{")[0]     # отбрасываем подписи в фигурных скобках
        if имя in ("vllm:num_requests_running", "vllm:num_requests_waiting"):
            в_работе += число
        elif имя in ("vllm:generation_tokens_total", "vllm:prompt_tokens_total"):
            токенов += число
    return в_работе, токенов


def ensure_tpro():
    """Убедиться, что T-pro отвечает; если нет — поднять, дождавшись
    своей очереди у видеокарты.

    2026-08-05: раньше тут поднимался sglang на Спарке по ssh. Модель
    переехала на 5090 (сжата в 4 бита, порт 8002), Спарк из цепочки
    вышел — и в первую же ночь прогон упал: «No route to host», Спарк
    просто выключен. Теперь поднимаем локально.

    2026-08-06: в ночь на 6-е выпуск не вышел — карту держал Квин 35Б
    (стилометрия, 28.7 ГБ из 32), места под T-pro не осталось.
    Решение автора: новостям НЕ выгонять Квина, а отступить и пробовать
    снова, пока карта не освободится (обычно автор заканчивает под утро).
    Ждём до 6 часов, пробуя каждые 20 минут; дольше смысла нет —
    к тому времени новости успевают устареть."""
    начало = time.time()
    попытка = 0
    прошлые_счётчики = None
    while True:
        попытка += 1
        rc = subprocess.run(
            ["/home/user/.local/bin/tpro-5090-start.sh"], check=False
        ).returncode
        if rc == 0:
            if попытка > 1:
                ждали = round((time.time() - начало) / 60)
                print(f"T-pro поднялась с {попытка}-й попытки "
                      f"(ждали карту {ждали} мин)", file=sys.stderr)
            return True
        if time.time() - начало + ПРОБОВАТЬ_РАЗ_В > ЖДАТЬ_КАРТУ:
            print(f"Видеокарта занята уже {round((time.time() - начало) / 3600, 1)} ч"
                  " — сдаюсь, выпуска сегодня не будет.", file=sys.stderr)
            return False
        # Квин простаивает? (решение автора 2026-08-25: работающего не
        # трогаем, но если висит без дела — забираем карту). Сравниваем
        # его счётчики с прошлой попыткой: если за 20 минут не прибавилось
        # ни одного токена и ничего не в работе — он просто забыт включённым.
        счётчики = _квин_счётчики()
        if счётчики is not None:
            if (прошлые_счётчики is not None and счётчики == прошлые_счётчики
                    and счётчики[0] == 0):
                print("Квин простаивает больше 20 минут — забираю у него карту",
                      file=sys.stderr)
                subprocess.run([КВИН_СТОП], check=False)
                time.sleep(10)
                прошлые_счётчики = None
                continue          # сразу пробуем поднять T-pro, не ждём
            прошлые_счётчики = счётчики
        else:
            прошлые_счётчики = None

        свободно = "?"
        try:
            свободно = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=30).stdout.split()[0]
        except Exception:
            pass
        print(f"[{time.strftime('%H:%M:%S')}] Видеокарта занята "
              f"(свободно {свободно} МиБ) — подожду 20 мин и попробую снова",
              file=sys.stderr)
        time.sleep(ПРОБОВАТЬ_РАЗ_В)


def main():
    прогресс("начинаю")
    _применить_конфиг()   # каналы и вкл/выкл лент с пульта (телефон)
    прогресс("поднимаю модель пересказа")
    if not ensure_tpro():
        print("T-pro недоступна — прекращаю, пустой пакет не шлю.", file=sys.stderr)
        прогресс_конец("модель не поднялась")
        sys.exit(1)
    segments, seen = collect_new()

    if not any(iid for _, iid in segments):
        print("Новых новостей нет — ничего не шлю.", file=sys.stderr)
        save_seen(seen)  # всё равно прунингуем старое
        прогресс_конец("новых новостей нет")
        return

    segments.append((CLOSING, None))

    # --- Партиями: озвучил партию -> сразу отослал (решение автора
    # 2026-08-03; отменяет пакетную доставку «всё в конце» от 2026-07-17).
    # Партия начинается со вступления источника (iid=None); финальная
    # фраза — своя партия. seen сохраняется после каждой партии: если
    # прогон упадёт позже, отосланное не повторится.
    # 2026-09-02: прошлый выпуск больше НЕ стираем, а отодвигаем в сторону —
    # иначе, когда автор жалуется на заикание, вещественных доказательств уже
    # нет (папку затирает следующий прогон). Держим один прошлый выпуск.
    ПРОШЛЫЙ = OGG_DIR + "_прошлый"
    if os.path.isdir(OGG_DIR):
        subprocess.run(["rm", "-rf", ПРОШЛЫЙ], check=False)
        try:
            os.rename(OGG_DIR, ПРОШЛЫЙ)
        except OSError:
            pass
    os.makedirs(OGG_DIR, exist_ok=True)
    batches = []
    for seg in segments:
        if seg[1] is None or not batches:
            batches.append([])
        batches[-1].append(seg)
    k = 0
    sent_total = 0
    for bi, batch in enumerate(batches, 1):
        prepared = []
        for text, iid in batch:
            path = os.path.join(OGG_DIR, f"{k:03d}.ogg")
            k += 1
            прогресс("озвучиваю и шлю", k, len(segments))
            print(f"  озвучиваю {k}/{len(segments)} (партия {bi}/{len(batches)})…",
                  file=sys.stderr)
            # текст кладём рядом со звуком: если автор услышит брак,
            # будет видно, что именно пошло в озвучку (урок 2026-09-03)
            try:
                with open(path[:-4] + ".txt", "w") as f:
                    f.write(text)
            except OSError:
                pass
            if make_ogg(text, path):
                prepared.append((path, iid))
            else:
                print(f"  озвучка сегмента {k - 1} не удалась", file=sys.stderr)
        print(f"Отсылаю партию {bi}/{len(batches)}: {len(prepared)} голосовых…",
              file=sys.stderr)
        for n, (path, iid) in enumerate(prepared):
            # 2026-08-05: без паузы телеграм на четвёртом-пятом голосовом
            # отвечает 429 («слишком часто»). Две секунды между ними —
            # и он спокоен; на общее время это почти не влияет.
            if n:
                time.sleep(2)
            if send_voice_file(path):
                sent_total += 1
                if iid:
                    # помечаем виденным только то, что ДОШЛО, — иначе
                    # потерянная новость завтра уже не повторится.
                    # У сводки один голосовой закрывает много роликов —
                    # тогда здесь список.
                    for один in (iid if isinstance(iid, list) else [iid]):
                        seen[один] = date.today().isoformat()
        save_seen(seen)
    print(f"Готово: отослано {sent_total} голосовых.", file=sys.stderr)
    прогресс_конец("готово")

    # Уложить T-pro после выпуска (решение автора 2026-08-07): иначе она
    # держит ~24 ГБ видеопамяти, и утром Квину стилометрии места нет.
    # Fish не трогаем — он резидентный, им говорит робот.
    subprocess.run(["/home/user/.local/bin/tpro-5090-stop.sh"], check=False)
    print("T-pro уложена, видеокарта свободна.", file=sys.stderr)


if __name__ == "__main__":
    main()
