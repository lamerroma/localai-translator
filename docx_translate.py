# docx_translate.py
# DOCX translation engine — XML manipulation logic ported from DocuTranslate
# (github.com/QinHan/DocuTranslate, MPL-2.0), translation calls replaced with
# direct Ollama integration.
#
# Public API:
#   translate_docx(content, lang_to, translate_batch_fn, stop_event, chunk_size)
#     -> bytes  (translated .docx)
#
# translate_batch_fn signature:
#   fn(batch: dict[str, str], lang_to: str, stop_event) -> dict[str, str]

import io
import logging
from collections import defaultdict
from copy import deepcopy
from typing import List, Dict, Any, Tuple, Callable
import threading

import docx
from docx.document import Document as DocxDocument
from docx.opc.part import Part
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.oxml.text.run import CT_R
from docx.section import _Header, _Footer
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from docx.table import _Cell, Table

log = logging.getLogger("docx_translate")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SIGNIFICANT_STYLES = frozenset([
    qn('w:u'),        # underline
    qn('w:strike'),   # strikethrough
    qn('w:dstrike'),  # double strikethrough
    qn('w:shd'),      # shading / background
    qn('w:highlight'),
    qn('w:bdr'),      # border
    qn('w:effectLst'),
    qn('w:em'),       # emphasis mark
])

IGNORED_TAGS = frozenset([
    qn('w:proofErr'), qn('w:lastRenderedPageBreak'),
    qn('w:bookmarkStart'), qn('w:bookmarkEnd'),
    qn('w:commentRangeStart'), qn('w:commentRangeEnd'),
    qn('w:del'), qn('w:ins'), qn('w:moveFrom'), qn('w:moveTo'),
])

RECURSIVE_CONTAINER_TAGS = frozenset([
    qn('w:smartTag'), qn('w:sdtContent'), qn('w:hyperlink'),
])

# ---------------------------------------------------------------------------
# Run helpers
# ---------------------------------------------------------------------------

def _is_image_run(run: Run) -> bool:
    xml = getattr(run.element, 'xml', '')
    return '<w:drawing' in xml or '<w:pict' in xml


def _is_formatting_only_run(run: Run) -> bool:
    return run.text == ""


def _is_tab_run(run: Run) -> bool:
    if run.text.strip():
        return False
    xml = getattr(run.element, 'xml', '')
    return '<w:tab' in xml or '<w:ptab' in xml


def _is_instr_text_run(run: Run) -> bool:
    return run.element.find(qn('w:instrText')) is not None


def _get_significant_styles(run: Run) -> frozenset:
    rPr = run.element.rPr
    if rPr is None:
        return frozenset()
    return frozenset(child.tag for child in rPr if child.tag in SIGNIFICANT_STYLES)


def _same_significant_styles(run1: Run, run2: Run) -> bool:
    return _get_significant_styles(run1) == _get_significant_styles(run2)


# ---------------------------------------------------------------------------
# Segment collection
# ---------------------------------------------------------------------------

def _process_element_children(element, parent_paragraph: Paragraph,
                               elements: List[Dict], texts: List[str],
                               state: Dict, top_level_para: Paragraph):
    """Recursively collect text runs from an XML element."""

    def flush():
        runs = state['current_runs']
        if not runs:
            return
        full_text = "".join(r.text for r in runs)
        if full_text.strip():
            elements.append({
                "type": "text_runs",
                "runs": list(runs),
                "paragraph": parent_paragraph,
                "top_level_paragraph": top_level_para,
            })
            texts.append(full_text)
        runs.clear()

    for child in element:
        if child.tag in IGNORED_TAGS:
            continue

        if child.tag in RECURSIVE_CONTAINER_TAGS:
            flush()
            _process_element_children(child, parent_paragraph, elements, texts, state, top_level_para)
            flush()
            continue

        # Handle fldChar begin/end as segment boundaries
        if isinstance(child, CT_R):
            fc = child.find(qn('w:fldChar'))
            if fc is not None:
                ftype = fc.get(qn('w:fldCharType'))
                if ftype in ('begin', 'end'):
                    flush()
                continue

        if isinstance(child, CT_R):
            run = Run(child, parent_paragraph)

            # Text boxes inside drawings
            if _is_image_run(run):
                for txbx in run.element.iter(qn('w:txbxContent')):
                    flush()
                    for p_elem in txbx.findall(qn('w:p')):
                        shape_para = Paragraph(p_elem, parent_paragraph)
                        _process_paragraph(shape_para, elements, texts, top_level_para=top_level_para)
                continue

            if _is_formatting_only_run(run) or _is_tab_run(run) or _is_instr_text_run(run):
                flush()
                continue

            last = state['current_runs'][-1] if state['current_runs'] else None
            if last and not _same_significant_styles(last, run):
                flush()

            state['current_runs'].append(run)
        else:
            flush()


