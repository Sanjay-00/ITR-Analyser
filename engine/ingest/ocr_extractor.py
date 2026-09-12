"""
ocr_extractor.py  -  scanned-PDF front-end for the ITR Extractor.

A photocopied / scanned ITR carries no extractable text. This module:
  1. OCRs every page to text with Tesseract (ocr_document) so the normal text
     parsers can run on the result, and
  2. provides a Gemini Vision fallback (vision_extract) used only when the
     OCR-fed rule-based parse fails validation.

Tesseract is the primary (free, local) path; Gemini Vision is the accuracy
safety net. See parser.parse() for the orchestration.

Ported from the AutoCAM CIBIL project's ocr_extractor.py - the geometry-aware
row reconstruction below matters even more for an ITR than for a bureau report,
since ITR forms are almost entirely "label ......... value" two-column rows.
"""

import os
import re
import json

import fitz  # PyMuPDF

from . import layout

# ── Tesseract binary discovery ────────────────────────────────────
# Streamlit Cloud installs it on PATH via packages.txt; on Windows it lands in
# Program Files. Allow an env override (TESSERACT_CMD) for custom installs.
_WIN_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


def _configure_tesseract():
    import pytesseract
    # We parallelise OCR across pages (one Tesseract call per worker thread), so pin
    # each Tesseract call to a single OpenMP thread  -  otherwise N workers × M internal
    # threads oversubscribe the CPU and run slower.
    os.environ["OMP_THREAD_LIMIT"] = "1"
    override = os.getenv("TESSERACT_CMD")
    if override and os.path.exists(override):
        pytesseract.pytesseract.tesseract_cmd = override
        return pytesseract
    for cand in _WIN_CANDIDATES:
        if os.path.exists(cand):
            pytesseract.pytesseract.tesseract_cmd = cand
            return pytesseract
    return pytesseract  # assume it's on PATH (Linux / Streamlit Cloud)


def tesseract_version() -> str:
    """
    The exact Tesseract build actually running, e.g. '5.4.0.20240606'. Local
    installs and Streamlit Cloud's unpinned `tesseract-ocr` apt package can
    resolve to different versions even from identical code - surfacing this
    lets a "works locally, not on Cloud" report be diagnosed directly instead
    of guessed at.
    """
    try:
        pytesseract = _configure_tesseract()
        return str(pytesseract.get_tesseract_version())
    except Exception:
        return "unavailable"


# Render scale for OCR (higher = sharper text, slower). 3x ≈ 216 DPI on A4.
_OCR_MATRIX    = fitz.Matrix(3, 3)
# Vision images can be smaller; 2x keeps tokens down while staying legible.
_VISION_MATRIX = fitz.Matrix(2, 2)

# Total stripped chars below this ⇒ treat the document as scanned. Imported by
# parser._extract() so the two never drift into disagreeing thresholds.
SCAN_TEXT_THRESHOLD = 100


# ─────────────────────────────────────────────────────────────────
# GEOMETRY-AWARE OCR
# ─────────────────────────────────────────────────────────────────
# An ITR page is a dense two-column form: a label and its value share a visual
# row but sit far apart horizontally (often separated by dot leaders). Tesseract's
# default image_to_string reads column-by-column, which ORPHANS values from their
# labels - "Gross Total Income" lands nowhere near "12,34,382", and the parser
# then reads 0 / None for the field.
#
# Instead we pull word boxes (image_to_data) and rebuild reading order in
# layout.rows_from_words - shared with the digital-PDF path so both produce the
# same tab-delimited column structure the T-account harvester needs.

_MIN_CONF = 30    # drop very-low-confidence words (mostly noise glyphs)


# Force the LSTM engine explicitly (--oem 1) rather than relying on each
# Tesseract build's own default (--oem 3, "whatever's available"). Different
# distro packages (e.g. Streamlit Cloud's unpinned apt tesseract-ocr vs a
# local Windows install) can ship with different legacy-engine data bundled,
# so leaving OEM implicit is one more axis two environments can silently
# disagree on - being explicit closes at least that one.
_TESS_CONFIG = "--oem 1"


