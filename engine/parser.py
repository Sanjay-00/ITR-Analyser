"""
parser.py  -  document plumbing for the ITR Extractor.

Opens a PDF, gets its text (embedded, or OCR'd if scanned), and hands the
result to columns.py, which locates financial statements, verifies them
against their own arithmetic, and maps them onto the spreading template. This
module owns none of that logic - it owns reading the PDF and, when needed,
talking to Gemini.

Public API:
    spread(sources, api_key=None, use_vision=False, on_progress=None) -> list
    extract_text(pdf_source) -> str
    python -m engine.parser <pdf> [pdf ...]      (debug harness)
"""

import re
import time

import fitz  # PyMuPDF

from .ingest import layout
from .ingest import ocr_extractor

# ─────────────────────────────────────────────────────────────────
# PDF TEXT EXTRACTION
# ─────────────────────────────────────────────────────────────────

def _open_doc(pdf_source) -> "fitz.Document":
    if isinstance(pdf_source, str):
        return fitz.open(pdf_source)
    pdf_source.seek(0)
    return fitz.open(stream=pdf_source.read(), filetype="pdf")


def _normalize_text(text: str) -> str:
    return text.replace("\xa0", " ").replace("–", "-").replace("—", "-")


def _digital_page(page) -> tuple:
    """
    A digital page as (text, cell_rows), with T-account columns preserved.

    Reconstructed from word boxes rather than taken from page.get_text(), whose
    reading order interleaves the two sides of a T-account - it emits

        Salary Paid
        72000.00 Gross Receipt

    for a row that reads "Salary Paid 72,000 | Gross Receipt 9,85,600", welding
    the left side's amount onto the right side's label. Any figure harvested
    from that lands on the wrong side of the account.

    Falls back to plain text if a page carries no word boxes.
    """
    rows = layout.rows_with_cells(layout.words_from_page(page), page.rect.width)
    if not rows:
        return page.get_text(), []
    text = "\n".join(layout.COL_SEP.join(t for _a, _b, t in cells)
                     for _y, cells in rows)
    return text, rows


# A page with fewer characters than this has nothing for the text layer to
# have returned - not a short page, an EMPTY one. Deliberately far below
# ocr_extractor.SCAN_TEXT_THRESHOLD (which asks "is there enough text
# somewhere in the WHOLE document"): a real page always clears this by a
# wide margin, so this only ever catches a page that is actually a picture.
_PAGE_BLANK_THRESHOLD = 20

# A figure with OCR letter look-alikes inside it - "51,65,06,94g",
# "l,06,2g,7g,ggo", "1,,33,94,6L,760". A PDF's "digital" text is not always
# typed: a scanner app often embeds its own OCR layer, and a poor one looks
# exactly like this. Confirmed on a real filing (Borrower I AY
# 2023-24): the Balance Sheet and P&L pages carried such a layer, were
# trusted as exact digital text because they were not blank, and both
# statements failed wildly (one side read 0, a Rs 1.33 crore "Total
# Expenses" read as Rs 55,935). Our own OCR of the page image reads them.
_GARBLED_FIGURE_RE = re.compile(
    r"(?<![A-Za-z])(?=[\dlIOoSgB,]*\d[\dlIOoSgB,]*\d)"
    r"(?:[\dlIOoSgB]{1,3}(?:,{1,2}[\dlIOoSgB]{1,3}){2,})(?![A-Za-z])")


def _looks_garbled(text: str) -> bool:
    """Does this page's text layer look like a poor embedded OCR rather than
    typed text? Two or more Indian-grouped figures carrying a letter or a
    doubled comma - one could be a typo, two is the layer itself."""
    bad = 0
    for m in _GARBLED_FIGURE_RE.finditer(text or ""):
        tok = m.group()
        if re.search(r"[lIOoSgB]", tok) or ",," in tok:
            bad += 1
            if bad >= 2:
                return True
    return False


