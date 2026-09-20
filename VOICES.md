# Дикторы / Voices

Голос клонируется локальным Fish Speech по эталону: паре файлов
`<имя>_ref.wav` (около 45 секунд речи) и `<имя>.txt` (её расшифровка).
Имя голоса указывается в `news-channels.json` для языкового канала
и в `SAY_TG_VOICE` для русского выпуска.

## Откуда брать эталон

Годится любая запись одного человека длиной 40–60 секунд. Важны два
показателя, и мерить надо оба — я однажды отобрал голоса только по
яркости и получил шипящие записи:

| показатель | как мерить | что годится |
|---|---|---|
| запас тишины | громкость речи минус громкость пауз | больше 60 дБ |
| ширина полосы | доля энергии выше 10 кГц | больше 3 % |

Открытые источники, из которых собраны голоса этого проекта:

* **аудиокниги общественного достояния** (archive.org, LibriVox) —
  чтецы-носители языка, запись 44,1 кГц;
* **VoxPopuli** — записи Европарламента, лицензия CC0, но 16 кГц:
  верхних частот нет, их приходится достраивать.

## Сборка эталона

1. **Шум.** Тихую, но глухую запись чистить мягко — только шум, без
   достройки верхов: `resemble-enhance --denoise_only`. Полная достройка
   придумывает верхние частоты, и на шипящих «сё-фё-шу-сю» это слышно.
2. **Темп.** Чтецы книг читают 120–165 слов в минуту, для новостей это
   вяло. Ускорять сам эталон: `rubberband tempo=1.3 formant=preserved`
   (сохранение формант обязательно, иначе голос звучит «подкрученно»).
   Точного попадания через эталон не ждите: связь не прямая, ускорение
   1,57 / 1,35 / 1,46 дало 216 / 170 / 155 слов в минуту. Нужна точность —
   растягивайте готовую речь, там арифметика.
3. **Промежутки между словами.** Обрезать прямо в эталоне —
   `obrezat-pauzy.py эталон.wav готовый.wav 60 180`. Голос перенимает
   ритм эталона, и доводка после озвучки не нужна. Мягкая обрезка
   (150/350) не переносится, жёсткая (60/180) — переносится.
4. **Громкость.** Новый голос канала нужно вписать в `say-to-ogg.sh`
   (список `case "$VOICE"`), иначе он выходит на −20…−28 вместо −15 и
   звучит заметно тише остальных.

## Подводные камни

* Fish держит разобранный эталон **в памяти**: подменили `*_ref.wav` —
  перезапускайте сервер, иначе звучит старое.
* Копилка готовых записей лежит по ключу `md5("голос|текст")` — её тоже
  надо чистить, иначе вернётся прежняя озвучка.
* Понижать голос сдвигом высоты можно, но выговор при этом портится:
  детский английский голос, опущенный до мужского, по-французски зазвучал
  карикатурно. Лучше искать носителя языка, чем переделывать чужого.

## О лицензиях

Эталоны, собранные из записей общественного достояния и CC0, выкладывать
можно. Эталоны, снятые с платных синтезаторов речи, здесь **не
публикуются** — только способ сборки.

---

# Voices (English)

A voice is cloned by local Fish Speech from a reference pair:
`<name>_ref.wav` (about 45 seconds of speech) and `<name>.txt` (its
transcript). Pick a source recording with a **noise gap above 60 dB** and
**more than 3 % of energy above 10 kHz** — measure both, not just one.

Public-domain audiobooks (archive.org / LibriVox) and VoxPopuli
(CC0, European Parliament, 16 kHz) both work. Clean noise with
`resemble-enhance --denoise_only`; full enhancement invents high
frequencies and makes sibilants sound wrong. Speed the reference with
`rubberband tempo=… formant=preserved`, and trim word gaps in the
reference itself (`obrezat-pauzy.py ref.wav out.wav 60 180`) — the clone
inherits the rhythm, so no post-processing is needed.

Restart the Fish server after replacing a reference file: the parsed
reference is held in memory. Add every new channel voice to the
`case "$VOICE"` list in `say-to-ogg.sh`, or it will be far too quiet.

Reference files derived from paid speech synthesisers are **not**
published here — only the method.
