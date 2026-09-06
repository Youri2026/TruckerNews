#!/usr/bin/env python3
"""Пульт новостей на телефон: расписание выпусков + управление каналами.

Заказ автора 2026-09-04: «пульт для управления настройками новостного
дайджеста — днями и временем выпусков, добавление и стирание каналов».

Открывать с телефона (Tailscale Connected):
    http://127.0.0.1:8091
Слушает ТОЛЬКО на адресе Tailscale — из интернета недоступен.

Что редактирует:
  • расписание digest-news-en-ru в личном cron (дни недели + часы выпусков);
  • ~/news-config.json — ютуб-каналы (добавить/удалить/вкл-выкл) и
    вкл/выкл текстовых лент (Иран, Аль-Масира, Electronic Intifada,
    Стрелков). Сам скрипт новостей читает этот конфиг при каждом прогоне.
"""
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Впишите адрес, на котором слушать пульт: свой Tailscale/LAN-адрес,
# либо 127.0.0.1 (только локально). НЕ выставляйте в интернет — пульт
# без пароля.
АДРЕС = "127.0.0.1"
ПОРТ = 8091
КОНФИГ = os.path.expanduser("~/news-config.json")
СКРИПТ = "/home/user/bin/digest-news-en-ru.py"
ПРОГРЕСС = "/home/user/news-progress.json"
ПРОГРЕСС_ИСТ = "/home/user/news-progress-hist.json"

ДНИ = [("1", "Пн"), ("2", "Вт"), ("3", "Ср"), ("4", "Чт"),
       ("5", "Пт"), ("6", "Сб"), ("0", "Вс")]
ЛЕНТЫ_ИМЕНА = {
    "iran-tehrantimes": "Тегеран Таймс (Иран)",
    "iran-mehr": "Мехр (Иран)",
    "almasirah": "Аль-Масира (Йемен)",
    "ei": "The Electronic Intifada (Газа)",
    "strelkov": "Стрелков (Гиркин)",
}

СТИЛЬ = """
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 0; padding: 16px;
       background: #14161a; color: #e8e8ea; }
h1 { font-size: 20px; margin: 0 0 4px; }
.раздел { color: #8a8f98; font-size: 13px; text-transform: uppercase;
          letter-spacing: .06em; margin: 26px 0 10px; }
.когда { color: #8a8f98; font-size: 13px; margin-bottom: 10px; }
.строка { padding: 11px 13px; margin-bottom: 7px; border-radius: 10px;
          background: #1e2128; font-size: 15px; }
.живая { background: #1b3a24; }
.тускло { opacity: .5; }
.кнопка { display: inline-block; padding: 12px 16px; font-size: 16px;
          font-weight: 600; border: 0; border-radius: 10px;
          background: #2f6feb; color: #fff; }
.кнопка:active { background: #1f4fb0; }
.стоп { background: #b03434; }
.мелкая { padding: 8px 12px; font-size: 14px; }
.пара { display: flex; gap: 7px; align-items: center; margin-bottom: 7px; }
.пара .строка { flex: 1; margin-bottom: 0; }
.крестик { border: 0; border-radius: 10px; background: #b03434; color: #fff;
           font-size: 18px; width: 46px; height: 42px; }
.тумблер { border: 0; border-radius: 10px; font-size: 14px; padding: 0 14px;
           height: 42px; color: #fff; }
.вкл { background: #2f7a44; }
.выкл { background: #6a4a1b; }
input[type=text] { background: #1e2128; border: 1px solid #333; color: #e8e8ea;
                   border-radius: 8px; padding: 11px; font-size: 15px;
                   width: 100%; margin-bottom: 7px; }
label.день { display: inline-block; padding: 10px 12px; margin: 0 5px 6px 0;
             border-radius: 9px; background: #1e2128; font-size: 15px; }
label.день input { margin-right: 5px; }
.итог { padding: 13px; border-radius: 10px; background: #1b3a24;
        margin-bottom: 16px; font-size: 15px; white-space: pre-wrap; }
.бар { height: 14px; border-radius: 7px; background: #1e2128;
       overflow: hidden; margin: 8px 0; }
.бар-в { height: 100%; background: #2f7a44; border-radius: 7px;
         transition: width .4s; }
.мон { padding: 13px; border-radius: 10px; background: #1b3a24;
       font-size: 15px; }
.мон-простой { background: #1e2128; color: #8a8f98; }
.канал { margin-bottom: 12px; }
.макс { display: flex; align-items: center; gap: 8px; margin: 2px 0 0 6px;
        color: #8a8f98; font-size: 14px; }
.макс input[type=number] { width: 78px; margin: 0; padding: 8px; }
.макс button { height: 38px; width: 46px; }
"""


