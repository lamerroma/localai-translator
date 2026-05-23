# translate_server.py

Локальний веб-сервер для перекладу тексту та файлів через LLM (LocalAI / OpenAI-сумісний API).  
Написаний на FastAPI, інтерфейс — одна HTML-сторінка у браузері, відповідь стрімиться через SSE.

---

## Запуск

```bash
python PDFMathTranslate/translate_server.py
```

Або через uvicorn:

```bash
uvicorn PDFMathTranslate.translate_server:app --host 0.0.0.0 --port 7860
```

Інтерфейс: `http://<ip-сервера>:7860`

---

## Конфігурація

Налаштування зберігаються у файлі `translator_config.json` поруч зі скриптом.  
При запуску файл завантажується автоматично; якщо його немає — використовуються стандартні значення.  
Змінити можна через веб-інтерфейс (розділ **Налаштування** внизу сторінки) або вручну в JSON.

| Параметр          | За замовчуванням                                  | Опис |
|-------------------|---------------------------------------------------|------|
| `base_url`        | `http://192.168.200.92:30286/v1`                  | Адреса LocalAI (OpenAI-сумісний endpoint) |
| `model`           | `yanolja_yanoljanext-rosetta-12b-2510`            | Назва моделі в LocalAI |
| `max_tokens`      | `2048`                                            | Максимум токенів у відповіді LLM |
| `llm_timeout`     | `180`                                             | Таймаут запиту до LLM (секунди) |
| `chunk_size`      | `3000`                                            | Розмір чанка тексту (символів) при перекладі файлів |
| `system_msg`      | `You are a professional translator...`            | Системне повідомлення для LLM |
| `prompt_template` | `{text}\n\n§ Translate the text above {direction}...` | Шаблон промпту |

Зміни набувають чинності одразу без перезапуску сервера.

---

## Підтримувані мови

34 мови: Arabic, Bulgarian, Chinese, Czech, Danish, Dutch, English, Esperanto, Finnish, French, German, Greek, Gujarati, Hebrew, Hindi, Hungarian, Indonesian, Italian, Japanese, Korean, Latin, Persian, Polish, Portuguese, Romanian, Russian, Slovak, Spanish, Swedish, Tagalog, Thai, Turkish, Ukrainian, Vietnamese.

Мову джерела можна не вказувати — у списку є **Автовизначення** (модель сама визначить мову з контексту).

---

## API

### `GET /`
Повертає HTML веб-інтерфейс.

---

### `GET /languages`
Повертає JSON-масив назв підтримуваних мов (відсортовано за алфавітом).

---

### `GET /config`
Повертає поточну конфігурацію (JSON).

---

### `GET /config/defaults`
Повертає стандартні значення конфігурації (JSON). Використовується кнопкою «Скинути до стандартних».

---

### `POST /config`
Зберігає нову конфігурацію.

**Тіло (JSON):** об'єкт з будь-якими ключами з таблиці конфігурації вище.  
**Відповідь:** `{"ok": true}` або `{"ok": false, "error": "..."}`.

---

### `POST /translate`
Переклад тексту. Відповідь — Server-Sent Events (SSE) стрім.

**Тіло (JSON):**

| Поле              | Тип    | Обов'язково | Опис |
|-------------------|--------|-------------|------|
| `text`            | string | так         | Текст для перекладу |
| `lang_from`       | string | так         | Мова оригіналу (назва, напр. `"German"`) або `"auto"` |
| `lang_to`         | string | так         | Цільова мова (назва, напр. `"Ukrainian"`) |
| `system_msg`      | string | ні          | Перевизначає системне повідомлення для цього запиту |
| `prompt_template` | string | ні          | Перевизначає шаблон промпту для цього запиту |
| `context`         | string | ні          | Тип документу (напр. `"технічний посібник"`) |

**SSE події:**

| `type`   | Поля               | Опис |
|----------|--------------------|------|
| `id`     | `text`             | Унікальний ID запиту (для зупинки через `/stop/{id}`) |
| `log`    | `text`             | Рядок лог-виводу |
| `token`  | `text`             | Черговий токен від LLM (стрімінг у реальному часі) |
| `result` | `text`             | Фінальний результат перекладу |
| `error`  | `text`             | Повідомлення про помилку |
| `done`   | —                  | Кінець стріму |

---

### `POST /translate-file`
Переклад файлу. Відповідь — SSE стрім.

**Тіло (multipart/form-data):**

| Поле              | Тип  | Обов'язково | Опис |
|-------------------|------|-------------|------|
| `file`            | file | так         | Файл (PDF, DOCX, TXT) |
| `lang_from`       | str  | ні          | Мова оригіналу або `"auto"` (default) |
| `lang_to`         | str  | ні          | Цільова мова (default: `Ukrainian`) |
| `system_msg`      | str  | ні          | Перевизначає системне повідомлення |
| `prompt_template` | str  | ні          | Перевизначає шаблон промпту |
| `context`         | str  | ні          | Тип документу |

**Додаткові SSE події (крім тих що в `/translate`):**

| `type`     | Поля              | Опис |
|------------|-------------------|------|
| `progress` | `text`, `pct`     | Статус (`"Chunk 2/5..."`) та відсоток (0–100) |
| `download` | `url`, `filename` | Шлях для завантаження та ім'я файлу |

---

### `POST /stop/{request_id}`
Перериває активний переклад.

**Параметр:** `request_id` — ID з події `id` на початку стріму.  
**Відповідь:** `{"ok": true}`

---

### `GET /download/{file_id}`
Завантаження перекладеного файлу (`.txt`, UTF-8).  
ID береться з події `download.url` після завершення `/translate-file`.

---

## Шаблон промпту

За замовчуванням:

```
{text}

§ Translate the text above {direction}. This is a {context}.
Keep all ⟨P⟩ and ⟨N⟩ markers exactly in place — they mark paragraph and line breaks.
Preserve technical terms, proper nouns, and numbers exactly.
Do not add notes or commentary. Output ONLY the translation. §
```

**Плейсхолдери:**

| Плейсхолдер  | Значення |
|--------------|----------|
| `{text}`      | Текст для перекладу (з маркерами `⟨P⟩`/`⟨N⟩`) |
| `{direction}` | `"from de into uk"` або `"into uk"` (якщо Автовизначення) |
| `{lang}`      | Код цільової мови, напр. `uk` |
| `{context}`   | Тип документу, напр. `document` |

**Маркери `⟨P⟩` / `⟨N⟩`** замінюють `\n\n` і `\n` перед відправкою в LLM і відновлюються у відповіді.  
**`§...§`** — роздільник: сервер відкидає все після першого `§`, щоб прибрати коментарі моделі.

---

## Обробка файлів

| Формат | Бібліотека    | Що витягується |
|--------|---------------|----------------|
| PDF    | `pypdf`       | Текст з кожної сторінки |
| DOCX   | `python-docx` | Текст з параграфів |
| TXT    | —             | Вміст файлу як є (UTF-8) |

Текст розбивається на чанки по `chunk_size` символів по межах параграфів.  
Кожен чанк перекладається окремим запитом до LLM.  
Результат зберігається в пам'яті сервера і доступний для завантаження за посиланням.

---

## Залежності

```
fastapi
uvicorn
requests
pypdf
python-docx
pydantic
```

Встановити:

```bash
pip install fastapi uvicorn requests pypdf python-docx
```
