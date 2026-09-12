r"""
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


def _x_overlap(a0, a1, b0, b1) -> bool:
    """Do two boxes share more than 30% of the narrower one's width? A
    touching edge or a sliver of kerning overlap is not a stacked line."""
    inter = min(a1, b1) - max(a0, b0)
    return inter > 0.3 * max(min(a1 - a0, b1 - b0), 1e-6)


# Money-shaped text: grouped digits or a paise part. Local to this module
# (financials.py imports layout, not the other way round).
_ROW_MONEY_RE = re.compile(r"\d[\d,]*[.,]\d{2}\b|\d{1,3}(?:,\d{2,3})+")


def _rejoin_wrapped(rows: list, tol: float) -> list:
    """
    Undo the stacked-box split for a WRAPPED CAPTION, keep it for two data
    lines.

    The stacking rule in rows_with_cells puts two horizontally-overlapping
    boxes on separate rows. That is right when both printed lines carry a
    figure ("45,77,061.00" over "96,250.00", Borrower B) - and wrong when
    one of them is only the second line of a long label ("(b) Total
    outstanding dues of creditors other than micro" / "and small
    enterprises"), which belongs with the figure on the other line. Applied
    without this pass, a Schedule III Balance Sheet that verified before
    (Borrower M FY2025) lost its caption/amount pairing and failed -
    verified by re-running with and without the rule.

    So adjacent rows within tolerance whose boxes overlap horizontally are
    merged back when AT MOST ONE of them carries money - exactly the old
    behaviour for a caption, while two money lines stay apart.
    """
    def _has_money(row):
        return any(_ROW_MONEY_RE.search(t) for _a, _b, t in row[2])

    out = []
    for row in rows:
        prev = out[-1] if out else None
        if (prev is not None and abs(row[0] - prev[0]) <= tol
                and not (_has_money(prev) and _has_money(row))
                and any(_x_overlap(a, b, c, d)
                        for a, b, _t in row[2] for c, d, _u in prev[2])):
            prev[2].extend(row[2])
            prev[1] += row[1]
            continue
        out.append(row)
    return out


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
    #
    # A word joins the NEAREST row within tolerance that has room for it -
    # never a row already holding a word at the same horizontal position. Two
    # boxes that overlap horizontally cannot sit on one printed line; they
    # are two lines, stacked. Tightly-spaced scans put consecutive lines
    # closer together than the tolerance, and first-fit merged them: RapidOCR
    # returned "45,77,061.00" and "96,250.00" as SEPARATE boxes (verified),
    # but they joined one row and then - same x - one cell,
    # "45,77,061.00 96,250.00", losing a line from each side of the account.
    # That is the root cause of docs/SHORTCOMINGS.md Cases 3, 6 and 21, not
    # the OCR engine. Digital and Tesseract words never overlap on a real
    # line, so this changes nothing for them.
    rows = []
    for x0, y0, x1, _y1, txt in words:
        best, best_d = None, None
        for row in rows:
            d = abs(y0 - row[0])
            if d > tol or (best_d is not None and d >= best_d):
                continue
            if any(_x_overlap(x0, x1, a, b) for a, b, _t in row[2]):
                continue
            best, best_d = row, d
        if best is not None:
            best[0] = (best[0] * best[1] + y0) / (best[1] + 1)
            best[1] += 1
            best[2].append((x0, x1, str(txt).strip()))
        else:
            rows.append([y0, 1, [(x0, x1, str(txt).strip())]])

    rows.sort(key=lambda r: r[0])
    rows = _rejoin_wrapped(rows, tol)

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
        words.append((x, y, x + data["width"][i], y + data["height"][i],
                      _fix_ocr_typos(txt)))
    return words


# RapidOCR misreads "Profit" as "Proflt" often enough to matter (i/l are
# near-identical in many scanned fonts) - confirmed on a real filing, where it
# silently dropped a page's whole P&L (no downstream regex expects the
# misspelling, so the page never matched as a statement at all). Corrected
# once, here, at the point RapidOCR's text enters the pipeline, rather than
# teaching every regex that ever checks for "profit" - across relevance.py,
# financials.py and taxonomy.py - to also tolerate the typo.
_OCR_TYPO_RE = re.compile(r"\bproflt\b", re.I)

# An Indian-grouped amount whose separators OCR has read inconsistently -
# "2.35.51,356.00" for "2,35,51,356.00" (Borrower B's printed Balance Sheet
# total, a real scan). The amount grammar downstream only accepts commas as
# group separators, so it matched just the tail "51,356.00" and the
# statement's own total read as Rs 51,356 against Rs 2.36 crore.
# Needs at least one 2-digit group before a 3-digit group, so a date
# ("31.03.2024": its last group has 4 digits) and an ordinary decimal
# ("12.345") never match.
_MIXED_SEP_RE = re.compile(
    r"(?<![\d.,])\d{1,3}(?:[.,]\d{2})+[.,]\d{3}(?:[.,]\d{2})?(?![\d.,])")


def _fix_separators(m) -> str:
    groups = re.split(r"[.,]", m.group())
    if len(groups[-1]) == 2 and len(groups[-2]) == 3:
        return ",".join(groups[:-1]) + "." + groups[-1]
    return ",".join(groups)


# A decimal point read as a comma: "5,106,21" for "5,106.21" (Borrower Q
# Mobility's FY2026 "Total current assets", in Rs lakhs). A final group of
# TWO digits straight after a three-digit group is never valid grouping in
# either the Indian or the international system (both end on three digits),
# so that last comma can only be the decimal point. Left alone, the total
# read as 5,10,621 lakh - 100x its items - and the Balance Sheet failed.
_DECIMAL_AS_COMMA_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})*,\d{3}),(\d{2})(?![\d.,])")


def _fix_ocr_typos(txt: str) -> str:
    txt = _MIXED_SEP_RE.sub(_fix_separators, _OCR_TYPO_RE.sub("Profit", txt))
    return _DECIMAL_AS_COMMA_RE.sub(r"\1.\2", txt)


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