# ── КОНФИГ ────────────────────────────────────────────────────────
def читать_конфиг():
    try:
        return json.load(open(КОНФИГ, encoding="utf-8"))
    except Exception:
        return {"ютуб": [], "ленты_вкл": {}}


def писать_конфиг(cfg):
    tmp = КОНФИГ + ".новый"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, КОНФИГ)


def префикс_из_url(url, занятые):
    """Технический ярлык канала (для дедупа виденного). Из @имени в url,
    только латиница; если пусто или занято — добавляем цифру."""
    m = re.search(r"@([A-Za-z0-9_]+)", url)
    осн = (m.group(1) if m else re.sub(r"[^a-z0-9]", "", url.lower())[:12])
    осн = осн.lower()[:16] or "kanal"
    ярлык, н = осн, 1
    while ярлык in занятые:
        н += 1
        ярлык = f"{осн}{н}"
    return ярлык


# ── РАСПИСАНИЕ (cron) ─────────────────────────────────────────────
def читать_расписание():
    """(часы[], дни[]) из строки digest-news-en-ru в личном cron."""
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True,
                              text=True).stdout
    except Exception:
        return [], []
    for ln in cron.splitlines():
        if "digest-news-en-ru.py" in ln and not ln.strip().startswith("#"):
            поля = ln.split()
            if len(поля) >= 5:
                часы = [ч for ч in поля[1].split(",") if ч.isdigit()]
                дни = _развернуть_дни(поля[4])
                return часы, дни
    return [], []


def _развернуть_дни(поле):
    дни = set()
    for кусок in поле.split(","):
        if "-" in кусок:
            a, b = кусок.split("-")
            if a.isdigit() and b.isdigit():
                дни.update(str(x) for x in range(int(a), int(b) + 1))
        elif кусок.isdigit():
            дни.add(кусок)
    return sorted(дни)


def записать_расписание(часы, дни):
    """Переписать строку digest-news-en-ru в cron с новыми часами и днями."""
    часы = [ч for ч in часы if ч.isdigit()] or ["6"]
    дни = [д for д in дни if д.isdigit()] or ["2", "3", "4", "5", "6"]
    строка_часов = ",".join(str(int(ч)) for ч in sorted(set(часы), key=int))
    строка_дней = ",".join(sorted(set(дни), key=int))
    новая = (f"0 {строка_часов} * * {строка_дней} /usr/bin/python3 "
             f"{СКРИПТ} >> /home/user/digest-news-en-ru.log 2>&1")
    # первый выпуск минус 10 минут — поднять голос Fish
    первый = min(int(ч) for ч in часы)
    рыба = (f"50 {первый - 1} * * {строка_дней} "
            "/home/user/bin/ensure-fish.sh "
            ">> /home/user/fish-autostart.log 2>&1")
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True,
                              text=True).stdout
    except Exception:
        cron = ""
    out = []
    for ln in cron.splitlines():
        if "digest-news-en-ru.py" in ln and not ln.strip().startswith("#"):
            out.append(новая)
        elif "ensure-fish.sh" in ln and "@reboot" not in ln:
            out.append(рыба)
        else:
            out.append(ln)
    if not any("digest-news-en-ru.py" in l for l in out):
        out.append(новая)
    subprocess.run(["crontab", "-"], input="\n".join(out) + "\n", text=True)


# ── МОНИТОР ПРОГРЕССА ─────────────────────────────────────────────
# Порядок этапов и их примерная доля в общем времени (для общего бара).
ЭТАПЫ = [
    ("начинаю", 0.02),
    ("поднимаю модель пересказа", 0.15),
    ("смотрю ютуб", 0.55),          # самый долгий: скачивание + расшифровка
    ("текстовые ленты", 0.10),
    ("свожу выпуск по событиям", 0.08),
    ("озвучиваю и шлю", 0.10),
]


def _мин(сек):
    сек = int(max(0, сек))
    м, с = divmod(сек, 60)
    return f"{м} мин {с:02d} с" if м else f"{с} с"


def средняя_длительность():
    try:
        ист = json.load(open(ПРОГРЕСС_ИСТ))
        return sum(ист) / len(ист) if ист else None
    except Exception:
        return None


def читать_прогресс():
    try:
        return json.load(open(ПРОГРЕСС, encoding="utf-8"))
    except Exception:
        return None