def _process_paragraph(para: Paragraph, elements: List[Dict], texts: List[str],
                        top_level_para: Paragraph = None):
    if top_level_para is None:
        top_level_para = para
    state = {'current_runs': []}
    _process_element_children(para._p, para, elements, texts, state, top_level_para)
    runs = state['current_runs']
    if runs:
        full_text = "".join(r.text for r in runs)
        if full_text.strip():
            elements.append({
                "type": "text_runs",
                "runs": list(runs),
                "paragraph": para,
                "top_level_paragraph": top_level_para,
            })
            texts.append(full_text)


def _process_body_elements(parent_element, container, elements: List[Dict], texts: List[str]):
    for child in parent_element:
        tag = child.tag
        if tag.endswith('}p'):
            _process_paragraph(Paragraph(child, container), elements, texts)
        elif tag.endswith('}tbl'):
            table = Table(child, container)
            for row in table.rows:
                for cell in row.cells:
                    _traverse_container(cell, elements, texts)
        elif tag.endswith('}sdt'):
            sdt_content = child.find(qn('w:sdtContent'))
            if sdt_content is not None:
                _process_body_elements(sdt_content, container, elements, texts)


def _traverse_container(container, elements: List[Dict], texts: List[str]):
    if container is None:
        return
    if isinstance(container, (DocxDocument, Part)):
        parent_element = container.element.body if hasattr(container.element, 'body') else container.element
    elif isinstance(container, (_Cell, _Header, _Footer)):
        parent_element = container._element
    else:
        log.warning(f"Unknown container type: {type(container)}")
        return

    if parent_element is not None and parent_element.tag in (qn('w:footnotes'), qn('w:endnotes')):
        for note_elem in parent_element:
            _process_body_elements(note_elem, container, elements, texts)
    elif parent_element is not None:
        _process_body_elements(parent_element, container, elements, texts)


def _collect_all_segments(doc: DocxDocument) -> Tuple[List[Dict], List[str]]:
    """Collect all translatable text segments from the entire document."""
    elements, texts = [], []

    _traverse_container(doc, elements, texts)

    for section in doc.sections:
        for hf in (section.header, section.first_page_header, section.even_page_header,
                   section.footer, section.first_page_footer, section.even_page_footer):
            if hf is not None:
                _traverse_container(hf, elements, texts)

    for attr in ('footnotes_part', 'endnotes_part'):
        part = getattr(doc.part, attr, None)
        if part is not None:
            _traverse_container(part, elements, texts)

    return elements, texts


# ---------------------------------------------------------------------------
# Apply translations
# ---------------------------------------------------------------------------

def _apply_translation(element_info: Dict, final_text: str):
    runs = element_info["runs"]
    if not runs:
        return
    first_idx = -1
    for i, run in enumerate(runs):
        if run.element.getparent() is not None:
            run._parent = element_info["paragraph"]
            run.text = final_text
            first_idx = i
            break
    if first_idx == -1:
        log.warning(f"No live run found for translation '{final_text[:40]}'")
        return
    for run in runs[first_idx + 1:]:
        parent = run.element.getparent()
        if parent is not None:
            try:
                parent.remove(run.element)
            except ValueError:
                pass