def _extract(doc, on_progress=None) -> tuple:
    """
    Return (text, is_scanned, page_texts, page_rows, page_confidence).
    Digital PDFs return embedded text - always full confidence, since it's
    read off the PDF's own object structure, not guessed from pixels - and
    scanned PDFs are OCR'd so the same downstream logic can run either way.
    Per-page text, cell geometry and confidence are kept for statement-block
    location and Vision page selection.

    A THIRD case sits between those two: a document that is mostly digital
    text can still carry a handful of pages that are pure images - a CA's
    signed financial statements, photocopied and scanned, then inserted into
    an otherwise digitally-generated ITR bundle. The whole-document check
    below only asks "is there enough text SOMEWHERE in this document" - it
    says nothing about WHICH pages have it, so a document that clears the
    bar overall can still have its most important pages sitting at zero and
    never get read at all. Confirmed on a real filing: 13 of a 20-page
    bundle - including the entire Balance Sheet - were pure images, while
    the computation-sheet pages alone carried enough digital text to route
    the whole document past OCR, and the tool concluded "NIL return" on a
    company with real financials it had never actually looked at. Any page
    whose own digital text comes back essentially empty is OCR'd on its own
    (`ocr_extractor.ocr_pages`) without forcing the OCR path onto every
    OTHER page, which already read correctly and for free.
    """
    pages = [_digital_page(page) for page in doc]
    page_texts = [t for t, _r in pages]
    page_rows  = [r for _t, r in pages]
    text = _normalize_text("\n".join(page_texts))

    if len(text.strip()) >= ocr_extractor.SCAN_TEXT_THRESHOLD:
        # Blank pages (a scanned image inside a digital bundle) and pages
        # whose text layer is a garbled embedded OCR (_looks_garbled) are
        # both read from the image instead.
        blank = [i for i, t in enumerate(page_texts)
                 if len(t.strip()) < _PAGE_BLANK_THRESHOLD or _looks_garbled(t)]
        if not blank:
            return text, False, page_texts, page_rows, [1.0] * len(doc)

        ocred = ocr_extractor.ocr_pages(doc, blank, on_progress=on_progress)
        page_conf = [1.0] * len(doc)
        for i, (t, r, conf) in ocred.items():
            page_texts[i], page_rows[i], page_conf[i] = t, r, conf
        text = _normalize_text("\n".join(page_texts))
        return text, True, page_texts, page_rows, page_conf

    combined, page_texts, page_rows, page_conf = ocr_extractor.ocr_document(
        doc, on_progress=on_progress)
    if len(combined.strip()) < ocr_extractor.SCAN_TEXT_THRESHOLD:
        raise ValueError(
            "This PDF appears to be scanned and OCR produced no readable text. "
            "Please upload a clearer scan or the digital ITR PDF."
        )
    return combined, True, page_texts, page_rows, page_conf


def extract_text(pdf_source) -> str:
    """Public helper - returns document text (OCR'd if the PDF is scanned)."""
    doc = _open_doc(pdf_source)
    try:
        return _extract(doc)[0]
    finally:
        doc.close()


def _extract_source(source, on_progress=None) -> tuple:
    """Open, read and close one document. Injected into columns.py so it stays
    independent of how documents are read."""
    doc = _open_doc(source)
    try:
        return _extract(doc, on_progress=on_progress)
    finally:
        doc.close()


# ─────────────────────────────────────────────────────────────────
# GEMINI MODEL CASCADE
# ─────────────────────────────────────────────────────────────────
# Shared by columns.py (leftover label classification) and vision_read()
# below (re-reading a statement page's image). Neither owns an LLM client of
# its own - both call this.

_LLM_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]