def _общая_доля(p):
    """Грубая общая готовность 0..1 по номеру этапа и прогрессу внутри него."""
    этап = p.get("этап", "")
    накопл = 0.0
    for имя, доля in ЭТАПЫ:
        if этап.startswith(имя):
            внутри = 0.0
            if p.get("всего"):
                внутри = min(1.0, p.get("сделано", 0) / p["всего"])
            return накопл + доля * внутри
        накопл += доля
    return накопл


def монитор_html():
    p = читать_прогресс()
    сред = средняя_длительность()
    к = ["<div class='раздел'>Ход выпуска</div>"]
    if not p:
        к.append("<div class='мон мон-простой'>Выпуск ещё не запускался.</div>")
        return "".join(к), False
    идёт_сек = p.get("время", 0) - p.get("старт", 0)
    if not p.get("работает"):
        сообщение = {
            "готово": "Последний выпуск собран и отослан",
            "новых новостей нет": "Последний прогон: нового не было",
            "модель не поднялась": "Последний прогон: модель не поднялась",
        }.get(p.get("этап"), f"Последний прогон: {p.get('этап', '—')}")
        к.append(f"<div class='мон мон-простой'>✅ {html.escape(сообщение)} "
                 f"(шёл {_мин(идёт_сек)}).</div>")
        return "".join(к), False
    # идёт прямо сейчас
    доля = _общая_доля(p)
    процент = int(доля * 100)
    этап = p.get("этап", "…")
    если_счёт = (f" ({p.get('сделано')} из {p.get('всего')})"
                 if p.get("всего") else "")
    прогноз = ""
    if сред:
        осталось = сред - идёт_сек
        прогноз = (f"осталось примерно {_мин(осталось)}"
                   if осталось > 5 else "вот-вот закончит")
    else:
        прогноз = "первый прогон — прогноза пока нет"
    к.append(
        "<div class='мон'>"
        f"🟢 Идёт выпуск · {html.escape(этап)}{html.escape(если_счёт)}"
        f"<div class='бар'><div class='бар-в' style='width:{процент}%'></div></div>"
        f"<div class='когда' style='margin:0'>идёт {_мин(идёт_сек)} · "
        f"{html.escape(прогноз)}</div>"
        "</div>")
    return "".join(к), True


# ── СТРАНИЦА ──────────────────────────────────────────────────────
def страница(итог=None):
    cfg = читать_конфиг()
    часы, дни = читать_расписание()
    монитор, идёт = монитор_html()
    к = [f"<style>{СТИЛЬ}</style>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<title>Пульт новостей</title>"]
    if идёт:   # пока выпуск идёт — страница сама обновляется каждые 8 секунд
        к.append("<meta http-equiv='refresh' content='8'>")
    к.append("<h1>Пульт новостей</h1>")
    к.append("<div class='когда'>Дайджест в телеграм — расписание и каналы</div>")
    if итог:
        к.append(f"<div class='итог'>{html.escape(итог)}</div>")
    к.append(монитор)

    # ── Расписание ──
    к.append("<div class='раздел'>Когда выходят новости</div>")
    к.append("<form method='post' action='/raspisanie'>")
    к.append("<div class='когда'>Дни недели:</div><div>")
    for зн, имя in ДНИ:
        от = "checked" if зн in дни else ""
        к.append(f"<label class='день'><input type='checkbox' name='день' "
                 f"value='{зн}' {от}>{имя}</label>")
    к.append("</div>")
    к.append("<div class='когда' style='margin-top:8px'>Часы выпусков "
             "(через запятую, например 6,8,10,12,14):</div>")
    к.append(f"<input type='text' name='часы' value='{','.join(часы)}'>")
    к.append("<button class='кнопка'>Сохранить расписание</button></form>")

    # ── Ютуб-каналы ──
    к.append("<div class='раздел'>Ютуб-каналы</div>")
    for i, c in enumerate(cfg.get("ютуб", [])):
        вкл = c.get("вкл", True)
        берём = c.get("берём")
        знач = str(берём) if берём else ""
        к.append(
            "<div class='канал'>"
            "<div class='пара'>"
            f"<div class='строка {'живая' if вкл else 'тускло'}'>"
            f"{'🟢' if вкл else '⚪'} {html.escape(c.get('имя', '?'))}</div>"
            f"<form method='post' action='/kanal-toggle'>"
            f"<input type='hidden' name='i' value='{i}'>"
            f"<button class='тумблер {'вкл' if вкл else 'выкл'}'>"
            f"{'вкл' if вкл else 'выкл'}</button></form>"
            f"<form method='post' action='/kanal-del' "
            "onsubmit=\"return confirm('Удалить канал совсем?')\">"
            f"<input type='hidden' name='i' value='{i}'>"
            f"<button class='крестик'>✕</button></form>"
            "</div>"
            f"<form method='post' action='/kanal-max' class='макс'>"
            f"<input type='hidden' name='i' value='{i}'>"
            "<span>брать последних:</span>"
            f"<input type='number' name='n' min='0' value='{знач}' "
            "placeholder='все'>"
            "<button class='тумблер вкл'>✓</button>"
            "</form></div>")
    к.append("<div class='когда' style='margin-top:12px'>Добавить канал:</div>")
    к.append("<form method='post' action='/kanal-add'>"
             "<input type='text' name='имя' placeholder='Имя (например Ратбон)'>"
             "<input type='text' name='url' "
             "placeholder='Ссылка на ютуб-канал (…youtube.com/@Имя)'>"
             "<button class='кнопка'>Добавить канал</button></form>")

    # ── Текстовые ленты ──
    к.append("<div class='раздел'>Текстовые ленты</div>")
    ленты = cfg.get("ленты_вкл", {})
    for ключ, имя in ЛЕНТЫ_ИМЕНА.items():
        вкл = ленты.get(ключ, True)
        к.append(
            "<div class='пара'>"
            f"<div class='строка {'живая' if вкл else 'тускло'}'>"
            f"{'🟢' if вкл else '⚪'} {html.escape(имя)}</div>"
            f"<form method='post' action='/lenta-toggle'>"
            f"<input type='hidden' name='k' value='{ключ}'>"
            f"<button class='тумблер {'вкл' if вкл else 'выкл'}'>"
            f"{'вкл' if вкл else 'выкл'}</button></form>"
            "</div>")

    # ── Запустить выпуск сейчас ──
    к.append("<div class='раздел'>Проверка</div>")
    к.append("<form method='post' action='/run-now'>"
             "<button class='кнопка мелкая'>Собрать выпуск сейчас</button>"
             "</form>"
             "<div class='когда'>Пустит новостной прогон вручную (идёт "
             "полчаса-час, голосовые придут в телеграм сами).</div>")
    return "".join(к).encode()


