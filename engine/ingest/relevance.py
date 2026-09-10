"""
relevance.py  -  decide which pages of an ITR bundle are worth working on.

An ITR filing bundle is mostly not the ITR. A real sample runs to 101 pages, of
which about six carry anything this tool needs: the acknowledgement, the
computation of income, and the financial statements. The rest is Form 3CD
clause-by-clause tables, TDS deductor listings, depreciation schedules,
accounting-policy notes, partner lists and verification pages.

Filtering them out matters three times over:
  - Gemini escalation is priced per page, and re-reading an audit report's
    boilerplate costs money for nothing;
  - OCR of a scanned bundle is the slowest thing this tool does, and a
    two-pass approach (cheap scan to classify, sharp re-read of the keepers)
    only works if something can classify pages cheaply;
  - the harvester finds fewer phantom "statements" when it never sees a
    schedule that happens to be laid out like one.

Classification is keyword-based on purpose. It runs on OCR output of unknown
quality and its job is only to decide "look at this page or not", so it is
deliberately biased toward keeping: a page wrongly kept costs a little time, a
page wrongly dropped loses data silently.
"""

import re

# Page categories, in priority order - a page is labelled by the first that hits.
STATEMENT   = "statement"     # Balance Sheet / P&L / Income & Expenditure
COMPUTATION = "computation"   # Computation of income
ACK         = "acknowledgement"
SCHEDULE    = "schedule"      # supporting schedules to the statements
AUDIT       = "audit"         # Form 3CB / 3CD / audit report
NOISE       = "noise"

# Categories worth extracting from.
KEEP = {STATEMENT, COMPUTATION, ACK, SCHEDULE}

# The STATEMENT pattern's two halves, kept separately so a caller can ask
# "did we find a BALANCE-SHEET-shaped page" and "did we find a P&L-shaped
# page" independently - see has_both_statement_kinds below for why that
# distinction matters and STATEMENT-page counting alone does not.
_BS_RE = re.compile(r"\bbalance\s*sheet\b", re.I)
_PL_RE = re.compile(
    # "prof[il]t", not "profit": RapidOCR read a real P&L heading as
    # "Proflt and Loss Account" (i/l are near-identical in many scanned
    # fonts) - the exact-spelling pattern missed it entirely, and with
    # no STATEMENT match this page fell through to a LATER pattern
    # (audit-report boilerplate elsewhere on the same page also matched),
    # silently dropping the whole P&L from the column it should have fed.
    r"\bprof[il]t\s*(?:&|and)\s*loss\b|"
    r"\bincome\s*(?:&|and)\s*expenditure\b|\btrading\s*(?:&|and)\s*prof[il]t\b|"
    r"\breceipts?\s*(?:&|and)\s*payments?\b|\bstatement\s+of\s+prof[il]t\b", re.I)
_STATEMENT_RE = re.compile(_BS_RE.pattern + "|" + _PL_RE.pattern, re.I)

_PATTERNS = [
    (STATEMENT, _STATEMENT_RE),
    (COMPUTATION, re.compile(
        r"\bcomputation\s+of\s+(?:total\s+)?income\b|\bgross\s+total\s+income\b|"
        r"\bincome\s+chargeable\s+under\s+the\s+head\b|"
        r"\bdeductions?\s+under\s+chapter\s+VI", re.I)),
    (ACK, re.compile(
        r"\bindian\s+income\s+tax\s+return\s+acknowledge?ment\b|\bITR\s*-?\s*V\b",
        re.I)),
    (SCHEDULE, re.compile(
        r"^\s*SCH\.?\s*[-A-Z]|\bschedule\s+[A-Z]\b|"
        r"\bsundry\s+(?:creditor|debtor)s?\b|\bloans?\s*&\s*advances?\b|"
        r"\bfixed\s+assets?\s+(?:schedule|chart)\b|\bdepreciation\s+chart\b", re.I)),
    (AUDIT, re.compile(
        r"\bform\s+no\.?\s*3C[ABD]\b|\bunder\s+section\s+44AB\b|"
        r"\bstatement\s+of\s+particulars\b|\bclause\s+\d+\b|"
        r"\baudit\s+report\b|\bUDIN\b", re.I)),
]

# A page with no money on it has nothing to extract regardless of its wording -
# a contents page or a clause narrative mentioning "balance sheet" in prose.
_MONEY_RE = re.compile(r"\d[\d\s]*[.,]\d{2}\b|\d{1,3}(?:[,\s]\d{2,3}){1,}")
_MIN_MONEY_HITS = 3