def _prune_unwanted_from_copy(p_element):
    """Remove images, PAGE/TOC field instructions from a paragraph copy."""
    runs_to_remove = []
    runs = p_element.findall(qn('w:r'))
    i = 0
    while i < len(runs):
        run_el = runs[i]
        if (run_el.find(qn('w:drawing')) is not None or
                run_el.find(qn('w:pict')) is not None):
            runs_to_remove.append(run_el)
            i += 1
            continue

        fc = run_el.find(qn('w:fldChar'))
        if fc is not None and fc.get(qn('w:fldCharType')) == 'begin':
            is_target = False
            is_toc = False
            for j in range(i + 1, len(runs)):
                instr = runs[j].find(qn('w:instrText'))
                if instr is not None and instr.text:
                    text = instr.text.strip().upper()
                    if 'PAGE' in text or 'NUMPAGES' in text:
                        is_target = True
                        break
                    if text.startswith('TOC'):
                        is_target = True
                        is_toc = True
                        break
                nfc = runs[j].find(qn('w:fldChar'))
                if nfc is not None and nfc.get(qn('w:fldCharType')) in ('begin', 'end'):
                    break

            if is_target:
                if is_toc:
                    runs_to_remove.append(run_el)
                    found_sep = False
                    for j in range(i + 1, len(runs)):
                        runs_to_remove.append(runs[j])
                        nfc = runs[j].find(qn('w:fldChar'))
                        if nfc is not None:
                            ft = nfc.get(qn('w:fldCharType'))
                            if ft == 'separate':
                                found_sep = True
                                i = j + 1
                                break
                            if ft == 'end':
                                i = j + 1
                                break
                    if found_sep:
                        continue
                else:
                    field_runs = [run_el]
                    end_found = False
                    end_idx = i
                    for j in range(i + 1, len(runs)):
                        field_runs.append(runs[j])
                        nfc = runs[j].find(qn('w:fldChar'))
                        if nfc is not None and nfc.get(qn('w:fldCharType')) == 'end':
                            end_found = True
                            end_idx = j
                            break
                    if end_found:
                        runs_to_remove.extend(field_runs)
                        i = end_idx + 1
                        continue
        i += 1

    for r in runs_to_remove:
        if r.getparent() is not None:
            p_element.remove(r)


def _apply_all_translations(doc: DocxDocument, elements: List[Dict],
                             translated: List[str], originals: List[str]) -> bytes:
    if len(elements) != len(translated):
        log.error(f"Segment count mismatch: orig={len(originals)}, trans={len(translated)}")
        n = min(len(elements), len(translated))
        elements, translated = elements[:n], translated[:n]

    for info, trans in zip(elements, translated):
        _apply_translation(info, trans)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def translate_docx(content: bytes,
                   lang_to: str,
                   translate_batch_fn: Callable,
                   stop_event: threading.Event,
                   chunk_size: int = 3000) -> bytes:
    """Translate a DOCX file, preserving all formatting.

    Args:
        content: raw .docx bytes
        lang_to: target language name, e.g. "Ukrainian"
        translate_batch_fn: fn(batch: dict[str,str], lang_to: str, stop_event) -> dict[str,str]
        stop_event: set this to abort mid-translation
        chunk_size: max characters per LLM request

    Returns:
        Translated .docx bytes. Raises on unrecoverable errors.
    """
    doc = docx.Document(io.BytesIO(content))
    elements, originals = _collect_all_segments(doc)

    if not originals:
        log.info("No translatable text found in DOCX")
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    # Group segments into batches by chunk_size
    translated: List[str] = [""] * len(originals)
    batch_ids: Dict[str, int] = {}   # batch_key -> index in originals
    current_batch: Dict[str, str] = {}
    current_chars = 0

    def flush_batch():
        if not current_batch or stop_event.is_set():
            return
        result = translate_batch_fn(current_batch, lang_to, stop_event)
        for k, v in result.items():
            idx = batch_ids.get(k)
            if idx is not None:
                translated[idx] = v or originals[idx]

    for i, text in enumerate(originals):
        if stop_event.is_set():
            break
        key = str(i)
        if current_chars + len(text) > chunk_size and current_batch:
            flush_batch()
            batch_ids.clear()
            current_batch.clear()
            current_chars = 0
        current_batch[key] = text
        batch_ids[key] = i
        current_chars += len(text)

    flush_batch()

    # Fill any untranslated segments with originals
    for i, t in enumerate(translated):
        if not t:
            translated[i] = originals[i]

    return _apply_all_translations(doc, elements, translated, originals)