# ── ДЕЙСТВИЯ ──────────────────────────────────────────────────────
def сохранить_расписание(поля):
    дни = поля.get("день", [])
    часы = [ч.strip() for ч in (поля.get("часы", [""])[0]).replace(" ", "").split(",")
            if ч.strip().isdigit()]
    if not часы:
        return "часы не разобраны — впиши, например, 6,8,10,12,14"
    if not дни:
        return "выбери хотя бы один день недели"
    записать_расписание(часы, дни)
    имена = ", ".join(dict(ДНИ)[д] for д in sorted(set(дни), key=int)
                      if д in dict(ДНИ))
    return f"расписание сохранено: {','.join(sorted(set(часы), key=int))} ч, дни {имена}"


def добавить_канал(поля):
    имя = (поля.get("имя", [""])[0]).strip()
    сырьё = (поля.get("url", [""])[0]).strip()
    if not имя:
        return "впиши имя канала"
    # принимаем ссылку в любом виде: полный url, youtube.com/@Имя,
    # @Имя или просто Имя — вытаскиваем @-хэндл и строим ссылку сами
    m = (re.search(r"@([A-Za-z0-9_.\-]+)", сырьё)
         or re.search(r"youtube\.com/(?:c/|channel/|user/)?([A-Za-z0-9_.\-]+)",
                      сырьё, re.I))
    хэндл = (m.group(1) if m else сырьё).lstrip("@").strip()
    if not хэндл:
        return "впиши ссылку на ютуб-канал или его @имя"
    url = f"https://www.youtube.com/@{хэндл}/videos"
    cfg = читать_конфиг()
    # уже есть такой канал? (по @хэндлу в ссылке) — не плодим дубли
    хл = хэндл.lower()
    for c in cfg.get("ютуб", []):
        if хл == (re.search(r"@([A-Za-z0-9_.\-]+)", c.get("url", "")) or
                  [None, ""])[1].lower():
            return f"канал @{хэндл} уже в списке — «{c.get('имя','?')}»"
    занятые = {c.get("prefix") for c in cfg.get("ютуб", [])}
    cfg.setdefault("ютуб", []).append({
        "имя": имя, "url": url,
        "prefix": префикс_из_url(url, занятые),
        "лимит": 15, "берём": None, "lang": "english",
        "intro": f"Ролики канала {имя} с ютуба.", "вкл": True,
    })
    писать_конфиг(cfg)
    return f"канал «{имя}» добавлен"


