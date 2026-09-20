#!/usr/bin/env bash
# Озвучивает русский текст (локальный Fish Speech) и пишет ГОЛОСОВОЙ ogg/opus
# в указанный файл. НЕ отправляет в телеграм — это делает вызывающий пакетом.
# Брат say-tg.sh, но вместо sendVoice — запись в файл.
#
# Usage:  say-to-ogg.sh "текст" /путь/выход.ogg
# Голос:  SAY_TG_VOICE=adam|river|matroskin|filatov|puffin (по умолчанию adam)
set -uo pipefail

TEXT="${1:-}"
OUT="${2:-}"
[ -z "${TEXT// }" ] && { echo "say-to-ogg: пустой текст" >&2; exit 1; }
[ -z "$OUT" ] && { echo "say-to-ogg: не задан выходной файл" >&2; exit 1; }

VOICE="${SAY_TG_VOICE:-adam}"
# Где живёт Фиш: на 5090 — в ~/avatar, на Спарке — в ~/fish (перенос 17.09.2026)
if [ -d "$HOME/fish/venv" ]; then
  GV="$HOME/bin/gen_voice_fish.sh"; KOPILKA="$HOME/fish/voice_cache"
  PYCHECK="$HOME/fish/venv/bin/python"
else
  GV="$HOME/avatar/gen_voice_fish.sh"; KOPILKA="$HOME/avatar/voice_cache_fish"
  PYCHECK="$HOME/tts_test/venv_fish/bin/python"
fi
MAX_CHARS=600
TMP="$(mktemp -d /tmp/say_ogg.XXXXXX)"
LIST="$TMP/list.txt"
WAV="$TMP/full.wav"

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# --- Режем текст на куски по предложениям (Fish давится большим блоком) ---
N="$(TEXT="$TEXT" MAX="$MAX_CHARS" DIR="$TMP" python3 - <<'PY'
import os, re
text = os.environ["TEXT"]; maxc = int(os.environ["MAX"]); outdir = os.environ["DIR"]
# Абзац = отдельная новость. Куски НЕ ПЕРЕСЕКАЮТ границу абзаца —
# иначе конец одной новости и начало другой попадают в один кусок и
# читаются слитно (автор 2026-09-17: «конец одного блока и начало
# следующего получаются слитно, без паузы»).
abzacy = [a.strip() for a in re.split(r'\n\s*\n|\n', text) if a.strip()]
def hard_split(s):
    out, line = [], ""
    for w in s.split():
        if line and len(line)+1+len(w) > maxc: out.append(line); line = w
        else: line = (line+" "+w).strip()
    if line: out.append(line)
    return out
chunks, granicy = [], []      # granicy[i] = True, если кусок закрывает абзац
for abz in abzacy:
    sentences = [p.strip() for p in re.split(r'(?<=[.!?…])\s+', abz) if p.strip()]
    свои, cur = [], ""
    for s in sentences:
        if len(s) > maxc:
            if cur: свои.append(cur); cur = ""
            свои.extend(hard_split(s)); continue
        if cur and len(cur)+1+len(s) > maxc: свои.append(cur); cur = s
        else: cur = (cur+" "+s).strip()
    if cur: свои.append(cur)
    for j, c in enumerate(свои):
        chunks.append(c)
        granicy.append(j == len(свои) - 1)
for i,c in enumerate(chunks):
    open(os.path.join(outdir, f"{i:03d}.txt"),"w").write(c)
    if granicy[i]:
        open(os.path.join(outdir, f"{i:03d}.abzac"),"w").write("1")
print(len(chunks))
PY
)"
[ -n "$N" ] && [ "$N" -ge 1 ] 2>/dev/null || { echo "say-to-ogg: не удалось разбить текст" >&2; exit 1; }

# --- Озвучиваем каждый кусок через Fish ---
: > "$LIST"
i=0
while [ "$i" -lt "$N" ]; do
  idx="$(printf '%03d' "$i")"
  CW="$TMP/$idx.wav"
  # Сторож заикания (2026-09-01, жалоба автора «слышу повторяющиеся заикания»):
  # Fish иногда срывается в петлю и по тридцать раз повторяет один слог.
  # Такой кусок всегда НЕНОРМАЛЬНО ДЛИННЫЙ для своего текста — ловим по
  # длительности и переозвучиваем заново (перед этим стираем испорченную
  # запись из копилки, иначе вернётся она же).
  CHUNK_TEXT="$(cat "$TMP/$idx.txt")"
  TRY=1
  while : ; do
    if ! GV_VOICE_NAME="$VOICE" bash "$GV" "$CHUNK_TEXT" "$CW" >/dev/null 2>&1 \
         || [ ! -s "$CW" ]; then
      echo "say-to-ogg: Fish недоступен (кусок $((i+1))/$N)" >&2; exit 1
    fi
    # Проверка на застревание — по самому звуку (настроена на живом сбое
    # 1 сентября: «песо-песо-песа…» двадцать секунд подряд).
    if NOTE=$("$PYCHECK" \
         "$HOME/bin/проверка-заикания.py" "$CW" 2>&1 >/dev/null); then
      break                       # чисто
    fi
    if [ "$TRY" -ge 3 ]; then
      echo "say-to-ogg: кусок $((i+1)) заикается и после трёх попыток — беру как есть" >&2
      break
    fi
    echo "say-to-ogg: кусок $((i+1)) — $NOTE; переозвучиваю (попытка $TRY)" >&2
    KEY=$(printf '%s' "$VOICE|$CHUNK_TEXT" | md5sum | cut -d' ' -f1)
    rm -f "$KOPILKA/$KEY.wav"
    TRY=$((TRY + 1))
  done
  echo "file '$CW'" >> "$LIST"
  # Пауза между кусками: внутри новости короткая, между новостями длиннее.
  # Раньше куски клеились впритык, и фразы наезжали друг на друга.
  if [ "$((i + 1))" -lt "$N" ]; then
    if [ -f "$TMP/$idx.abzac" ]; then PAUZA=0.75; else PAUZA=0.3; fi
    TIH="$TMP/${idx}_pauza.wav"
    ffmpeg -y -f lavfi -i anullsrc=r=44100:cl=mono -t "$PAUZA" \
      -sample_fmt s16 "$TIH" >/dev/null 2>&1 \
      && echo "file '$TIH'" >> "$LIST"
  fi
  i=$((i + 1))