def classify_page(text: str) -> str:
    """One page's text → its category."""
    if not text or not text.strip():
        return NOISE
    money = len(_MONEY_RE.findall(text))
    for kind, rx in _PATTERNS:
        if rx.search(text):
            # Only the categories we would EXTRACT from need to prove they
            # carry figures - a page titled like a statement but holding none
            # is prose about one (an audit clause, a contents entry).
            # ACK is kept for its identity fields even when every figure is
            # zero, and AUDIT is reported as what it is: it gets dropped
            # either way, and labelling it "noise" would make the page summary
            # shown to the analyst misleading.
            if kind in (STATEMENT, SCHEDULE, COMPUTATION) and money < _MIN_MONEY_HITS:
                # A genuine heading (near the top of the page, short - see
                # _has_heading) is stronger evidence than a money count. The
                # money gate exists to reject PROSE that merely mentions a
                # statement; it must not also reject a page whose own real
                # heading proves it IS one, just badly OCR'd. Confirmed on a
                # real filing: a Balance Sheet page's heading read perfectly
                # ("Balance Sheet as at 31 March 2023") while its entire
                # number table OCR'd to noise, leaving 1 money-like pattern
                # on the whole page - the low count is evidence OCR failed
                # on the body, not evidence there's no statement here.
                if kind == STATEMENT and (_has_heading(text, _BS_RE)
                                          or _has_heading(text, _PL_RE)):
                    return kind
                return NOISE
            return kind
    return NOISE


def classify(page_texts: list) -> list:
    return [classify_page(t) for t in (page_texts or [])]


# A genuine statement's own heading sits near the top of its page (company
# name, maybe a CIN, then the heading itself) and is short - "PROFIT AND LOSS
# ACCOUNT FOR THE YEAR ENDED ON 31ST MARCH 2026" is 64 characters. Neither is
# true of a mention of the SAME wording elsewhere: an audit report's own
# prose ("...which comprise the Balance Sheet as at March 31, 2023, the
# Statement of Profit and Loss for the year ended on that date...", 95
# characters, nine lines into the page) or a Notes page's internal
# cross-reference ("Statement of Profit and loss" as a schedule item's own
# caption - short enough to pass a length check alone, but several lines
# into a page whose OWN heading is "Notes forming part of the Financial
# Statements"). Both are real, confirmed false positives from a plain
# substring search over the whole page.
_HEADING_LINE_LIMIT = 5
_HEADING_MAX_LEN    = 80
# A real heading LEADS with the phrase ("Balance Sheet as at...",
# "Statement of Profit and loss for..."); prose that merely mentions the same
# words has narrative text in front of it ("This document discusses balance
# sheet matters..."). Position, not just shortness, is what tells them apart -
# a short SENTENCE is still a sentence.
_HEADING_START_MAX = 10


def _has_heading(text: str, rx) -> bool:
    for line in (text or "").splitlines()[:_HEADING_LINE_LIMIT]:
        line = line.strip()
        if not line or len(line) > _HEADING_MAX_LEN:
            continue
        m = rx.search(line)
        if m and m.start() <= _HEADING_START_MAX:
            return True
    return False


def has_both_statement_kinds(page_texts: list) -> bool:
    """
    Does some page's own HEADING look like a Balance Sheet, and some page's
    own HEADING look like a P&L - not just any two pages that each happened
    to mention that wording somewhere in their body text?

    Counting STATEMENT-classified pages (the previous approach) is too easy
    to satisfy by accident: a Chartered Accountant's audit-report letterhead
    and a Notes/Schedule page (e.g. "Reserves and Surplus", which cross-refers
    to "Statement of Profit and loss" as its own sub-caption) can each
    independently classify STATEMENT and match the wording via a plain
    substring search, without either one being the actual Balance Sheet or
    P&L. Confirmed on a real filing: exactly that combination satisfied the
    old ">= 2 STATEMENT pages" bar used to decide whether OCR's cheap
    classify pass could be trusted, while the real Balance Sheet and P&L sat
    on OTHER pages the cheap pass had misread badly enough to miss - and
    nothing re-read them, because the bar had already been cleared by two
    pages that were never going to help. Requiring the match to be a genuine
    heading (`_has_heading` - near the top of the page, and short) closes
    that gap without needing to know anything about `financials.py`'s own,
    considerably more thorough title-detection logic.
    """
    return (any(_has_heading(t, _BS_RE) for t in page_texts or [])
           and any(_has_heading(t, _PL_RE) for t in page_texts or []))


def select(page_texts: list, keep=KEEP) -> list:
    """
    Indices of the pages worth extracting from.

    A statement can spill onto the following page (its own heading appears only
    once), so the page after a statement is kept too - but ONLY when that page
    has no identity of its own. A continuation carries no heading and so
    classifies as NOISE; a page that classifies as an audit report or a fresh
    acknowledgement is the next document, not the rest of this statement, and
    dragging it in just feeds the harvester more to trip over.
    """
    kinds = classify(page_texts)
    chosen = set()
    for i, kind in enumerate(kinds):
        if kind in keep:
            chosen.add(i)
            if kind == STATEMENT and i + 1 < len(kinds) and kinds[i + 1] == NOISE:
                chosen.add(i + 1)
    # Nothing recognised - fall back to the whole document rather than
    # returning an empty extraction. A bundle we can't classify is a bundle we
    # should still try to read.
    return sorted(chosen) if chosen else list(range(len(kinds)))


def summary(page_texts: list) -> dict:
    """{category: count} - for the UI and the debug harness."""
    out = {}
    for kind in classify(page_texts):
        out[kind] = out.get(kind, 0) + 1
    return out