def _mean_conf(values: list, scale: float = 1.0) -> float:
    """Mean of a list of confidence values, each divided by `scale` (Tesseract
    reports 0-100, RapidOCR 0-1) - or 1.0 for an empty list, since "no words
    scored" is not evidence of a bad read, just an OCR engine that (like
    Tesseract's plain-text fallback) never produced per-word confidence."""
    return (sum(values) / len(values) / scale) if values else 1.0


def _ocr_image(pytesseract, img) -> tuple:
    """
    Geometry-aware OCR of one rendered page image, with a plain-text fallback.
    Returns (page_text, cell_rows, confidence) - cell_rows carries the
    x-positions the T-account harvester needs, and is empty when the
    fallback path was used; confidence is this page's mean word confidence
    (0-1), or 1.0 when the engine reported none.
    """
    from pytesseract import Output
    try:
        data = pytesseract.image_to_data(img, output_type=Output.DICT, config=_TESS_CONFIG)
        confs = [float(c) for c in data["conf"] if _is_number(c) and float(c) >= 0]
        rows = layout.rows_with_cells(
            layout.words_from_tesseract(data, _MIN_CONF), img.size[0])
        if rows:
            text = "\n".join(layout.COL_SEP.join(t for _a, _b, t in cells)
                             for _y, cells in rows)
            return text, rows, _mean_conf(confs, scale=100.0)
    except Exception:
        pass
    return pytesseract.image_to_string(img, config=_TESS_CONFIG), [], 1.0


