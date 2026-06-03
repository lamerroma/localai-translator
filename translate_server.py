import io
import json
import os
import sqlite3
import logging
import traceback
import datetime
import uuid
import threading
import queue as _queue_mod
import secrets
import base64
import time
from urllib.parse import quote
import requests as req_lib
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("translator")

# ── Configuration ────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "translator_config.json")

DEFAULTS = {
    "base_url":        "http://127.0.0.1:11434/v1",
    "model":           "rinex20/translategemma3:12b",
    "max_tokens":      2048,
    "llm_timeout":     180,
    "chunk_size":      3000,
    "temperature":     0.7,
    "retry":           2,
    "insert_mode":     "replace",
    "separator":       "\n",
    "custom_prompt":   "",
    "auth_user":       "admin",
    "auth_pass":       "translate",
    "max_pdf_pages":   10,
    "max_chars":       30000,
}

HOST = "0.0.0.0"
PORT = 7860

LANG_NAMES_UK = {
    "Arabic":     "Арабська",
    "Bulgarian":  "Болгарська",
    "Chinese":    "Китайська",
    "Czech":      "Чеська",
    "Danish":     "Данська",
    "Dutch":      "Нідерландська",
    "English":    "Англійська",
    "Esperanto":  "Есперанто",
    "Finnish":    "Фінська",
    "French":     "Французька",
    "German":     "Німецька",
    "Greek":      "Грецька",
    "Gujarati":   "Гуджараті",
    "Hebrew":     "Іврит",
    "Hindi":      "Гінді",
    "Hungarian":  "Угорська",
    "Indonesian": "Індонезійська",
    "Italian":    "Італійська",
    "Japanese":   "Японська",
    "Korean":     "Корейська",
    "Latin":      "Латинська",
    "Persian":    "Перська",
    "Polish":     "Польська",
    "Portuguese": "Португальська",
    "Romanian":   "Румунська",
    "Russian":    "Російська",
    "Slovak":     "Словацька",
    "Spanish":    "Іспанська",
    "Swedish":    "Шведська",
    "Tagalog":    "Тагальська",
    "Thai":       "Тайська",
    "Turkish":    "Турецька",
    "Ukrainian":  "Українська",
    "Vietnamese": "В'єтнамська",
}

LANG_MAP = {
    "Arabic":     "ar",
    "Bulgarian":  "bg",
    "Chinese":    "zh",
    "Czech":      "cs",
    "Danish":     "da",
    "Dutch":      "nl",
    "English":    "en",
    "Esperanto":  "eo",
    "Finnish":    "fi",
    "French":     "fr",
    "German":     "de",
    "Greek":      "el",
    "Gujarati":   "gu",
    "Hebrew":     "he",
    "Hindi":      "hi",
    "Hungarian":  "hu",
    "Indonesian": "id",
    "Italian":    "it",
    "Japanese":   "ja",
    "Korean":     "ko",
    "Latin":      "la",
    "Persian":    "fa",
    "Polish":     "pl",
    "Portuguese": "pt",
    "Romanian":   "ro",
    "Russian":    "ru",
    "Slovak":     "sk",
    "Spanish":    "es",
    "Swedish":    "sv",
    "Tagalog":    "tl",
    "Thai":       "th",
    "Turkish":    "tr",
    "Ukrainian":  "uk",
    "Vietnamese": "vi",
}



def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return {**DEFAULTS, **data}
        except Exception:
            pass
    return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


CFG = load_config()

# ── Stats DB ─────────────────────────────────────────────────────────────────

STATS_DB = os.path.join(os.path.dirname(__file__), "stats.db")
_stats_lock = threading.Lock()