done

# --- Склейка и кодирование в ogg/opus (как в say-tg: голосовое, авто-проигрывание) ---
ffmpeg -y -f concat -safe 0 -i "$LIST" -ar 48000 -ac 1 "$WAV" >/dev/null 2>&1 \
  && [ -s "$WAV" ] || { echo "say-to-ogg: ffmpeg concat не удался" >&2; exit 1; }
# Отделка по голосу. voice_fr собран из записей Европарламента (16 кГц):
# верхов в исходнике нет, поэтому досинтезируем «воздух» и поднимаем
# громкость до обычных для голосовых -14 (было -18, автор 2026-09-14:
# «а можно сделать его чуть погромче?»). Остальные голоса не трогаем.
case "$VOICE" in
  # VOICE_FR — француз для канала. Образцу верхние частоты достроила
  # программа resemble-enhance (в записи Европарламента их не было),
  # а поверх — подъём на 3 кГц: там разборчивость согласных.
  # Выбрано автором 2026-09-15 из семи проб («#4 самый лучший»).
  voice_fr) POLISH="equalizer=f=3000:width_type=o:width=1.2:g=3" ;;
  # voice_fr2 собран с образца, которому верхние частоты ДОСТРОИЛА
  # программа (resemble-enhance) — досинтезировать ещё раз не нужно,
  # только выравниваем громкость
  # английский голос канала — звучание не трогаем, только доводим
  # громкость до той же, что у французского (иначе каналы звучат по-разному)
  voice_alt) POLISH="anull" ;;
  # voice_en — английский голос канала (River, опущенный и ускоренный).
  # Звучание не трогаем, но громкость доводим, как у прочих голосов канала:
  # без этого он выходил на -28 вместо -15 (поймано 20.09.2026).
  voice_en) POLISH="anull" ;;
  # chtets6 — французский чтец из открытой аудиокниги (с 20.09.2026).
  # Звучание не трогаем: верхи у него настоящие, достраивать нечего.
  # Но громкость доводим — сырым он выходит на -20 вместо -15.
  chtets6) POLISH="anull" ;;
  voice_fr2) POLISH="anull" ;;
  *)        POLISH="" ;;
esac
if [ -n "$POLISH" ]; then
  # Громкость доводим В ДВА ПРОХОДА: сперва мерим, потом поднимаем ровно
  # на недостающее. Обычный loudnorm за один проход не тянет: речь с
  # достроенного образца выходит около -28, а ему столько не осилить
  # (упирался в -18 вместо -14).
  # сначала отделка, потом замер (она сама добавляет громкости),
  # и только потом точный подъём до -15
  POLWAV="$TMP/polished.wav"
  ffmpeg -y -i "$WAV" -af "$POLISH" "$POLWAV" >/dev/null 2>&1
  CUR=$(ffmpeg -nostdin -hide_banner -i "$POLWAV" -af ebur128 -f null - 2>&1 \
        | grep -oP '^\s+I:\s+\K-?[0-9.]+' | tail -1)
  GAIN=$(awk -v c="${CUR:--23}" 'BEGIN{printf "%.1f", (-15) - c}')
  ffmpeg -y -i "$POLWAV" -af "volume=${GAIN}dB,alimiter=limit=0.89" \
    -c:a libopus -b:a 128k -vbr on -compression_level 10 \
    -application audio -ar 48000 -ac 1 "$OUT" >/dev/null 2>&1 \
    && [ -s "$OUT" ] || { echo "say-to-ogg: ffmpeg -> opus не удался" >&2; exit 1; }
  exit 0
fi
ffmpeg -y -i "$WAV" -c:a libopus -b:a 128k -vbr on -compression_level 10 \
  -application audio -ar 48000 -ac 1 "$OUT" >/dev/null 2>&1 \
  && [ -s "$OUT" ] || { echo "say-to-ogg: ffmpeg -> opus не удался" >&2; exit 1; }
exit 0