def удалить_канал(поля):
    try:
        i = int(поля.get("i", ["-1"])[0])
    except Exception:
        return "канал не выбран"
    cfg = читать_конфиг()
    ю = cfg.get("ютуб", [])
    if 0 <= i < len(ю):
        имя = ю[i].get("имя", "?")
        del ю[i]
        писать_конфиг(cfg)
        return f"канал «{имя}» удалён"
    return "канал не найден"


def тумблер_канала(поля):
    try:
        i = int(поля.get("i", ["-1"])[0])
    except Exception:
        return "канал не выбран"
    cfg = читать_конфиг()
    ю = cfg.get("ютуб", [])
    if 0 <= i < len(ю):
        ю[i]["вкл"] = not ю[i].get("вкл", True)
        писать_конфиг(cfg)
        return f"канал «{ю[i].get('имя','?')}» — {'вкл' if ю[i]['вкл'] else 'выкл'}"
    return "канал не найден"


def макс_канала(поля):
    """Сколько последних выпусков брать: число → берём=N, пусто/0 → все."""
    try:
        i = int(поля.get("i", ["-1"])[0])
    except Exception:
        return "канал не выбран"
    n_сыр = (поля.get("n", [""])[0]).strip()
    cfg = читать_конфиг()
    ю = cfg.get("ютуб", [])
    if not (0 <= i < len(ю)):
        return "канал не найден"
    if n_сыр and n_сыр.isdigit() and int(n_сыр) > 0:
        ю[i]["берём"] = int(n_сыр)
        весть = f"брать последних: {int(n_сыр)}"
    else:
        ю[i]["берём"] = None
        весть = "брать все новые"
    писать_конфиг(cfg)
    return f"канал «{ю[i].get('имя','?')}» — {весть}"


def тумблер_ленты(поля):
    ключ = (поля.get("k", [""])[0])
    if ключ not in ЛЕНТЫ_ИМЕНА:
        return "лента не найдена"
    cfg = читать_конфиг()
    л = cfg.setdefault("ленты_вкл", {})
    л[ключ] = not л.get(ключ, True)
    писать_конфиг(cfg)
    return f"«{ЛЕНТЫ_ИМЕНА[ключ]}» — {'вкл' if л[ключ] else 'выкл'}"


def запустить_сейчас():
    try:
        лог = open("/home/user/digest-news-en-ru.log", "a")
        subprocess.Popen(["/usr/bin/python3", СКРИПТ],
                         stdout=лог, stderr=лог, start_new_session=True)
        return ("запускаю выпуск — идёт полчаса-час; голосовые придут "
                "в телеграм сами")
    except Exception as e:
        return f"сбой запуска: {str(e)[:120]}"


class Пульт(BaseHTTPRequestHandler):
    def _ответ(self, тело):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(тело)))
        self.end_headers()
        self.wfile.write(тело)

    def _поля(self):
        длина = int(self.headers.get("Content-Length") or 0)
        тело = self.rfile.read(длина).decode("utf-8", "replace")
        return urllib.parse.parse_qs(тело)

    def do_GET(self):
        self._ответ(страница())

    def do_POST(self):
        путь = self.path.rstrip("/")
        поля = self._поля()
        if путь == "/raspisanie":
            итог = сохранить_расписание(поля)
        elif путь == "/kanal-add":
            итог = добавить_канал(поля)
        elif путь == "/kanal-del":
            итог = удалить_канал(поля)
        elif путь == "/kanal-toggle":
            итог = тумблер_канала(поля)
        elif путь == "/kanal-max":
            итог = макс_канала(поля)
        elif путь == "/lenta-toggle":
            итог = тумблер_ленты(поля)
        elif путь == "/run-now":
            итог = запустить_сейчас()
        else:
            итог = None
        self._ответ(страница(итог))

    def log_message(self, формат, *аргументы):
        print(f"заход: {self.client_address[0]} {self.command} {self.path}",
              flush=True)


def main():
    import time
    сервер = None
    for _ in range(36):
        try:
            сервер = ThreadingHTTPServer((АДРЕС, ПОРТ), Пульт)
            break
        except OSError as ошибка:
            print(f"адрес {АДРЕС} ещё не готов ({ошибка}), жду 5 секунд",
                  flush=True)
            time.sleep(5)
    if сервер is None:
        sys.exit(f"адрес {АДРЕС} так и не появился — Tailscale не поднялся")
    print(f"пульт новостей слушает http://{АДРЕС}:{ПОРТ}", flush=True)
    сервер.serve_forever()


if __name__ == "__main__":
    main()