def _stats_conn():
    conn = sqlite3.connect(STATS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_stats_db():
    with _stats_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ip TEXT,
                kind TEXT NOT NULL,
                filename TEXT,
                file_ext TEXT,
                lang_from TEXT,
                lang_to TEXT,
                chars INTEGER,
                pages INTEGER,
                duration REAL,
                status TEXT NOT NULL,
                error TEXT
            )
        """)
        conn.commit()


def log_stat(**kwargs):
    fields = ["timestamp", "ip", "kind", "filename", "file_ext",
              "lang_from", "lang_to", "chars", "pages", "duration",
              "status", "error"]
    values = [kwargs.get(f) for f in fields]
    if not values[0]:
        values[0] = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        with _stats_lock, _stats_conn() as conn:
            conn.execute(
                f"INSERT INTO stats ({','.join(fields)}) "
                f"VALUES ({','.join(['?'] * len(fields))})",
                values,
            )
            conn.commit()
    except Exception as e:
        log.error(f"log_stat failed: {e}")


def get_recent_stats(limit=100):
    with _stats_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM stats ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_stats_summary():
    with _stats_conn() as conn:
        row = conn.execute("""
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
              SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
              SUM(CASE WHEN status='stopped' THEN 1 ELSE 0 END) AS stopped,
              SUM(chars) AS total_chars,
              SUM(duration) AS total_seconds,
              SUM(pages) AS total_pages
            FROM stats
        """).fetchone()
    return dict(row) if row else {}


def clear_stats():
    with _stats_lock, _stats_conn() as conn:
        conn.execute("DELETE FROM stats")
        conn.commit()


init_stats_db()

# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI()


ADMIN_PATHS = ("/admin", "/config")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    path = request.url.path
    if not any(path.startswith(p) for p in ADMIN_PATHS):
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            user, _, pwd = decoded.partition(":")
            if (secrets.compare_digest(user, CFG.get("auth_user", "admin")) and
                    secrets.compare_digest(pwd, CFG.get("auth_pass", "translate"))):
                return await call_next(request)
        except Exception:
            pass
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Translator Admin"'},
    )


_active: dict[str, threading.Event] = {}
_results: dict[str, tuple[str, bytes]] = {}
_preview_cache: dict[str, bytes] = {}   # file_id -> HTML bytes from mammoth

_job_queue: _queue_mod.Queue = _queue_mod.Queue()
_ticket_lock = threading.Lock()
_ticket_issued = 0
_ticket_serving = 0


def _queue_worker():
    global _ticket_serving
    while True:
        start_evt, done_evt = _job_queue.get()
        with _ticket_lock:
            _ticket_serving += 1
        start_evt.set()
        done_evt.wait()


threading.Thread(target=_queue_worker, daemon=True).start()


def extract_text(filename: str, content: bytes) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text)
        return "\n\n".join(pages)
    elif ext == "docx":
        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    else:
        return content.decode("utf-8", errors="replace")


def split_chunks(text: str) -> list[str]:
    max_chars = CFG["chunk_size"]
    paragraphs = text.split("\n\n")
    chunks, current, current_len = [], [], 0
    for para in paragraphs:
        if current and current_len + len(para) > max_chars:
            chunks.append("\n\n".join(current))
            current, current_len = [para], len(para)
        else:
            current.append(para)
            current_len += len(para)
    if current:
        chunks.append("\n\n".join(current))
    return [c for c in chunks if c.strip()]


def call_llm(messages: list, stop_event: threading.Event) -> str | None:
    resp = req_lib.post(
        f"{CFG['base_url']}/chat/completions",
        headers={"Authorization": "Bearer dummy", "Content-Type": "application/json"},
        json={"model": CFG["model"], "stream": True, "max_tokens": CFG["max_tokens"], "messages": messages},
        timeout=CFG["llm_timeout"],
        stream=True,
    )
    collected = []
    for raw_line in resp.iter_lines():
        if stop_event.is_set():
            resp.close()
            return None
        if not raw_line:
            continue
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        token = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
        if token:
            collected.append(token)
    raw = "".join(collected).strip()
    result = raw.split("§")[0].strip()
    return result.replace("⟨P⟩", "\n\n").replace("⟨N⟩", "\n")


def _ollama_native_host() -> str:
    """Strip the /v1 suffix used by OpenAI-compatible endpoint."""
    url = CFG["base_url"].rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def _translate_unit(text: str, lang_from: str, lang_to: str,
                    stop_event: threading.Event) -> str | None:
    """Translate a single short piece of text (one run / one block).

    Uses native Ollama /api/generate with a minimal prompt — much faster than
    the OpenAI-compatible /v1/chat/completions path with the heavy template,
    because for short units the template was costing more tokens than the text.
    """
    if not text or len(text.strip()) < 2:
        return text
    if stop_event.is_set():
        return None
    # Use /api/chat so Ollama applies the model's baked-in chat template
    # (Open WebUI does the same; /api/generate skips the template and the
    # model was trained to expect it).
    target = lang_to if lang_to else "Ukrainian"
    # Natural-language instruction matches what works for users in Open WebUI;
    # the model card's "To <Lang>:" anchor turned out to give worse term
    # choices than a plain "Translate to <Lang>" prompt.
    content = f"Translate to {target}:\n\n{text}"
    try:
        resp = req_lib.post(
            f"{_ollama_native_host()}/api/chat",
            json={
                "model": CFG["model"],
                "messages": [{"role": "user", "content": content}],
                "stream": False,
            },
            timeout=CFG["llm_timeout"],
        )
        if stop_event.is_set():
            return None
        if resp.status_code != 200:
            log.warning(f"Ollama returned {resp.status_code}: {resp.text[:200]}")
            return text
        data = resp.json()
        return (data.get("message", {}).get("content") or "").strip() or text
    except req_lib.exceptions.Timeout:
        raise
    except Exception as e:
        log.warning(f"_translate_unit failed: {e}")
        return text


def _parse_html_to_parts(html: str):
    """Parse HTML into (parts, text_indices, batch_text) for translation."""
    from html.parser import HTMLParser

    parts = []

    class _Parser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            parts.append(('tag', self.get_starttag_text()))
        def handle_endtag(self, tag):
            parts.append(('tag', f'</{tag}>'))
        def handle_data(self, data):
            if data.strip():
                parts.append(('text', data))
            else:
                parts.append(('tag', data))
        def handle_entityref(self, name):
            parts.append(('tag', f'&{name};'))
        def handle_charref(self, name):
            parts.append(('tag', f'&#{name};'))

    _Parser().feed(html)
    text_indices = [i for i, (t, _) in enumerate(parts) if t == 'text']
    batch = ''.join(f'⟦{i}⟧{parts[i][1]}' for i in text_indices)
    return parts, text_indices, batch


def _reconstruct_html(parts: list, text_indices: list, translated_batch: str) -> str:
    """Put translated text nodes back into HTML structure using ⟦N⟧ markers."""
    for idx in text_indices:
        marker = f'⟦{idx}⟧'
        start = translated_batch.find(marker)
        if start == -1:
            continue
        start += len(marker)
        next_marker = translated_batch.find('⟦', start)
        end = next_marker if next_marker != -1 else len(translated_batch)
        parts[idx] = ('text', translated_batch[start:end])
    return ''.join(content for _, content in parts)


def _translate_html_nodes(html: str, lang_from: str, lang_to: str,
                          stop_event: threading.Event) -> str:
    """Translate HTML preserving all tags. Text nodes only, single Ollama call."""
    parts, text_indices, batch = _parse_html_to_parts(html)
    if not text_indices:
        return html
    translated = _translate_unit(batch, lang_from, lang_to, stop_event)
    if not translated:
        return html
    return _reconstruct_html(parts, text_indices, translated)


def _translate_unit_streaming(text: str, lang_from: str, lang_to: str,
                              stop_event: threading.Event):
    """Generator that yields tokens from Ollama stream one by one."""
    if not text or len(text.strip()) < 2:
        yield text
        return
    if stop_event.is_set():
        return

    target = lang_to if lang_to else "Ukrainian"
    content = f"Translate to {target}:\n\n{text}"

    try:
        resp = req_lib.post(
            f"{_ollama_native_host()}/api/chat",
            json={
                "model": CFG["model"],
                "messages": [{"role": "user", "content": content}],
                "stream": True,
            },
            timeout=CFG["llm_timeout"],
            stream=True,
        )
        if resp.status_code != 200:
            log.warning(f"Ollama stream returned {resp.status_code}")
            yield text
            return

        for raw_line in resp.iter_lines():
            if stop_event.is_set():
                resp.close()
                return
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            token = data.get("message", {}).get("content", "")
            if token:
                yield token

    except req_lib.exceptions.Timeout:
        raise
    except Exception as e:
        log.warning(f"_translate_unit_streaming failed: {e}")
        yield text


# ── DOCX translation — see docx_translate.py ─────────────────────────────────


def _translate_json_segments(batch: dict, lang_to: str,
                              stop_event: threading.Event,
                              token_stats: dict | None = None) -> dict:
    """Translate a batch of text segments via JSON.

    Sends {"1": "text1", "2": "text2", ...} to the LLM, expects back a JSON
    object with the same keys and translated values.  On any failure, returns
    the original texts so the document still saves cleanly.
    If token_stats dict is provided, accumulates prompt_eval_count / eval_count.
    Retries up to CFG["retry"] times on failure.
    """
    if stop_event.is_set():
        return batch

    json_input = json.dumps(batch, ensure_ascii=False)
    custom_prompt = CFG.get("custom_prompt", "").strip()
    base_instruction = (
        f"Translate each JSON value into {lang_to}. "
        "Return ONLY a valid JSON object with exactly the same keys and translated values. "
        "Do not add commentary."
    )
    if custom_prompt:
        instruction = f"{custom_prompt}\n{base_instruction}"
    else:
        instruction = base_instruction
    prompt = f"{instruction}\n\n{json_input}"

    max_retries = max(1, int(CFG.get("retry", 2)))
    temperature = float(CFG.get("temperature", 0.7))

    for attempt in range(max_retries):
        if stop_event.is_set():
            return batch
        try:
            resp = req_lib.post(
                f"{_ollama_native_host()}/api/chat",
                json={
                    "model": CFG["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": temperature},
                },
                timeout=CFG["llm_timeout"],
            )
            if stop_event.is_set():
                return batch
            if resp.status_code != 200:
                log.warning(f"_translate_json_segments HTTP {resp.status_code} (attempt {attempt+1})")
                continue

            data = resp.json()

            if token_stats is not None:
                token_stats["tok_in"]  = token_stats.get("tok_in",  0) + data.get("prompt_eval_count", 0)
                token_stats["tok_out"] = token_stats.get("tok_out", 0) + data.get("eval_count", 0)

            content = (data.get("message", {}).get("content") or "").strip()

            # Strip markdown code fences if present
            for fence in ("```json", "```"):
                if content.startswith(fence):
                    content = content[len(fence):]
                    break
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

            parsed = json.loads(content)

            # Accept either {"1": "text"} or [{"id": "1", "t": "text"}]
            if isinstance(parsed, list):
                result = {str(item["id"]): item.get("t", "") for item in parsed
                          if isinstance(item, dict) and "id" in item}
            elif isinstance(parsed, dict):
                result = {str(k): str(v) for k, v in parsed.items()}
            else:
                log.warning(f"_translate_json_segments unexpected type {type(parsed)} (attempt {attempt+1})")
                continue

            # Fill in any missing keys with original text
            for k in batch:
                if k not in result or not result[k].strip():
                    result[k] = batch[k]
            return result

        except Exception as e:
            log.warning(f"_translate_json_segments attempt {attempt+1} failed: {e}")

    log.warning("_translate_json_segments all retries exhausted, returning originals")
    return batch


def translate_docx_bytes(content, base_name, lang_from, lang_to, stop_event):
    """Generator. Yields ('log'|'progress'|'error'|'stopped'|'done', ...)."""
    try:
        from docx_translate import translate_docx, _collect_all_segments
        import docx as _docx_lib
        import io as _io
    except ImportError as e:
        yield ("error", f"Не вдалось імпортувати модуль перекладу DOCX: {e}")
        return

    # Quick pre-check: open doc to count segments for size validation
    try:
        doc_check = _docx_lib.Document(_io.BytesIO(content))
        _, originals = _collect_all_segments(doc_check)
    except Exception as e:
        yield ("error", f"Не вдалось відкрити DOCX: {e}")
        return

    total_chars = sum(len(t) for t in originals)
    total_segs = len(originals)
    yield ("log", f"DOCX: {total_segs} сегментів, {total_chars} символів")

    if total_segs == 0:
        yield ("error", "У файлі не знайдено тексту для перекладу")
        return

    max_chars = CFG.get("max_chars", 30000)
    if total_chars > max_chars:
        yield ("error",
               f"Файл занадто великий: {total_chars} символів "
               f"(максимум {max_chars})")
        return

    yield ("meta", {"chars": total_chars, "pages": None})

    target_lang = lang_to if lang_to else "Ukrainian"
    chunk_size  = CFG.get("chunk_size",  3000)
    insert_mode = CFG.get("insert_mode", "replace")
    separator   = CFG.get("separator",   "\n")

    # Count batches for progress reporting
    n_batches = max(1, -(-total_chars // chunk_size))  # ceil division
    yield ("log", f"DOCX: ~{n_batches} батчів для перекладу (режим: {insert_mode})")

    token_stats = {"tok_in": 0, "tok_out": 0}
    batch_counter = [0]

    def translate_batch_with_progress(batch, lang, stop_ev):
        result = _translate_json_segments(batch, lang, stop_ev, token_stats=token_stats)
        batch_counter[0] += 1
        return result

    try:
        translated_bytes = translate_docx(
            content,
            target_lang,
            translate_batch_with_progress,
            stop_event,
            chunk_size=chunk_size,
            insert_mode=insert_mode,
            separator=separator,
        )
    except Exception as e:
        if stop_event.is_set():
            yield ("stopped",)
        else:
            yield ("error", f"Помилка перекладу DOCX: {e}")
        return

    if stop_event.is_set():
        yield ("stopped",)
        return

    yield ("progress", "Завершено", 100)
    yield ("stats", token_stats["tok_in"], token_stats["tok_out"])
    yield ("done",
           f"{base_name}_translated.docx",
           "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           translated_bytes)


def translate_pdf_bytes(content, base_name, lang_from, lang_to, stop_event):
    try:
        import fitz
    except ImportError:
        yield ("error", "Бібліотека PyMuPDF не встановлена")
        return

    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as e:
        yield ("error", f"Не вдалось відкрити PDF: {e}")
        return

    total_pages = len(doc)
    yield ("log", f"PDF: {total_pages} сторінок")

    max_pages = CFG.get("max_pdf_pages", 10)
    if total_pages > max_pages:
        doc.close()
        yield ("error",
               f"PDF занадто великий: {total_pages} сторінок "
               f"(максимум {max_pages})")
        return

    yield ("meta", {"chars": None, "pages": total_pages})

    for page_num in range(total_pages):
        if stop_event.is_set():
            doc.close()
            yield ("stopped",)
            return

        page = doc[page_num]
        raw = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        blocks = [b for b in raw["blocks"] if b["type"] == 0]

        items = []
        for block in blocks:
            lines_text = []
            fontsize = 11
            color = 0
            for line in block["lines"]:
                for span in line["spans"]:
                    if span["text"].strip():
                        lines_text.append(span["text"])
                        fontsize = span["size"]
                        color = span["color"]
            full_text = " ".join(lines_text).strip()
            if full_text:
                items.append((fitz.Rect(block["bbox"]), full_text, fontsize, color))

        pct = round((page_num + 1) / total_pages * 100)
        if not items:
            yield ("progress", f"Сторінка {page_num + 1}/{total_pages}", pct)
            continue

        for rect, _, _, _ in items:
            page.add_redact_annot(rect, fill=(1, 1, 1))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        for rect, text, fontsize, color_int in items:
            if stop_event.is_set():
                doc.close()
                yield ("stopped",)
                return

            try:
                translated = _translate_unit(text, lang_from, lang_to, stop_event)
            except Exception as e:
                doc.close()
                yield ("error", f"Помилка перекладу: {e}")
                return

            if translated is None:
                doc.close()
                yield ("stopped",)
                return

            r = ((color_int >> 16) & 0xFF) / 255
            g = ((color_int >> 8) & 0xFF) / 255
            b = (color_int & 0xFF) / 255

            page.insert_textbox(
                rect, translated,
                fontsize=max(fontsize - 0.5, 6),
                color=(r, g, b),
                align=0,
                overflow="ignore",
            )

        yield ("progress", f"Сторінка {page_num + 1}/{total_pages}", pct)

    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    yield ("done",
           f"{base_name}_translated.pdf",
           "application/pdf",
           out.getvalue())


def translate_txt_bytes(content, base_name, lang_from, lang_to, stop_event):
    """Per-paragraph TXT translation."""
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception as e:
        yield ("error", f"Не вдалось прочитати файл: {e}")
        return

    max_chars = CFG.get("max_chars", 30000)
    if len(text) > max_chars:
        yield ("error",
               f"Файл занадто великий: {len(text)} символів "
               f"(максимум {max_chars})")
        return

    if not text.strip():
        yield ("error", "Файл порожній")
        return

    paragraphs = text.replace("\r\n", "\n").split("\n\n")
    total = len(paragraphs)
    yield ("log", f"TXT: {total} абзаців, {len(text)} символів")
    yield ("meta", {"chars": len(text), "pages": None})

    translated_parts = []
    for i, para in enumerate(paragraphs, 1):
        if stop_event.is_set():
            yield ("stopped",)
            return

        if not para.strip():
            translated_parts.append(para)
            continue

        try:
            result = _translate_unit(para, lang_from, lang_to, stop_event)
        except Exception as e:
            yield ("error", str(e))
            return

        if result is None:
            yield ("stopped",)
            return

        translated_parts.append(result)
        yield ("progress", f"Абзац {i}/{total}", round(i / total * 100))

    final = "\n\n".join(translated_parts)
    yield ("done",
           f"{base_name}_translated.txt",
           "text/plain; charset=utf-8",
           final.encode("utf-8"))


USER_HTML = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Перекладач</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #f8fafc;
    --card: #ffffff;
    --border: #e2e8f0;
    --text: #1e293b;
    --muted: #64748b;
    --primary: #2563eb;
    --primary-hover: #1d4ed8;
    --primary-light: #dbeafe;
    --danger: #dc2626;
    --danger-hover: #b91c1c;
    --success: #16a34a;
    --success-hover: #15803d;
    --shadow: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.06);
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 32px 20px;
    min-height: 100vh;
    line-height: 1.5;
  }
  .container { width: 80%; max-width: 100%; margin: 0 auto; }
  header { text-align: center; margin-bottom: 32px; }
  header h1 { font-size: 1.75rem; font-weight: 600; color: var(--text); }
  header p { color: var(--muted); font-size: 0.95rem; margin-top: 4px; }

  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    box-shadow: var(--shadow);
    margin-bottom: 16px;
  }

  .lang-bar {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    align-items: end;
  }
  .lang-bar label { display: block; font-size: 0.8rem; color: var(--muted); margin-bottom: 6px; font-weight: 500; }
  .lang-bar select {
    width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.95rem; background: var(--card); cursor: pointer;
  }
  .lang-bar select:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-light); }
  .swap-btn {
    width: 40px; height: 40px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--card); cursor: pointer; font-size: 1.1rem; color: var(--muted);
    display: flex; align-items: center; justify-content: center;
    transition: all 0.15s;
  }
  .swap-btn:hover { border-color: var(--primary); color: var(--primary); }

  .tabs { display: flex; gap: 4px; margin-bottom: 16px; background: var(--border); padding: 4px; border-radius: 10px; }
  .tab {
    flex: 1; padding: 10px; border: none; background: transparent; cursor: pointer;
    border-radius: 7px; font-size: 0.95rem; font-weight: 500; color: var(--muted);
    transition: all 0.15s;
  }
  .tab.active { background: var(--card); color: var(--text); box-shadow: var(--shadow); }

  .panel { display: none; }
  .panel.active { display: block; }

  textarea {
    width: 100%; padding: 14px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.95rem; font-family: inherit; resize: vertical; min-height: 140px;
    line-height: 1.6; background: var(--card); color: var(--text);
  }
  textarea:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-light); }
  textarea[readonly] { background: #f1f5f9; }

  .rich-editor {
    width: 100%; padding: 14px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.95rem; font-family: inherit; min-height: 140px; max-height: 400px;
    line-height: 1.6; background: var(--card); color: var(--text);
    overflow-y: auto; overflow-x: hidden; box-sizing: border-box; outline: none;
    word-break: break-word; overflow-wrap: break-word;
  }
  .rich-editor:focus { border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-light); }
  .rich-editor[contenteditable=true]:empty:before {
    content: attr(data-placeholder); color: #aaa; pointer-events: none;
  }
  .rich-editor b, .rich-editor strong { font-weight: bold; }
  .rich-editor i, .rich-editor em { font-style: italic; }
  .rich-editor u { text-decoration: underline; }
  .rich-editor h1, .rich-editor h2, .rich-editor h3 { margin: 0.3em 0; font-weight: bold; }
  .rich-editor h1 { font-size: 1.4em; }
  .rich-editor h2 { font-size: 1.2em; }
  .rich-editor h3 { font-size: 1.05em; }
  .rich-editor ul, .rich-editor ol { margin: 4px 0 4px 20px; padding: 0; }
  .rich-editor table { border-collapse: collapse; width: 100%; margin: 4px 0; }
  .rich-editor td, .rich-editor th { border: 1px solid var(--border); padding: 4px 8px; }

  .result-content {
    width: 100%; padding: 14px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.95rem; font-family: inherit; min-height: 80px;
    line-height: 1.6; background: #f1f5f9; color: var(--text);
    box-sizing: border-box; overflow-y: auto; overflow-x: hidden;
    word-break: break-word; overflow-wrap: break-word;
  }
  .result-content b, .result-content strong { font-weight: bold; }
  .result-content i, .result-content em { font-style: italic; }
  .result-content u { text-decoration: underline; }
  .result-content h1, .result-content h2, .result-content h3 { margin: 0.3em 0; font-weight: bold; }
  .result-content h1 { font-size: 1.4em; }
  .result-content h2 { font-size: 1.2em; }
  .result-content h3 { font-size: 1.05em; }
  .result-content ul, .result-content ol { margin: 4px 0 4px 20px; padding: 0; }
  .result-content table { border-collapse: collapse; width: 100%; margin: 4px 0; }
  .result-content td, .result-content th { border: 1px solid var(--border); padding: 4px 8px; }

  /* ── Drop zone ─────────────────────────────────────────────────────────── */
  .drop-zone {
    border: 2px dashed var(--border); border-radius: 10px; padding: 40px 20px;
    text-align: center; cursor: pointer; transition: all 0.15s; background: var(--card);
  }
  .drop-zone:hover, .drop-zone.drag-over { border-color: var(--primary); background: var(--primary-light); }
  .drop-zone.has-file { border-color: var(--success); background: #f0fdf4; padding: 18px 20px; }
  .drop-zone-icon { font-size: 2rem; margin-bottom: 8px; }
  .drop-zone-text { color: var(--muted); font-size: 0.95rem; }
  .drop-zone-hint { color: var(--muted); font-size: 0.8rem; margin-top: 4px; }
  .drop-zone input { display: none; }
  .drop-zone-selected { display: none; align-items: center; gap: 12px; }
  .drop-zone.has-file .drop-zone-empty { display: none; }
  .drop-zone.has-file .drop-zone-selected { display: flex; }
  .dz-check { width: 40px; height: 40px; border-radius: 50%; background: var(--success);
    display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .dz-check svg { width: 20px; height: 20px; stroke: white; fill: none; stroke-width: 2.5; }
  .dz-file-info { text-align: left; flex: 1; min-width: 0; }
  .dz-file-name { font-weight: 600; font-size: 0.95rem; color: var(--text);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .dz-file-meta { font-size: 0.8rem; color: var(--muted); margin-top: 2px; }
  .dz-clear { background: none; border: none; cursor: pointer; color: var(--muted);
    font-size: 1.2rem; padding: 4px; line-height: 1; flex-shrink: 0; }
  .dz-clear:hover { color: var(--danger); }

  /* ── File task area ─────────────────────────────────────────────────────── */
  .file-task-id { font-size: 0.78rem; color: var(--muted); margin-top: 10px;
    display: none; padding: 4px 8px; background: var(--bg); border-radius: 6px;
    border: 1px solid var(--border); }
  .file-task-id.visible { display: block; }
  .file-task-id span { font-family: monospace; color: var(--primary); }

  .file-log-box {
    display: none; margin-top: 12px; padding: 12px 14px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 8px;
    max-height: 180px; overflow-y: auto; font-size: 0.78rem; line-height: 1.6;
    color: var(--text); font-family: monospace; white-space: pre-wrap; word-break: break-word;
  }
  .file-log-box.visible { display: block; }
  .file-log-box .log-done { color: #d97706; font-weight: 600; }
  .file-log-box .log-error { color: var(--danger); }

  .file-progress-wrap { display: none; margin-top: 10px; }
  .file-progress-wrap.visible { display: block; }
  .file-progress-bar { height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }
  .file-progress-fill { height: 100%; background: var(--primary); border-radius: 2px;
    transition: width 0.3s; width: 0%; }
  .file-progress-label { font-size: 0.78rem; color: var(--muted); margin-top: 4px; }

  .file-stats { display: none; margin-top: 12px; }
  .file-stats.visible { display: flex; gap: 0; }
  .stat-cell { flex: 1; text-align: center; padding: 8px 4px;
    border: 1px solid var(--border); border-right: none; background: var(--card); }
  .stat-cell:first-child { border-radius: 8px 0 0 8px; }
  .stat-cell:last-child { border-right: 1px solid var(--border); border-radius: 0 8px 8px 0; }
  .stat-label { font-size: 0.68rem; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.05em; font-weight: 600; }
  .stat-value { font-size: 0.9rem; font-weight: 600; color: var(--text); margin-top: 2px; }

  .file-actions { display: flex; gap: 10px; margin-top: 14px; align-items: center; flex-wrap: wrap; }

  /* ── Generic buttons ────────────────────────────────────────────────────── */
  .actions { display: flex; gap: 10px; margin-top: 14px; align-items: center; flex-wrap: wrap; }
  button.primary, .download-link {
    background: var(--primary); color: white; border: none; border-radius: 8px;
    padding: 11px 22px; font-size: 0.95rem; font-weight: 500; cursor: pointer;
    transition: background 0.15s; text-decoration: none; display: inline-flex; align-items: center; gap: 6px;
  }
  button.primary:hover { background: var(--primary-hover); }
  button.primary:disabled { background: #93c5fd; cursor: not-allowed; }
  button.secondary {
    background: var(--card); color: var(--text); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 18px; font-size: 0.95rem; font-weight: 500; cursor: pointer;
    transition: background 0.15s; display: none; align-items: center; gap: 6px;
  }
  button.secondary:hover { background: var(--bg); }
  button.secondary.visible { display: inline-flex; }
  button.stop {
    background: var(--danger); color: white; border: none; border-radius: 8px;
    padding: 11px 22px; font-size: 0.95rem; font-weight: 500; cursor: pointer;
    transition: background 0.15s; display: none;
  }
  button.stop:hover { background: var(--danger-hover); }
  button.stop.visible { display: inline-block; }
  .download-link { background: var(--success); display: none; }
  .download-link:hover { background: var(--success-hover); }
  .download-link.visible { display: inline-flex; }
  button.preview-btn { background: #7c3aed; display: none; }
  button.preview-btn:hover { background: #6d28d9; }
  button.preview-btn.visible { display: inline-flex; }

  /* Preview modal */
  #preview-modal {
    display: none; position: fixed; inset: 0; z-index: 1000;
    background: rgba(0,0,0,.55); flex-direction: column;
  }
  #preview-modal.open { display: flex; }
  #preview-toolbar {
    background: #1e293b; color: white; display: flex; align-items: center;
    gap: 10px; padding: 8px 16px; flex-shrink: 0;
  }
  #preview-toolbar span { font-size: .9rem; flex: 1; opacity: .7; }
  #preview-toolbar button {
    padding: 6px 16px; font-size: .85rem; border-radius: 6px;
    border: none; cursor: pointer; font-family: inherit;
  }
  #btn-preview-pdf { background: #2563eb; color: white; }
  #btn-preview-pdf:hover { background: #1d4ed8; }
  #btn-preview-close { background: #475569; color: white; }
  #btn-preview-close:hover { background: #334155; }
  #preview-iframe {
    flex: 1; border: none; background: white;
    margin: 12px; border-radius: 8px; overflow: hidden;
  }

  .status { font-size: 0.85rem; color: var(--muted); display: flex; align-items: center; gap: 8px; }
  .status.error { color: var(--danger); }
  .status.success { color: var(--success); }
  .spinner {
    width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--primary);
    border-radius: 50%; animation: spin 0.8s linear infinite; display: none;
  }
  .spinner.visible { display: inline-block; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .result-label { font-size: 0.85rem; color: var(--muted); margin-bottom: 8px; font-weight: 500; }
  .footer { text-align: center; margin-top: 24px; font-size: 0.8rem; color: var(--muted); }
  .footer a { color: var(--muted); text-decoration: none; }
  .footer a:hover { color: var(--primary); }

  /* Side-by-side text translation layout */
  .split-layout {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    align-items: start;
  }
  @media (max-width: 640px) {
    .split-layout { grid-template-columns: 1fr; }
  }
  .split-panel { display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
  .panel-label {
    font-size: 0.8rem; color: var(--muted); font-weight: 500;
    margin-bottom: 8px; display: flex; align-items: center; gap: 8px; min-height: 22px;
  }
  .rich-editor { min-height: 320px; max-height: 70vh; }
  .result-content { min-height: 320px; max-height: 70vh; }
  .result-content.streaming { color: var(--muted); font-style: italic; white-space: pre-wrap; }
  .result-placeholder { color: #aaa; font-size: 0.9rem; padding: 14px; }

  .model-bar { display: flex; align-items: center; gap: 16px; margin-top: 12px; flex-wrap: wrap; }
  .conn-status { display: flex; align-items: center; gap: 6px; font-size: 0.82rem; color: var(--muted); white-space: nowrap; }
  .conn-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; background: #94a3b8; transition: background 0.3s; }
  .conn-dot.ok { background: var(--success); }
  .conn-dot.err { background: var(--danger); }
  .model-select-wrap { flex: 1; min-width: 200px; }
  .model-select-wrap label { display: block; font-size: 0.78rem; color: var(--muted); margin-bottom: 4px; }
  .model-select-wrap select { width: 100%; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Перекладач</h1>
    <p>Локальний переклад документів і тексту</p>
  </header>

  <div class="card">
    <div class="lang-bar">
      <div>
        <label>З мови</label>
        <select id="lang_from"></select>
      </div>
      <button class="swap-btn" onclick="swapLangs()" title="Поміняти мови місцями">⇄</button>
      <div>
        <label>На мову</label>
        <select id="lang_to"></select>
      </div>
    </div>
    <div class="model-bar">
      <div class="conn-status">
        <span class="conn-dot" id="conn-dot"></span>
        <span id="conn-text">Перевірка...</span>
      </div>
      <div class="model-select-wrap">
        <label>Модель</label>
        <select id="model_select" onchange="onModelChange()"></select>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="tabs">
      <button class="tab active" onclick="showTab('text')">Текст</button>
      <button class="tab" onclick="showTab('file')">Файл</button>
    </div>

    <div id="panel-text" class="panel active">
      <div class="split-layout">
        <div class="split-panel">
          <div class="panel-label">Оригінал</div>
          <div id="input" class="rich-editor" contenteditable="true" spellcheck="false"
               data-gramm="false" data-placeholder="Введіть текст для перекладу..."></div>
          <div class="actions">
            <button id="btn-translate" class="primary" onclick="doTranslate()">Перекласти</button>
            <button id="btn-stop" class="stop" onclick="doStop()">Зупинити</button>
            <span class="spinner" id="text-spinner"></span>
            <span class="status" id="text-status"></span>
          </div>
        </div>
        <div class="split-panel">
          <div class="panel-label" id="result-label">Переклад</div>
          <div id="result" class="result-content">
            <div class="result-placeholder">Переклад з'явиться тут...</div>
          </div>
        </div>
      </div>
    </div>

    <div id="panel-file" class="panel">
      <!-- Drop zone -->
      <div class="drop-zone" id="drop-zone" onclick="dzClick()">
        <input type="file" id="file-input" accept=".pdf,.docx,.txt" onchange="fileSelected(this.files[0])">
        <div class="drop-zone-empty">
          <div class="drop-zone-icon">📄</div>
          <div class="drop-zone-text">Натисніть або перетягніть файл</div>
          <div class="drop-zone-hint">Підтримуються DOCX, PDF, TXT</div>
        </div>
        <div class="drop-zone-selected">
          <div class="dz-check">
            <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
          </div>
          <div class="dz-file-info">
            <div class="dz-file-name" id="dz-file-name"></div>
            <div class="dz-file-meta" id="dz-file-meta">Файл обрано</div>
          </div>
          <button class="dz-clear" onclick="clearFile(event)" title="Видалити">&#x2715;</button>
        </div>
      </div>

      <!-- Task ID -->
      <div class="file-task-id" id="file-task-id">ID завдання: <span id="file-task-id-val"></span></div>

      <!-- Progress bar -->
      <div class="file-progress-wrap" id="file-progress-wrap">
        <div class="file-progress-bar"><div class="file-progress-fill" id="file-progress-fill"></div></div>
        <div class="file-progress-label" id="file-progress-label"></div>
      </div>

      <!-- Log box -->
      <div class="file-log-box" id="file-log-box"></div>

      <!-- Stats -->
      <div class="file-stats" id="file-stats">
        <div class="stat-cell"><div class="stat-label">Сегменти</div><div class="stat-value" id="stat-segs">—</div></div>
        <div class="stat-cell"><div class="stat-label">Символи</div><div class="stat-value" id="stat-chars">—</div></div>
        <div class="stat-cell"><div class="stat-label">Батчі</div><div class="stat-value" id="stat-batches">—</div></div>
        <div class="stat-cell"><div class="stat-label">Токени in</div><div class="stat-value" id="stat-tok-in">—</div></div>
        <div class="stat-cell"><div class="stat-label">Токени out</div><div class="stat-value" id="stat-tok-out">—</div></div>
        <div class="stat-cell"><div class="stat-label">Час</div><div class="stat-value" id="stat-time">—</div></div>
      </div>

      <!-- Actions -->
      <div class="file-actions">
        <button id="btn-file" class="primary" onclick="doTranslateFile()">Перекласти файл</button>
        <button id="btn-file-stop" class="stop" onclick="doFileStop()">Зупинити</button>
        <a id="download-link" class="download-link">↓ Завантажити</a>
        <button id="btn-preview" class="preview-btn" onclick="openPreview()">&#128065; Переглянути</button>
        <button id="btn-file-retry" class="secondary" onclick="doTranslateFile()">↺ Перекласти знову</button>
        <span class="spinner" id="file-spinner"></span>
      </div>
    </div>
  </div>

  <div class="footer">
    <a href="/admin">Адміністрування</a>
    <span style="margin: 0 12px; color: var(--border);">|</span>
    <span>v1.6</span>
  </div>
</div>

<script>
// ── Tabs ──────────────────────────────────────────────────────────────
function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('panel-' + name).classList.add('active');
}

// ── Languages ─────────────────────────────────────────────────────────
async function initLangs() {
  const r = await fetch('/languages');
  const langs = await r.json();
  const from = document.getElementById('lang_from');
  const to = document.getElementById('lang_to');
  from.appendChild(new Option('Автовизначення', 'auto'));
  langs.forEach(({label, value}) => {
    from.appendChild(new Option(label, value));
    to.appendChild(new Option(label, value));
  });
  to.value = 'Ukrainian';
}

function swapLangs() {
  const from = document.getElementById('lang_from');
  const to = document.getElementById('lang_to');
  if (from.value === 'auto') return;
  const tmp = from.value;
  from.value = to.value;
  to.value = tmp;
}

// ── Status helpers ────────────────────────────────────────────────────
function setStatus(id, text, type='') {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'status' + (type ? ' ' + type : '');
}
function setSpinner(id, on) {
  document.getElementById(id).classList.toggle('visible', on);
}
function setWorking(prefix, on) {
  document.getElementById('btn-' + (prefix === 'text' ? 'translate' : 'file')).disabled = on;
  document.getElementById('btn-' + (prefix === 'text' ? 'stop' : 'file-stop')).classList.toggle('visible', on);
  setSpinner(prefix + '-spinner', on);
}

// ── Text translation ──────────────────────────────────────────────────
let _textController = null;
let _textRequestId = null;
let _textTokens = '';

async function doTranslate() {
  const inputEl = document.getElementById('input');
  const html = inputEl.innerHTML.trim();
  const text = inputEl.textContent.trim();
  if (!text) { setStatus('text-status', 'Введіть текст', 'error'); return; }

  setWorking('text', true);
  setStatus('text-status', 'Перекладаю...');
  document.getElementById('result').innerHTML = '';
  _textTokens = '';
  _textController = new AbortController();

  try {
    const resp = await fetch('/translate-html', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: _textController.signal,
      body: JSON.stringify({
        html,
        lang_from: document.getElementById('lang_from').value,
        lang_to: document.getElementById('lang_to').value,
      }),
    });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let evt; try { evt = JSON.parse(line.slice(6)); } catch { continue; }
        if (evt.type === 'id') _textRequestId = evt.text;
        else if (evt.type === 'queue' && evt.ahead > 0) setStatus('text-status', `У черзі: попереду ${evt.ahead}`);
        else if (evt.type === 'queue' && evt.ahead === 0) setStatus('text-status', 'Перекладаю...');
        else if (evt.type === 'token') {
          // Stream tokens into result as plain text (fast visual feedback)
          _textTokens += evt.text;
          const resultEl = document.getElementById('result');
          resultEl.classList.add('streaming');
          resultEl.textContent = _textTokens;
        }
        else if (evt.type === 'result') {
          showResult(evt.text, evt.format);
          setStatus('text-status', 'Готово', 'success');
        }
        else if (evt.type === 'error') {
          setStatus('text-status', 'Помилка: ' + (evt.text || 'невідома'), 'error');
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') setStatus('text-status', 'Помилка з\'єднання', 'error');
  }
  setWorking('text', false);
  _textController = null;
  _textRequestId = null;
}

function showResult(text, format) {
  const resultEl = document.getElementById('result');
  resultEl.classList.remove('streaming');
  if (format === 'markdown' && typeof marked !== 'undefined') {
    resultEl.innerHTML = marked.parse(text);
  } else {
    resultEl.innerHTML = text;
  }
}

document.getElementById('input').addEventListener('keydown', function(e) {
  if (e.ctrlKey && e.key === 'Enter') doTranslate();
});

async function doStop() {
  if (_textController) { _textController.abort(); _textController = null; }
  if (_textRequestId) {
    await fetch('/stop/' + _textRequestId, {method: 'POST'}).catch(() => {});
    _textRequestId = null;
  }
  setStatus('text-status', 'Зупинено');
  setWorking('text', false);
}

// ── File translation ──────────────────────────────────────────────────────
let _fileController = null;
let _fileRequestId = null;
let _selectedFile = null;
let _fileStartTime = null;
let _previewUrl = null;

function dzClick() {
  if (!_selectedFile) document.getElementById('file-input').click();
}

function fileSelected(f) {
  _selectedFile = f;
  const dz = document.getElementById('drop-zone');
  if (f) {
    dz.classList.add('has-file');
    document.getElementById('dz-file-name').textContent = f.name;
    document.getElementById('dz-file-meta').textContent =
      (f.size / 1024).toFixed(1) + ' KB · ' + (f.name.split('.').pop().toUpperCase());
  } else {
    dz.classList.remove('has-file');
  }
  fileResetResult();
}

function clearFile(e) {
  e.stopPropagation();
  _selectedFile = null;
  document.getElementById('file-input').value = '';
  fileSelected(null);
}

function fileResetResult() {
  _previewUrl = null;
  document.getElementById('download-link').classList.remove('visible');
  document.getElementById('btn-preview').classList.remove('visible');
  document.getElementById('btn-file-retry').classList.remove('visible');
  document.getElementById('file-task-id').classList.remove('visible');
  document.getElementById('file-log-box').classList.remove('visible');
  document.getElementById('file-log-box').innerHTML = '';
  document.getElementById('file-progress-wrap').classList.remove('visible');
  document.getElementById('file-stats').classList.remove('visible');
  setProgress(0, '');
}

function fileLog(msg, cls) {
  const box = document.getElementById('file-log-box');
  box.classList.add('visible');
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = msg;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function setProgress(pct, label) {
  document.getElementById('file-progress-fill').style.width = pct + '%';
  document.getElementById('file-progress-label').textContent = label;
}

function showStats(data) {
  const el = document.getElementById('file-stats');
  el.classList.add('visible');
  if (data.segs !== undefined) document.getElementById('stat-segs').textContent = data.segs;
  if (data.chars !== undefined) document.getElementById('stat-chars').textContent =
    data.chars >= 1000 ? (data.chars/1000).toFixed(1)+'K' : data.chars;
  if (data.batches !== undefined) document.getElementById('stat-batches').textContent = data.batches;
  if (data.tok_in !== undefined) document.getElementById('stat-tok-in').textContent =
    data.tok_in >= 1000 ? (data.tok_in/1000).toFixed(1)+'K' : data.tok_in;
  if (data.tok_out !== undefined) document.getElementById('stat-tok-out').textContent =
    data.tok_out >= 1000 ? (data.tok_out/1000).toFixed(1)+'K' : data.tok_out;
  if (data.elapsed !== undefined) document.getElementById('stat-time').textContent =
    data.elapsed.toFixed(1)+'с';
}

const dropZone = document.getElementById('drop-zone');
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  if (e.dataTransfer.files[0]) fileSelected(e.dataTransfer.files[0]);
});

async function doTranslateFile() {
  if (!_selectedFile) { fileLog('Оберіть файл', 'log-error'); return; }

  setWorking('file', true);
  fileResetResult();
  document.getElementById('file-progress-wrap').classList.add('visible');
  _fileController = new AbortController();
  _fileStartTime = Date.now();

  const fd = new FormData();
  fd.append('file', _selectedFile, _selectedFile.name);
  fd.append('lang_from', document.getElementById('lang_from').value);
  fd.append('lang_to', document.getElementById('lang_to').value);

  const statsAccum = { segs: 0, chars: 0, batches: 0, tok_in: 0, tok_out: 0 };

  try {
    const resp = await fetch('/translate-file', {
      method: 'POST', signal: _fileController.signal, body: fd,
    });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let evt; try { evt = JSON.parse(line.slice(6)); } catch { continue; }

        if (evt.type === 'id') {
          _fileRequestId = evt.text;
          const tid = document.getElementById('file-task-id');
          document.getElementById('file-task-id-val').textContent = evt.text.slice(0, 8);
          tid.classList.add('visible');
        }
        else if (evt.type === 'log') {
          fileLog(evt.text);
          // Parse meta from log text
          const segsM = evt.text.match(/(\d+)\s+сегмент/);
          const charsM = evt.text.match(/(\d+)\s+симв/);
          const batchM = evt.text.match(/~?(\d+)\s+батч/);
          if (segsM) statsAccum.segs = parseInt(segsM[1]);
          if (charsM) statsAccum.chars = parseInt(charsM[1]);
          if (batchM) statsAccum.batches = parseInt(batchM[1]);
        }
        else if (evt.type === 'progress') {
          setProgress(evt.pct || 0, evt.text);
        }
        else if (evt.type === 'stats') {
          statsAccum.tok_in = (evt.tok_in || 0);
          statsAccum.tok_out = (evt.tok_out || 0);
        }
        else if (evt.type === 'download') {
          const elapsed = (Date.now() - _fileStartTime) / 1000;
          fileLog('Переклад завершено! Тривалість ' + elapsed.toFixed(2) + ' сек.', 'log-done');
          setProgress(100, '');
          showStats({ ...statsAccum, elapsed });
          const link = document.getElementById('download-link');
          link.href = evt.url;
          link.download = evt.filename;
          link.classList.add('visible');
          document.getElementById('btn-file-retry').classList.add('visible');
        }
        else if (evt.type === 'preview') {
          _previewUrl = evt.url;
          document.getElementById('btn-preview').classList.add('visible');
        }
        else if (evt.type === 'error') {
          fileLog('Помилка: ' + (evt.text || 'невідома'), 'log-error');
          setProgress(0, '');
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') fileLog('Помилка з\'єднання: ' + e.message, 'log-error');
  }
  setWorking('file', false);
  _fileController = null;
  _fileRequestId = null;
}

function openPreview() {
  if (!_previewUrl) return;
  const modal = document.getElementById('preview-modal');
  const iframe = document.getElementById('preview-iframe');
  modal.classList.add('open');
  // Load via src so the browser handles it as a separate page
  iframe.src = _previewUrl;
}

function closePreview() {
  const modal = document.getElementById('preview-modal');
  modal.classList.remove('open');
  document.getElementById('preview-iframe').src = 'about:blank';
}

function savePdf() {
  const iframe = document.getElementById('preview-iframe');
  if (!iframe.contentWindow) return;
  iframe.contentWindow.focus();
  iframe.contentWindow.print();
}

// Close on backdrop click
document.getElementById('preview-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('preview-modal')) closePreview();
});

async function doFileStop() {
  if (_fileController) { _fileController.abort(); _fileController = null; }
  if (_fileRequestId) {
    await fetch('/stop/' + _fileRequestId, {method: 'POST'}).catch(() => {});
    _fileRequestId = null;
  }
  fileLog('Зупинено користувачем');
  setWorking('file', false);
}

// ── Models & connection status ─────────────────────────────────────────
async function loadModels() {
  const dot  = document.getElementById('conn-dot');
  const txt  = document.getElementById('conn-text');
  const sel  = document.getElementById('model_select');
  try {
    const r    = await fetch('/models');
    const data = await r.json();
    sel.innerHTML = '';
    if (data.ok && data.models.length > 0) {
      dot.className = 'conn-dot ok';
      txt.textContent = 'Ollama підключена';
      data.models.forEach(m => {
        const opt = new Option(m, m);
        if (m === data.current) opt.selected = true;
        sel.appendChild(opt);
      });
      if (!data.models.includes(data.current) && data.current) {
        const opt = new Option(data.current + ' (не знайдено)', data.current);
        opt.selected = true;
        sel.insertBefore(opt, sel.firstChild);
      }
    } else {
      dot.className = 'conn-dot err';
      txt.textContent = 'Ollama недоступна';
      if (data.current) sel.appendChild(new Option(data.current, data.current));
    }
  } catch (e) {
    dot.className = 'conn-dot err';
    txt.textContent = "Помилка з'єднання";
  }
}

async function onModelChange() {
  const model = document.getElementById('model_select').value;
  await fetch('/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model })
  });
}

setInterval(loadModels, 30000);

// ── Init ──────────────────────────────────────────────────────────────
initLangs();
loadModels();
document.getElementById('input').addEventListener('keydown', e => {
  if (e.ctrlKey && e.key === 'Enter') doTranslate();
});
</script>

<!-- Preview modal -->
<div id="preview-modal">
  <div id="preview-toolbar">
    <span id="preview-title">Перегляд перекладу</span>
    <button id="btn-preview-pdf" onclick="savePdf()">&#128438; Зберегти PDF</button>
    <button id="btn-preview-close" onclick="closePreview()">&#10005; Закрити</button>
  </div>
  <iframe id="preview-iframe" sandbox="allow-same-origin allow-scripts"></iframe>
</div>
</body>
</html>"""


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<title>Перекладач — Адмін</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: sans-serif; background: #f5f5f5; padding: 24px; max-width: 900px; margin: 0 auto; }
  h1 { margin-bottom: 20px; font-size: 1.4rem; color: #333; }
  .card { background: white; border-radius: 8px; padding: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.1); margin-bottom: 16px; }
  label { display: block; font-size: .85rem; color: #555; margin-bottom: 4px; }
  input[type=text], input[type=number], select, textarea { width: 100%; border: 1px solid #ddd; border-radius: 6px; padding: 8px 10px; font-size: .95rem; font-family: inherit; }
  textarea { resize: vertical; min-height: 60px; }
  select { background: white; cursor: pointer; }
  .btn-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  button { background: #2563eb; color: white; border: none; border-radius: 6px; padding: 10px 24px; font-size: 1rem; cursor: pointer; transition: background .2s; }
  button:hover { background: #1d4ed8; }
  .btn-save { background: #16a34a; }
  .btn-save:hover { background: #15803d; }
  .status { font-size: .8rem; color: #888; }
  .status.ok { color: #16a34a; }
  .status.err { color: #dc2626; }
  details { margin-top: 0; }
  summary { font-size: 1rem; font-weight: 600; color: #333; cursor: pointer; user-select: none; }
  summary:hover { color: #2563eb; }
  .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 16px; }
  .settings-grid .full { grid-column: 1 / -1; }
  .back-link { display: inline-block; margin-bottom: 16px; font-size: .9rem; color: #2563eb; text-decoration: none; }
  .back-link:hover { text-decoration: underline; }
</style>
</head>
<body>
<a class="back-link" href="/">&#8592; На головну</a>
<h1>Адміністрування</h1>

<!-- Stats -->
<div class="card">
  <details id="stats-details">
    <summary>&#128202; Статистика</summary>
    <div style="margin-top:16px;">
      <div id="stats-summary" style="display:grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap:10px; margin-bottom:14px;"></div>
      <div style="overflow-x:auto;">
        <table style="width:100%; border-collapse:collapse; font-size:.82rem;">
          <thead>
            <tr style="background:#f1f5f9; text-align:left;">
              <th style="padding:6px 8px; border-bottom:1px solid #e2e8f0;">Час</th>
              <th style="padding:6px 8px; border-bottom:1px solid #e2e8f0;">IP</th>
              <th style="padding:6px 8px; border-bottom:1px solid #e2e8f0;">Тип</th>
              <th style="padding:6px 8px; border-bottom:1px solid #e2e8f0;">Файл</th>
              <th style="padding:6px 8px; border-bottom:1px solid #e2e8f0;">Мови</th>
              <th style="padding:6px 8px; border-bottom:1px solid #e2e8f0;">Симв.</th>
              <th style="padding:6px 8px; border-bottom:1px solid #e2e8f0;">Стор.</th>
              <th style="padding:6px 8px; border-bottom:1px solid #e2e8f0;">Час, с</th>
              <th style="padding:6px 8px; border-bottom:1px solid #e2e8f0;">Статус</th>
              <th style="padding:6px 8px; border-bottom:1px solid #e2e8f0;">Помилка</th>
            </tr>
          </thead>
          <tbody id="stats-tbody"></tbody>
        </table>
      </div>
      <div class="btn-row" style="margin-top:10px;">
        <button onclick="loadStats()" style="background:#6b7280;">&#8635; Оновити</button>
        <button onclick="clearStats()" style="background:#dc2626;">&#128465; Очистити статистику</button>
      </div>
    </div>
  </details>
</div>

<!-- Settings -->
<div class="card">
  <details id="settings-details">
    <summary>&#9881; Налаштування</summary>
    <div class="settings-grid">
      <div class="full">
        <label>Ollama URL</label>
        <input type="text" id="cfg_base_url" placeholder="http://127.0.0.1:11434/v1">
      </div>
      <div class="full">
        <label>Модель</label>
        <input type="text" id="cfg_model" placeholder="назва моделі">
      </div>
      <div>
        <label>Макс. токенів</label>
        <input type="number" id="cfg_max_tokens" min="128" max="32000" step="128">
      </div>
      <div>
        <label>Таймаут LLM (секунди)</label>
        <input type="number" id="cfg_llm_timeout" min="10" max="600">
      </div>
      <div>
        <label>Розмір чанка (символів)</label>
        <input type="number" id="cfg_chunk_size" min="500" max="20000" step="100">
      </div>
      <div>
        <label>Макс. сторінок PDF</label>
        <input type="number" id="cfg_max_pdf_pages" min="1" max="500" step="1">
      </div>
      <div class="full">
        <label>Макс. символів (текст/DOCX/TXT)</label>
        <input type="number" id="cfg_max_chars" min="1000" max="1000000" step="1000">
      </div>
      <div>
        <label>Temperature (0 — точно, 2 — творчо)</label>
        <input type="number" id="cfg_temperature" min="0" max="2" step="0.05">
      </div>
      <div>
        <label>Повторів при помилці (retry)</label>
        <input type="number" id="cfg_retry" min="1" max="6" step="1">
      </div>
      <div>
        <label>Режим вставки перекладу</label>
        <select id="cfg_insert_mode" onchange="toggleSeparator()">
          <option value="replace">replace — замінити оригінал</option>
          <option value="append">append — після оригіналу</option>
          <option value="prepend">prepend — перед оригіналом</option>
        </select>
      </div>
      <div id="cfg_separator_row">
        <label>Роздільник (append/prepend)</label>
        <input type="text" id="cfg_separator" placeholder="&#10;(новий рядок)">
      </div>
      <div class="full">
        <label>Кастомна інструкція перекладу (custom_prompt, необов'язково)</label>
        <textarea id="cfg_custom_prompt" placeholder="Наприклад: Зберігай технічні терміни без перекладу."></textarea>
      </div>
    </div>
    <div class="btn-row" style="margin-top:16px;">
      <button class="btn-save" onclick="saveSettings()">Зберегти</button>
      <button onclick="resetSettings()" style="background:#6b7280;">Скинути до стандартних</button>
      <span class="status" id="cfg-status"></span>
    </div>
  </details>
</div>

<script>
function toggleSeparator() {
  const mode = document.getElementById('cfg_insert_mode').value;
  document.getElementById('cfg_separator_row').style.display = (mode === 'replace') ? 'none' : '';
}

function applyCfg(cfg) {
  document.getElementById('cfg_base_url').value      = cfg.base_url      ?? '';
  document.getElementById('cfg_model').value         = cfg.model         ?? '';
  document.getElementById('cfg_max_tokens').value    = cfg.max_tokens    ?? 2048;
  document.getElementById('cfg_llm_timeout').value   = cfg.llm_timeout   ?? 180;
  document.getElementById('cfg_chunk_size').value    = cfg.chunk_size    ?? 3000;
  document.getElementById('cfg_max_pdf_pages').value = cfg.max_pdf_pages ?? 10;
  document.getElementById('cfg_max_chars').value     = cfg.max_chars     ?? 30000;
  document.getElementById('cfg_temperature').value   = cfg.temperature   ?? 0.7;
  document.getElementById('cfg_retry').value         = cfg.retry         ?? 2;
  document.getElementById('cfg_insert_mode').value   = cfg.insert_mode   ?? 'replace';
  document.getElementById('cfg_separator').value     = cfg.separator     ?? '\n';
  document.getElementById('cfg_custom_prompt').value = cfg.custom_prompt ?? '';
  toggleSeparator();
}

async function loadSettings() {
  const r = await fetch('/config');
  applyCfg(await r.json());
}

async function saveSettings() {
  const cfg = {
    base_url:      document.getElementById('cfg_base_url').value.trim(),
    model:         document.getElementById('cfg_model').value.trim(),
    max_tokens:    parseInt(document.getElementById('cfg_max_tokens').value),
    llm_timeout:   parseInt(document.getElementById('cfg_llm_timeout').value),
    chunk_size:    parseInt(document.getElementById('cfg_chunk_size').value),
    max_pdf_pages: parseInt(document.getElementById('cfg_max_pdf_pages').value),
    max_chars:     parseInt(document.getElementById('cfg_max_chars').value),
    temperature:   parseFloat(document.getElementById('cfg_temperature').value),
    retry:         parseInt(document.getElementById('cfg_retry').value),
    insert_mode:   document.getElementById('cfg_insert_mode').value,
    separator:     document.getElementById('cfg_separator').value,
    custom_prompt: document.getElementById('cfg_custom_prompt').value,
  };
  const st = document.getElementById('cfg-status');
  try {
    const r = await fetch('/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(cfg) });
    const res = await r.json();
    if (res.ok) { st.textContent = 'Збережено'; st.className = 'status ok'; }
    else        { st.textContent = 'Помилка: ' + (res.error ?? 'невідома'); st.className = 'status err'; }
  } catch(e) { st.textContent = 'Помилка: ' + e; st.className = 'status err'; }
  setTimeout(() => { st.textContent = ''; st.className = 'status'; }, 3000);
}

async function resetSettings() {
  const r = await fetch('/config/defaults');
  applyCfg(await r.json());
}

async function loadStats() {
  try {
    const r = await fetch('/admin/stats?limit=100');
    const d = await r.json();
    const s = d.summary || {};
    const cards = [
      ['Всього', s.total ?? 0], ['Успішно', s.success ?? 0],
      ['Помилки', s.errors ?? 0], ['Зупинено', s.stopped ?? 0],
      ['Символів', s.total_chars ?? 0], ['Сторінок', s.total_pages ?? 0],
      ['Секунд', Math.round(s.total_seconds ?? 0)],
    ];
    document.getElementById('stats-summary').innerHTML = cards.map(([k, v]) =>
      `<div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:10px;">
         <div style="font-size:.7rem; color:#64748b; text-transform:uppercase;">${k}</div>
         <div style="font-size:1.2rem; font-weight:600; color:#1e293b; margin-top:2px;">${v}</div>
       </div>`).join('');
    const sc = { success:'#16a34a', error:'#dc2626', stopped:'#f59e0b' };
    const rows = (d.recent || []).map(r => {
      const t = (r.timestamp||'').replace('T',' ').slice(5,19);
      return `<tr>
        <td style="padding:5px 8px; border-bottom:1px solid #f1f5f9;">${t}</td>
        <td style="padding:5px 8px; border-bottom:1px solid #f1f5f9;">${r.ip||''}</td>
        <td style="padding:5px 8px; border-bottom:1px solid #f1f5f9;">${r.kind||''}</td>
        <td style="padding:5px 8px; border-bottom:1px solid #f1f5f9; max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${r.filename||''}</td>
        <td style="padding:5px 8px; border-bottom:1px solid #f1f5f9;">${r.lang_from||''}→${r.lang_to||''}</td>
        <td style="padding:5px 8px; border-bottom:1px solid #f1f5f9; text-align:right;">${r.chars??''}</td>
        <td style="padding:5px 8px; border-bottom:1px solid #f1f5f9; text-align:right;">${r.pages??''}</td>
        <td style="padding:5px 8px; border-bottom:1px solid #f1f5f9; text-align:right;">${r.duration??''}</td>
        <td style="padding:5px 8px; border-bottom:1px solid #f1f5f9; color:${sc[r.status]||'#64748b'}; font-weight:500;">${r.status}</td>
        <td style="padding:5px 8px; border-bottom:1px solid #f1f5f9; color:#dc2626; max-width:250px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${(r.error||'').replace(/"/g,'&quot;')}">${r.error||''}</td>
      </tr>`;
    }).join('');
    document.getElementById('stats-tbody').innerHTML = rows || '<tr><td colspan="10" style="padding:20px; text-align:center; color:#94a3b8;">Немає даних</td></tr>';
  } catch(e) {
    document.getElementById('stats-tbody').innerHTML = `<tr><td colspan="10" style="padding:10px; color:#dc2626;">Помилка: ${e}</td></tr>`;
  }
}

async function clearStats() {
  if (!confirm('Видалити всю статистику?')) return;
  await fetch('/admin/stats/clear', { method: 'POST' });
  loadStats();
}

document.getElementById('stats-details').addEventListener('toggle', e => {
  if (e.target.open) loadStats();
});

loadSettings();
</script>
</body>
</html>"""


class TranslateRequest(BaseModel):
    text: str
    lang_from: str = "auto"
    lang_to: str


class TranslateHtmlRequest(BaseModel):
    html: str
    lang_from: str = "auto"
    lang_to: str


@app.get("/", response_class=HTMLResponse)
def index():
    return USER_HTML


@app.get("/admin", response_class=HTMLResponse)
def admin():
    return ADMIN_HTML


@app.get("/admin/stats")
def admin_stats(limit: int = 100):
    return JSONResponse({
        "summary": get_stats_summary(),
        "recent": get_recent_stats(limit),
    })


@app.post("/admin/stats/clear")
def admin_stats_clear():
    clear_stats()
    return JSONResponse({"ok": True})



@app.get("/languages")
def get_languages():
    langs = sorted(LANG_MAP.keys(), key=lambda k: LANG_NAMES_UK[k])
    return JSONResponse([{"label": LANG_NAMES_UK[k], "value": k} for k in langs])


@app.get("/models")
def get_models():
    try:
        resp = req_lib.get(f"{_ollama_native_host()}/api/tags", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            return JSONResponse({"ok": True, "models": models, "current": CFG.get("model", "")})
        return JSONResponse({"ok": False, "models": [], "current": CFG.get("model", ""), "error": f"HTTP {resp.status_code}"})
    except Exception as e:
        return JSONResponse({"ok": False, "models": [], "current": CFG.get("model", ""), "error": str(e)})


@app.get("/config")
def get_config():
    return JSONResponse(CFG)


@app.get("/config/defaults")
def get_defaults():
    return JSONResponse(DEFAULTS)


@app.post("/config")
def post_config(data: dict):
    global CFG
    allowed = set(DEFAULTS.keys())
    for key in list(data.keys()):
        if key not in allowed:
            return JSONResponse({"ok": False, "error": f"Unknown key: {key}"}, status_code=400)
    CFG = {**CFG, **data}
    try:
        save_config(CFG)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})


@app.post("/stop/{request_id}")
def stop_translation(request_id: str):
    event = _active.get(request_id)
    if event:
        event.set()
    return JSONResponse({"ok": True})


@app.post("/translate")
def translate(req: TranslateRequest, request: Request):
    global _ticket_issued
    request_id = str(uuid.uuid4())
    stop_event = threading.Event()
    _active[request_id] = stop_event
    client_ip = request.client.host if request.client else None

    start_evt = threading.Event()
    done_evt = threading.Event()
    with _ticket_lock:
        _ticket_issued += 1
        my_ticket = _ticket_issued
    _job_queue.put((start_evt, done_evt))

    def generate():
        def log_event(msg: str):
            return f"data: {json.dumps({'type': 'log', 'text': msg})}\n\n"

        def ts():
            return datetime.datetime.now().strftime("%H:%M:%S")

        started = time.time()
        final_status = "error"
        final_error = None

        try:
            yield f"data: {json.dumps({'type': 'id', 'text': request_id})}\n\n"

            # Length check up front
            max_chars = CFG.get("max_chars", 30000)
            if len(req.text) > max_chars:
                final_error = f"Text too long: {len(req.text)} > {max_chars}"
                msg = (f"Текст занадто довгий: {len(req.text)} символів "
                       f"(максимум {max_chars})")
                yield log_event(f"[{ts()}] ERROR: {msg}")
                yield f"data: {json.dumps({'type': 'error', 'text': msg})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            # Wait in queue until our turn
            while not start_evt.wait(timeout=2):
                if stop_event.is_set():
                    final_status = "stopped"
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return
                with _ticket_lock:
                    ahead = my_ticket - _ticket_serving
                if ahead > 0:
                    yield f"data: {json.dumps({'type': 'queue', 'ahead': ahead})}\n\n"

            yield f"data: {json.dumps({'type': 'queue', 'ahead': 0})}\n\n"

            normalized = req.text.replace("\r\n", "\n")

            yield log_event(f"[{ts()}] URL:   {CFG['base_url']}")
            yield log_event(f"[{ts()}] Model: {CFG['model']}")
            yield log_event(f"[{ts()}] Lang:  {req.lang_from} → {req.lang_to}")
            yield log_event(f"[{ts()}] Text:  {len(req.text)} chars")

            try:
                # Single LLM call with whole text — preserves cross-paragraph
                # context (terminology, document domain). The model keeps the
                # paragraph breaks naturally in its output.
                translated = _translate_unit(
                    normalized, req.lang_from, req.lang_to, stop_event,
                )
                if translated is None:
                    final_status = "stopped"
                    yield log_event(f"[{ts()}] Stopped by user")
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                final_status = "success"
                yield log_event(f"[{ts()}] Done — {len(translated)} chars")
                yield f"data: {json.dumps({'type': 'token', 'text': translated})}\n\n"
                yield f"data: {json.dumps({'type': 'result', 'text': translated})}\n\n"

            except req_lib.exceptions.Timeout:
                final_error = "Timeout"
                log.error(f"Translation timeout after {CFG['llm_timeout']}s")
                yield log_event(f"[{ts()}] ERROR: Timeout after {CFG['llm_timeout']}s")
                yield f"data: {json.dumps({'type': 'error', 'text': 'Сервер не встиг відповісти. Спробуйте пізніше.'})}\n\n"
            except Exception as e:
                final_error = str(e)
                log.error(f"Translation error: {e}\n{traceback.format_exc()}")
                yield log_event(f"[{ts()}] ERROR: {e}")
                yield log_event(traceback.format_exc())
                yield f"data: {json.dumps({'type': 'error', 'text': 'Помилка сервера. Деталі в адмін-логах.'})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        finally:
            done_evt.set()
            _active.pop(request_id, None)
            log_stat(
                ip=client_ip,
                kind="text",
                filename=None,
                file_ext=None,
                lang_from=req.lang_from,
                lang_to=req.lang_to,
                chars=len(req.text),
                pages=None,
                duration=round(time.time() - started, 2),
                status=final_status,
                error=final_error,
            )

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/translate-html")
def translate_html(req: TranslateHtmlRequest, request: Request):
    """Translate HTML preserving formatting tags — text nodes only."""
    global _ticket_issued
    request_id = str(uuid.uuid4())
    stop_event = threading.Event()
    _active[request_id] = stop_event
    client_ip = request.client.host if request.client else None

    start_evt = threading.Event()
    done_evt = threading.Event()
    with _ticket_lock:
        _ticket_issued += 1
        my_ticket = _ticket_issued
    _job_queue.put((start_evt, done_evt))

    def generate():
        def log_event(msg):
            return f"data: {json.dumps({'type': 'log', 'text': msg})}\n\n"
        def ts():
            return datetime.datetime.now().strftime("%H:%M:%S")

        started = time.time()
        final_status = "error"
        final_error = None

        try:
            yield f"data: {json.dumps({'type': 'id', 'text': request_id})}\n\n"

            max_chars = CFG.get("max_chars", 30000)
            if len(req.html) > max_chars:
                msg = f"Текст занадто довгий: {len(req.html)} символів (максимум {max_chars})"
                yield f"data: {json.dumps({'type': 'error', 'text': msg})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            while not start_evt.wait(timeout=2):
                if stop_event.is_set():
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return
                with _ticket_lock:
                    ahead = my_ticket - _ticket_serving
                if ahead > 0:
                    yield f"data: {json.dumps({'type': 'queue', 'ahead': ahead})}\n\n"

            yield f"data: {json.dumps({'type': 'queue', 'ahead': 0})}\n\n"
            yield log_event(f"[{ts()}] HTML translation: {len(req.html)} bytes")

            try:
                import html2text as h2t

                # Convert HTML → Markdown (like TipTap does in OpenWebUI)
                converter = h2t.HTML2Text()
                converter.ignore_links = False
                converter.body_width = 0  # no line wrapping
                markdown_text = converter.handle(req.html).strip()

                if not markdown_text:
                    final_status = "error"
                    yield f"data: {json.dumps({'type': 'error', 'text': 'Порожній текст'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                yield log_event(f"[{ts()}] Markdown: {len(markdown_text)} chars")

                # Stream translation of Markdown — model preserves ** ## - naturally
                translated_md = ""
                for token in _translate_unit_streaming(
                    markdown_text, req.lang_from, req.lang_to, stop_event
                ):
                    if stop_event.is_set():
                        final_status = "stopped"
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return
                    translated_md += token
                    yield f"data: {json.dumps({'type': 'token', 'text': token})}\n\n"

                if not translated_md:
                    final_status = "error"
                    yield f"data: {json.dumps({'type': 'error', 'text': 'Ollama не повернув результат'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                final_status = "success"
                yield log_event(f"[{ts()}] Done — {len(translated_md)} chars")
                # Send Markdown — frontend renders it via marked.js (like OpenWebUI)
                yield f"data: {json.dumps({'type': 'result', 'text': translated_md, 'format': 'markdown'})}\n\n"

            except req_lib.exceptions.Timeout:
                final_error = "Timeout"
                yield f"data: {json.dumps({'type': 'error', 'text': 'Сервер не встиг відповісти.'})}\n\n"
            except Exception as e:
                final_error = str(e)
                log.error(f"HTML translation error: {e}\n{traceback.format_exc()}")
                yield f"data: {json.dumps({'type': 'error', 'text': 'Помилка сервера.'})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        finally:
            done_evt.set()
            _active.pop(request_id, None)
            log_stat(
                ip=client_ip, kind="text", filename=None, file_ext=None,
                lang_from=req.lang_from, lang_to=req.lang_to,
                chars=len(req.html), pages=None,
                duration=round(time.time() - started, 2),
                status=final_status, error=final_error,
            )

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/translate-file")
async def translate_file_endpoint(
    request: Request,
    file: UploadFile = File(...),
    lang_from: str = Form("auto"),
    lang_to: str = Form("Ukrainian"),
):
    content = await file.read()
    filename = file.filename or "file.txt"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    client_ip = request.client.host if request.client else None

    request_id = str(uuid.uuid4())
    stop_event = threading.Event()
    _active[request_id] = stop_event

    def generate():
        def ts():
            return datetime.datetime.now().strftime("%H:%M:%S")

        def log_event(msg: str):
            return f"data: {json.dumps({'type': 'log', 'text': f'[{ts()}] {msg}'})}\n\n"

        started = time.time()
        meta = {"chars": None, "pages": None}
        final_status = "error"
        final_error = None

        try:
            yield f"data: {json.dumps({'type': 'id', 'text': request_id})}\n\n"
            yield log_event(f"File: {filename} ({len(content)} bytes)")

            if ext == "docx":
                it = translate_docx_bytes(content, base, lang_from, lang_to, stop_event)
            elif ext == "pdf":
                it = translate_pdf_bytes(content, base, lang_from, lang_to, stop_event)
            else:
                it = translate_txt_bytes(content, base, lang_from, lang_to, stop_event)

            try:
                for event in it:
                    kind = event[0]
                    if kind == "log":
                        yield f"data: {json.dumps({'type': 'log', 'text': event[1]})}\n\n"
                    elif kind == "meta":
                        meta.update(event[1])
                    elif kind == "progress":
                        yield f"data: {json.dumps({'type': 'progress', 'text': event[1], 'pct': event[2]})}\n\n"
                    elif kind == "stats":
                        yield f"data: {json.dumps({'type': 'stats', 'tok_in': event[1], 'tok_out': event[2]})}\n\n"
                    elif kind == "error":
                        final_error = event[1]
                        log.warning(f"[{filename}] {event[1]}")
                        yield f"data: {json.dumps({'type': 'error', 'text': event[1]})}\n\n"
                        return
                    elif kind == "stopped":
                        final_status = "stopped"
                        yield f"data: {json.dumps({'type': 'log', 'text': 'Зупинено користувачем'})}\n\n"
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return
                    elif kind == "done":
                        _, out_filename, mime, data = event
                        file_id = str(uuid.uuid4())
                        _results[file_id] = (out_filename, data, mime)
                        final_status = "success"
                        yield f"data: {json.dumps({'type': 'download', 'url': f'/download/{file_id}', 'filename': out_filename})}\n\n"
                        # Generate HTML preview for DOCX files
                        if ext == "docx":
                            try:
                                import mammoth as _mammoth
                                html_val = _mammoth.convert_to_html(io.BytesIO(data)).value
                                _preview_cache[file_id] = html_val.encode("utf-8")
                                yield f"data: {json.dumps({'type': 'preview', 'url': f'/preview/{file_id}'})}\n\n"
                            except Exception as _e:
                                log.warning(f"mammoth preview failed: {_e}")
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return
            except req_lib.exceptions.Timeout:
                final_error = "Timeout від сервера перекладу"
                log.error(f"[{filename}] Timeout")
                yield log_event("ERROR: Timeout")
                yield f"data: {json.dumps({'type': 'error', 'text': 'Сервер не встиг відповісти. Спробуйте пізніше.'})}\n\n"
                return
            except Exception as e:
                final_error = str(e)
                log.error(f"[{filename}] {e}\n{traceback.format_exc()}")
                yield log_event(f"ERROR: {e}")
                yield log_event(traceback.format_exc())
                yield f"data: {json.dumps({'type': 'error', 'text': 'Помилка сервера. Деталі в адмін-логах.'})}\n\n"
                return

        finally:
            _active.pop(request_id, None)
            log_stat(
                ip=client_ip,
                kind="file",
                filename=filename,
                file_ext=ext,
                lang_from=lang_from,
                lang_to=lang_to,
                chars=meta.get("chars"),
                pages=meta.get("pages"),
                duration=round(time.time() - started, 2),
                status=final_status,
                error=final_error,
            )

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/download/{file_id}")
def download_file(file_id: str):
    entry = _results.get(file_id)
    if not entry:
        return JSONResponse({"error": "not found"}, status_code=404)
    filename, data, mime = entry
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/preview/{file_id}")
def preview_file(file_id: str):
    html = _preview_cache.get(file_id)
    if not html:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Wrap in a minimal page with basic typography
    wrapped = (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        "<style>body{font-family:sans-serif;max-width:860px;margin:40px auto;"
        "padding:0 24px;line-height:1.6;color:#222;}"
        "table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #ccc;padding:6px 10px}"
        "img{max-width:100%}</style></head><body>"
        + html.decode("utf-8")
        + "</body></html>"
    )
    return Response(content=wrapped, media_type="text/html; charset=utf-8")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
