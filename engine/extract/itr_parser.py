"""
itr_parser.py  -  format-independent primitives shared across the pipeline.

Two things live here because every document type needs them regardless of
what kind of statement it turns out to carry:

  - to_int()          Indian-format number parsing, used everywhere a rupee
                       figure is read off a page (financials.py).
  - extract_identity() PAN / Assessment Year / name / form type / regime -
                       these patterns are the same whether the document turns
                       out to be an ITR-V, a full return, or a CA computation
                       sheet, so they run before anything else does.
"""

import re

IDENTITY_FIELDS = [
    "ay", "pan", "name", "form", "ack_no", "filing_date", "assessee_status", "regime",
]


def to_int(s):
    """
    Indian-format number → int. Handles '12,34,567', '12,34,567.00', '₹ 1,000',
    '(1,234)' negatives, and stray OCR spaces inside the digits.
    Returns None when there is no number to read - callers must distinguish that
    from a genuine 0.
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    txt = str(s).strip()
    if not txt:
        return None
    neg = txt.startswith("(") and txt.endswith(")")
    # Match the number out of the string rather than deleting non-digits: a
    # currency prefix like "Rs." leaves a leading '.' behind under deletion,
    # turning "Rs. 1,000" into ".1000" → 0.1 → 0. A wrong zero is worse than a
    # None here, since 0 reads as a confident value everywhere downstream.
    compact = txt.replace(" ", "").replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", compact)
    if not m:
        return None
    try:
        val = int(round(float(m.group())))
    except ValueError:
        return None
    return -val if neg else val


# ─────────────────────────────────────────────────────────────────
# IDENTITY BLOCK
# ─────────────────────────────────────────────────────────────────
# These patterns are format-independent: every ITR document type (ITR-V,
# full form, CA computation sheet) carries PAN and Assessment Year in a
# recognisable form, so this layer works before any per-layout tuning.

_PAN_RE = re.compile(r"\b([A-Z]{5}\d{4}[A-Z])\b")

# AY appears as "2023-24", "2023-2024" or "A.Y. 2023-24".
_AY_RE = re.compile(
    r"(?:assessment\s*year|\bA\.?\s*Y\.?)\D{0,12}(20\d{2})\s*[-–/]\s*(\d{2}(?:\d{2})?)",
    re.I,
)
_AY_BARE_RE = re.compile(r"\b(20\d{2})\s*[-–]\s*(\d{2})\b")

_FORM_RE = re.compile(r"\bITR\s*[-–]?\s*([1-7])\b", re.I)

_ACK_RE = re.compile(r"acknowledgement\s*(?:number|no\.?)\D{0,10}(\d{9,20})", re.I)

_DATE_RE = re.compile(r"\b(\d{2}[-/]\d{2}[-/]\d{4})\b")

_NAME_RE = re.compile(
    r"(?:^|\n)\s*(?:name(?:\s*of\s*(?:the\s*)?(?:assessee|taxpayer))?)\s*[:\-]?\s*"
    r"([A-Z][A-Za-z&.\'\- ]{2,60}?)\s*(?:\n|$)",
    re.I,
)

_STATUS_RE = re.compile(
    r"\b(individual|huf|firm|company|aop|boi|llp|trust|society|"
    r"partnership\s*firm|private\s*limited)\b", re.I,
)


# ─────────────────────────────────────────────────────────────────
# BOOK PROFIT FROM THE COMPUTATION OF INCOME
# ─────────────────────────────────────────────────────────────────
# The computation sheet opens its business-income head by restating the P&L's
# own bottom line ("Net Profit / (Loss) as per profit & loss A/c (15,32,419)",
# "Net Profit Before Tax as per P & L a/c 19,81,650", "Net Profit (Loss)
# 5470889"). It is typed separately from the statement and usually sits on a
# digital page, so it is a genuinely INDEPENDENT reading of the profit - unlike
# a Vision re-read of the same statement image. Used only as a cross-check;
# it never replaces a figure.

_COMPUTATION_PAGE_RE = re.compile(
    r"computation|profits?\s+and\s+gains\s+of\s+business|income\s+from\s+business",
    re.I)

_BOOK_PROFIT_RE = re.compile(
    r"net\s+profit\s*(?:/\s*)?(?:\(\s*loss\s*\))?\s*(?:before\s+tax\s*)?"
    r"(?:as\s+per\s+(?:p\s*&\s*l|profit\s*(?:&|and)\s*loss)\s*(?:a/?c|account)?\.?)?"
    r"\s*:?\s*(\(?-?\s*\d[\d,]{2,}(?:\.\d+)?\)?)", re.I)


def extract_book_profit(page_texts: list):
    """
    (profit, page_index) as restated on the computation of income, or
    (None, None). Negative for a loss (brackets or a minus sign).
    """
    for i, text in enumerate(page_texts or []):
        flat = re.sub(r"\s+", " ", text or "")
        if not _COMPUTATION_PAGE_RE.search(flat):
            continue
        m = _BOOK_PROFIT_RE.search(flat)
        if not m:
            continue
        raw = m.group(1).replace(" ", "")
        val = to_int(raw)
        if val is None:
            continue
        if raw.startswith("-") and val > 0:
            val = -val
        return val, i
    return None, None


# ─────────────────────────────────────────────────────────────────
# FIGURES FROM THE ITR-V ACKNOWLEDGEMENT
# ─────────────────────────────────────────────────────────────────
# The Income Tax department's acknowledgement is one fixed, numbered table
# ("Total Income  1A  0", "Taxes Paid  7  3,19,862"). A proprietor's filing
# often arrives as this page plus the CA's computation and nothing else, so
# these are the only return figures there are. Read, never inferred.

_ACK_PAGE_RE = re.compile(r"income\s+tax\s+return\s+acknowledge?ment|\bITR\s*-?\s*V\b", re.I)

# label -> field; each value follows the row number ("1", "1A", "8").
_ACK_FIELDS = [
    ("current_year_business_loss", r"current\s+year\s+business\s+loss(?:\s*,\s*if\s+any)?"),
    ("gross_total_income",         r"gross\s+total\s+income"),
    ("total_income",               r"(?<!gross\s)(?<!adjusted\s)total\s+income(?!\s+under)"),
    ("net_tax_payable",            r"net\s+tax\s+payable"),
    ("taxes_paid",                 r"taxes\s+paid"),
    ("tax_payable_or_refund",      r"\(\+\)\s*tax\s+payable\s*/\s*\(-\)\s*refundable\s*\(\s*\d+\s*-\s*\d+\s*\)"),
]
_ACK_VALUE = r"\s+\d{1,2}[A-Za-z]?\s+(\(-\)\s*|-\s*)?(\d[\d,]*)"

ACK_FIELDS = [f for f, _ in _ACK_FIELDS]


def extract_return_figures(page_texts: list) -> dict:
    """
    {field: int} from the first acknowledgement page, or {} when the document
    has none. A refund reads negative, as the form itself prints "(-)".
    """
    for text in page_texts or []:
        flat = re.sub(r"\s+", " ", text or "")
        if not _ACK_PAGE_RE.search(flat):
            continue
        out = {}
        for field, label in _ACK_FIELDS:
            m = re.search(label + _ACK_VALUE, flat, re.I)
            if m:
                val = to_int(m.group(2))
                out[field] = -val if (m.group(1) and val) else val
        if out:
            return out
    return {}


def _norm_ay(m) -> str:
    """('2023', '24' | '2024') → '2023-24'."""
    start, end = m.group(1), m.group(2)
    return f"{start}-{end[-2:]}"


def extract_identity(text: str) -> dict:
    """PAN / AY / name / form / acknowledgement / status / regime."""
    out = {k: None for k in IDENTITY_FIELDS}
    flat = re.sub(r"[ \t]+", " ", text)

    m = _AY_RE.search(flat) or _AY_BARE_RE.search(flat)
    if m:
        out["ay"] = _norm_ay(m)

    m = _PAN_RE.search(flat.upper())
    if m:
        out["pan"] = m.group(1)

    m = _FORM_RE.search(flat)
    if m:
        out["form"] = f"ITR-{m.group(1)}"

    m = _ACK_RE.search(flat)
    if m:
        out["ack_no"] = m.group(1)

    m = _NAME_RE.search(flat)
    if m:
        name = m.group(1).strip(" .-")
        # Guard against the label bleeding into the value on OCR'd text.
        if name and not re.fullmatch(r"(?i)(of|the|assessee|taxpayer)", name):
            out["name"] = name

    m = _STATUS_RE.search(flat)
    if m:
        out["assessee_status"] = m.group(1).title()

    low = flat.lower()
    if "115bac" in low or "new tax regime" in low:
        out["regime"] = "New"
    elif "old tax regime" in low or "opting out" in low:
        out["regime"] = "Old"

    m = _DATE_RE.search(flat)
    if m:
        out["filing_date"] = m.group(1).replace("/", "-")

    return out