def _is_number(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


# ─────────────────────────────────────────────────────────────────
# RAPIDOCR  -  default scanned-page engine
# ─────────────────────────────────────────────────────────────────
# Chosen over Tesseract after a real side-by-side test on this project's own
# problem documents (not benchmark claims): on a badly scanned trading
# account, RapidOCR read "16,37,66,493.00" correctly where Tesseract dropped
# a digit and produced a figure ~75x too small. It also runs in a few seconds
# on ONNXRuntime/CPU with a ~77MB model footprint - no GPU, no PyTorch, no
# meaningful RAM pressure, which matters on the modest hardware this runs on
# (Docling and PaddleOCR were tried and ruled out for exactly that reason:
# minutes per page and >1GB working sets against a machine with ~3GB free).
#
# Tesseract is kept, not deleted: OCR_ENGINE=tesseract reverts to it, and it
# stays the automatic fallback if rapidocr/onnxruntime aren't installed.
_RAPIDOCR_MIN_CONF = 0.5
_rapidocr_engine = None


def _get_rapidocr():
    """Lazily construct and cache the RapidOCR engine (loads ONNX models once
    per process, not once per page - that cost would otherwise dominate).

    Left at ONNXRuntime's own default thread settings (-1, use every core)
    deliberately - measured, not assumed. Pinning to 1 thread per call (the
    same OMP_THREAD_LIMIT=1 reasoning that helps Tesseract, on the theory
    that OCR_WORKERS concurrent calls oversubscribing every core would be
    worse than a few single-threaded calls) was tried and MEASURED SLOWER on
    this hardware: one page went from 1.94s unpinned to 3.8s pinned, almost
    2x worse, because a single call finishing faster by using every core
    available beats a handful of concurrent calls each crawling on one.
    Left here as a documented dead end so it isn't quietly re-tried later
    without the same measurement.
    """
    global _rapidocr_engine
    if _rapidocr_engine is None:
        from rapidocr import RapidOCR
        _rapidocr_engine = RapidOCR()
    return _rapidocr_engine


def _ocr_image_rapidocr(engine, img) -> tuple:
    """Geometry-aware OCR of one rendered page image via RapidOCR. Same
    (page_text, cell_rows, confidence) shape as _ocr_image, so callers don't
    care which engine produced it."""
    import numpy as np
    arr = np.asarray(img)
    result = engine(arr)
    conf = _mean_conf(list(result.scores) if result and result.scores is not None else [])
    rows = layout.rows_with_cells(
        layout.words_from_rapidocr(result, _RAPIDOCR_MIN_CONF), img.size[0])
    if not rows:
        return "", [], conf
    text = "\n".join(layout.COL_SEP.join(t for _a, _b, t in cells)
                     for _y, cells in rows)
    return text, rows, conf


def _active_engine():
    """
    ('rapidocr', engine) or ('tesseract', pytesseract), chosen once per
    document. RapidOCR is the default; OCR_ENGINE=tesseract or a missing
    rapidocr/onnxruntime install falls back to Tesseract automatically rather
    than failing the whole extraction.
    """
    if os.getenv("OCR_ENGINE", "").strip().lower() == "tesseract":
        return "tesseract", _configure_tesseract()
    try:
        return "rapidocr", _get_rapidocr()
    except Exception:
        return "tesseract", _configure_tesseract()


# Per-page OCR is ~95% of runtime and pages are independent, so we OCR them in
# parallel. Rendering (MuPDF) stays on the main thread  -  MuPDF documents are NOT
# thread-safe  -  but it runs as a pipeline: the main thread renders page N+1 while the
# worker pool OCRs pages already rendered, so the render cost is hidden behind OCR.
# The GIL is released during the tesseract subprocess, so worker threads truly run in
# parallel. Results are reassembled in page order ⇒ output is byte-identical to serial
# OCR, so accuracy is unchanged by construction.
def _env_int(name: str, default: int) -> int:
    """Positive int from env var `name`, else `default` (deploy-time tuning)."""
    try:
        v = int(os.getenv(name, ""))
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


# Auto-scale to the host's cores, but allow per-deploy overrides:
#   OCR_WORKERS        -  parallel Tesseract workers (lower on a small box to save RAM)
#   OCR_MAX_INFLIGHT   -  rendered-but-not-yet-OCR'd pages held in memory at once
# e.g. on Streamlit Cloud (~1 GB / ~2 cores) set OCR_WORKERS=2, OCR_MAX_INFLIGHT=4.
_OCR_WORKERS      = _env_int("OCR_WORKERS", max(1, min(12, (os.cpu_count() or 4))))
_OCR_MAX_INFLIGHT = _env_int("OCR_MAX_INFLIGHT", _OCR_WORKERS + 4)


def _render_pil(page, matrix=None):
    """Render a page straight to a PIL image from the raw pixmap (no PNG round-trip).

    Going via tobytes("png") + Image.open costs a full PNG encode/decode for
    byte-identical pixels - measured ~13x slower in the CIBIL project, and this
    sits on the single rendering thread ahead of every OCR page.
    """
    from PIL import Image
    pix  = page.get_pixmap(matrix=matrix or _OCR_MATRIX)
    mode = {1: "L", 3: "RGB", 4: "RGBA"}.get(pix.n, "RGB")
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


# ─────────────────────────────────────────────────────────────────
# TWO-PASS OCR  -  cheap classify, then sharp re-read of the keepers only
# ─────────────────────────────────────────────────────────────────
# An ITR bundle is mostly not the ITR: a 15-30 page filing usually carries the
# Balance Sheet and P&L on 2-4 of those pages, the rest being computation
# sheets, TDS schedules and audit-report boilerplate (see relevance.py's own
# docstring, which names this exact two-pass design without it ever having
# been wired up). OCR is the slowest thing this tool does, so doing the full
# 3x-scale geometry-aware pass on every page - most of which nothing will ever
# read - is the single biggest avoidable cost on a scanned bundle.
#
# The fix: render every page small and cheap first, run just enough OCR to
# classify it (relevance.classify_page needs only plain text, not geometry),
# then spend the expensive full-quality pass only on the pages relevance.select
# would keep anyway. A page not selected still gets its cheap-pass text folded
# into page_texts (identity extraction and the "nothing recognised, read
# everything" fallback both need SOME text for every page), just not the
# geometry reconstruction nothing downstream will use for it.
_FAST_MATRIX = fitz.Matrix(1.3, 1.3)


def _fast_page_text(engine_name, engine, img) -> str:
    """Cheap, geometry-free OCR of one page - text good enough to classify,
    not to harvest figures from. `engine` is whatever _active_engine()
    returned (the RapidOCR instance, or the configured pytesseract module)."""
    if engine_name == "rapidocr":
        import numpy as np
        result = engine(np.asarray(img))
        text = "\n".join(result.txts) if result and result.txts else ""
        # Same fix as words_from_rapidocr - this path builds text straight
        # from result.txts rather than going through that function, so the
        # "Proflt"->"Profit" correction needs applying here too, or the cheap
        # pass keeps mis-classifying this page and paying for the safety
        # net's full-quality fallback every time instead of just once.
        return layout._fix_ocr_typos(text)
    return engine.image_to_string(img, config=_TESS_CONFIG)


# ─────────────────────────────────────────────────────────────────
# SIDEWAYS SCANS
# ─────────────────────────────────────────────────────────────────
# A scanned page can be rotated in its IMAGE while the PDF says rotation 0 -
# confirmed on a real filing (docs/SHORTCOMINGS.md Case 9): a correctly drawn
# Balance Sheet came back as unreadable fragments and its heading never
# matched, so the whole statement was invisible, not merely unverified.
#
# Detected by trying it: a page whose cheap-pass text holds almost no real
# words is re-OCR'd at each other right angle, and the angle that reads as
# plainly more text wins. Chosen over Tesseract's OSD (image_to_osd), which
# needs a separate traineddata file some installs lack and reports an angle
# whose direction convention differs between versions - scoring the actual
# output has neither problem. Costs three cheap OCRs, only on pages that are
# already failing to read.
#
# Scored by KNOWN words, not by letter runs: measured on real pages,
# Tesseract turns sideways text into letter-shaped junk that is just as
# "wordy" (115 three-letter runs sideways vs 108 upright on the same Balance
# Sheet) - a letter-run count never rotated anything. Known-vocabulary hits
# separate cleanly: 42-128 upright against at most 5 at 90/180 degrees, on a
# digital page, a poor scan and a clean scan alike.
_VOCAB = frozenset(
    "the and of to for as at on by total balance sheet profit loss account "
    "capital assets asset liabilities current fixed loans loan cash bank "
    "sundry creditors debtors income expenses expense sales tax year ended "
    "march share reserves surplus investments depreciation interest salary "
    "rent net gross particulars amount schedule note from other advances "
    "stock payable receivable provision".split())
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_UPRIGHT_MIN_WORDS = 8
_ROTATIONS = (90, 270, 180)


def _word_score(text: str) -> int:
    return sum(1 for w in _WORD_RE.findall(text or "") if w.lower() in _VOCAB)


def _best_rotation(read_fn, img, text: str) -> tuple:
    """
    (angle, text) - the counter-clockwise rotation that makes `img` read as
    text, and that reading. `read_fn(img) -> str` is the cheap OCR. Angle 0
    unless another orientation reads at least twice as many words AND clears
    _UPRIGHT_MIN_WORDS on its own, so a sparse but upright page is never
    turned on its side by noise.
    """
    base = _word_score(text)
    if base >= _UPRIGHT_MIN_WORDS:
        return 0, text
    best = (0, base, text)
    for angle in _ROTATIONS:
        try:
            t = read_fn(img.rotate(angle, expand=True))
        except Exception:
            continue
        s = _word_score(t)
        if s > best[1]:
            best = (angle, s, t)
    angle, score, t = best
    if angle and score >= _UPRIGHT_MIN_WORDS and score >= 3 * max(base, 1):
        return angle, t
    return 0, text


def _rotated(img, angle: int):
    return img.rotate(angle, expand=True) if angle else img


def _fast_upright(pytess, img) -> tuple:
    """Cheap-pass OCR of one page with rotation detection: (angle, text)."""
    def read(im):
        return _fast_page_text("tesseract", pytess, im)
    return _best_rotation(read, img, read(img))


def _render_upright(page, pytess):
    """Full-quality render, turned upright when the cheap pass says the scan
    is sideways. For callers with no cheap pass of their own (ocr_pages)."""
    full = _render_pil(page)
    if pytess is None:
        return full
    try:
        angle, _t = _fast_upright(pytess, _render_pil(page, _FAST_MATRIX))
    except Exception:
        return full
    return _rotated(full, angle)


def _run_pool(indices, render, ocr_one, engine, on_progress=None,
             done_start=0, total=None):
    """
    Render-then-OCR pipeline over exactly `indices`, in a bounded producer
    (main thread, MuPDF-safe) / consumer (worker pool) pipeline. Returns
    {page_index: ocr_one's result}. Shared by both OCR passes below so the
    threading/backpressure logic is written once.
    """
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    total = total if total is not None else len(indices)
    done  = done_start
    out, inflight = {}, {}

    def _drain(finished):
        nonlocal done
        for fut in finished:
            idx = inflight.pop(fut)
            out[idx] = fut.result()
            done += 1
            if on_progress:
                on_progress(done, total)

    with ThreadPoolExecutor(max_workers=_OCR_WORKERS) as pool:
        for i in indices:
            if len(inflight) >= _OCR_MAX_INFLIGHT:        # keep memory bounded
                finished, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                _drain(finished)
            img = render(i)                               # main thread (MuPDF safe)
            inflight[pool.submit(ocr_one, engine, img)] = i
        _drain(list(inflight))                            # drain the rest
    return out


def ocr_pages(doc, indices: list, on_progress=None) -> dict:
    """
    Full-quality OCR of SPECIFIC page indices only - the mixed-document
    counterpart to ocr_document's whole-file two-pass path.

    A "digital" PDF can still carry pages with nothing for PyMuPDF's text
    layer to return: a CA's signed financial statements, photocopied and
    scanned, then inserted as a raster image into an otherwise
    digitally-generated ITR bundle. `parser._extract` finds those pages
    itself (their own digital text comes back empty) and calls this with
    exactly that list - there is no cheap classify pass here, unlike
    ocr_document, because the caller already knows which few pages need it;
    that pre-filter exists only to avoid paying full-quality OCR for every
    page of a WHOLLY scanned document before knowing which ones are worth
    it, which isn't the situation here.

    Returns {page_index: (text, rows, confidence)}.
    """
    if not indices:
        return {}
    engine_name, engine = _active_engine()
    ocr_one = _ocr_image_rapidocr if engine_name == "rapidocr" else _ocr_image
    try:
        pytess = _configure_tesseract()
    except Exception:
        pytess = None
    return _run_pool(indices, render=lambda i: _render_upright(doc[i], pytess),
                     ocr_one=ocr_one, engine=engine, on_progress=on_progress,
                     total=len(indices))


def ocr_document(doc, on_progress=None) -> tuple:
    """
    OCR the document in two passes. Returns (combined_text, page_texts,
    page_rows, page_confidence) where page_texts[i] is page i's OCR text,
    page_rows[i] its cells with x-positions (geometry-reconstructed, or empty
    for a page the second pass skipped), and page_confidence[i] that page's
    mean word confidence (0-1) - a page that read cleanly enough to balance
    can still have been a low-confidence guess throughout, which is exactly
    the case a Vision re-read catches that a pure arithmetic check cannot: a
    coincidentally-balancing wrong read.

    Pass 1 renders every page small and OCRs it with TESSERACT, not the
    active engine - measured at ~0.5s/page against RapidOCR's ~2-4s/page on
    this hardware, a real 3-6x gap RapidOCR's own resolution/threading
    settings could not close (both were tried and measured NOT to help; see
    _get_rapidocr's docstring). Classifying a page only needs to spot a
    keyword, not read a digit correctly, so Tesseract's lower raw accuracy
    costs nothing here - relevance.classify_page is deliberately biased
    toward keeping a page anyway. Pass 2 then spends RapidOCR's real accuracy
    only on the pages relevance.select() would keep - most of a real ITR
    bundle is computation sheets and audit boilerplate the statement
    harvester will never look at, and this is where that's actually worth
    paying for. A page dropped by pass 1 keeps its cheap-pass text (identity
    extraction, and the "nothing recognised - read everything" fallback, both
    need SOME text for every page) but empty geometry, which costs nothing
    downstream since find_blocks is only ever asked to look at kept pages.

    on_progress(current, total) fires as pages complete, across both passes.
    """
    from . import relevance

    engine_name, engine = _active_engine()
    ocr_one    = _ocr_image_rapidocr if engine_name == "rapidocr" else _ocr_image
    fast_pytess = _configure_tesseract()
    total      = len(doc)
    page_texts = [""] * total
    page_rows  = [[] for _ in range(total)]
    page_conf  = [1.0] * total

    # Pass 1: cheap classify, always Tesseract regardless of engine_name.
    # Two passes' worth of progress ticks are folded into one counter so
    # on_progress still ends at (total, total) instead of jumping back to a
    # smaller number when pass 2 starts.
    fast_total = total * 2
    fast = _run_pool(
        range(total),
        render=lambda i: _render_pil(doc[i], _FAST_MATRIX),
        ocr_one=_fast_upright,
        engine=fast_pytess, on_progress=on_progress, total=fast_total)
    # Rotation found by the cheap pass (see _best_rotation) is applied to
    # every later full-quality render of the same page.
    rot = {}
    for i in range(total):
        rot[i], page_texts[i] = fast.get(i, (0, ""))

    keep = set(relevance.select(page_texts))

    def _render_full(i):
        return _rotated(_render_pil(doc[i]), rot.get(i, 0))

    # Pass 2: full geometry-aware OCR, only on the pages worth harvesting.
    full = _run_pool(
        sorted(keep), render=_render_full, ocr_one=ocr_one,
        engine=engine, on_progress=on_progress, done_start=total,
        total=fast_total)
    for i, (text, rows, conf) in full.items():
        page_texts[i], page_rows[i], page_conf[i] = text, rows, conf

    # Safety net: a page skipped by pass 1 has NO geometry at all, not just
    # unfiltered geometry - unlike the old single-pass design, there is
    # nothing for columns.py's own "nothing found, retry with every page"
    # fallback to retry WITH if the cheap classifier's guess was wrong. So
    # that fallback is reproduced here, one level lower: if the pages read so
    # far don't yet cover BOTH a balance-sheet-shaped page and a P&L-shaped
    # page, the cheap pass may have mis-guessed on one of them (a badly
    # degraded scan can read differently at each render resolution) rather
    # than the bundle genuinely carrying only one. Confirmed on a real
    # filing: the fast pass read "Balance Sheet as at 31st March 2024"
    # cleanly enough to keep page 6, but not "Profit and Loss Account for the
    # year ended" on page 7, which dropped the whole P&L from the column
    # silently - both read perfectly once given the full-quality pass.
    #
    # Counting STATEMENT pages (the old check: "< 2 found") is NOT the same
    # test and is too easy to satisfy by accident - confirmed on a second
    # real filing, a poor phone-scan where a Chartered Accountant's
    # letterhead page and a "Reserves and Surplus" Notes page each
    # independently classified STATEMENT, clearing the old ">= 2" bar while
    # the real Balance Sheet and P&L sat unread elsewhere in the same
    # bundle. `has_both_statement_kinds` checks for one of EACH kind, not
    # just a headcount, which is what actually predicts whether find_blocks
    # downstream has anything to work with.
    if not relevance.has_both_statement_kinds(page_texts):
        rest = [i for i in range(total) if i not in keep]
        if rest:
            widened = _run_pool(rest, render=_render_full,
                                ocr_one=ocr_one, engine=engine,
                                on_progress=on_progress, done_start=total,
                                total=total + len(rest))
            for i, (text, rows, conf) in widened.items():
                page_texts[i], page_rows[i], page_conf[i] = text, rows, conf

    combined = "\n".join(page_texts)
    combined = combined.replace("\xa0", " ").replace("\u2013", "-").replace("\u2014", "-")
    return combined, page_texts, page_rows, page_conf


# ─────────────────────────────────────────────────────────────────
# GEMINI VISION FALLBACK
# ─────────────────────────────────────────────────────────────────

import base64
import re as _re


def _img_data_uri(page) -> str:
    pix = page.get_pixmap(matrix=_VISION_MATRIX)
    b64 = base64.b64encode(pix.tobytes("png")).decode()
    return f"data:image/png;base64,{b64}"


def _strip_json(text: str) -> str:
    text = _re.sub(r'^```(?:json)?\s*', '', text.strip())
    return _re.sub(r'\s*```$', '', text).strip()


STATEMENT_PROMPT = """\
This page image is a financial statement from an Indian filing - a Balance \
Sheet, a Profit & Loss / Income & Expenditure account, or a Capital account.

Read EVERY line item and return ONLY a JSON object:

{
  "kind": "balance_sheet" | "profit_loss" | "capital_account",
  "entity": "<the business name printed above the heading, or \\"\\">",
  "year": <the 4-digit year the period ENDS in, or null>,
  "printed_total": <the total the statement itself prints, or null>,
  "left":  [["label", amount, "group heading"], ...],
  "right": [["label", amount, "group heading"], ...]
}

Rules - follow these exactly:
- The THIRD element of every row is the group heading that line sits under, \
copied verbatim ("Non-current liabilities", "Current liabilities", \
"Current assets", "Shareholders' funds"). Use "" only if the line sits under \
no heading. This matters: a balance sheet lists "Borrowings" under BOTH \
Non-current and Current liabilities, and without the heading the two are \
indistinguishable and get added together.
- Amounts as plain integers in RUPEES: no commas, no decimals, no symbols. \
"1,35,24,153.00" is 13524153, NOT 1352415300 - drop the paise, do not append them.
- A figure printed in brackets is NEGATIVE: "(15,32,419)" is -1532419.
- For a two-sided account, "left" is Liabilities / To / Debit and "right" is \
Assets / By / Credit. Put every entry on the side it is actually printed on.
- For a single-column statement (labels down the left, one column per year), \
put ALL items in "left" and read ONLY the CURRENT period's column - the \
right-hand column is last year's comparative. Ignore any "Note No." column.
- Where a line shows workings ("Furniture 65,980 less depreciation 6,598 = \
59,382"), report ONLY the final figure that enters the total: 59382.
- Do NOT include the statement's own totals or subtotals as line items. Put \
the closing total in "printed_total" instead.
- Do not invent, merge or omit lines. The items you return must add up to the \
total the statement prints.
"""


def vision_read_page(doc, page_index: int, api_key: str, invoke_fn) -> dict:
    """
    Re-read one statement page from its image. Used only where OCR produced a
    block that failed its arithmetic check - the text is already corrupt at
    that point, so no amount of reparsing recovers it, but the pixels are
    intact.

    The caller re-runs the same balance check on the result and keeps it only
    if it reconciles, which is what makes it safe to let a model read figures
    at all. Returns {} on any failure.
    """
    content = [
        {"type": "text", "text": STATEMENT_PROMPT},
        {"type": "image_url", "image_url": _img_data_uri(doc[page_index])},
    ]
    try:
        parsed = json.loads(_strip_json(invoke_fn(api_key, content)))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def vision_extract(doc, page_indices: list, prompt: str, api_key: str,
                   invoke_fn, chunk_size: int = 8) -> dict:
    """
    Send selected page images to the Gemini model cascade and parse the JSON
    object it returns. `invoke_fn(api_key, message_content)` is injected by
    parser.py so this reuses its model cascade (_llm_invoke) rather than owning
    an LLM client of its own.

    Chunked because an ITR with all its schedules can run past the model's
    input limits in a single call; per-chunk results are shallow-merged in page
    order, so a later chunk only fills keys an earlier one left absent (an ITR's
    headline figures appear once, so genuine conflicts are not expected - and
    when they happen, the earliest page wins, which is where Part B-TI/TTI sit).

    Chunks are independent Gemini calls, so all page images are rendered on the
    main thread first (PyMuPDF isn't thread-safe), then the calls run in
    parallel. Returns {} on total failure; individual chunk failures are skipped.
    """
    if not page_indices:
        return {}

    chunks = [page_indices[start:start + chunk_size]
              for start in range(0, len(page_indices), chunk_size)]

    chunk_contents = []
    for chunk in chunks:
        content = [{"type": "text", "text": prompt}]
        for idx in chunk:
            content.append({"type": "image_url", "image_url": _img_data_uri(doc[idx])})
        chunk_contents.append(content)

    def _call_one(content):
        parsed = json.loads(_strip_json(invoke_fn(api_key, content)))
        return parsed if isinstance(parsed, dict) else None

    if len(chunk_contents) == 1:
        try:
            return _call_one(chunk_contents[0]) or {}
        except Exception:
            return {}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = [None] * len(chunk_contents)

    def _call(i):
        return i, _call_one(chunk_contents[i])

    with ThreadPoolExecutor(max_workers=min(len(chunk_contents), 8)) as pool:
        futures = [pool.submit(_call, i) for i in range(len(chunk_contents))]
        for fut in as_completed(futures):
            try:
                i, obj = fut.result()
                results[i] = obj
            except Exception:
                continue

    merged = {}
    for obj in results:                  # page order; earlier chunks win
        if not obj:
            continue
        for k, v in obj.items():
            if merged.get(k) is None:
                merged[k] = v
    return merged