def _content_to_text(content) -> str:
    """
    Normalise a LangChain response .content to plain text. Newer Gemini models
    return a list of parts (e.g. {'type': 'text', 'text': ...}) instead of a
    string; concatenate the text parts.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            parts.append(p.get("text", "") if isinstance(p, dict) else str(p))
        return "".join(parts)
    return str(content)


def _is_transient(err: Exception) -> bool:
    """Rate-limit / overload / timeout errors - worth a short backoff+retry
    rather than either failing the call outright or burning a model swap."""
    s = str(err)
    return any(tok in s for tok in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE",
                                    "DeadlineExceeded", "Timeout"))


def _llm_invoke(api_key: str, prompt) -> str:
    """
    Invoke the Gemini model cascade. `prompt` may be a plain string or a
    multimodal content list (text + image_url parts) for Vision extraction.

    Transient errors (rate-limit/overload) get a short backoff-retry on the
    SAME model before moving on; a 404 skips straight to the next model.
    """
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langchain_core.messages import HumanMessage
    except ImportError:
        raise RuntimeError("langchain_google_genai not installed")

    for model in _LLM_MODELS:
        # temperature=0, not 0.1. Reading figures off a page is transcription,
        # not generation - there is no upside to sampling. Two runs over the
        # same balance sheet returned readings of differing completeness at
        # 0.1, which changed the spread between runs even though both
        # readings balanced.
        llm = ChatGoogleGenerativeAI(
            model=model, google_api_key=api_key,
            temperature=0, max_tokens=8192,
        )
        last_err = None
        for delay in (0, 1.5, 3):
            if delay:
                time.sleep(delay)
            try:
                return _content_to_text(llm.invoke([HumanMessage(content=prompt)]).content)
            except Exception as e:
                last_err = e
                if "404" in str(e) or "NOT_FOUND" in str(e):
                    break  # model doesn't exist - no point retrying, try next one
                if not _is_transient(e):
                    raise
        if last_err and ("404" in str(last_err) or "NOT_FOUND" in str(last_err)):
            continue
    raise RuntimeError(f"No Gemini model responded. Tried: {_LLM_MODELS}")


def vision_read(source, page_index: int, api_key: str) -> dict:
    """Re-read one statement page from its image via the Gemini cascade."""
    doc = _open_doc(source)
    try:
        return ocr_extractor.vision_read_page(doc, page_index, api_key, _llm_invoke)
    finally:
        doc.close()


# ─────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────

def spread(sources, api_key: str = None, on_progress=None,
           use_vision: bool = False) -> list:
    """Several ITR bundles -> the spreading sheet's year columns, oldest first."""
    from .columns import spread_many
    return spread_many(sources, _extract_source, api_key=api_key,
                       invoke_fn=_llm_invoke, on_progress=on_progress,
                       vision_fn=(vision_read if use_vision else None))


# ─────────────────────────────────────────────────────────────────
# DEBUG HARNESS   (python parser.py <pdf> [<pdf> ...])
# ─────────────────────────────────────────────────────────────────

def debug(pdf_paths: list) -> None:
    from .mapping import taxonomy as T

    columns = spread(pdf_paths, api_key=None, use_vision=False)
    for col in columns:
        print("=" * 70)
        print(f"file        : {col.get('source_name')}")
        print(f"entity      : {col.get('entity')}")
        print(f"year        : {col.get('year')}")
        print(f"scanned     : {col.get('scanned')}")
        print(f"pages read  : {col.get('pages_used')} / {col.get('pages_total')}")
        for b in col.get("blocks", []):
            print(f"  [{b['status']:<10}] {b['kind']:<12} page {b['page'] + 1:<3} "
                  f"({b.get('entity') or 'unnamed'})")
        for w in col.get("warnings", []):
            print(f"  ! {w}")
        vals = col.get("values") or {}
        for key in ("sales_other_income", "profit_after_tax", "total_assets", "networth"):
            v = vals.get(key)
            print(f"  {T.LABELS.get(key, key):<32} "
                  f"{'Check ITR' if v is None else f'{v:,.0f}'}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m engine.parser <pdf_path> [<pdf_path> ...]")
        sys.exit(1)
    debug(sys.argv[1:])
