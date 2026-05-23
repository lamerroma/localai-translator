import io
import json
import os
import datetime
import uuid
import threading
from urllib.parse import quote
import requests as req_lib
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from pydantic import BaseModel

# ── Configuration ────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "translator_config.json")

DEFAULTS = {
    "base_url":        "http://192.168.200.92:30286/v1",
    "model":           "yanolja_yanoljanext-rosetta-12b-2510",
    "max_tokens":      2048,
    "llm_timeout":     180,
    "chunk_size":      3000,
    "system_msg":      "You are a professional translator. Translate accurately and naturally, preserving the original tone and style.",
    "prompt_template": "{text}\n\n§ Translate the text above {direction}. This is a {context}. Keep all ⟨P⟩ and ⟨N⟩ markers exactly in place — they mark paragraph and line breaks. Preserve technical terms, proper nouns, and numbers exactly. Do not add notes or commentary. Output ONLY the translation. §",
}

HOST = "0.0.0.0"
PORT = 7860

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

# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI()

_active: dict[str, threading.Event] = {}
_results: dict[str, tuple[str, bytes]] = {}


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


HTML = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<title>Перекладач</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: sans-serif; background: #f5f5f5; padding: 24px; max-width: 900px; margin: 0 auto; }
  h1 { margin-bottom: 20px; font-size: 1.4rem; color: #333; }
  .card { background: white; border-radius: 8px; padding: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.1); margin-bottom: 16px; }
  label { display: block; font-size: .85rem; color: #555; margin-bottom: 4px; }
  select, textarea, input[type=text], input[type=number] { width: 100%; border: 1px solid #ddd; border-radius: 6px; padding: 8px 10px; font-size: .95rem; font-family: inherit; }
  textarea { resize: vertical; }
  .row { display: flex; gap: 12px; margin-bottom: 12px; }
  .row > div { flex: 1; }
  .btn-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  button {
    background: #2563eb; color: white; border: none; border-radius: 6px;
    padding: 10px 24px; font-size: 1rem; cursor: pointer; transition: background .2s;
  }
  button:hover { background: #1d4ed8; }
  button:disabled { background: #93c5fd; cursor: not-allowed; }
  .btn-stop { background: #dc2626; display: none; }
  .btn-stop:hover { background: #b91c1c; }
  .btn-save { background: #16a34a; }
  .btn-save:hover { background: #15803d; }
  .btn-download { background: #16a34a; text-decoration: none; display: none; padding: 10px 20px; border-radius: 6px; font-size: 1rem; color: white; }
  .btn-download:hover { background: #15803d; }
  .log-box {
    background: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: .8rem;
    padding: 12px; border-radius: 6px; height: 160px; overflow-y: auto;
    white-space: pre-wrap; word-break: break-all; margin-top: 12px;
  }
  .status { font-size: .8rem; color: #888; margin-top: 6px; }
  .status.ok { color: #16a34a; }
  .status.err { color: #dc2626; }
  details { margin-top: 12px; }
  summary { font-size: .85rem; color: #555; cursor: pointer; user-select: none; }
  summary:hover { color: #333; }
  .hint { font-size: .75rem; color: #aaa; margin-top: 4px; }
  .file-drop {
    border: 2px dashed #ddd; border-radius: 6px; padding: 24px; text-align: center;
    color: #aaa; font-size: .9rem; cursor: pointer; transition: border-color .2s, color .2s;
  }
  .file-drop.drag-over { border-color: #2563eb; color: #2563eb; }
  .file-drop input { display: none; }
  .file-name { font-size: .85rem; color: #555; margin-top: 6px; }
  .progress-bar-wrap { height: 6px; background: #e5e7eb; border-radius: 3px; margin-top: 10px; display: none; }
  .progress-bar { height: 100%; background: #2563eb; border-radius: 3px; width: 0; transition: width .3s; }
  .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
  .settings-grid .full { grid-column: 1 / -1; }
  .section-title { font-size: 1rem; font-weight: 600; color: #333; margin-bottom: 12px; }
</style>
</head>
<body>
<h1>LocalAI Перекладач</h1>

<!-- Language selectors + Advanced (shared) -->
<div class="card">
  <div class="row">
    <div>
      <label>Перекласти з</label>
      <select id="lang_from"></select>
    </div>
    <div>
      <label>Перекласти на</label>
      <select id="lang_to"></select>
    </div>
  </div>

  <details>
    <summary>Додатково (перевизначення для цього запиту)</summary>
    <div style="margin-top:8px; display:flex; flex-direction:column; gap:10px;">
      <div>
        <label>Тип документу (необов'язково)</label>
        <input type="text" id="context" placeholder="напр. технічний посібник, юридичний договір, медичний звіт">
      </div>
      <div>
        <label>Системне повідомлення</label>
        <textarea id="system_msg" rows="2"></textarea>
      </div>
      <div>
        <label>Шаблон промпту</label>
        <textarea id="prompt_tpl" rows="4"></textarea>
        <div class="hint">Плейсхолдери: <b>{text}</b>, <b>{lang}</b>, <b>{context}</b></div>
      </div>
    </div>
  </details>
</div>

<!-- Text translation -->
<div class="card">
  <label style="margin-bottom:8px; font-size:1rem; color:#333; font-weight:600;">Переклад тексту</label>
  <div style="margin-bottom:12px; margin-top:8px;">
    <textarea id="input" rows="6" placeholder="Введіть текст тут..."></textarea>
  </div>
  <div class="btn-row">
    <button id="btn" onclick="doTranslate()">Перекласти</button>
    <button id="stopBtn" class="btn-stop" onclick="doStop()">Зупинити</button>
    <span class="status" id="status"></span>
  </div>
</div>

<div class="card">
  <label style="margin-bottom:8px">Результат перекладу</label>
  <textarea id="result" rows="6" readonly placeholder="Результат з'явиться тут..."></textarea>
</div>

<div class="card">
  <label style="margin-bottom:8px">Лог</label>
  <div id="log" class="log-box"></div>
</div>

<!-- File translation -->
<div class="card">
  <label style="margin-bottom:8px; font-size:1rem; color:#333; font-weight:600;">Переклад файлу</label>
  <div style="margin-top:8px; margin-bottom:12px;">
    <div class="file-drop" id="file-drop" onclick="document.getElementById('file-input').click()"
         ondragover="fileDragOver(event)" ondragleave="fileDragLeave(event)" ondrop="fileDrop(event)">
      <input type="file" id="file-input" accept=".pdf,.txt,.docx" onchange="fileSelected(this)">
      Натисніть або перетягніть файл сюди<br>
      <span style="font-size:.75rem">PDF, DOCX, TXT</span>
    </div>
    <div class="file-name" id="file-name"></div>
    <div class="progress-bar-wrap" id="progress-wrap"><div class="progress-bar" id="progress-bar"></div></div>
  </div>
  <div class="btn-row">
    <button id="file-btn" onclick="doTranslateFile()">Перекласти файл</button>
    <button id="file-stopBtn" class="btn-stop" onclick="doFileStop()">Зупинити</button>
    <a id="download-link" class="btn-download">&#8595; Завантажити</a>
    <span class="status" id="file-status"></span>
  </div>
  <div id="file-log" class="log-box" style="display:none;"></div>
</div>

<!-- Settings -->
<div class="card">
  <details id="settings-details">
    <summary class="section-title" style="margin-bottom:0">&#9881; Налаштування</summary>
    <div class="settings-grid">
      <div class="full">
        <label>LocalAI URL</label>
        <input type="text" id="cfg_base_url" placeholder="http://192.168.x.x:port/v1">
      </div>
      <div class="full">
        <label>Модель</label>
        <input type="text" id="cfg_model" placeholder="назва моделі як у LocalAI">
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
      <div></div>
      <div class="full">
        <label>Системне повідомлення за замовчуванням</label>
        <textarea id="cfg_system_msg" rows="2"></textarea>
      </div>
      <div class="full">
        <label>Шаблон промпту за замовчуванням</label>
        <textarea id="cfg_prompt_template" rows="4"></textarea>
        <div class="hint">Плейсхолдери: <b>{text}</b>, <b>{direction}</b>, <b>{lang}</b>, <b>{context}</b> — <b>{direction}</b> = "from de into uk" або "into uk" при автовизначенні</div>
      </div>
    </div>
    <div class="btn-row" style="margin-top:12px">
      <button class="btn-save" onclick="saveSettings()">Зберегти</button>
      <button onclick="resetSettings()" style="background:#6b7280">Скинути до стандартних</button>
      <span class="status" id="cfg-status"></span>
    </div>
  </details>
</div>

<script>
// ── Settings ──────────────────────────────────────────────────────────
async function loadSettings() {
  const r = await fetch('/config');
  const cfg = await r.json();
  document.getElementById('cfg_base_url').value        = cfg.base_url        ?? '';
  document.getElementById('cfg_model').value           = cfg.model           ?? '';
  document.getElementById('cfg_max_tokens').value      = cfg.max_tokens      ?? 2048;
  document.getElementById('cfg_llm_timeout').value     = cfg.llm_timeout     ?? 180;
  document.getElementById('cfg_chunk_size').value      = cfg.chunk_size      ?? 3000;
  document.getElementById('cfg_system_msg').value      = cfg.system_msg      ?? '';
  document.getElementById('cfg_prompt_template').value = cfg.prompt_template ?? '';
  // populate per-request overrides with current defaults
  document.getElementById('system_msg').value  = cfg.system_msg      ?? '';
  document.getElementById('prompt_tpl').value  = cfg.prompt_template ?? '';
}

async function saveSettings() {
  const cfg = {
    base_url:        document.getElementById('cfg_base_url').value.trim(),
    model:           document.getElementById('cfg_model').value.trim(),
    max_tokens:      parseInt(document.getElementById('cfg_max_tokens').value),
    llm_timeout:     parseInt(document.getElementById('cfg_llm_timeout').value),
    chunk_size:      parseInt(document.getElementById('cfg_chunk_size').value),
    system_msg:      document.getElementById('cfg_system_msg').value,
    prompt_template: document.getElementById('cfg_prompt_template').value,
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
  const cfg = await r.json();
  document.getElementById('cfg_base_url').value        = cfg.base_url;
  document.getElementById('cfg_model').value           = cfg.model;
  document.getElementById('cfg_max_tokens').value      = cfg.max_tokens;
  document.getElementById('cfg_llm_timeout').value     = cfg.llm_timeout;
  document.getElementById('cfg_chunk_size').value      = cfg.chunk_size;
  document.getElementById('cfg_system_msg').value      = cfg.system_msg;
  document.getElementById('cfg_prompt_template').value = cfg.prompt_template;
}

// ── Text translation ──────────────────────────────────────────────────
let _controller = null;
let _requestId = null;

function setWorking(on) {
  document.getElementById('btn').disabled = on;
  document.getElementById('stopBtn').style.display = on ? 'inline-block' : 'none';
}

async function doStop() {
  if (_controller) { _controller.abort(); _controller = null; }
  if (_requestId) {
    await fetch('/stop/' + _requestId, { method: 'POST' }).catch(() => {});
    _requestId = null;
  }
  document.getElementById('status').textContent = 'Зупинено';
  setWorking(false);
}

async function doTranslate() {
  const text = document.getElementById('input').value.trim();
  if (!text) { document.getElementById('status').textContent = 'ПОМИЛКА: текст порожній'; return; }

  const log = document.getElementById('log');
  const result = document.getElementById('result');
  const status = document.getElementById('status');

  setWorking(true);
  log.textContent = '';
  result.value = '';
  status.textContent = 'Перекладаю...';

  const startTime = Date.now();
  _controller = new AbortController();

  try {
    const response = await fetch('/translate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: _controller.signal,
      body: JSON.stringify({
        text,
        lang_from: document.getElementById('lang_from').value,
        lang_to: document.getElementById('lang_to').value,
        prompt_template: document.getElementById('prompt_tpl').value,
        system_msg: document.getElementById('system_msg').value,
        context: document.getElementById('context').value || 'document',
      }),
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let evt; try { evt = JSON.parse(line.slice(6)); } catch { continue; }
        if (evt.type === 'id') { _requestId = evt.text; }
        else if (evt.type === 'log') { log.textContent += evt.text + '\n'; log.scrollTop = log.scrollHeight; }
        else if (evt.type === 'token') { result.value += evt.text; }
        else if (evt.type === 'result') {
          result.value = evt.text;
          status.textContent = `Готово за ${((Date.now()-startTime)/1000).toFixed(1)}с`;
        } else if (evt.type === 'error') { result.value = 'Помилка: ' + evt.text; status.textContent = 'Помилка'; }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') { log.textContent += 'Помилка запиту: ' + e + '\n'; status.textContent = 'Помилка'; }
  }
  setWorking(false); _controller = null; _requestId = null;
}

document.getElementById('input').addEventListener('keydown', e => {
  if (e.ctrlKey && e.key === 'Enter') doTranslate();
});

// ── File translation ──────────────────────────────────────────────────
let _fileController = null;
let _fileRequestId = null;
let _selectedFile = null;

function setFileWorking(on) {
  document.getElementById('file-btn').disabled = on;
  document.getElementById('file-stopBtn').style.display = on ? 'inline-block' : 'none';
  if (on) document.getElementById('download-link').style.display = 'none';
}

function fileSelected(input) {
  _selectedFile = input.files[0] || null;
  document.getElementById('file-name').textContent = _selectedFile ? _selectedFile.name : '';
}

function fileDragOver(e) { e.preventDefault(); document.getElementById('file-drop').classList.add('drag-over'); }
function fileDragLeave(e) { document.getElementById('file-drop').classList.remove('drag-over'); }
function fileDrop(e) {
  e.preventDefault();
  document.getElementById('file-drop').classList.remove('drag-over');
  const f = e.dataTransfer.files[0];
  if (f) { _selectedFile = f; document.getElementById('file-name').textContent = f.name; }
}

async function doFileStop() {
  if (_fileController) { _fileController.abort(); _fileController = null; }
  if (_fileRequestId) {
    await fetch('/stop/' + _fileRequestId, { method: 'POST' }).catch(() => {});
    _fileRequestId = null;
  }
  document.getElementById('file-status').textContent = 'Зупинено';
  setFileWorking(false);
  document.getElementById('progress-wrap').style.display = 'none';
}

async function doTranslateFile() {
  if (!_selectedFile) { document.getElementById('file-status').textContent = 'Спочатку оберіть файл'; return; }

  const fileLog = document.getElementById('file-log');
  const fileStatus = document.getElementById('file-status');
  const progressWrap = document.getElementById('progress-wrap');
  const progressBar = document.getElementById('progress-bar');

  setFileWorking(true);
  fileLog.style.display = 'block';
  fileLog.textContent = '';
  progressWrap.style.display = 'block';
  progressBar.style.width = '0%';
  fileStatus.textContent = 'Завантаження...';

  const startTime = Date.now();
  _fileController = new AbortController();

  const formData = new FormData();
  formData.append('file', _selectedFile, _selectedFile.name);
  formData.append('lang_from', document.getElementById('lang_from').value);
  formData.append('lang_to', document.getElementById('lang_to').value);
  formData.append('system_msg', document.getElementById('system_msg').value);
  formData.append('prompt_template', document.getElementById('prompt_tpl').value);
  formData.append('context', document.getElementById('context').value || 'document');

  try {
    const response = await fetch('/translate-file', {
      method: 'POST',
      signal: _fileController.signal,
      body: formData,
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let evt; try { evt = JSON.parse(line.slice(6)); } catch { continue; }
        if (evt.type === 'id') { _fileRequestId = evt.text; }
        else if (evt.type === 'log') { fileLog.textContent += evt.text + '\n'; fileLog.scrollTop = fileLog.scrollHeight; }
        else if (evt.type === 'progress') {
          fileStatus.textContent = evt.text;
          if (evt.pct !== undefined) progressBar.style.width = evt.pct + '%';
        } else if (evt.type === 'download') {
          const link = document.getElementById('download-link');
          link.href = evt.url;
          link.download = evt.filename;
          link.style.display = 'inline-block';
          progressBar.style.width = '100%';
          fileStatus.textContent = `Готово за ${((Date.now()-startTime)/1000).toFixed(1)}с`;
        } else if (evt.type === 'error') {
          fileStatus.textContent = 'Помилка: ' + evt.text;
          progressWrap.style.display = 'none';
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') { fileLog.textContent += 'Помилка запиту: ' + e + '\n'; fileStatus.textContent = 'Помилка'; }
  }
  setFileWorking(false); _fileController = null; _fileRequestId = null;
}

// ── Init selects ──────────────────────────────────────────────────────
async function initSelects() {
  const r = await fetch('/languages');
  const langs = await r.json();
  const from = document.getElementById('lang_from');
  const to   = document.getElementById('lang_to');
  from.appendChild(new Option('Автовизначення', 'auto'));
  langs.forEach(l => {
    from.appendChild(new Option(l, l));
    to.appendChild(new Option(l, l));
  });
  to.value = 'Ukrainian';
}

// ── Init ──────────────────────────────────────────────────────────────
initSelects();
loadSettings();
</script>
</body>
</html>"""


class TranslateRequest(BaseModel):
    text: str
    lang_from: str = "auto"
    lang_to: str
    prompt_template: str = ""
    system_msg: str = ""
    context: str = "document"


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML



@app.get("/languages")
def get_languages():
    return JSONResponse(sorted(LANG_MAP.keys()))


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
def translate(req: TranslateRequest):
    request_id = str(uuid.uuid4())
    stop_event = threading.Event()
    _active[request_id] = stop_event

    def generate():
        def log(msg: str):
            return f"data: {json.dumps({'type': 'log', 'text': msg})}\n\n"

        def ts():
            return datetime.datetime.now().strftime("%H:%M:%S")

        try:
            yield f"data: {json.dumps({'type': 'id', 'text': request_id})}\n\n"

            lang_to_code = LANG_MAP.get(req.lang_to, req.lang_to)
            lang_from_code = LANG_MAP.get(req.lang_from)
            direction = f"from {lang_from_code} into {lang_to_code}" if lang_from_code else f"into {lang_to_code}"

            yield log(f"[{ts()}] URL:   {CFG['base_url']}")
            yield log(f"[{ts()}] Model: {CFG['model']}")
            yield log(f"[{ts()}] Lang:  {direction}")
            yield log(f"[{ts()}] Text:  {len(req.text)} chars")

            marked = req.text.replace("\r\n", "\n").replace("\n\n", "⟨P⟩").replace("\n", "⟨N⟩")
            tpl = req.prompt_template.strip() or CFG["prompt_template"]
            context = req.context.strip() or "document"
            prompt = (tpl
                      .replace("{text}", marked)
                      .replace("{direction}", direction)
                      .replace("{lang}", lang_to_code)
                      .replace("{context}", context))

            system_msg = req.system_msg.strip() or CFG["system_msg"]
            messages = [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ]

            yield log(f"[{ts()}] Sending request (streaming)...")

            try:
                resp = req_lib.post(
                    f"{CFG['base_url']}/chat/completions",
                    headers={"Authorization": "Bearer dummy", "Content-Type": "application/json"},
                    json={"model": CFG["model"], "stream": True, "max_tokens": CFG["max_tokens"], "messages": messages},
                    timeout=CFG["llm_timeout"],
                    stream=True,
                )
                yield log(f"[{ts()}] HTTP {resp.status_code} — receiving tokens...")

                collected = []
                for raw_line in resp.iter_lines():
                    if stop_event.is_set():
                        resp.close()
                        yield log(f"[{ts()}] Stopped by user")
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return
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
                    if "error" in chunk:
                        yield log(f"[{ts()}] ERROR: {chunk['error']}")
                        yield f"data: {json.dumps({'type': 'error', 'text': str(chunk['error'])})}\n\n"
                        return
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        collected.append(token)
                        yield f"data: {json.dumps({'type': 'token', 'text': token})}\n\n"

                raw = "".join(collected).strip()
                result = raw.split("§")[0].strip()
                result = result.replace("⟨P⟩", "\n\n").replace("⟨N⟩", "\n")
                yield log(f"[{ts()}] Done — {len(result)} chars (raw: {len(raw)})")
                yield f"data: {json.dumps({'type': 'result', 'text': result})}\n\n"

            except req_lib.exceptions.Timeout:
                timeout_msg = f"Timeout after {CFG['llm_timeout']}s"
                yield log(f"[{ts()}] ERROR: {timeout_msg}")
                yield f"data: {json.dumps({'type': 'error', 'text': timeout_msg})}\n\n"
            except Exception as e:
                import traceback
                yield log(f"[{ts()}] ERROR: {e}")
                yield log(traceback.format_exc())
                yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        finally:
            _active.pop(request_id, None)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/translate-file")
async def translate_file_endpoint(
    file: UploadFile = File(...),
    lang_from: str = Form("auto"),
    lang_to: str = Form("Ukrainian"),
    system_msg: str = Form(""),
    prompt_template: str = Form(""),
    context: str = Form("document"),
):
    content = await file.read()
    filename = file.filename or "file.txt"

    request_id = str(uuid.uuid4())
    stop_event = threading.Event()
    _active[request_id] = stop_event

    def generate():
        def log(msg: str):
            return f"data: {json.dumps({'type': 'log', 'text': msg})}\n\n"

        def ts():
            return datetime.datetime.now().strftime("%H:%M:%S")

        try:
            yield f"data: {json.dumps({'type': 'id', 'text': request_id})}\n\n"

            yield log(f"[{ts()}] File: {filename} ({len(content)} bytes)")
            try:
                text = extract_text(filename, content)
            except Exception as e:
                yield log(f"[{ts()}] ERROR extracting text: {e}")
                yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
                return

            yield log(f"[{ts()}] Extracted {len(text)} chars")

            chunks = split_chunks(text)
            total = len(chunks)
            yield log(f"[{ts()}] Split into {total} chunk(s)")

            lang_to_code = LANG_MAP.get(lang_to, lang_to)
            lang_from_code = LANG_MAP.get(lang_from)
            direction = f"from {lang_from_code} into {lang_to_code}" if lang_from_code else f"into {lang_to_code}"
            tpl = prompt_template.strip() or CFG["prompt_template"]
            ctx = context.strip() or "document"
            sys_msg = system_msg.strip() or CFG["system_msg"]

            translated_chunks = []
            for i, chunk in enumerate(chunks, 1):
                if stop_event.is_set():
                    yield log(f"[{ts()}] Stopped by user")
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                pct = round((i - 1) / total * 100)
                yield f"data: {json.dumps({'type': 'progress', 'text': f'Chunk {i}/{total}...', 'pct': pct})}\n\n"
                yield log(f"[{ts()}] Translating chunk {i}/{total} ({len(chunk)} chars)...")

                marked = chunk.replace("\r\n", "\n").replace("\n\n", "⟨P⟩").replace("\n", "⟨N⟩")
                prompt = (tpl
                          .replace("{text}", marked)
                          .replace("{direction}", direction)
                          .replace("{lang}", lang_to_code)
                          .replace("{context}", ctx))
                messages = [
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": prompt},
                ]

                try:
                    result = call_llm(messages, stop_event)
                except req_lib.exceptions.Timeout:
                    yield log(f"[{ts()}] ERROR: Timeout on chunk {i}")
                    yield f"data: {json.dumps({'type': 'error', 'text': f'Timeout on chunk {i}'})}\n\n"
                    return
                except Exception as e:
                    yield log(f"[{ts()}] ERROR: {e}")
                    yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
                    return

                if result is None:
                    yield log(f"[{ts()}] Stopped by user")
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                translated_chunks.append(result)
                yield log(f"[{ts()}] Chunk {i}/{total} done — {len(result)} chars")

            final = "\n\n".join(translated_chunks)
            file_id = str(uuid.uuid4())
            base = filename.rsplit(".", 1)[0] if "." in filename else filename
            out_filename = base + "_translated.txt"
            _results[file_id] = (out_filename, final.encode("utf-8"))

            yield log(f"[{ts()}] All done — {len(final)} chars total")
            yield f"data: {json.dumps({'type': 'download', 'url': f'/download/{file_id}', 'filename': out_filename})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        finally:
            _active.pop(request_id, None)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/download/{file_id}")
def download_file(file_id: str):
    entry = _results.get(file_id)
    if not entry:
        return JSONResponse({"error": "not found"}, status_code=404)
    filename, data = entry
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
