"""
layout.py  -  word boxes → reading-ordered rows with column boundaries.

Financial statements attached to an ITR are T-accounts: Liabilities on the left,
Assets on the right (or "To" / "By" for a P&L), sharing a visual row. Plain text
extraction destroys that - PyMuPDF's page.get_text() emits

    Salary Paid
    72000.00 Gross Receipt
    985600.00

from a page that visually reads "Salary Paid 72,000 | Gross Receipt 9,85,600",
welding one side's amount onto the other side's label. Tesseract's default
image_to_string does the same. Either way the harvester would attribute figures
to the wrong side of the account, and the balance check that proves the whole
extraction would be meaningless.

So both the scanned path (Tesseract word boxes) and the digital path (PyMuPDF
word boxes) feed the same reconstruction here: cluster words into visual rows by
y-centre, order each row left to right, and insert a TAB wherever a horizontal
gap is wide enough to be a column boundary rather than a word space.

TAB is deliberate: `\s+` in the ordinary label regexes still matches it, so the
line-item engine is unaffected, while the T-account harvester can split on it.
"""

import re
import statistics

# A gap this fraction of the page width or wider is a column boundary, not a
# word space. Financial statements put a lot of air between the label and its
# amount, and much more between the two sides of the account.
_COL_GAP_FRAC = 0.035

# Row band tolerance as a fraction of the median word height.
_ROW_TOL_FRAC = 0.6

COL_SEP = "\t"


def rows_with_cells(words, page_width: float) -> list:
    """
    `words` is an iterable of (x0, y0, x1, y1, text). Returns
    [(row_y, [(x0, x1, cell_text), ...]), ...] ordered top to bottom, cells left
    to right, where a cell is a run of words with no column-width gap inside it.

    Cell x-positions are what the T-account harvester needs: which side of the
    account a figure belongs to is a question about where it sits on the page,
    not about how many cells happen to precede it on its row (rows carry
    different cell counts depending on which entries are blank).
    """
    words = [w for w in words if str(w[4]).strip()]
    if not words:
        return []

    heights = [w[3] - w[1] for w in words if w[3] > w[1]]
    med_h   = statistics.median(heights) if heights else 12
    tol     = (med_h or 12) * _ROW_TOL_FRAC
    gap_min = page_width * _COL_GAP_FRAC

    words.sort(key=lambda w: (w[1], w[0]))

    # rows: [running_mean_y, count, [(x0, x1, text), ...]]
    rows = []
    for x0, y0, x1, _y1, txt in words:
        for row in rows:
            if abs(y0 - row[0]) <= tol:
                row[0] = (row[0] * row[1] + y0) / (row[1] + 1)
                row[1] += 1
                row[2].append((x0, x1, str(txt).strip()))
                break
        else:
            rows.append([y0, 1, [(x0, x1, str(txt).strip())]])

    rows.sort(key=lambda r: r[0])

    out = []
    for mean_y, _n, items in rows:
        items.sort(key=lambda it: it[0])
        cells, cur_x0, cur_x1, parts, prev_end = [], None, None, [], None
        for x0, x1, txt in items:
            if prev_end is not None and (x0 - prev_end) >= gap_min:
                cells.append((cur_x0, cur_x1, " ".join(parts)))
                parts, cur_x0 = [], None
            if cur_x0 is None:
                cur_x0 = x0
            cur_x1 = x1
            parts.append(txt)
            prev_end = x1
        if parts:
            cells.append((cur_x0, cur_x1, " ".join(parts)))
        out.append((mean_y, cells))
    return out


def rows_from_words(words, page_width: float) -> list:
    """
    [(row_y, row_text), ...] with COL_SEP marking column boundaries.

    TAB-joined view of rows_with_cells, for the label-based line-item engine,
    which works on plain text and only needs `\\s+` to tolerate the joins.
    """
    return [(y, COL_SEP.join(t for _a, _b, t in cells))
            for y, cells in rows_with_cells(words, page_width)]


def split_columns(row_text: str) -> list:
    """Row text → its column cells, empty cells dropped."""
    return [c.strip() for c in row_text.split(COL_SEP) if c.strip()]


def words_from_page(page) -> list:
    """PyMuPDF page → (x0, y0, x1, y1, text) tuples. Digital-PDF path."""
    return [(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")]


def words_from_tesseract(data: dict, min_conf: float = 30) -> list:
    """pytesseract image_to_data DICT → (x0, y0, x1, y1, text) tuples."""
    words = []
    for i in range(len(data["text"])):
        txt = data["text"][i].strip()
        if not txt:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < min_conf:
            continue
        x, y = data["left"][i], data["top"][i]
        words.append((x, y, x + data["width"][i], y + data["height"][i], txt))
    return words


# RapidOCR misreads "Profit" as "Proflt" often enough to matter (i/l are
# near-identical in many scanned fonts) - confirmed on a real filing, where it
# silently dropped a page's whole P&L (no downstream regex expects the
# misspelling, so the page never matched as a statement at all). Corrected
# once, here, at the point RapidOCR's text enters the pipeline, rather than
# teaching every regex that ever checks for "profit" - across relevance.py,
# financials.py and taxonomy.py - to also tolerate the typo.
_OCR_TYPO_RE = re.compile(r"\bproflt\b", re.I)


def _fix_ocr_typos(txt: str) -> str:
    return _OCR_TYPO_RE.sub("Profit", txt)


def words_from_rapidocr(result, min_conf: float = 0.5) -> list:
    """
    RapidOCR result → (x0, y0, x1, y1, text) tuples.

    RapidOCR detects text at phrase/line granularity (its detector segments on
    the same visual gaps a reader would), not word-by-word like Tesseract -
    but that is exactly the granularity rows_with_cells needs: a label and its
    amount already come back as separate boxes because a real statement prints
    real whitespace between them, so treating each detected box as one "word"
    for row/column reconstruction works unchanged.
    """
    if result is None or result.boxes is None or len(result.boxes) == 0:
        return []
    words = []
    for box, txt, score in zip(result.boxes, result.txts, result.scores):
        txt = _fix_ocr_typos(str(txt).strip())
        if not txt or score < min_conf:
            continue
        xs, ys = box[:, 0], box[:, 1]
        words.append((float(xs.min()), float(ys.min()),
                      float(xs.max()), float(ys.max()), txt))
    return words
