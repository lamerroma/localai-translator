# План розвитку проєкту

## Поточний стан

Локальний веб-сервер для перекладу тексту та документів (FastAPI + Uvicorn).
Бекенд: **Ollama** на `http://localhost:11434/v1`, модель **TranslateGemma 12B**.
GPU: відеокарта з **12 GB VRAM** — модель повністю у VRAM.
Контекстне вікно: **8k** (`max_tokens: 4096`, `chunk_size: 6000`).

Що працює зараз:
- Переклад тексту зі стримінгом токенів у реальному часі
- Кнопка зупинки
- Переклад файлів (PDF, DOCX, TXT) — але **без збереження форматування** (вивід у TXT)
- Налаштування через веб-інтерфейс без перезапуску

---

## Крок 1 — Розгорнути LinguaHaru (короткостроково)

Поки власний сервер розвивається — розгорнути готове рішення для перекладу документів з форматуванням.

**Репозиторій:** https://github.com/YANG-Haruka/LinguaHaru

### Що дає LinguaHaru
- DOCX зі збереженням форматування (JSON pipeline → записує назад у DOCX)
- PDF зі збереженням макету (через `babeldoc`)
- XLSX, PPTX як бонус
- Ollama підключений
- Білінгвальний режим (оригінал + переклад)
- Глосарій термінів

### Що потрібно налаштувати
1. Змінити модель в `config/system_config.json`:
   ```json
   "default_local_model": "(Ollama) translategemma:12b",
   "max_token": 4096,
   "default_src_lang": "English",
   "default_dst_lang": "Ukrainian"
   ```
2. Додати **Ukrainian** в `config/languages_config.py` (у базовому списку відсутня)
3. Створити промпт-файл для української мови

### Розгортання
```bash
git clone https://github.com/YANG-Haruka/LinguaHaru
cd LinguaHaru
pip install -r requirements.txt
python app.py
# http://localhost:9980
```

### Обмеження LinguaHaru
- Gradio UI — важко кастомізувати
- Немає швидкого перекладу тексту (тільки файли)
- При 5 одночасних користувачах — черга (обмеження GPU, не LinguaHaru)

---

## Крок 2 — Розглянути HY-MT замість TranslateGemma (середньостроково)

**Репозиторій:** https://github.com/Tencent-Hunyuan/HY-MT

Модель від Tencent, переможець WMT25. Спеціалізована на переклад.

| | TranslateGemma 12B | HY-MT 7B |
|---|---|---|
| VRAM | ~8 GB (Q4) | ~5 GB |
| Спеціалізація | переклад | переклад (fine-tuned) |
| Захист від зациклювань | немає | `repetition_penalty: 1.05` |
| Контекстний переклад | немає | є |

GGUF версія доступна → завантажується в Ollama через Modelfile.

Рекомендовані параметри інференсу:
```
top_k: 20 / top_p: 0.6 / temperature: 0.7 / repetition_penalty: 1.05
```

---

## Крок 3 — Розвиток власного сервера (довгостроково)

Власний FastAPI-сервер має переваги яких немає в LinguaHaru:
- Швидкий переклад тексту зі стримінгом
- Повний контроль над UI та промптом
- Легковісний (без Gradio/CUDA залежностей)
- Краще для одночасних користувачів (кожен бачить свій прогрес)

### Що потрібно реалізувати

**DOCX з форматуванням** — запозичити підхід з:
- https://github.com/petkovplamen1989/local-docx-translator (переклад на рівні `run`)
- https://github.com/YANG-Haruka/LinguaHaru (JSON pipeline, таблиці, Smart Layout Protection)

Замість поточного `extract_text()` → плаский TXT:
```
DOCX → ітерувати paragraphs + tables → перекласти кожен run → зберегти .docx
```
Smart Layout Protection: якщо переклад на 30%+ довший → зменшити шрифт на 1pt.

**PDF** — використати `babeldoc` напряму з Ollama-адаптером:
```python
class OllamaTranslator(BaseTranslator):
    def do_translate(self, text):
        return call_ollama(text)
```

**Параметри Ollama** — передавати `options` в запит:
```json
"options": {"num_ctx": 8192, "repetition_penalty": 1.05}
```

**Prompt для HY-MT** — спрощений формат:
```
Translate to {lang}:
{text}
```

### Інші розглянуті проєкти

| Проєкт | Посилання | Висновок |
|--------|-----------|----------|
| local-docx-translator | https://github.com/petkovplamen1989/local-docx-translator | Запозичити run-рівневий підхід для DOCX |
| LinguaHaru | https://github.com/YANG-Haruka/LinguaHaru | Використати як готове рішення зараз |
| DocuTranslate | https://github.com/xunbu/docutranslate | Немає Ollama — не підходить |
| LLM_PDF_Translator | https://github.com/poppanda/LLM_PDF_Translator | Проєкт мертвий |
| local-translator | https://github.com/dwain-barnes/local-translator | Занадто просто, нічого нового |
| HY-MT | https://github.com/Tencent-Hunyuan/HY-MT | Розглянути як основну модель |

---

## Чому відмовились від попередніх рішень

| Рішення | Причина відмови |
|---------|-----------------|
| **LocalAI** | Перехід на Ollama — GPU 12 GB VRAM, швидший інференс |
| **Yanolja Rosetta 12B** | Схильна до зациклювань при перекладі |
| **TranslateGemma 4B** | Перехід на 12B — дозволяє GPU |
