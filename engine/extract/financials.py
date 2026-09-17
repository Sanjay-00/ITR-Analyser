"""
financials.py  -  locate and harvest the financial statements attached to an ITR.

An ITR filing bundle carries, after the return and the computation sheet, the
actual financial statements: a Balance Sheet and a Profit & Loss (or Income and
Expenditure) account per business entity. Those are the source of the spreading
sheet - the computation sheet alone only ever states net figures.

The design point: DO NOT recognise formats. Every CA package (Adv. Singhania,
TaxPro, Winman, Genius...) lays these out differently, and chasing layouts is a
treadmill. What never varies is the structure -

    a titled block of `label ... amount` pairs, in two columns, that balances.

So this module anchors on the title, harvests every label/amount pair without
interpreting any of them, and then proves the harvest by arithmetic: both sides
of the account must agree with each other and with the printed total. Mapping
those raw labels onto the output taxonomy is a separate concern (taxonomy.py) -
that is where genuine format variance lives, and it is a short-string
classification problem, not a layout problem.

The balance check is not a heuristic. On a real scanned bundle it caught OCR
silently dropping an entire "Deferred tax liability 78,95,238" row - the
liabilities side came up short by exactly that amount.
"""

import re

from .itr_parser import to_int
from ..ingest.layout import split_columns

# ─────────────────────────────────────────────────────────────────
# STATEMENT TYPES
# ─────────────────────────────────────────────────────────────────

BALANCE_SHEET   = "balance_sheet"
PROFIT_LOSS     = "profit_loss"
CAPITAL_ACCOUNT = "capital_account"
OTHER           = "other"

# Title markers, checked against a whitespace-collapsed line. OCR mangles these
# routinely, so matching is deliberately loose ("PROFIT & LOSS A/C.",
# "PROFIT AND LOSS ACCOUNT", "PROFIT & LOSS AIC").
#
# The last pattern catches ANY "<something> Account for the year ended" heading.
# That breadth is deliberate: a real filing labelled its Income & Expenditure
# account "RECEIPT & PAYMENTS ACCOUNT" - the heading a CA types is not reliable
# evidence of what the statement is. So headings only open a block; what the
# block actually IS gets decided from its contents by _classify below.
_TITLES = [
    re.compile(r"\bbalance\s*sheet\b", re.I),
    # "prof[il]t", not "profit": an OCR engine can read "Profit" as "Proflt"
    # (i/l are near-identical in many scanned fonts) - the exact-spelling
    # pattern missed a real "Proflt and Loss Account" heading entirely on a
    # real filing, and with no title recognised the whole statement sat
    # before the next heading this DID match and was silently dropped.
    re.compile(r"\bprof[il]t\s*(?:&|and)\s*loss\b", re.I),
    re.compile(r"\bincome\s*(?:&|and)\s*expenditure\b", re.I),
    re.compile(r"\btrading\s*(?:&|and)\s*profit\b", re.I),
    re.compile(r"\breceipts?\s*(?:&|and)\s*payments?\b", re.I),
    # Anchored to the first ~15 characters, not literally the start of the
    # line: "Capital A/c" also appears mid-line as an ordinary item ("Net
    # Income (Trf to Capital A/c)  4,98,650"), and treating THAT as a heading
    # splits the P&L in half and loses every entry above it. But a firm's own
    # heading convention can prefix it too ("PROV. CAPITAL A/C FOR THE YEAR
    # ENDING..."), and a literal ^ anchor rejected that outright, silently
    # merging the whole Capital Account into the P&L above it on a real
    # filing - the P&L's own printed total was replaced by the Capital
    # Account's, and the harvest broke on both sides of that boundary. A
    # short leading-word allowance catches the heading case without opening
    # the door to the mid-sentence one.
    #
    # The trailing `(?!\s*$)` matters separately: a real Balance Sheet's own
    # Equity & Liabilities section routinely prints a bare "Capital account"
    # LINE ITEM (naming the schedule that details it, no owner name, nothing
    # else on the line) - on a real filing that line alone matched this
    # pattern and was treated as a fresh statement heading, splitting the
    # actual Balance Sheet into an empty stub (title, then nothing before the
    # next match) and a second block that held all its real data but was
    # mislabelled "Capital account" instead. A genuine standalone Capital
    # Account statement always has something else on its title line - an
    # owner's name ("CAPITAL ACCOUNT OF SHRI ...") or a period ("... FOR THE
    # YEAR ENDING") - so requiring the line not to end right after the phrase
    # keeps that case while excluding the bare line-item label.
    #
    # `(?!.*\basset)` guards a THIRD case, distinct from both above: a
    # bank-format Balance Sheet's two column captions - "CAPITAL ACCOUNT :"
    # (liabilities side) and "FIXED ASSETS :" (assets side) - reconstruct
    # onto one joined line with no amount of its own, and that line still has
    # "something else" after "Capital Account" (the other caption), so the
    # (?!\s*$) guard above does not catch it. Confirmed on a real filing
    # (FC6, Borrower C AY 2024-25): without this, that line
    # opened a phantom heading, leaving an empty stub in place of the real
    # Balance Sheet and a second block that started too late to find its own
    # period line above it - the column got no year, and _find_entity's
    # lookback window no longer reached the real business name either, both
    # for a page whose figures (including its own printed TOTAL) were
    # otherwise read perfectly. `_classify` already excludes "asset" from its
    # own capital-account content scoring for the same reason; this applies
    # the same guard one step earlier, so the line never opens a block at all.
    re.compile(r"^.{0,15}?\bcapital\s+a/?c(?:count)?\b(?!\s*$)(?!.*\basset)", re.I),
    re.compile(r"\baccount\b.{0,30}\byear\s*end", re.I),
    # A trading account is sometimes named after the business itself rather
    # than generically ("PUMP ACCOUNT" for a petrol pump) - on a real filing
    # that heading matched none of the patterns above, so the whole trading
    # section (Sales, Purchases, Opening/Closing Stock) sat before the first
    # recognised title and was silently dropped rather than opening its own
    # block. Anchored near line start for the same reason as Capital A/c
    # above: "credited to his account for the period" is prose, not a
    # heading, and a bare \baccount\b would open a phantom block on it.
    #
    # Excludes any digit, "chartered account(ant)?s?", and "capital account":
    # on a real filing this pattern also caught "16.1 Branch Account" (a
    # numbered note reference, not a statement), "CHARTERED ACCOUNT[ANTS]"
    # (an auditor's signature block, line-wrapped so "ANTS" fell onto the
    # next line), and a bare "Capital account" LINE ITEM inside a real
    # Balance Sheet's own Equity & Liabilities section (naming the schedule
    # that details it, nothing else on the line) - all three opened a
    # phantom block that cut the real statement's body short, which is what
    # actually flipped Profit After Tax's sign on one filing and, on
    # another, split a genuine Balance Sheet into an empty stub plus a
    # second block holding all its real data but mislabelled "Capital
    # account". "Capital account" specifically is excluded here rather than
    # merely relying on match order, since the capital-a/c pattern above
    # only opens a block for it when something ELSE follows the phrase on
    # the same line (an owner's name, a period) - a bare line-item label has
    # nothing else, and this pattern would otherwise still catch it anyway.
    re.compile(r"^(?!.*\d)(?!.*\bchartered\b)(?!\s*capital\s+a(?:ccount|/?c)\s*$)"
              r".{0,20}?\baccount\b\s*$", re.I),
]

# Content signatures. A statement is what its line items say it is.
_SIG = {
    BALANCE_SHEET: re.compile(
        r"\b(sundry\s+creditor|sundry\s+debtor|fixed\s+asset|current\s+asset|"
        r"unsecured\s+loan|secured\s+loan|closing\s+stock|cash\s+(?:in\s+hand|"
        r"&|and)|investment|capital\s+account|loans?\s*&\s*advance)\b", re.I),
    PROFIT_LOSS: re.compile(
        # The trailing \b sits outside this whole alternation, so an
        # alternative that doesn't itself allow for a plural silently stops
        # matching plural text: "purchase" needs its own "s?" here, since
        # "Purchases" leaves no word boundary right after "purchase" for the
        # closing \b to land on. Missed exactly that on a real filing whose
        # trading account said "Purchases," tipping a content-based
        # classification tie-break the wrong way (misread as a Balance Sheet).
        r"\b(gross\s+(?:receipt|profit|turnover)|sales?\b|net\s+(?:profit|income)|"
        r"depreciation|salar(?:y|ies)|wages|rent\b|electricity|expenses?\b|"
        r"purchases?|freight|interest\s+(?:paid|&\s*bank))\b", re.I),
    # Deliberately NOT matching "transferred to Capital A/c" - that phrase is
    # the closing line of a P&L, not evidence of a capital account, and on a
    # real filing it made the whole Income & Expenditure account classify as
    # one. Two distinct signals are required below for the same reason.
    CAPITAL_ACCOUNT: re.compile(
        r"\b(drawings?|opening\s+capital|closing\s+capital|capital\s+introduced)\b",
        re.I),
}


def tag_pl_sides(kind: str, sides: dict) -> None:
    """
    In a T-account P&L, which SIDE a line sits on is what makes it income or
    expense - the label alone does not say.

    A transport operator's credit side reads "Bus Fare - AC", "Bus Fare - Non
    AC"; those are revenue, but every word in them also appears in transport
    OPERATING costs. Read by label alone they were filed as expenses, which on
    a real filing put the whole revenue line into Gross Expenses: 4085 lakhs of
    costs against an actual 2062, and a 33-lakh loss reported as 4079.

    Debit side is expenses, credit side is income. That is the definition of
    the account, so it is applied before any label matching gets a say.
    """
    if kind != PROFIT_LOSS or sides.get("sections"):
        return
    for side, token in (("left", "EXP"), ("right", "INC")):
        sides[side] = [(f"[{token}] {lab}" if not lab.startswith("[") else lab, amt)
                       for lab, amt in sides.get(side, [])]


# Backwards-compatible private alias used inside this module.
_tag_pl_sides = tag_pl_sides


def _has_items(sides: dict) -> bool:
    """Did this block harvest anything, in either the T-account or the
    vertical shape?"""
    if sides.get("sections"):
        return any(s["items"] for s in sides["sections"])
    return bool(sides["left"] or sides["right"])


def all_items(sides: dict) -> list:
    """Every (label, amount) in a block, whichever shape it came in."""
    if sides.get("sections"):
        return [it for s in sides["sections"] for it in s["items"]]
    return list(sides["left"]) + list(sides["right"])


def _classify(title_line: str, sides: dict) -> str:
    """
    What this block actually is, from its own contents.

    The heading is only a weak hint - an unambiguous one ("Balance Sheet",
    "Profit & Loss") is trusted, anything vaguer is decided by scoring the
    harvested labels against each statement's characteristic vocabulary.
    """
    flat = re.sub(r"\s+", " ", title_line)
    if re.search(r"\bbalance\s*sheet\b", flat, re.I):
        return BALANCE_SHEET
    if re.search(r"\b(prof[il]t\s*(?:&|and)\s*loss|income\s*(?:&|and)\s*expenditure"
                 r"|trading)\b", flat, re.I):
        return PROFIT_LOSS
    # "CAPITAL ACCOUNT OF SHRI ..." is exactly as unambiguous a heading as
    # "BALANCE SHEET" - trusted the same way, rather than falling through to
    # vocabulary scoring below. On a real filing that scoring missed it
    # entirely (the page said "Withdrawals", not the "drawings" the
    # signature list expects) and the block was misclassified as a Balance
    # Sheet - a real one already existed for the same entity/year, so both
    # got kept and the proprietor's capital was double-counted into net worth.
    #
    # Excludes any title that ALSO mentions assets: a genuine standalone
    # capital-account statement never does, but a Balance Sheet's own two
    # column sub-headings reconstruct onto one joined line ("CAPITAL ACCOUNT
    # OF : FIXED ASSETS :" - liabilities-column heading + assets-column
    # heading, read side by side) and _TITLES already treats that as its own
    # block boundary. Without this guard the real Balance Sheet that block
    # actually holds gets relabelled a Capital Account too, and BOTH pages
    # end up excluded from the sheet's balance-sheet figures entirely.
    if (re.search(r"\bcapital\s+a/?c(?:count)?\b", flat, re.I)
            and not re.search(r"\basset", flat, re.I)):
        return CAPITAL_ACCOUNT

    labels = " ; ".join(l for l, _a in all_items(sides))
    haystack = f"{flat} ; {labels}"
    # Capital accounts share vocabulary with both, so check their own
    # distinctive terms first - but require two of them, since a single
    # capital-flavoured line appears in statements that are not capital accounts.
    if len(set(m.lower() for m in _SIG[CAPITAL_ACCOUNT].findall(labels))) >= 2:
        return CAPITAL_ACCOUNT
    scores = {k: len(_SIG[k].findall(haystack))
              for k in (BALANCE_SHEET, PROFIT_LOSS)}
    best = max(scores, key=scores.get)
    return best if scores[best] else OTHER

# A title line must be mostly title - "In the case of the balance sheet, of the
# state of the affairs of the assessee" inside an audit report is prose, not a
# statement heading, and would otherwise open a phantom block. Long enough to
# admit single-line headings that carry their own period ("RECEIPT & PAYMENTS
# ACCOUNT FOR THE YEAR ENDED ON 31ST MARCH 2026" is 64 characters).
_MAX_TITLE_LEN = 80

# "as on 31st March, 2024" / "for the year ended 31/03/2024"
# Three date shapes, the year always group 3:
#   "31st March, 2024" / "31 MAR 2024"   day then month
#   "March 31, 2025"                     month then day - Schedule III / Ind AS
#   "31.03.2024" / "31/03/2024"          numeric
# The month-first form used to be missed outright: "Balance Sheet as at March
# 31, 2025" matched nothing, the statement got no year of its own and fell
# back to its FILE's year - on a real case (Borrower H) filing FY2025's
# Balance Sheet and P&L under 2024 once files were pooled by year.
_PERIOD_RE = re.compile(
    r"(?:as\s*on|for\s*the\s*(?:year|period)\s*end\w*(?:\s*on)?|as\s*at)\D{0,20}?"
    # Any run of spaces and commas around one optional -/. separator: "31st,
    # March , 2025" (A CUSTOMER AY2025-26) had a comma the old single
    # separator refused, and the P&L lost its year - and with it its whole
    # comparative year (see find_blocks).
    r"(?:(\d{1,2})\s*(?:st|nd|rd|th)?[\s,]*[-/.]?[\s,]*([A-Za-z]{3,9})\.?[,\s]*"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s*"
    r"|\d{1,2}\s*[./-]\s*\d{1,2}\s*[./-]\s*)?(\d{4})",
    re.I,
)

# A harvestable line: some label text, then an amount at the end of its cell.
# Indian grouping, optional paise, optional bracketed negative. OCR spacing and
# stray separators are tolerated - the balance check is what actually decides
# whether the read was right, so being permissive here costs nothing.
#
# A genuine rupee amount is comma-grouped OR carries a decimal (Indian
# accounting practice essentially never prints one bare) - a label ending in
# a raw account/reference number ("SBI EDFS loan CC 5754", "SBI COVID loan
# ... 94961") has neither, and on a real filing both got harvested as if
# they were amounts, corrupting the label's real figure into a phantom
# second line item and leaving the balance sheet short by exactly their sum.
# The trailing `(?<!\d)` on the short-integer fallback matters for the same
# reason a 3-decimal-digit figure once did: without it, a 4+ digit bare
# number that correctly fails the "no formatting" test would still find a
# match by backtracking onto its own trailing 1-3 digits ("5754" -> "754"),
# recreating the exact bug this exists to prevent instead of just refusing
# to match a reference number at all.
#
# That fallback requires exactly 3 digits, not 1-3: a Schedule III row
# prints its own Note No. as a bare 1-2 digit reference next to a blank/NIL
# figure ("Cost of Materials Consumed  25  -  -"), and allowing 1-2 digits
# here read the note number itself as a phantom ₹25 (scaled to 25,000 by
# this statement's own "Amount in Thousand" unit) - the same failure mode
# this whole regex exists to prevent, just at the short end instead of the
# long end. _NOTE_MAX below documents the same 1-2-digit boundary.
_AMOUNT_RE = re.compile(
    r"\(?\s*(?:Rs\.?|₹)?\s*"
    r"(\d{1,3}(?:[,\s]\d{2,3})+(?:[.,]\d{1,3})?"  # comma-grouped, optional decimal
    r"|\d+[.,]\d{1,3}"                             # any digit run WITH a decimal/paise part
    r"|(?<!\d)\d{3})"                              # standalone bare integer, exactly 3 digits
    r"\s*\)?\s*$"
)

# A cell that is NOTHING BUT digits (the whole cell, not a trailing suffix of
# a longer string) is a different, much safer signal than the reference-number
# risk _AMOUNT_RE's exact-3-digit fallback guards against: "SBI EDFS loan CC
# 5754" is a label ending in a reference number, but a cell containing only
# "145644604" and nothing else has no label for that number to be a reference
# WITHIN. Confirmed on a real filing (FC4, Borrower A AY 2024-25)
# whose credit-side total was printed with none of the thousands separators
# every OTHER figure on the same page used - _AMOUNT_RE's comma/decimal/
# exact-3-digit grammar matches none of that, and the entire income figure
# was silently dropped from the harvest.
_BARE_LONG_INT_RE = re.compile(r"^(-?)(\d{4,})$")

# Outcome rows in a vertical statement: derived from the items above them, not
# line items themselves. Skipped so they neither double-count nor make a
# section look unbalanced (a P&L's tail - "Profit before tax", "Profit for the
# period" - is not a sum of anything preceding it).
#
# Matched with .search, not anchored to the very start: an OCR engine can
# fuse two vertically-close printed lines into one detected row ("VIII
# Exceptional items" + "IX Profit before tax 35,622.534" read as one label,
# "VIII IX Extraordinary items Profit before tax"). Requiring the phrase at
# position 0 missed it once merged, and "Profit before tax" - already a
# derived figure - got harvested a second time as a fresh expense, flipping
# the sign of every profit figure computed from that block. None of these
# phrases plausibly appear as a substring of a genuine, unrelated expense
# label, so allowing leading text before the match doesn't risk a false skip.
_DERIVED_ROW_RE = re.compile(
    r"(?:^|\s)(?:\[[A-Z]{2,3}\]\s*)?"
    # A schedule roman numeral is sometimes typeset with its own column rule
    # ("XVII | Profit/(Loss) Carried over..."), printed as a literal "|" in
    # the extracted text - allow it alongside the usual space/period/paren.
    r"(?:[IVX]{1,4}[\s.)|]+|\(?\d{1,2}\)?[\s.)|]+)?"
    r"(?:(?:nett?\s+)?profit\s+(?:before|after|for\s+the)"
    # The (before|after|for) group is closed HERE. It used to stay open, so
    # the two alternatives below only ever matched after a "Profit / Loss "
    # prefix - "Net Profit / Loss Transferred to ..." slipped through as a
    # line item (found via the Borrower E summary P&L).
    r"|(?:nett?\s+)?profit\s*/?\s*\(?loss\)?\s+(?:before|after|for)"
    # "...Carried over to Balance Sheet" restates the bottom-line figure
    # rather than adding a new one - on a real filing it was harvested as a
    # plain expense line and folded into Other Expenses, corrupting every
    # profit figure derived downstream.
    r"|carried\s+(?:over\s+)?to\s+(?:the\s+)?balance\s+sheet"
    # "Net Profit / Loss Transferred to Proprietor's Capital Account" - the
    # result line of a proprietor's summary P&L (Borrower E).
    r"|(?:nett?\s+)?(?:profit|loss)\b[^|]{0,30}?\btransferred\s+to\b"
    r"|loss\s+for\s+the|total\s+comprehensive|other\s+comprehensive"
    r"|earnings?\s+per|basic\s*\(|diluted\s*\()", re.I)

# Lines that are structure, not data.
# "Proprietor" / "Partner" / "Director" are skipped only when the line is JUST
# that word - the signature caption at the foot of a statement. As part of a
# longer line they are content: "Proprietor's Capital Account" is the heading
# that identifies a sole trader's capital, and skipping it left the capital
# line (often nothing but the owner's name) with no way to classify it, so the
# whole equity figure fell off the balance sheet.
_SKIP_RE = re.compile(
    r"^\s*(?:(?:proprietors?|partners?|directors?|to|by)\s*$|"
    r"(?:particulars?|paticulars?|liabilities|assets|sch\.?|schedule|amount|"
    # "total outstanding dues ..." is Schedule III's spelling of TRADE
    # PAYABLES - a line item, not a total row (see _ANCHOR_RE).
    r"total(?!\s+outstanding\s+dues)|as\s+per\s+our\s+report|place|date|udin|for[,\s]|"
    r"chartered\s+account|signature)\b)",
    re.I,
)

_TOTAL_RE = re.compile(r"^\s*total\b", re.I)

# "Less: Depreciation" under a fixed asset's own name (COMPUTER / 10,172 then
# LESS : DEP / 4,069 / 6,103 on the next line) is a standard Indian
# proprietorship convention for showing gross cost written down to net book
# value. It is not a new line item - see the note in _harvest_at.
_LESS_DEP_RE = re.compile(r"^less\s*:?\s*dep", re.I)

# Everything below one of these is the attestation block, not the statement:
# auditor's firm, partners' names, DINs, membership numbers, place and date.
# Those carry numbers that read perfectly well as amounts (a DIN is nine
# digits) and would otherwise be harvested as line items.
# "Director" / "Proprietor" / "Partner" only end the statement when the line is
# JUST that word - the signature caption. As part of a longer line they are
# ordinary balance-sheet entries ("Proprietor's Capital Account",
# "Partners' Capital"), and treating those as the footer truncates the
# statement to nothing.
_FOOTER_RE = re.compile(
    r"^\s*(?:(?:directors?|proprietors?|partners?)\s*$"
    r"|as\s+per\s+(?:my|our)\s+report"
    r"|for\s+[A-Z][\w&.\s]{3,}(?:and\s+)?(?:associates|co\.?|company)\s*$"
    r"|chartered\s+account|significant\s+accounting|notes?\s+(?:to|forming)"
    r"|m\.?\s*no\.?\s*[:.]?\s*\d|frn\b|udin\b|place\s*:|date\s*:|\(?din\b)",
    re.I)


def _footer_at(rows: list):
    """Index of the first attestation-block row, or None."""
    for i, (_y, cells) in enumerate(rows):
        line = " ".join(t for _a, _b, t in cells).strip()
        if _FOOTER_RE.match(line):
            return i
    return None

# Unambiguously an amount of money: has thousands grouping, or paise. Used
# where a bare run of digits would be too easily confused with a year or a
# pincode sitting in a heading or address line.
_MONEY_RE = re.compile(r"\d[\d\s]*[.,]\d{2}\b|\d{1,3}(?:[,\s]\d{2,3}){1,}")


def _clean_amount(raw: str):
    """
    '1,35,24,153.00' → 13524153.0.  Strips the paise part BEFORE the thousands
    separators: doing it the other way round welds the paise onto the rupees
    ('1,35,24,153.00' → 1352415300) and inflates every figure 100x.

    Returns a FLOAT, and keeps any decimal that is not paise. Statements
    presented "in Rs. lakhs" print real decimals that carry rupees ("6,029.41"
    lakhs is ₹6,02,94,100), so rounding here would throw away up to ₹99,999 per
    line before the unit scaling below ever runs.
    """
    if raw is None:
        return None
    txt = re.sub(r"(?<=\d)[.,]\s?0{2}\s*$", "", str(raw).strip())
    txt = re.sub(r"(?<=\d)[,\s](?=\d)", "", txt)
    m = re.search(r"-?\d+(?:\.\d+)?", txt.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────
# PRESENTATION UNITS
# ─────────────────────────────────────────────────────────────────
# Statements declare their own scale, usually just under the heading:
# "(All amounts in Rs. lakhs, unless otherwise stated)". Ignoring it is a
# silent 100,000x error on every figure - and one that still BALANCES, because
# both sides are scaled equally, so no arithmetic check can catch it. This is
# the one class of mistake the balance identity cannot protect against, which
# is why it is read explicitly rather than assumed.

_UNITS = [
    (10000000, re.compile(r"\bin\s+(?:Rs\.?|INR|₹)?\s*crores?\b", re.I)),
    (1000000,  re.compile(r"\bin\s+(?:Rs\.?|INR|₹)?\s*(?:millions?|mn)\b", re.I)),
    (100000,   re.compile(r"\bin\s+(?:Rs\.?|INR|₹)?\s*(?:lakhs?|lacs?)\b", re.I)),
    (1000,     re.compile(r"\bin\s+(?:Rs\.?|INR|₹)?\s*(?:thousands?|'000|000s)\b", re.I)),
]

# How far around the heading to look for the declaration.
_UNIT_BAND = (4, 8)


def _unit_scale(lines: list, idx: int) -> int:
    """Rupees per printed unit for the statement whose heading is at `idx`."""
    lo = max(0, idx - _UNIT_BAND[0])
    window = " ".join(lines[lo:idx + _UNIT_BAND[1]])
    for scale, rx in _UNITS:
        if rx.search(window):
            return scale
    return 1


def _rescale(sides: dict, scale: int) -> None:
    """Convert a harvested block from its printed units into whole rupees."""
    def _fix(pairs):
        return [(lab, int(round(amt * scale))) for lab, amt in pairs]

    if sides.get("sections"):
        for s in sides["sections"]:
            s["items"] = _fix(s["items"])
            if s["total"] is not None:
                s["total"] = int(round(s["total"] * scale))
    for side in ("left", "right"):
        if sides.get(side):
            sides[side] = _fix(sides[side])


# A hyphen directly before the amount (only whitespace between) is a sign,
# not part of the label - "Short - Term Borrowings" has one too, but not
# immediately before its amount, so checking the PREFIX up to the amount
# match (not the whole cell) never confuses the two.
_NEG_PREFIX_RE = re.compile(r"-\s*$")


def _is_negative(cell: str, prefix: str = None) -> bool:
    """
    Accounts print negatives in brackets ('(273,004,216.00)') - almost
    always, but not without exception: a real filing printed a loss as
    'TO NET PROFIT -4,391,651', a plain leading minus sign with no
    brackets at all, and it was silently read as a positive profit of the
    same magnitude because nothing here ever looked for one.

    `prefix` is the text up to the amount match, when the caller has it -
    passing it catches the leading-minus case; omitting it (as the few
    remaining bracket-only call sites do) just skips that check.
    """
    if "(" in cell and ")" in cell:
        return True
    return bool(prefix is not None and _NEG_PREFIX_RE.search(prefix))


def parse_line(cell: str):
    """
    One column cell → (label, amount), or None when it carries no figure.
    The label is whatever precedes the trailing amount.
    """
    cell = cell.strip()
    if not cell or _SKIP_RE.match(cell):
        return None
    m = _AMOUNT_RE.search(cell)
    if not m:
        return None
    val = _clean_amount(m.group(1))
    if val is None:
        return None
    label = cell[:m.start()].strip(" .:-|")
    # Drop a leading schedule reference ("SUNDRY CREDITORS B", "Sundry Debtors E")
    label = re.sub(r"\s+[A-Z]{1,2}$", "", label).strip(" .:-|")
    if not label or len(label) < 3:
        return None
    # A real label always carries at least one letter. Its absence means the
    # "label" is leftover digits from an earlier amount in the SAME cell -
    # OCR can merge two visually-close printed rows into one detected row,
    # concatenating their amounts with no label text between them at all
    # ("4,41,330 6,38,687 3,29,53,179": two individual figures plus their
    # own group subtotal, all on what OCR read as one line). _AMOUNT_RE
    # anchors to the END of the cell, so without this check every amount but
    # the last becomes a nonsense numeric "label" - worse than dropping the
    # cell, since that garbage label then enters the harvest carrying the
    # WRONG (usually largest, often a subtotal) figure rather than its own.
    # Confirmed on a real filing: this exact row cost the Balance Sheet its
    # Cash in Hand / Cash at Bank figures (see docs/SHORTCOMINGS.md Case 3).
    if not re.search(r"[A-Za-z]", label):
        return None
    return label, (-val if _is_negative(cell, cell[:m.start()]) else val)


# ─────────────────────────────────────────────────────────────────
# BLOCK LOCATION
# ─────────────────────────────────────────────────────────────────

# How many lines above a title to search for the entity name, and below it for
# the period. Statement headings are consistently
#     ENTITY NAME, PLACE
#     BALANCE SHEET
#     as on 31st March, 2024
_ENTITY_LOOKBACK = 3
_PERIOD_LOOKAHEAD = 3

# A block runs until the next statement title or the end of the page's text.
# Statements do not span pages in these bundles (each is printed whole), so a
# page boundary also ends a block.


# A dotted/slashed date ("31.03.2024", "31/03/2024"). "31.03" alone reads as
# a figure with paise to _MONEY_RE.
_DATE_IN_LINE_RE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")


def _match_title(line: str) -> bool:
    flat = re.sub(r"\s+", " ", line).strip()
    if len(flat) > _MAX_TITLE_LEN:
        return False
    # A heading carries no MONEY of its own, which rejects the line items that
    # would otherwise read as headings. Tested for grouping or paise rather
    # than with _AMOUNT_RE: most headings end in their own year, and a bare
    # "2026" is indistinguishable from an amount to a general number pattern.
    #
    # The heading's own DATE is removed first: "BALANCE SHEET AS ON
    # 31.03.2024" contains "31.03", which reads as rupees-and-paise, so the
    # heading was rejected outright. On a real filing (Borrower B, both
    # years) the Balance Sheet then merged into the Capital Account printed
    # above it on the same page, the whole block was classed as a capital
    # account, and the year had no Balance Sheet at all.
    undated = _DATE_IN_LINE_RE.sub(" ", flat)
    if _MONEY_RE.search(undated):
        return False
    # A P&L's closing line REFERS to the Balance Sheet ("XVII Profit/(Loss)
    # Carried over to Balance Sheet") - it is not one. On a real filing
    # (Borrower M FY2025) it opened a phantom Balance Sheet block in
    # the middle of the P&L page.
    if re.search(r"\b(?:carried|transferred|trf|shown|taken)\b.{0,20}\bto\b.{0,10}"
                 r"\bbalance\s*sheet\b", undated, re.I):
        return False
    # Nor does a heading carry a bare figure. "CAPITAL ACCOUNT | COMPUTER |
    # 10172" is a Balance Sheet ROW - the liabilities side's capital line
    # beside an asset and its cost, printed without separators (Borrower J /
    # Borrower J AY 2023-24, a digital page). It passed every other
    # test, opened a phantom "Capital Account" block, and cut the real Balance
    # Sheet in two - neither half reconciled and the year had no Balance
    # Sheet. Only a 4-digit year may stand alone in a heading.
    #
    # "Stand alone" means its own whitespace-delimited word. A scanner's text
    # layer read Borrower F' FY2025 heading as "Balance Sheet as at 31st
    # March,?025" - the "?025" is a damaged year, not a figure, and a plain
    # \b\d{3,}\b match rejected the whole heading.
    if any(not (len(n) == 4 and 1900 <= int(n) <= 2100)
           for n in re.findall(r"(?<!\S)\d{3,}(?!\S)", undated)):
        return False
    return any(rx.search(flat) for rx in _TITLES)


# An address line sits between the entity name and the heading in most layouts.
_ADDRESS_RE = re.compile(
    r"\b(road|nagar|marg|street|chowk|complex|shop\s*no|near|dist|"
    r"pin|opp\.?|building|plot|floor|house|sector|wing|tower|lane|"
    r"colony|society|premises|midc|industrial\s+area|taluka|flat)\b", re.I)

# Keywords alone are not enough - a real filing's address used none of the
# ones above ("6-A, H & G House, Sector 11, C B D Belapur, Navi Mumbai,
# Maharashtra, India, 400614") and was taken for the business name, which
# then titled the whole workbook. An address also has a SHAPE a business
# name does not: a six-digit PIN code, or a string of comma-separated
# locality parts. Either is enough on its own.
_PIN_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_ADDRESS_PARTS = 3

# An address keyword alone is not enough: "road", "nagar" and "complex" are
# common words in a genuine Indian BUSINESS name too ("Borrower A Road
# Carrier" is a real filing's actual borrower). A genuine address line
# carries some OTHER tell an address has and a bare business name does not -
# a house/shop/plot number or a pincode, or comma-separated locality parts.
_ADDRESS_DETAIL_RE = re.compile(r"\d|,")


def _looks_like_address(cand: str) -> bool:
    if _PIN_RE.search(cand) or cand.count(",") >= _ADDRESS_PARTS:
        return True
    return bool(_ADDRESS_RE.search(cand) and _ADDRESS_DETAIL_RE.search(cand))


# "(Proprietor : Borrower S)" / "Partner : ..." sits directly above the
# heading in a proprietorship/partnership layout, one line below the actual
# business name. It reads perfectly well as an entity on its own - passes
# every other check in _find_entity - so without this it wins outright and
# the workbook titles itself after the proprietor instead of the business.
# Confirmed on a real filing (FC6 / Borrower C): without
# Vision, the resolved entity degraded to just this parenthetical.
# "Prop." (not spelled out) is a common abbreviation on a real filing (FC4,
# Borrower A) - without it, only the spelled-out form is caught.
_PROPRIETOR_LINE_RE = re.compile(
    r"^\(?\s*(?:proprietors?|prop|partners?|owners?)\s*[:.]", re.I)


def _find_entity(lines: list, idx: int) -> str:
    """
    The entity name printed above the heading.

    Skips the address line and any neighbouring statement heading - the same
    page often stacks two accounts, so the line directly above a Balance Sheet
    is frequently the previous statement's own title, not the entity. Also
    looks PAST a bare "(Proprietor : <name>)" line for the business name one
    line further up, falling back to the proprietor line only if nothing else
    in the lookback window qualifies.
    """
    fallback = ""
    for j in range(idx - 1, max(-1, idx - 1 - _ENTITY_LOOKBACK), -1):
        cand = re.sub(r"\s+", " ", lines[j]).strip(" .:-|=\t")
        if len(cand) < 4 or _looks_like_address(cand) or _SKIP_RE.match(cand):
            continue
        # Reject a neighbouring heading regardless of length - _match_title's
        # own length cap is about opening blocks, not about what counts as a
        # name, and a long heading directly above would otherwise be taken as
        # the entity.
        if any(rx.search(cand) for rx in _TITLES):
            continue
        # Skip lines that are mostly digits (a stray figure above the heading).
        if sum(c.isdigit() for c in cand) > len(cand) * 0.3:
            continue
        if _PROPRIETOR_LINE_RE.match(cand):
            fallback = fallback or cand
            continue
        return cand
    return fallback


# A numeric date written with a two-digit year - "FOR THE YEAR ENDED
# 31.03.26" (Borrower D' provisional set), "as at 31/03/25".
# _PERIOD_RE wants four digits, so such a statement had NO period at all:
# it could not be pooled with the other file's Balance Sheet for the same
# year and became its own undated column. Widened only inside a complete
# numeric date, never for a bare two-digit number, which is how a note
# reference or a stray figure would otherwise read as a year.
_SHORT_YEAR_RE = re.compile(r"(?<!\d)(\d{1,2}\s*[./-]\s*\d{1,2}\s*[./-]\s*)(\d{2})(?!\d)")


# The same with a month NAME - "1-Apr-25 to 31-Mar-26", Tally's own period
# line. Still only inside a complete date: day, month name, then the year.
_SHORT_YEAR_NAMED_RE = re.compile(
    r"(?<!\d)(\d{1,2}\s*[-/.\s]\s*[A-Za-z]{3,9}\.?\s*[-/.,\s]\s*)(\d{2})(?!\d)")


def _widen_short_year(line: str) -> str:
    def widen(m):
        return f"{m.group(1)}{'19' if int(m.group(2)) >= 70 else '20'}{m.group(2)}"
    return _SHORT_YEAR_NAMED_RE.sub(widen, _SHORT_YEAR_RE.sub(widen, line))


def _find_period(lines: list, idx: int):
    """
    (ending_year, raw_text) for the statement's period.

    Checked on the heading line first: layouts split roughly evenly between
    "BALANCE SHEET / as on 31st March 2024" and the single-line
    "BALANCE SHEET AS ON 31 ST MARCH 2026", and only looking below the heading
    misses every instance of the latter.
    """
    for j in range(idx, min(len(lines), idx + 1 + _PERIOD_LOOKAHEAD)):
        m = _PERIOD_RE.search(_widen_short_year(lines[j]))
        if m:
            return int(m.group(3)), re.sub(r"\s+", " ", lines[j]).strip()
    return None, ""


# A statement too long for one page. Tally bridges the break with its own
# running subtotal - "Carried Over" at the foot of one page, "Brought
# Forward" at the head of the next - and prints the real Total only at the
# end. Read a page at a time (the old assumption: each statement is printed
# whole), the first half can never balance because its other side continues
# overleaf, and the second half reads as a separate statement whose two
# brought-forward figures fit no row at all. Confirmed on a real filing (YES
# A CUSTOMER FY2026: Balance Sheet across two pages, Rs 21.05 crore of
# "Brought Forward" excluded and the statement failed by Rs 26.76 crore).
#
# The bridge rows themselves are NOT items - they restate what is already
# above - so both are dropped as the pages are joined.
_CARRIED_RE = re.compile(r"^\s*carried\s*(?:over|forward|fwd)\b", re.I)
_BROUGHT_RE = re.compile(r"^\s*brought\s*(?:forward|over|fwd)\b|^\s*b/?f\b", re.I)
# Tally's other spelling of the same break: no running subtotal at all, just
# "continued ..." at the foot and the statement's heading repeated overleaf.
# The P&L of a real filing (A CUSTOMER) breaks this way, and its whole
# Indirect Expenses block - Rs 1.24 crore, including Rs 86.98 lakh of
# employee cost - sat on the far side of the break, with the statement's only
# real Total. Profit read Rs 292.50 lakh against a printed Rs 176.75 lakh.
_CONTINUED_RE = re.compile(r"^\s*\(?\s*continued\s*[.\u2026]*\s*\)?\s*$", re.I)


def _row_matching(rows: list, rx) -> int:
    for i, (_y, cells) in enumerate(rows):
        if rx.match(" ".join(t for _a, _b, t in cells).strip()):
            return i
    return None


def _join_continuations(page_rows: list) -> list:
    """
    Join a statement that runs onto the next page, at its own Carried Over /
    Brought Forward bridge. The continuation page is emptied rather than
    removed, so every other page keeps its number.

    Deliberately requires BOTH halves of the bridge: two unrelated statements
    printed back to back have neither, and stay separate.
    """
    pages = [list(rows or []) for rows in (page_rows or [])]
    for i in range(len(pages) - 1):
        cur = pages[i]
        if not cur:
            continue
        carried = _row_matching(cur, _CARRIED_RE)
        continued = _row_matching(cur, _CONTINUED_RE)
        if carried is None and continued is None:
            continue
        for j in range(i + 1, len(pages)):      # the next page holding anything
            if pages[j]:
                break
        else:
            continue
        nxt = pages[j]
        brought = _row_matching(nxt, _BROUGHT_RE)
        if brought is not None:
            start = brought + 1
        elif continued is not None and _repeats_heading(nxt):
            # No bridge figure: the continuation simply reprints the heading.
            # Everything above its first figure is that repeated header.
            start = next((k for k, (_y, cells) in enumerate(nxt)
                          if _row_has_amount(cells)), None)
            if start is None:
                continue
        else:
            continue
        cut = carried if carried is not None else continued
        # Keep the joined rows in reading order: the continuation's own y
        # coordinates start again at the top of its page.
        drop = max((y for y, _c in cur), default=0) + 1000
        pages[i] = cur[:cut] + [(y + drop, cells) for y, cells in nxt[start:]]
        pages[j] = []
    return pages


def _row_has_amount(cells: list) -> bool:
    return any(_cell_amount(t.strip()) is not None for _a, _b, t in cells)


_HEADING_SCAN = 4


def _repeats_heading(rows: list) -> bool:
    """Does this page open by reprinting a statement's own heading? That is
    what marks it a continuation rather than the next statement."""
    return any(_match_title(" ".join(t for _a, _b, t in cells))
               for _y, cells in rows[:_HEADING_SCAN])


# A column header naming its period's year: "2025", "As at 31st March, 2025",
# "31.03.2024". Read only ABOVE the statement's first figure - a year printed
# further down is a line item's, not a column's.
_HEADER_YEAR_RE = re.compile(r"(?<![\d,.])((?:19|20)\d{2})(?![\d,])")
# "1-Apr-2023 to 31-Mar-2024" is ONE period, and it ends in 2024. Tally heads
# every statement this way; read year by year, the range's START year was
# taken and Borrower L's FY2024 statements were dated 2023 (69 rows off a
# sheet that had matched in full). Within such a cell only the end counts.
_PERIOD_RANGE_RE = re.compile(r"\d.*?\s(?:to|till|upto|[-\u2013\u2014])\s.*\d", re.I)


def _header_years(rows: list) -> list:
    """
    The years a statement's own column headers name, left to right - this
    period first, then its comparatives. The heading is the usual source of a
    statement's year, but a heading can print no date, or one the period
    pattern cannot parse; the columns still say which year each holds.
    """
    found = []
    for _y, cells in rows:
        if _row_has_amount(cells):
            break
        for x0, _x1, txt in sorted(cells, key=lambda c: c[0]):
            wide = _widen_short_year(txt)
            ys = [int(m.group(1)) for m in _HEADER_YEAR_RE.finditer(wide)]
            if ys and _PERIOD_RANGE_RE.search(wide):
                ys = ys[-1:]            # a range: the period it ENDS in
            found.extend((x0, y) for y in ys)
    years = []
    for _x, y in sorted(found):
        if y not in years:
            years.append(y)
    return years


def find_blocks(page_rows: list, only_pages=None) -> list:
    """
    Locate every financial-statement block across the document.

    `page_rows[i]` is the page's [(y, [(x0, x1, text), ...]), ...] from
    layout.rows_with_cells.

    Returns a list of dicts:
        kind        - BALANCE_SHEET | PROFIT_LOSS
        entity      - the name printed above the title
        year        - the period's ending year, if stated
        period      - the raw period line
        page        - page index the title sits on
        sides       - {"left": [(label, amount)...], "right": [...]}
        printed_total - the total the statement itself prints, if found
    """
    wanted = None if only_pages is None else set(only_pages)
    blocks = []
    for page_no, rows in enumerate(_join_continuations(page_rows)):
        if not rows or (wanted is not None and page_no not in wanted):
            continue
        lines = [" ".join(t for _a, _b, t in cells) for _y, cells in rows]
        page_width = max((c[1] for _y, cells in rows for c in cells), default=1) or 1

        titles = [i for i, ln in enumerate(lines) if _match_title(ln)]
        for n, idx in enumerate(titles):
            end   = titles[n + 1] if n + 1 < len(titles) else len(lines)
            body  = rows[idx + 1:end]
            year, period = _find_period(lines, idx)
            header_years = _header_years(rows[idx + 1:end])
            if year is None and header_years:
                # No readable date in the heading: the columns name the years.
                year = header_years[0]
                period = period or f"column header {year}"
            foot = _footer_at(body)
            if foot is not None:
                body = body[:foot]
            total, total_at = _printed_total(body)
            # Only an unambiguous "Balance Sheet" heading is passed on - the
            # case where a generic check picked the wrong reading. A vaguer
            # heading keeps the old behaviour.
            hint = (BALANCE_SHEET
                    if _classify(lines[idx], {"left": [], "right": []}) == BALANCE_SHEET
                    else None)
            sides = _harvest(body, page_width, total, total_at,
                            title_cells=rows[idx][1], kind_hint=hint)
            # A heading with no body - typically a statement's title reprinted
            # above the one that follows it. Not a statement, just noise.
            if not _has_items(sides):
                continue

            # _harvest_segmented found this to be a combined statement (a
            # Trading Account feeding a Profit & Loss Account, each with its
            # own printed subtotal) and verified each half independently.
            # The single `total` above only ever closed the SECOND half, so
            # it no longer describes the merged reading - the sum of every
            # segment's own (already-proven) total does.
            if "segmented_total" in sides:
                total = sides.pop("segmented_total")

            kind = _classify(lines[idx], sides)
            _tag_pl_sides(kind, sides)

            # A vertical statement's comparative-column grand totals (see
            # _harvest_vertical) - a free second reading of the PRIOR year,
            # printed right next to this one. Popped before rescaling so it
            # doesn't ride along inside `sides` for check_block/all_items to
            # ever see, then scaled the same way every other figure on this
            # page is (it was harvested in the statement's own printed units).
            comparative = sides.pop("comparative", None)
            comp_sections = sides.pop("comparative_sections", None)

            # Everything downstream works in whole rupees.
            scale = _unit_scale(lines, idx)
            _rescale(sides, scale)
            if total is not None:
                total = int(round(total * scale))
            if comparative:
                comparative = {k: int(round(v * scale)) for k, v in comparative.items()}

            if comp_sections:
                for sec in comp_sections:
                    sec["items"] = [(l, int(round(a * scale))) for l, a in sec["items"]]
                    if sec["total"] is not None:
                        sec["total"] = int(round(sec["total"] * scale))

            blocks.append({
                "unit_scale":    scale,
                "kind":          kind,
                "title":         re.sub(r"\s+", " ", lines[idx]).strip(),
                "entity":        _find_entity(lines, idx),
                "year":          year,
                "period":        period,
                "page":          page_no,
                "sides":         sides,
                "printed_total": total,
                "comparative":   ({"year": year - 1, "totals": comparative}
                                  if comparative and year else None),
            })

            # The comparative column as a statement of the PRIOR year. It
            # carries the same labels, so it maps like any other statement,
            # and it is checked against its own printed totals rather than
            # trusted for being adjacent. Marked so the caller can prefer a
            # real statement for that year (columns.spread_many).
            if comp_sections and year and _has_items({"sections": comp_sections}):
                prior_year = (header_years[1]
                              if len(header_years) > 1 and header_years[1] < year
                              else year - 1)
                prior = dict(blocks[-1])
                prior.update({
                    "year":             prior_year,
                    "period":           f"comparative column of: {period}"[:120],
                    "sides":            {"sections": comp_sections},
                    "printed_total":    None,
                    "comparative":      None,
                    "from_comparative": True,
                })
                blocks.append(prior)
    return blocks


# ─────────────────────────────────────────────────────────────────
# SCHEDULES  -  the breakdown behind a statement's group lines
# ─────────────────────────────────────────────────────────────────
# A proprietorship's P&L face often prints only "Direct Expenses (Sch 8)
# 4,69,84,240" and "Indirect Expenses (Sch 9) 22,69,814"; the salary, the
# loan interest and the transport charges the spread needs live on the
# schedule pages behind them. Read only from the face, a real filing (Borrower E
# Enterprises FY2023) reported Employee Costs and Interest as zero - no
# interest cover, and 2.88 crore of wages filed as transport.
#
# A schedule is recognised by its own numbered heading ("Sch 08 Direct
# Expenses", "Note 15 : Employee benefits expense", "Schedule E - Cash") and
# is only trusted when its lines add up to the Total it prints itself - the
# same arithmetic proof the statements get.
_SCHEDULE_HEAD_RE = re.compile(
    r"^\s*(?i:sch(?:edule)?|note|annexure)\s*\.?\s*(?i:no\.?\s*)?[-:]?\s*"
    # The number: 1-2 digits, "o7" (OCR's zero), or ONE capital letter.
    # Case-sensitive on purpose: "Notes forming part..." must not read its
    # "s" as schedule "S".
    # The name may open with an accented OCR letter ("Schedule H : Éxpenses").
    r"(\d{1,2}|[oO]\d|[A-Z])(?![A-Za-z0-9])\s*[.:\-)'\"]*\s*([^\W\d_].*)$")


def _schedule_heading(line: str):
    flat = re.sub(r"\s+", " ", line).strip()
    m = _SCHEDULE_HEAD_RE.match(flat)
    if not m or _MONEY_RE.search(flat):
        return None
    name = m.group(2).strip(" .:-|")
    if not re.search(r"[A-Za-z]{3}", name):
        return None
    return m.group(1).upper().replace("O", "0"), name


def _schedule_row(cells: list):
    """(label, [amounts]) for one schedule row. A label cell may carry its own
    amount welded on ("Transport Charges Paid 1,73,42,490")."""
    labels, amounts = [], []
    for _a, _b, txt in _split_amount_runs(cells):
        t = txt.strip()
        if _has_label_text(t):
            m = _AMOUNT_RE.search(t)
            if m and _has_label_text(t[:m.start()]):
                amounts.append(_clean_amount(m.group(1)))
                t = t[:m.start()]
            labels.append(t.strip())
        else:
            amt = _cell_amount(t)
            if amt is not None:
                amounts.append(amt)
    return " ".join(labels).strip(" .:-|"), amounts


def _schedule(number, name, caption, page, items, total, scale) -> dict:
    items = [(l, int(round(a * scale))) for l, a in items]
    total = None if total is None else int(round(total * scale))
    verified = (total is not None and abs(sum(a for _l, a in items) - total)
                <= max(TOLERANCE, len(items)) * scale)
    return {"number": number, "name": name, "caption": caption, "page": page,
            "items": items, "total": total, "verified": verified, "scale": scale}


def find_schedules(page_rows: list) -> list:
    """
    Every numbered schedule / note in the document, one entry per GROUP:
        {"number", "name", "caption", "page", "items": [(label, rupees)],
         "total", "verified"}
    `verified` means the lines sum to the group's own printed Total. Only
    the FIRST amount on a line is the line's own - a second one is the
    running group total printed beside the group's last line ("Salary and
    Wages 2,87,60,157 | 4,69,84,240"), or last year's comparative.

    One heading can hold several groups, each under its own caption and
    closed by its own Total - Borrower I's "Schedule H : Expenses"
    is "Direct expenses ... Total" then "Indirect expenses ... Total". So a
    Total closes the current group, not the schedule, and `caption` is the
    label-only line that opened the group.
    """
    out = []
    for page_no, rows in enumerate(page_rows or []):
        if not rows:
            continue
        lines = [" ".join(t for _a, _b, t in cells) for _y, cells in rows]
        heads = [(i, h) for i, ln in enumerate(lines) if (h := _schedule_heading(ln))]
        for n, (idx, (number, name)) in enumerate(heads):
            end = heads[n + 1][0] if n + 1 < len(heads) else len(rows)
            scale = _unit_scale(lines, idx)
            caption, items = "", []
            for _y, cells in rows[idx + 1:end]:
                label, amounts = _schedule_row(cells)
                if not amounts:
                    if label and not items:
                        caption = label     # the group's own sub-heading
                    continue
                if not label:
                    continue                # a running subtotal
                if _TOTAL_RE.match(label):
                    if items:
                        out.append(_schedule(number, name, caption, page_no,
                                             items, amounts[0], scale))
                    caption, items = "", []
                    continue
                items.append((label, amounts[0]))
            if items:
                out.append(_schedule(number, name, caption, page_no, items, None, scale))
    return out


# Only a GROUP line is replaced by its schedule - one that names a class of
# expense rather than an expense. "Other expenses" is deliberately excluded:
# analysts keep it as one row, and its schedule's lines (bank charges,
# audit fees) would otherwise scatter into buckets the reference never uses.
_EXPANDABLE_RE = re.compile(
    r"\b(?:direct|indirect|operating|operational|operation|administrative|"
    r"admin|establishment|general|selling|manufacturing)\b.*\bexp", re.I)
_NAME_STOP = {"expenses", "expense", "xpenses", "account", "charges", "other",
              "particulars", "amount", "total"}


def _name_words(label: str) -> set:
    label = re.sub(r"^\[[A-Z]+\]\s*", "", label)
    return {w.lower() for w in re.findall(r"[A-Za-z]{4,}", label)} - _NAME_STOP


def expand_with_schedules(block: dict, schedules: list) -> list:
    """
    Replace a P&L group line with its schedule's own lines, in place, when the
    schedule proves itself: verified against its printed Total, that Total
    equal to the group line's amount, and a name word in common ("Indirect
    Expenses" / "Sch 09 Indirect Expenses"). The block's totals are
    unchanged by construction, so its balance proof still holds. The line's
    section tag ("[EXP]") is carried onto every replacement line.

    Returns the names of the schedules used, for the audit trail.
    """
    if block.get("kind") != PROFIT_LOSS or not schedules:
        return []
    used, taken = [], set()
    face = [(it[0], it[1]) for it in all_items(block["sides"])]

    def _lines_for(label, amount, s):
        """The schedule's lines standing for this face line, or None."""
        tol = max(TOLERANCE, s["scale"], block.get("unit_scale", 1))
        kids = list(s["items"])
        extra = s["total"] - amount
        if abs(extra) <= tol:
            return kids
        # The schedule may also list a line the face prints on its own:
        # Borrower I's "Indirect expenses" schedule includes Depreciation,
        # which its P&L shows separately - schedule 110.01 cr = face 94.43 cr
        # + Depreciation 15.59 cr. Dropped only when those lines account for
        # the whole difference, so the proof still holds.
        dup = [k for k in kids
               if any(fl != label and abs(k[1] - fa) <= tol
                      and _name_words(k[0]) & _name_words(fl) for fl, fa in face)]
        if dup and abs(sum(k[1] for k in dup) - extra) <= tol:
            return [k for k in kids if k not in dup]
        return None

    def _expand(items):
        out = []
        for it in items:
            label, amount = it[0], it[1]
            pick = None
            if _EXPANDABLE_RE.search(label):
                want = _name_words(label)
                for n, s in enumerate(schedules):
                    if (n in taken or not s["verified"]
                            or not want & _name_words(f"{s['name']} {s['caption']}")):
                        continue
                    kids = _lines_for(label, amount, s)
                    if kids is not None:
                        pick = (n, kids)
                        break
            if pick is None:
                out.append(it)
                continue
            n, kids = pick
            taken.add(n)
            tag = re.match(r"^\[[A-Z]+\]\s*", label)
            prefix = tag.group(0) if tag else ""
            out.extend((prefix + l, a) for l, a in kids)
            s = schedules[n]
            used.append(f"{s['caption'] or s['name']} (p{s['page'] + 1})")
        return out

    sides = block["sides"]
    if sides.get("sections"):
        for s in sides["sections"]:
            s["items"] = _expand(s["items"])
    else:
        for side in ("left", "right"):
            if side in sides:
                sides[side] = _expand(sides[side])
    return used


# A cell holding an amount immediately followed by text is two columns whose gap
# fell just under the column threshold - "522115.00 Furniture & Fixtures" is the
# left side's amount welded to the right side's label. Split it back apart.
#
# The second half is NOT required to start with a letter (\D.* is too strict):
# a scanned statement's roman-numeral note markers ("II", "I") routinely OCR
# as digit-led noise ("1I", "n1"), so "30,000.00 1I Rent received" starts its
# label half with what reads as a digit. Requiring a non-digit first character
# missed the split entirely, welding a real expense line into the neighbouring
# income label and losing it from its own side - the statement's total then
# came up short by exactly that dropped amount. The looser pattern below still
# only splits where the tail contains a real word (checked separately, below),
# so a second genuine number never gets mistaken for a label.
# Separator after the amount is OPTIONAL (`\s*`, not `\s+`): a real filing
# welded "64,26,780.00As per Schedule" with NO space at all between the
# liabilities-side figure and the assets-side label it happened to sit next
# to on the page, and requiring at least one space missed the split
# entirely - the whole liabilities entry (a proprietor's capital account)
# vanished, rather than just losing its label's word-break.
_WELDED_RE = re.compile(r"^(\(?\s*[\d,\s]+(?:[.,]\d{1,3})?\s*\)?)\s*(.+)$")

# An Indian vehicle registration - "MH-12-PQ-9115", "Mh 12 Nx 3915",
# "Mh-12-1615". A transport business's Fixed Assets schedule is a column of
# nothing else, and none of them contains a three-letter run, which is what
# every "is this a label?" test here used to require. On a real filing (a
# Tally-exported Balance Sheet, Borrower L AY 2024-25) every vehicle label
# was therefore invisible, and each vehicle's amount was handed to whatever
# LIABILITY label sat on the same printed row - the Balance Sheet failed by
# ~9.7 lakh with vehicle values filed as capital.
_VEHICLE_REG_RE = re.compile(
    r"\b[A-Za-z]{2}[\s-]*\d{1,2}[\s-]*(?:[A-Za-z]{1,3}[\s-]*)?\d{3,4}\b")


def _has_label_text(txt: str) -> bool:
    """Does this cell carry label text (as opposed to only a figure)?"""
    return bool(re.search(r"[A-Za-z]{3}", txt) or _VEHICLE_REG_RE.search(txt))


# A cell holding nothing but two or more money figures - "24,18,44,856
# 12,32,46,427": this year and last year, their word boxes close enough to
# fuse into one cell.
# Comma-grouped, or a bare run of 3+ digits: a CA's depreciation working is
# often typed without separators ("12973 116759.00", Borrower J FY2023) - which
# the grouped-only form never split. Safe because a cell is only split when
# it holds NOTHING but such figures; a note reference ("1 26") is too short
# to match, and a label never matches at all.
_AMOUNT_TOKEN = (r"\(?-?\s*(?:\d{1,3}(?:,\d{2,3})+|\d{3,})(?:\.\d{1,3})?\s*\)?")
_AMOUNT_RUN_RE = re.compile(rf"^{_AMOUNT_TOKEN}(?:\s+{_AMOUNT_TOKEN})+$")


def _split_amount_runs(cells: list) -> list:
    """
    Split a cell of several figures into one cell per figure, each given its
    share of the cell's width in order - so each lands under its own period
    column. A vertical statement picks this year's figure by position; left
    fused, the cell's LAST figure (last year's) was the one read. Confirmed
    on a real filing (Borrower F FY2025, Balance Sheet: "Long-term
    borrowings 24,18,44,856 12,32,46,427" read as 12.32 crore).
    """
    out = []
    for x0, x1, txt in cells:
        t = txt.strip()
        if not _AMOUNT_RUN_RE.match(t):
            out.append((x0, x1, txt))
            continue
        toks = re.findall(_AMOUNT_TOKEN, t)
        total_len = sum(len(k) for k in toks) or 1
        pos = x0
        for k in toks:
            w = (x1 - x0) * len(k) / total_len
            out.append((pos, pos + w, k.strip()))
            pos += w
    return out


def _split_cells(cells: list) -> list:
    out = []
    for x0, x1, txt in cells:
        m = _WELDED_RE.match(txt.strip())
        if m and _has_label_text(m.group(2)):
            # Apportion the x-range by character count - approximate, but the
            # only thing it decides is which side of the page-midpoint each
            # part lands on, and the two parts sit either side of it by
            # construction.
            frac = len(m.group(1)) / max(len(txt.strip()), 1)
            xm   = x0 + (x1 - x0) * frac
            out.append((x0, xm, m.group(1).strip()))
            out.append((xm, x1, m.group(2).strip()))
        else:
            out.append((x0, x1, txt))
    return out


# Amount x-positions within this fraction of the page width belong to the same
# printed column.
_COL_CLUSTER_FRAC = 0.06


def _cluster_columns(entries: list) -> list:
    """[(x, label, amount)] → list of columns, each a list of those entries."""
    if not entries:
        return []
    entries = sorted(entries, key=lambda e: e[0])
    span = max(e[0] for e in entries) - min(e[0] for e in entries)
    tol  = max(span, 1) * _COL_CLUSTER_FRAC + 1
    cols, cur = [], [entries[0]]
    for e in entries[1:]:
        if e[0] - cur[-1][0] <= tol:
            cur.append(e)
        else:
            cols.append(cur)
            cur = [e]
    cols.append(cur)
    return cols


def _pick_columns(cols: list, target):
    """
    Choose which amount columns actually belong in the account.

    A T-account routinely prints working columns beside the real one:

        Furniture & Fixtures   65,980
        Less Dep @ 10 %         6,598      59,382   ← only this enters the total

    Guessing by position is fragile (which column is "outer" varies), so
    instead every combination of columns is scored against the figure the
    statement itself prints, and the closest wins. The arithmetic doesn't just
    check the extraction - it selects it.

    Ties break toward fewer columns, then the rightmost, which is what a
    working-columns layout looks like.
    """
    if not cols:
        return []
    if target is None:
        # Nothing to solve against - take every column and let check_block
        # report whether the sides agree.
        return [e for col in cols for e in col]

    best, best_key = None, None
    for mask in range(1, 1 << len(cols)):
        chosen = [cols[i] for i in range(len(cols)) if mask >> i & 1]
        entries = [e for col in chosen for e in col]
        diff = abs(sum(e[2] for e in entries) - target)
        key = (diff, len(chosen), -max(e[0] for e in entries))
        if best_key is None or key < best_key:
            best, best_key = entries, key
    return best or []


def _harvest(rows: list, page_width: float, printed_total, total_at=None,
            title_cells=None, kind_hint=None) -> dict:
    """
    Collect (label, amount) pairs per side of the account.

    Side is decided by x-position against the account's own divider, not by
    cell index: rows carry different numbers of cells depending on which
    entries are blank, so "third cell on the row" means nothing consistent.

    A vertical (Schedule III) statement is routed to _harvest_vertical instead
    and comes back as sections rather than sides.

    `total_at` is the row index of the closing-total footer. A T-account must
    drop it - harvesting a statement's own total alongside its items
    double-counts the statement into itself. A vertical statement must NOT:
    there the TOTAL row is the section anchor every check is made against, and
    dropping it leaves the section unverifiable.

    `title_cells` is the heading row's own cells, used to seed each side's
    section context before any body row is read. A bank-format Balance Sheet
    can print its two column captions ("CAPITAL ACCOUNT OF :" / "FIXED
    ASSETS :") joined into one line that _match_title recognises as the
    block's own heading rather than a body row - the heading text is real,
    but having become the title it can never again act as an in-body group
    heading, so the section context it would have set is seeded here instead.
    """
    body = rows[:total_at] if total_at is not None else rows

    t_best, t_score = None, None
    for divider in _divider_candidates(body, page_width):
        sides = _harvest_at(body, divider, printed_total, title_cells)
        score = _score_split(sides, printed_total)
        if t_score is None or score < t_score:
            t_best, t_score = sides, score
    t_best = t_best or {"left": [], "right": []}

    vert = _harvest_vertical(rows)
    v_ok  = any(s["items"] for s in vert["sections"])
    vert  = {"left": [], "right": [], **vert} if v_ok else None

    # Orientation detection gets the answer right nearly always, but when it
    # doesn't the cost is silently wrong numbers. Both readings are cheap, and
    # each one can be checked against the statement's own totals - so compute
    # both and keep whichever actually reconciles, falling back to the detector
    # only when neither does (or both do).
    preferred = vert if _orientation(rows) == VERTICAL and vert else t_best
    other     = t_best if preferred is vert else vert
    # Checked as the statement its heading plainly names (see find_blocks),
    # not as a generic block. Checked generically, a vertical mis-read of a
    # two-sided Balance Sheet "verified" on one big section that reconciled
    # to the page's total - and won, although the real Balance Sheet check
    # (which needs both grand totals) then rejected it. On a real filing
    # (Borrower A, both years) the T-account reading reconciled
    # exactly at every candidate divider and was never used.
    if _verifies(preferred, kind_hint):
        return preferred
    if other is not None and _verifies(other, kind_hint):
        return other

    # Neither verified, but they are not equally wrong. A vertical mis-parse
    # of a genuine two-sided T-account concatenates labels from BOTH sides
    # into meaningless merged section names - if that leaves NOTHING even
    # checkable (no section prints a total over 2+ items), it cannot be a
    # better answer than a T-account reading that at least harvested real
    # figures on both sides. Confirmed on a real filing (FC6, Borrower C
    # Transport Service AY 2024-25): a Balance Sheet with no period-column
    # header - so orientation had already fallen through to its weaker
    # heuristic - was misread as vertical, producing one garbage merged
    # label; the T-account reading of the same rows was off by only one
    # duplicated subtotal against an otherwise-correct ~11.13 crore total.
    if preferred is vert and _prefer_t_account_over_empty_vertical(t_best, vert):
        preferred = t_best

    # Neither the T-account nor the vertical reading reconciles. One more
    # shape is worth trying before giving up on this block: a single heading
    # can cover TWO sequential T-accounts - a Trading Account whose Gross
    # Profit is carried down to open a Profit & Loss Account - each closing
    # with its own printed subtotal. Read as one continuous account, every
    # row from both halves gets summed against a total that was only ever
    # meant to close the SECOND half, and the check fails even though every
    # individual figure was read correctly. Deterministic, not a model call:
    # the split point is the statement's OWN printed subtotal, not a guess.
    segmented = _harvest_segmented(body, page_width, printed_total)
    if segmented is not None:
        return segmented

    combined = _harvest_combined(body, page_width, title_cells)
    if combined is not None:
        return combined

    return preferred


def _prefer_t_account_over_empty_vertical(t_best: dict, vert: dict) -> bool:
    """
    Is the T-account reading strictly the better fallback here?

    Only true when the T-account side has real data on BOTH sides (an empty
    side means it found nothing either, so there is nothing to prefer it
    for) AND the vertical reading found not one section worth checking (no
    section prints its own total over 2+ items - `_check_sections`' own bar
    for "checkable"). A vertical guess that clears that bar, even without
    fully reconciling, may still be the genuine statement shape; this only
    catches the case where it plainly is not.
    """
    if not (t_best.get("left") and t_best.get("right")):
        return False
    return not any(s.get("total") is not None and len(s.get("items") or []) >= 2
                  for s in vert.get("sections", []))


def _harvest_segmented(body: list, page_width: float, final_total) -> dict:
    """
    Split `body` at every internal subtotal row and verify each resulting
    segment independently against ITS OWN subtotal - the last segment's
    against `final_total` (the block's own printed close), each earlier one
    against the subtotal row that ends it.

    Returns None unless EVERY segment balances. A partial success is not
    trusted: a genuinely single-account statement can coincidentally contain
    an early row that happens to print two equal amounts, and that must not
    be mistaken for a real sub-account boundary. Requiring all segments to
    reconcile is what makes it safe to adopt this reading at all - the same
    "only trust it if it proves itself" rule the Vision fallback uses.

    On success, the merged reading's own left/right sums no longer equal the
    single `final_total` the caller started with (they equal the SUM of every
    segment's total instead) - "segmented_total" carries that corrected
    figure back to find_blocks, which uses it in place of the original
    printed_total when recording this block.
    """
    if final_total is None:
        return None
    marks = _subtotal_rows(body)
    if not marks:
        return None

    segments, start = [], 0
    for idx, total in marks:
        segments.append((body[start:idx], total))
        start = idx + 1
    segments.append((body[start:], final_total))
    if len(segments) < 2:
        return None

    merged = {"left": [], "right": []}
    for seg_rows, seg_total in segments:
        if not seg_rows:
            return None
        sides = _harvest(seg_rows, page_width, seg_total, None)
        if not _verifies(sides):
            return None
        merged["left"]  += sides.get("left", [])
        merged["right"] += sides.get("right", [])

    merged["segmented_total"] = sum(t for _r, t in segments)
    return merged


def _verifies(sides: dict, kind=None) -> bool:
    """Does this reading reconcile against the statement's own totals? `kind`
    applies that statement's own stricter checks when the heading names it."""
    if not sides or not _has_items(sides):
        return False
    return check_block({"sides": sides, "kind": kind or OTHER})["balanced"]


def _score_split(sides: dict, printed_total) -> tuple:
    """
    How well a candidate split reconciles. Lower is better. A split that leaves
    one side empty is ranked last unless nothing else is on offer - a genuine
    single-column statement still scores, it just never beats a real two-column
    reading of the same rows.
    """
    left  = sum(a for _l, a in sides["left"])
    right = sum(a for _l, a in sides["right"])
    lopsided = 0 if (sides["left"] and sides["right"]) else 1
    if printed_total is not None:
        err = abs(left - printed_total) + abs(right - printed_total)
        if lopsided:
            err = abs(max(left, right) - printed_total)
    else:
        err = abs(left - right)
    return (lopsided, err)


# A Note No. is a bare 1-2 digit reference sitting between the label and the
# figures. Never an amount worth reporting, and always small.
_NOTE_MAX = 99

# Schedule III group headings, and the token each contributes to the labels
# underneath it.
#
# The heading is often the ONLY thing that says what an item is. A real filing
# lists "(a) Term Borrowings" under "4. Current Liabilities" - the CA dropped
# the word "Short". Read on its own the label says long-term debt; read in its
# section it is plainly a current liability, which is where the analyst's own
# spreading sheet puts it. Ordered most specific first, since "Non - Current
# Liabilities" contains "Current Liabilities".
_SECTIONS = [
    ("NCL", re.compile(r"non\s*-?\s*current\s+liabilit", re.I)),
    ("NCA", re.compile(r"non\s*-?\s*current\s+asset", re.I)),
    ("CL",  re.compile(r"\bcurrent\s+liabilit", re.I)),
    ("CA",  re.compile(r"\bcurrent\s+asset", re.I)),
    # A proprietorship's capital line is often just the owner's NAME
    # ("an advocate  5,22,115") under a "Proprietor's Capital Account"
    # heading. Nothing in the label itself says capital, so without the heading
    # the figure is unclassifiable and the whole equity line disappears.
    #
    # Deliberately NOT "equity and liabilities" (removed after a real Vision
    # read exposed it): that phrase is the whole LEFT SIDE'S umbrella caption
    # in a layout with no Schedule III sub-headings ("Equity and liabilities:
    # Capital account / Loans / Current liabilities" all under one banner,
    # nothing further) - it says "here comes the liabilities side", not
    # "this item is equity". Tagging every item beneath it [EQ] folded
    # Loans and Current Liabilities into equity_capital wholesale, on a real
    # filing overstating equity by the entire liabilities side and leaving
    # the real secured-loan and current-liability figures unreported.
    # "Shareholders' funds" is kept: unlike the umbrella phrase, that heading
    # genuinely names a NARROW equity-only sub-section.
    ("EQ",  re.compile(r"shareholders?'?\s+fund|"
                       r"(?:proprietor|partners?|owner)'?s?\s+capital|"
                       r"^\s*capital\s+a/?c(?:count)?\b", re.I)),
    # Prefixed by a roman numeral ("IV EXPENSES", "III. Expenses:") or a
    # bracketed letter ("B] Expenditure :-", Borrower E' summary P&L),
    # and "Expenditure" as well as "Expenses". Without the letter form and
    # the word, that P&L's two sections were both just "Total" and neither
    # could be recognised as its income or its expense side.
    ("EXP", re.compile(r"^\s*(?:[A-Z]{1,4}\s*[\].):]\s*|[IVX]{1,4}\s+)?"
                       r"expen(?:ses?|diture)\s*[:\-\s]*$", re.I)),
    ("INC", re.compile(r"^\s*(?:[A-Z]{1,4}\s*[\].):]\s*|[IVX]{1,4}\s+)?"
                       r"(?:income|revenue)\s*[:\-\s]*$", re.I)),
    # A proprietorship's own bank-format Balance Sheet routinely groups its
    # liabilities/assets under bare section captions like "SECURED LOAN :" /
    # "FIXED ASSETS :" rather than Schedule III wording, and the individual
    # lines underneath are lender or debtor NAMES ("Bandhan Finance HL",
    # "a lender") or a bare "As per Schedule" placeholder pointing at
    # an attached (unread) annexure - neither carries any vocabulary of its
    # own to map by. On a real filing this left the entire liabilities side
    # of an otherwise correctly-verified Balance Sheet unmapped and excluded
    # from every downstream figure.
    ("SL",  re.compile(r"^\s*secured\s+loan\s*:?\s*$", re.I)),
    ("UL",  re.compile(r"^\s*unsecured\s+loan\s*:?\s*$", re.I)),
    ("FA",  re.compile(r"^\s*fixed\s+assets?\s*:?\s*$", re.I)),
    ("INV", re.compile(r"^\s*investments?\s*:?\s*$", re.I)),
    # Generic "somewhere on the liabilities side" - NOT a specific bucket.
    # "Equity and liabilities" names the whole left side in a layout with no
    # further sub-headings, so a bare "Loans" line under it carries no
    # vocabulary of its own to say secured vs. unsecured. LIAB only ever
    # gives such a line a same-side hint (see the LIAB fallback rule in
    # template_config.py) - it must never be confused with EQ, which means
    # "this item itself is equity", or the exact overstatement bug this
    # section exists to prevent comes right back for a different token.
    # Tally's own primary-group caption for borrowings, "Loans (Liability)",
    # holds BOTH secured and unsecured ledgers - a generic liabilities hint,
    # not a bucket. Without it the capital section's [EQ] context ran on
    # down the side and filed "Secured Loans" as equity.
    ("LIAB", re.compile(r"\bequity\s*(?:&|and)\s*liabilit|"
                        r"^\s*loans?\s*\(\s*liabilit", re.I)),
]


# Tally names its retained-profit group "Profit & Loss A/c" and prints it on
# WHICHEVER SIDE the balance falls: a credit balance down the liabilities
# side (A CUSTOMER FY2026 - Rs 1.76 crore of retained profit, which
# without a tag inherited the Current Liabilities context above it and was
# spread as a current liability), a debit balance among the ASSETS (Borrower A
# Road Carrier FY2024 - Rs 310.29 lakh of accumulated loss, which the
# analyst's own sheet carries as an asset).
#
# So the name alone cannot say what it is; the side it sits on does. It is
# read as equity only on a side that has already shown equity or liability
# groups - never on the assets side.
_PL_GROUP_RE = re.compile(r"^\s*profit\s*(?:&|and)\s*loss(?:\s*a/?c(?:count)?)?\s*$", re.I)
_LIABILITY_SIDE_TOKENS = {"EQ", "LIAB", "SL", "UL", "CL", "NCL"}


def _section_token(label: str):
    for token, rx in _SECTIONS:
        if rx.search(label):
            return token
    return None


# A bare 1-2 digit cell: either a Note No. reference or a genuinely small
# amount. Nothing in the text itself tells them apart (see _AMOUNT_RE's
# exactly-3-digit fallback) - only WHERE it sits does.
_SMALL_BARE_RE = re.compile(r"^\(?\s*-?\s*(\d{1,2})\s*\)?$")


def _period_right_edge(rows: list, cur, prior: list):
    """
    Where this period's figures END on the page - the median right edge of
    the amounts already readable without ambiguity.

    Statement columns are RIGHT-aligned, so every figure of one period shares
    a right edge, while the Note No. column keeps its own far to the left.
    That is what separates a real Rs 63 from "Note 63"; the digit count
    cannot (Borrower D FY2025, docs/SHORTCOMINGS.md Case 28).
    """
    if not cur:
        return None
    edges = []
    for _y, cells in rows:
        best, best_d = None, None
        for x0, x1, txt in _split_amount_runs(cells):
            t = txt.strip()
            if re.search(r"[A-Za-z]{3}", t) or not _AMOUNT_RE.search(t):
                continue
            if any(x0 < p[1] and x1 > p[0] for p in prior):
                continue
            d = abs((x0 + x1) / 2 - (cur[0] + cur[1]) / 2)
            if best_d is None or d < best_d:
                best, best_d = x1, d
        if best is not None:
            edges.append(best)
    if not edges:
        return None
    edges.sort()
    return edges[len(edges) // 2]


def _current_period_amount(cells: list, cur, prior: list, cur_right=None):
    """
    The current period's figure on this row.

    Chosen by proximity to the current-period column rather than by strict
    overlap with it: Schedule III headers wrap ("Note | For the year ended
    March" / "No. | 31, 2024 | 2023"), so the header cell's x-range is only
    approximately above the figures it labels, and demanding overlap drops
    rows whose amounts are perfectly readable. Columns belonging to a
    comparative period are excluded outright - those are last year's numbers.
    """
    best, best_d = None, None
    tol = None if not cur else max(4.0, 0.15 * (cur[1] - cur[0]))
    for x0, x1, txt in cells:
        txt = txt.strip()
        m = _AMOUNT_RE.search(txt)
        if re.search(r"[A-Za-z]{3}", txt):
            continue
        small = None
        if not m:
            # A genuinely small amount ("63") is invisible to _AMOUNT_RE,
            # which only takes a bare integer of exactly 3 digits so that a
            # Note No. is never harvested as money. Admitted here only when
            # it ends where this period's other figures end - the note
            # column's own right edge is nowhere near.
            sm = _SMALL_BARE_RE.match(txt)
            if not (sm and cur_right is not None and abs(x1 - cur_right) <= tol):
                continue
            small = float(sm.group(1))
        if any(x0 < p[1] and x1 > p[0] for p in prior):
            continue
        val = small if small is not None else _clean_amount(m.group(1))
        if val is None:
            continue
        signed = (-val if _is_negative(txt, txt[:m.start()] if m else "")
                  else val)
        if cur is None:
            # No header to go on - skip a bare note reference, take the first
            # real figure.
            if abs(signed) <= _NOTE_MAX and "," not in txt and "." not in txt:
                continue
            return signed
        d = abs((x0 + x1) / 2 - (cur[0] + cur[1]) / 2)
        if best_d is None or d < best_d:
            best, best_d = signed, d
    return best


def _drop_restated_parent(items: list, total):
    """
    A Schedule III line printed WITH its own breakdown underneath:

        (b) Trade Payables                                  278.85
            (A) total outstanding dues of micro enterprises    -
            (B) total outstanding dues of other creditors  278.85

    Both the line and its breakdown carry figures, so harvesting both counts
    the payables twice - on a real filing (Borrower H FY2025) Current
    Liabilities summed to 1,129.51 lakh against a printed 850.65, over by
    exactly 278.85. Dropped only when the arithmetic proves it: the section
    is over its printed total by one line's amount, and the lines right after
    that one add up to it. (Figures are in the statement's printed units
    here, before rescaling.)
    """
    if total is None or len(items) < 2:
        return items
    excess = sum(a for _l, a in items) - total
    if abs(excess) <= 0.011 * len(items):
        return items
    for i, (_l, a) in enumerate(items):
        if abs(a - excess) > 0.011 * len(items):
            continue
        run = 0
        for j in range(i + 1, len(items)):
            run += items[j][1]
            if abs(run - a) <= 0.011 * (j - i):
                return items[:i] + items[i + 1:]
    return items


def _harvest_vertical(rows: list) -> dict:
    """
    Harvest a Schedule III statement: labels down the left, one column per
    period, sections closed by a TOTAL row.

    Only the CURRENT period's column is taken. It is located from the header
    row rather than by "first amount on the line", because a Note No. column
    sits between the label and the figures ("(a) Share Capital | 2 | 1,00,000 |
    -") and reading positionally would harvest the note number as the amount.

    Unlabelled figures are skipped: those are the sub-totals a vertical
    statement prints under each group, and counting them alongside the items
    they summarise double-counts the whole section.
    """
    # Which columns are OTHER PERIODS depends on how the columns were found.
    #
    # From the header, every column after the first names a comparative period,
    # so all of them are excluded. From the figures alone we cannot tell a
    # comparative period from an indented SUBTOTAL column - and Schedule III
    # prints its subtotals in exactly such a column. Treating those as a prior
    # period threw away the "Total Revenue" and "Total Expense" figures, which
    # are the anchors the whole statement is checked against; the P&L then read
    # perfectly but could never be proved. Only the rightmost column is a
    # comparative; anything between it and the items belongs to this period.
    header_cols = _period_columns(rows)
    cols = header_cols if len(header_cols) >= 2 else _infer_columns(rows)
    cur  = cols[0] if cols else None

    # Two different exclusion rules, because two different kinds of row.
    #
    # An ITEM's figure is in the item column, so every other column is off
    # limits - a line that is nil this year still prints last year's number,
    # and admitting it reports the comparative as though it were current.
    #
    # A SUBTOTAL's figure is NOT in the item column: Schedule III indents
    # "Total Revenue" and "Total Expense" into a column of their own. Excluding
    # everything but the item column threw those away, and they are the anchors
    # the statement is checked against - the P&L then read perfectly but could
    # never be proved. So for subtotal rows only the rightmost column (the
    # genuine comparative period) is excluded.
    prior_item   = cols[1:] if len(cols) > 1 else []
    prior_anchor = (header_cols[1:] if len(header_cols) >= 2
                    else (cols[-1:] if len(cols) >= 2 else []))

    # The immediate comparative column - a free second reading of the PRIOR
    # year's own grand totals, printed right next to this year's. Captured
    # only for the anchors matched below (Total Revenue/Income, Total
    # Expenses, Total Assets, Total Equity and Liabilities) - never for
    # ordinary line items, which a real filing showed a new auditor
    # reclassifying between categories (long-term vs short-term borrowings)
    # across filings while the grand totals held. See has_both_statement_kinds's
    # neighbour in relevance.py for the same "totals are trustworthy,
    # sub-buckets are not" principle applied to a different problem.
    # Where this period's figures end, so a small bare figure in that column
    # can be told from a Note No. (see _period_right_edge).
    cur_right = _period_right_edge(rows, cur, prior_item)

    prior_col = cols[1] if len(cols) > 1 else None
    _comparative_others = ([c for c in cols if c is not prior_col]
                           if prior_col else [])
    comparative = {}
    # The prior column read line by line, not just at its anchors - a
    # provisional set can be the only record of last year (Borrower D
    # FY2026). Emitted by find_blocks as a statement of its own, so it is
    # checked against its own printed totals; a real statement for that year
    # always wins over it (columns.spread_many).
    prior_right = (_period_right_edge(rows, prior_col, _comparative_others)
                   if prior_col else None)
    comp_sections, comp_current = [], []

    sections, current, anchors = [], [], []
    section_token = None
    section_heading = None
    last_derived = None     # amount of the last result row skipped (see below)
    dangling = None         # first line of a caption wrapped onto the next row
    for _y, cells in rows:
        cells = sorted(_split_amount_runs(cells), key=lambda c: c[0])
        # Join every label fragment on the row, don't just take the first.
        # Schedule III wraps long captions across cells, and "(a) Short - |
        # Term Borrowings" read as only "(a) Short -" loses the word that
        # decides whether the figure is a current liability or a term loan.
        parts = [t for _a, _b, t in cells if re.search(r"[A-Za-z]{3}", t)]
        label = re.sub(_AMOUNT_RE.pattern, "", " ".join(parts)).strip(" .:-|")

        # A caption wrapped onto the next line: the first line ends on a
        # connecting word and carries no figure ("Net Profit / Loss
        # Transferred to" / "E] Proprietor's Capital Account 32,69,400",
        # Borrower E). Read apart, the second half looked like a
        # line item and the first was dropped as a heading; joined, it is
        # the P&L's result line and is recognised as one.
        if dangling:
            if label:
                label = f"{dangling} {label}"
            dangling = None
        if label and not any(_AMOUNT_RE.search(t.strip()) for _a, _b, t in cells) \
                and re.search(r"(?:\b(?:to|of|and|for|from|in|on)|[&/-])\s*$", label, re.I):
            dangling = label
            continue

        is_anchor = bool(label and _ANCHOR_RE.match(label))
        amount = _current_period_amount(
            cells, cur, prior_anchor if is_anchor else prior_item, cur_right)

        if is_anchor and amount is not None:
            # A bare "Total" (its "(A)"/"(B)" marker is too short to survive
            # as label text) takes the name of the heading it closes, so the
            # P&L check can tell "Total Income" from "Total Expenditure".
            name = label
            if section_heading and re.fullmatch(r"\s*(?:gross\s+)?total\s*", label, re.I):
                name = "Total " + re.sub(
                    r"^\s*(?:[A-Z]{1,4}\s*[\].):]\s*|[IVX]{1,4}\s+)", "",
                    section_heading).strip(" :-")
            sections.append({"name": name, "items": _drop_restated_parent(current, amount),
                             "total": amount})
            anchors.append(amount)
            if prior_col:
                prior_total = _current_period_amount(
                    cells, prior_col, _comparative_others, prior_right)
                if comp_current or prior_total is not None:
                    comp_sections.append({
                        "name": name,
                        "items": _drop_restated_parent(comp_current, prior_total),
                        "total": prior_total})
                comp_current = []
                for key, pat in _COMPARATIVE_ANCHOR_PATTERNS.items():
                    if pat.search(label):
                        prior_amt = _current_period_amount(
                            cells, prior_col, _comparative_others)
                        if prior_amt is not None:
                            comparative[key] = prior_amt
                        break
            current = []
            section_token = None
            section_heading = None
            continue

        # A heading carries no figure of its own; it sets the context for the
        # rows beneath it.
        if label and amount is None:
            # ... but a line that is NIL this year and real last year
            # ("(b) Deferred tax | - | (13,69,133)") is a heading only for
            # THIS period. Without this the comparative year lost every such
            # line - sample_d's FY2025 deferred tax credit among them, which
            # left that year's loss overstated by Rs 13.69 lakh.
            if prior_col:
                prior_only = _current_period_amount(
                    cells, prior_col, _comparative_others, prior_right)
                if prior_only is not None:
                    comp_label = (f"[{section_token}] {label}" if section_token
                                  else label)
                    if _ANCHOR_RE.match(label):
                        comp_sections.append({
                            "name": label,
                            "items": _drop_restated_parent(comp_current, prior_only),
                            "total": prior_only})
                        comp_current = []
                    else:
                        comp_current.append((comp_label, prior_only))
            token = _section_token(label)
            if token:
                section_token = token
                # Remembered so an UNLABELLED subtotal below (see next branch)
                # can be named after the heading it actually closes.
                section_heading = label
            continue

        # A Schedule III subsection's own subtotal is usually printed with NO
        # label at all - just the figure, indented under the item column
        # ("(a) Share capital ... / (b) Reserves ... / [blank] 1,440.78").
        # Skipping it outright (the old behaviour, on the reasoning that
        # counting it again would double the section) also threw away the
        # one number that PROVES that subsection - so a mismatch anywhere
        # inside "Shareholders' Funds" surfaced only as a shortfall against
        # the statement's single distant grand total, never pointing at
        # which of the three subsections it was actually in. Naming it after
        # the heading just above it turns that into a per-subsection check,
        # the same proof already applied to every LABELLED anchor here.
        if not label and amount is not None and section_heading and current:
            sections.append({"name": section_heading,
                             "items": _drop_restated_parent(current, amount),
                             "total": amount})
            anchors.append(amount)
            current = []
            section_token = None
            section_heading = None
            continue

        if label and amount is not None and not _SKIP_RE.match(label):
            if _DERIVED_ROW_RE.search(label):
                last_derived = amount
                continue
            # The same result figure printed again a line or two further down
            # is a restatement, not a new item. OCR set Borrower M's
            # FY2025 profit after tax (Rs 303 lakh) a second time against
            # "XVI Deffered Tax (Liability) - Earlier Year"; counted, it was
            # Rs 303 lakh of deferred tax and wiped out the year's profit.
            if last_derived is not None and abs(amount - last_derived) <= TOLERANCE:
                continue
            if section_token:
                label = f"[{section_token}] {label}"
            current.append((label, amount))
            if prior_col:
                prior_amount = _current_period_amount(
                    cells, prior_col, _comparative_others, prior_right)
                if prior_amount is not None:
                    comp_current.append((label, prior_amount))

    if current:
        sections.append({"name": section_heading or "", "items": current,
                         "total": None})
    if comp_current:
        comp_sections.append({"name": section_heading or "", "items": comp_current,
                              "total": None})

    out = {"sections": sections, "comparative": comparative}
    if comp_sections:
        out["comparative_sections"] = comp_sections
    return out


def _harvest_at(rows: list, mid: float, printed_total, title_cells=None,
                joint: bool = False) -> dict:
    """Harvest with the sides split at `mid`. `joint` picks both sides'
    columns so they agree with each other rather than with `printed_total`
    (see _harvest_combined)."""
    raw = {"left": [], "right": []}
    page_span = max((c[1] for _y, cells in rows for c in cells), default=1) or 1
    carried = {"left": "", "right": ""}   # last label seen, for amount-only cells
    # A figure with no usable label on its own side, waiting for the caption
    # printed just BELOW it. Scans often set a figure one line above its
    # caption: on a real filing (Borrower F FY2023) "8,26,44,874" sat
    # above "Fixed Assets" and was harvested as "(unlabelled)" - Rs 8.26 crore
    # of fixed assets then fitted no row - and "5,67,565" / "97,150" took the
    # PREVIOUS caption instead of "Investment" / "Deposits" below them. The
    # next caption-only cell on that side (a caption with no figure of its
    # own) names the pending figure; any figure-bearing line cancels it.
    pending = {"left": None, "right": None}
    section = {"left": "", "right": ""}   # last group heading, per side
    seen = {"left": set(), "right": set()}   # every token this side has shown

    # Seed section context from the heading row itself (see _harvest's
    # docstring) - split by the SAME divider used for the body, so "CAPITAL
    # ACCOUNT OF :" and "FIXED ASSETS :" land on the sides they actually
    # head even though they were reconstructed onto one joined title line.
    if title_cells:
        for x0, x1, txt in title_cells:
            txt = txt.strip(" .:-|")
            if not txt:
                continue
            token = _section_token(txt)
            if token:
                where = "left" if (x0 + x1) / 2 < mid else "right"
                section[where] = token
                seen[where].add(token)

    for _y, cells in rows:
        # A bare amount belongs to the label on ITS OWN ROW, and takes that
        # label's side - not the side its own x-position falls on. A liability
        # and an asset can print their figures in near-identical columns
        # ("Sundry Creditors  16,950" vs "Sundry Debtors  35,260"); only the
        # label says which side of the account each one is.
        row_label, row_side = "", None
        # _split_amount_runs first: a CA's depreciation working often prints
        # the depreciation and the resulting net value fused in one cell
        # ("LESS : DEP | 12973 116759.00"). Left whole, only its last figure
        # was read and given the cell's LEFT edge - which sits in the
        # gross/depreciation working column - so the net values ended up
        # split across two columns and no set of columns reconciled (Borrower J /
        # Borrower J FY2023: assets over by Rs 7,37,587). Split, each
        # net value sits with the other nets and the column picker finds them.
        for x0, x1, txt in _split_cells(_split_amount_runs(cells)):
            txt = txt.strip()
            if not txt or _SKIP_RE.match(txt):
                continue
            side = "left" if (x0 + x1) / 2 < mid else "right"
            parsed = parse_line(txt)
            if parsed:
                lab = parsed[0]
                if section[side]:
                    lab = f"[{section[side]}] {lab}"
                raw[side].append((x0, lab, parsed[1]))
                row_label, row_side = lab, side
                pending[side] = None
                continue
            m = _AMOUNT_RE.search(txt)
            bare = _BARE_LONG_INT_RE.match(txt) if not m else None
            if (m or bare) and not _has_label_text(txt):
                if m:
                    val = _clean_amount(m.group(1))
                    neg = _is_negative(txt, txt[:m.start()])
                else:
                    val = _clean_amount(bare.group(2))
                    neg = bool(bare.group(1))
                if val is None:
                    continue
                # Prefer this row's own label; fall back to the last section
                # heading seen on the column's side ("Fixed Assets:").
                #
                # Except when the figure sits FAR across the divider from
                # that label. The row-label rule exists for figures printed
                # near the divider, where a liability's and an asset's
                # columns can nearly touch. But an asset figure printed one
                # row above its own caption lands on a row whose only label
                # is a liability: on a real filing (Borrower F FY2023 and
                # FY2024) "Investment 5,67,565" and "Deposits 97,150" were
                # filed as liabilities and the assets side came up short by
                # exactly their sum. Far across the divider, position wins.
                use_side  = row_side or side
                far_side  = bool(row_side and row_side != side and
                                 abs((x0 + x1) / 2 - mid) > 0.15 * page_span)
                if far_side:
                    use_side, row_label = side, ""
                use_label = row_label or carried[use_side] or "(unlabelled)"
                if section[use_side] and not use_label.startswith("["):
                    use_label = f"[{section[use_side]}] {use_label}"
                raw[use_side].append((x0, use_label, -val if neg else val))
                # No label of its own on this side: wait for the caption below.
                pending[use_side] = (len(raw[use_side]) - 1
                                     if (far_side or not row_label) else None)
            elif _has_label_text(txt):
                heading = txt.strip(" .:-|")
                if _LESS_DEP_RE.match(heading) and carried[side]:
                    # This reduces the PRECEDING row's asset to a net figure -
                    # it does not name a new item. Keep that asset's name in
                    # force (do not overwrite `carried`), and drop the gross
                    # amount its own row recorded: the net figure on THIS row
                    # replaces it, rather than the asset ending up with both
                    # its gross cost AND its net value counted, under the
                    # label "Less: Dep" instead of its own name.
                    if raw[side] and raw[side][-1][1] == carried[side]:
                        raw[side].pop()
                    row_label, row_side = carried[side], side
                    continue
                carried[side] = heading
                row_label, row_side = heading, side
                # A label-only cell that names a group sets the context for the
                # entries beneath it on that side of the account.
                token = _section_token(heading)
                if (token is None and _PL_GROUP_RE.match(heading.strip())
                        and seen[side] & _LIABILITY_SIDE_TOKENS):
                    token = "EQ"        # retained profit, on the liabilities side
                if token:
                    section[side] = token
                    seen[side].add(token)
                # ...and names a figure printed just above it, if one is
                # waiting (see `pending`) - but only when this caption has no
                # figure of its own on the same row.
                if pending[side] is not None and not any(
                        _cell_amount(t.strip()) for _a, _b, t in cells):
                    i = pending[side]
                    _x, _old, amt = raw[side][i]
                    new = f"[{section[side]}] {heading}" if section[side] else heading
                    raw[side][i] = (_x, new, amt)
                    pending[side] = None

    if joint:
        picked = _pick_joint(_cluster_columns(raw["left"]),
                             _cluster_columns(raw["right"]),
                             accept=joint if callable(joint) else None)
        if picked is None:
            return {"left": [], "right": []}
        chosen = dict(zip(("left", "right"), picked))
    else:
        chosen = {side: _pick_columns(_cluster_columns(raw[side]), printed_total)
                  for side in ("left", "right")}
    return {
        side: [(lab, amt) for _x, lab, amt in
               _expand_groups(raw[side], chosen[side])]
        for side in ("left", "right")
    }


def _expand_groups(entries: list, chosen: list) -> list:
    """
    Replace a group total with its own breakdown, when the breakdown proves it.

    Tally (the accounting package most small Indian businesses use) prints
    each primary group's TOTAL in the outer column and its ledgers in an inner
    column beneath it:

        Direct Expenses          2,00,53,326   <- chosen: outer column
            Fuel Charges   90,60,500
            Salary & Wages 18,27,696
            Transport Chg  91,65,130

    _pick_columns rightly chooses the outer column (it is what reconciles), but
    stopping there loses the breakdown the spread needs: on a real filing,
    Depreciation (81.4 lakh) and Bank Interest (32.7 lakh) sat inside
    "Indirect Expenses" and would have been reported as zero - wrong cash
    profit, no interest cover.

    So after the columns are chosen, a chosen entry is replaced by the
    unchosen entries that FOLLOW it, but only by the shortest run of them
    that sums exactly (within TOLERANCE) to its amount. The side's total is
    unchanged by construction, so the balance proof still holds; a CA's
    working columns ("Furniture 65,980 / Less Dep 6,598 / 59,382") never sum
    to the NEXT figure, so they are left alone.

    `entries` is the side's full harvest in row order; `chosen` the subset
    _pick_columns kept (same tuple objects).
    """
    keep = {id(e) for e in chosen}
    out, i = [], 0
    while i < len(entries):
        e = entries[i]
        if id(e) not in keep:
            i += 1
            continue
        j, kids = i + 1, []
        while j < len(entries) and id(entries[j]) not in keep:
            kids.append(entries[j])
            j += 1
        taken, run = None, 0
        for n, k in enumerate(kids, 1):
            run += k[2]
            if abs(run - e[2]) <= TOLERANCE:
                taken = kids[:n]
                break
        out.extend(taken or [e])
        i = j
    return out


def _pick_joint(cols_l: list, cols_r: list, accept=None):
    """
    Column subsets for BOTH sides at once, chosen so the two sides agree -
    for a statement whose printed total cannot be used as the target (see
    _harvest_combined). Fewest columns wins, so the outer (group-total)
    columns are preferred over inner working columns.

    `accept(left_entries, right_entries) -> bool` rejects agreeing readings
    that are not the account at all. Needed, not optional: on a real Tally
    P&L the Trading half's own closing total (Rs 3,69,85,488) is printed as a
    lone figure on each side, and those two single-entry "columns" agree with
    each other trivially - fewer columns than the real reading, and nothing
    like the statement. Returns (left_entries, right_entries) or None.
    """
    if not cols_l or not cols_r or len(cols_l) > 8 or len(cols_r) > 8:
        return None

    def _subsets(cols):
        for mask in range(1, 1 << len(cols)):
            ch = [cols[i] for i in range(len(cols)) if mask >> i & 1]
            ents = [e for col in ch for e in col]
            yield sum(e[2] for e in ents), len(ch), ents

    rights = list(_subsets(cols_r))
    found = []
    for sl, nl, ents_l in _subsets(cols_l):
        for sr, nr, ents_r in rights:
            if abs(sl - sr) <= TOLERANCE:
                key = (nl + nr, -max(e[0] for e in ents_l) - max(e[0] for e in ents_r))
                found.append((key, ents_l, ents_r))
    for _key, ents_l, ents_r in sorted(found, key=lambda f: f[0]):
        if accept is None or accept(ents_l, ents_r):
            return ents_l, ents_r
    return None


# The transfer between the two halves of a combined Trading and P&L account:
# "Gross Profit c/o" closing the Trading half, "Gross Profit b/f" opening the
# P&L half - the same figure on both sides.
_TRANSFER_RE = re.compile(r"gross\s+(?:pr\w{1,4}t|loss)|\b[cb]\s*/\s*[ofd]\b|\btrf\b",
                          re.I)


def _harvest_combined(body: list, page_width: float, title_cells=None):
    """
    A Trading Account and a P&L Account printed under ONE heading, with a
    single printed "Total" that closes only the P&L half - Tally's standard
    "Profit & Loss A/c" layout. Harvested against its printed total, the
    Trading half can never fit; _harvest_segmented cannot split it either,
    because Tally prints the Trading half's own closing total on two
    different rows (left and right figures offset by a line), so no row
    shows the equal pair it looks for. Confirmed on a real filing (Borrower L AY 2024-25): the P&L failed "short by 2.37 crore" and the year
    reported no P&L at all.

    Instead the two sides are made to agree with EACH OTHER across the whole
    account (_pick_joint). That identity holds for the combined account -
    the carried-down gross profit appears once on each side - and it is
    accepted only when a matching transfer pair ("Gross Profit c/o" /
    "Gross Profit b/f" of equal amount) is actually present, which is what
    makes this a combined account rather than a coincidence. The agreed sum
    becomes the block's total, the same way _harvest_segmented reports one.
    """
    def _has_transfer(ents_l, ents_r):
        carried = {round(e[2]) for e in ents_l if _TRANSFER_RE.search(e[1])}
        return any(round(e[2]) in carried
                   for e in ents_r if _TRANSFER_RE.search(e[1]))

    for divider in _divider_candidates(body, page_width):
        sides = _harvest_at(body, divider, None, title_cells, joint=_has_transfer)
        if not (sides["left"] and sides["right"]):
            continue
        sides["segmented_total"] = sum(a for _l, a in sides["left"])
        return sides
    return None


def _cell_amount(text: str):
    """
    A cell's trailing amount, including a WHOLE-CELL bare integer with no
    separators ("168012440"). _AMOUNT_RE alone misses the latter, which is
    how a real filing (Borrower A FY2025, every figure printed
    without commas) never had its closing TOTAL row recognised: the total
    was then harvested as a line item on both sides, with the unlabelled
    expense subtotal as a third, and the P&L failed by Rs 16.5 crore.
    A four-digit YEAR is not accepted this way - "2024 | 2025" in a header
    would otherwise read as a matching pair of totals 1 apart.
    """
    m = _AMOUNT_RE.search(text)
    if m:
        return _clean_amount(m.group(1))
    b = _BARE_LONG_INT_RE.match(text)
    if b and not (len(b.group(2)) == 4 and 1900 <= int(b.group(2)) <= 2100):
        return float(b.group(2))
    return None


def _printed_total(rows: list):
    """
    The statement's own total, as (value, row_index) so the caller can drop the
    footer row before harvesting. In a T-account the total is the figure
    repeated under both sides ("441,784,183.00  441,784,183.00"); in a vertical
    statement it is the line labelled Total. (None, None) when neither reads.
    """
    for i in range(len(rows) - 1, -1, -1):
        # Split first: a fused "21,84,30,367 13,42,85,686" (this year and
        # last year in one cell) otherwise welds into one 18-digit figure.
        texts   = [t.strip() for _a, _b, t in _split_amount_runs(rows[i][1])]
        amounts = [a for t in texts if (a := _cell_amount(t))]
        if len(amounts) >= 2 and abs(amounts[0] - amounts[-1]) <= 2:
            return amounts[0], i
        if amounts and _TOTAL_RE.match(" ".join(texts)):
            return max(amounts), i
    return None, None


def _subtotal_rows(rows: list) -> list:
    """
    Every row that looks like a printed subtotal - two equal amounts, side by
    side - in the order they appear. `_printed_total` only needs the LAST
    one; splitting a combined statement into its true sub-accounts needs
    all of them (see _harvest_segmented).
    """
    out = []
    for i, (_y, cells) in enumerate(rows):
        texts   = [t.strip() for _a, _b, t in cells]
        amounts = [a for t in texts if (a := _cell_amount(t))]
        if len(amounts) >= 2 and abs(amounts[0] - amounts[-1]) <= 2:
            out.append((i, amounts[0]))
    return out


# ─────────────────────────────────────────────────────────────────
# ORIENTATION  -  T-account vs vertical (Schedule III)
# ─────────────────────────────────────────────────────────────────
# Two structurally different families turn up, and confusing them silently
# produces wrong numbers rather than an error:
#
#   T-ACCOUNT (proprietor / partnership, CA-typed) - liabilities left, assets
#   right, both sides carrying labels, one period.
#
#   VERTICAL (company, Companies Act Schedule III) - one column of labels, then
#   a Note No. column, then ONE COLUMN PER PERIOD. The two amount columns are
#   this year and last year, NOT two sides of an account. Splitting such a page
#   down the middle files last year's figures as though they were assets.
#
# Orientation is decided by where the labels are: a T-account has them on both
# sides, a vertical statement only on the left.

T_ACCOUNT = "t_account"
VERTICAL  = "vertical"

# Column headers that name a period, e.g. "As at March 31, 2024",
# "For the year ended March 31, 2024", "31/03/2024", "2023-24".
_PERIOD_COL_RE = re.compile(
    r"(?:as\s*at|as\s*on|for\s*the\s*(?:year|period)|year\s*end\w*|"
    r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\b(?:19|20)\d{2}\b)", re.I)

# A row is a section anchor when its label is a Total. Vertical statements print
# sub-totals unlabelled and section totals as "TOTAL" - often numbered, as
# "III Total Revenue (I+II)" or "4. Total Expenses".
# "(?=[A-Z])" as well as \b: OCR can glue "Total" to the next word
# ("TotalAssets", Borrower H FY2024) - left unrecognised, the grand total was
# harvested as a Rs 24.19 crore line item instead of anchoring the check.
# "total outstanding dues of micro enterprises ..." / "... of creditors
# other than micro enterprises and small enterprises" is how Schedule III
# spells out TRADE PAYABLES: two line items that happen to OPEN with the
# word "total", not a section total. Read as anchors they closed the
# Current liabilities group - the payables figure became a section total
# with no items (so it vanished from the sheet) and the lines below it
# lost their [CL] tag, which spread a current tax LIABILITY as the P&L's
# tax expense. On a real filing (Borrower D FY2025) Total of
# Liabilities came up Rs 499 lakh short of Total of Assets and the year's
# loss was overstated by Rs 66.75 lakh.
_ANCHOR_RE = re.compile(
    r"^\s*(?:[IVX]{1,4}[\s.)]+|\(?\d{1,2}\)?[\s.)]+)?"
    r"(?:total|gross\s+total)(?!\s+outstanding\s+dues)(?:\b|(?=[A-Z]))", re.I)

# Grand-total anchors worth recovering from a comparative column - deliberately
# the same small set check_block's own BS/P&L grand-total checks already key
# on, not every "Total ..." row (a bare "Total" closing a sub-group like
# "Shareholders' funds" is not safe to recover the same way).
_COMPARATIVE_ANCHOR_PATTERNS = {
    "total_revenue":            re.compile(r"total\s+revenue|total\s+income\b", re.I),
    "total_expenses":           re.compile(r"total\s+expenses?\b", re.I),
    "total_assets":             re.compile(r"total\s+assets\b", re.I),
    "total_equity_liabilities": re.compile(
        r"total\s+equity\s+(?:and|&)\s+liabilit|"
        r"total\s+liabilit\w*\s+(?:and|&)\s+equity", re.I),
}

# The T-account divider is found in this band of the page. Outside it, a wide
# gap is just an indented label or a lone figure, not the split between the two
# sides of the account.
_DIVIDER_BAND = (0.30, 0.70)


# How many rows at the top of a statement can make up its column header.
# Schedule III headers wrap freely ("Note | For the year ended March" on one
# row, "No. | 31, 2024 | 2023" on the next), and some layouts put each period
# on its own row, so the header is gathered across a band rather than found on
# a single line.
_HEADER_BAND = 10


def _period_columns(rows: list) -> list:
    """
    x-ranges of the period columns, gathered from the statement's header band
    and clustered by position. Returned left to right, which for Schedule III
    is current period first, comparative after.

    Empty when fewer than two period columns are present - which is itself the
    signal that this is a T-account, not a vertical statement.
    """
    def _cells(cells, max_frac=1.0, width=None):
        return [(x0, x1) for x0, x1, t in cells
                if _PERIOD_COL_RE.search(t) and not _MONEY_RE.search(t)
                and (width is None or (x1 - x0) <= width * max_frac)]

    # A single row carrying both period headers is the clean case, and is
    # trusted directly - clustering it against other rows only risks merging
    # the columns back together.
    for _y, cells in rows[:_HEADER_BAND]:
        row_hits = _cells(cells)
        if len(row_hits) >= 2:
            return sorted(row_hits)

    # Otherwise gather across the header band. Cells spanning most of the page
    # are wrapped headers holding several columns' text at once ("Note For the
    # year ended March For the year ended March 31,"); their x-range covers
    # every column, so including them collapses the clustering to one column.
    page_width = max((c[1] for _y, cells in rows for c in cells), default=0)
    hits = []
    for _y, cells in rows[:_HEADER_BAND]:
        hits.extend(_cells(cells, 0.35, page_width))
    if len(hits) < 2:
        return []

    hits.sort()
    span = hits[-1][1] - hits[0][0]
    tol  = max(span * 0.10, 5)
    cols, cur = [], list(hits[0])
    for x0, x1 in hits[1:]:
        if x0 - cur[1] <= tol:
            cur[1] = max(cur[1], x1)
        else:
            cols.append(tuple(cur))
            cur = [x0, x1]
    cols.append(tuple(cur))
    return cols if len(cols) >= 2 else []


def _infer_columns(rows: list) -> list:
    """
    Period columns inferred from where the FIGURES sit, for statements whose
    header cannot be resolved (a wrapped header can land both period captions
    in a single cell spanning the whole page).

    Amount cells are clustered by x; clusters that hold only small bare
    integers are Note No. references and are dropped. What remains is one
    cluster per period, left to right.

    This is also more correct than reading positionally, not merely a fallback.
    When a line is nil for the current year the row still prints the
    comparative ("II Other Income | - | 683"), and "first number on the line"
    would report last year's 683 as this year's income - a wrong figure that
    balances nowhere and reads as real.
    """
    page_width = max((c[1] for _y, cells in rows for c in cells), default=0)
    if not page_width:
        return []

    found = []
    for _y, cells in rows:
        for x0, x1, txt in _split_amount_runs(cells):
            txt = txt.strip()
            m = _AMOUNT_RE.search(txt)
            if not m or re.search(r"[A-Za-z]{3}", txt):
                continue
            val = _clean_amount(m.group(1))
            if val is None:
                continue
            note_like = abs(val) <= _NOTE_MAX and not re.search(r"[.,]", txt)
            found.append((x1, x0, x1, note_like))
    if not found:
        return []

    # Clustered on the RIGHT edge, not the centre. Amounts in a financial
    # statement are right-aligned, so a column's figures share x1 almost
    # exactly while their centres scatter with the number of digits - centre
    # clustering split a single column of figures into several, and the extra
    # phantom columns were then treated as comparative periods and discarded.
    found.sort()
    tol = page_width * 0.02
    clusters, cur = [], [found[0]]
    for f in found[1:]:
        if f[0] - cur[-1][0] <= tol:
            cur.append(f)
        else:
            clusters.append(cur)
            cur = [f]
    clusters.append(cur)

    out = []
    for cl in clusters:
        if len(cl) < 2 or all(f[3] for f in cl):
            continue          # too sparse to be a column, or a Note No. column
        out.append((min(f[1] for f in cl), max(f[2] for f in cl)))
    return out


def _orientation(rows: list) -> str:
    """
    T-account or vertical, decided by whether the statement heads its amount
    columns with PERIODS.

    That is the actual difference between the two families: Schedule III prints
    "As at March 31, 2024 | As at March 31, 2023" and its columns are periods;
    a T-account prints "Particulars | Amount | Particulars | Amount" and its
    columns are the two sides of the account.

    An earlier version counted labels either side of the divider and called it
    vertical when the right side had few - which misread a genuine T-account
    P&L whose credit side held a single entry ("Gross Receipt") against eleven
    debits. How many entries a side happens to have says nothing about the
    layout; what the column headers say does.

    When the header itself is unreadable, the same question is answered from
    the layout: BOTH families have two amount columns, so counting columns
    settles nothing. What separates them is whether labels sit BETWEEN those
    columns. A T-account's second column of figures is preceded by its own
    entries ("... 72,000 | Gross Receipt | 9,85,600"); a vertical statement's
    second column is last year's figures with nothing but whitespace before it.
    """
    if len(_period_columns(rows)) >= 2:
        return VERTICAL

    cols = _infer_columns(rows)
    if not cols:
        return T_ACCOUNT

    # ONE amount column is the normal shape for a company's first year - there
    # is no comparative period to print. Requiring two columns misread every
    # such statement as a T-account and harvested the heading years as figures.
    # The question is the same either way: is there anything LABELLED to the
    # right of the figures? Only a T-account has that.
    gap_start = cols[0][1]
    gap_end   = cols[1][1] if len(cols) > 1 else float("inf")

    for _y, cells in rows:
        for x0, x1, txt in _split_cells(cells):
            txt = txt.strip()
            if len(re.findall(r"[A-Za-z]", txt)) < 4:
                continue
            if _SKIP_RE.match(txt) or _PERIOD_COL_RE.search(txt):
                continue
            if x0 >= gap_start - 2 and x1 <= gap_end:
                return T_ACCOUNT
    return VERTICAL


def _divider_candidates(rows: list, page_width: float) -> list:
    """
    Plausible x-positions for the split between the two sides of the account.

    NOT just the page midpoint: a T-account's left side puts its amounts in a
    column that frequently sits at or past the centre, so splitting at the
    midpoint files the left side's own figures under the right side. (Confirmed
    on a real balance sheet - the liability "Sundry Creditors 16,950" landed
    under assets and broke the balance by exactly that amount.)

    Every wide gap in the middle band is a candidate; _harvest then picks
    whichever one makes the account balance. Same principle as the column
    solver: let the arithmetic decide, rather than guessing from geometry.
    """
    lo, hi = page_width * _DIVIDER_BAND[0], page_width * _DIVIDER_BAND[1]
    gaps = []
    for _y, cells in rows:
        ordered = sorted(cells, key=lambda c: c[0])
        for a, b in zip(ordered, ordered[1:]):
            midpoint = (a[1] + b[0]) / 2
            if b[0] > a[1] and lo <= midpoint <= hi:
                gaps.append((b[0] - a[1], midpoint))

    gaps.sort(reverse=True)
    out, tol = [(lo + hi) / 2], page_width * 0.02
    for _gap, mid in gaps:
        if all(abs(mid - seen) > tol for seen in out):
            out.append(mid)
        if len(out) >= 8:      # more than enough; keeps the search trivial
            break
    return out


# ─────────────────────────────────────────────────────────────────
# VALIDATION  -  the arithmetic proof
# ─────────────────────────────────────────────────────────────────
# This is the part the CIBIL project never had an equivalent of. A bureau report
# could only be checked against a summary it printed elsewhere; a financial
# statement proves itself. If both sides agree with each other and with the
# printed total, essentially every figure and sign on the block was read
# correctly. If they don't, the shortfall names what went wrong - and it is
# usually a whole line OCR dropped, not a mistyped digit.

# Statements are printed to the rupee, but OCR of a scanned page can lose a
# paise rounding. A couple of rupees of slack costs nothing; anything larger is
# a genuine miss worth surfacing.
TOLERANCE = 2

# Three outcomes, deliberately distinguished. "Unverified" is not a failure:
# it means the statement printed no total to check against, so the figures may
# be perfect but nothing proves it. Collapsing it into either "verified" or
# "failed" would either overstate confidence or discard good data.
VERIFIED   = "verified"
UNVERIFIED = "unverified"
FAILED     = "failed"

# Wording that marks a line as income. Used only to tell whether a one-sided
# P&L harvest carries its revenue at all (check_block); deliberately broad -
# a false "yes" merely falls back to the old behaviour.
_INCOME_LABEL_RE = re.compile(
    r"^\[INC\]|revenue|\bsales?\b|turnover|receipts?\b|\bincome\b|"
    r"\bfare\b|freight\s+(?:received|income)|commission\s+received", re.I)

_UNVERIFIABLE = ("no printed total", "no section totals", "nothing to check",
                 "no grand total found",
                 "single-column statement with no printed total")


def _tol(block: dict, n_items: int) -> float:
    """
    How far a sum may miss its printed total and still reconcile, in rupees.

    A statement printed in rupees is checked to TOLERANCE. One printed in
    thousands or lakhs cannot be: each line was ROUNDED to the printed unit
    before the total was, so the lines legitimately miss it by up to half a
    unit each. On a real filing (Borrower H, "Rs in lakhs") a correct
    Current Liabilities section summed to 850.66 lakh against a printed
    850.65 - Rs 1,000 - and failed; Borrower P's '000 statement missed by Rs 190.
    Half the last printed unit per line: thousands print whole units (Rs 500),
    lakhs and crores print two decimals (Rs 500 / Rs 50,000).
    """
    scale = block.get("unit_scale") or 1
    if scale <= 1:
        return TOLERANCE
    half_unit = scale / 2 if scale <= 1000 else scale / 200
    return TOLERANCE + half_unit * max(n_items, 1)


def status_of(check: dict) -> str:
    if check["balanced"]:
        return VERIFIED
    if any(s in check["reason"] for s in _UNVERIFIABLE):
        return UNVERIFIED
    return FAILED


# ─────────────────────────────────────────────────────────────────
# PER-SECTION RESULTS AND SALVAGE  (consistency checks, stage 1)
# ─────────────────────────────────────────────────────────────────
# A statement used to be verified or failed as a WHOLE, and a failed one was
# dropped - one unreadable Rs 63 line cost sample_d FY2025 every P&L row. A
# section that misses its own printed total by at most SALVAGE_SHARE is kept
# instead, with only its own lines marked doubtful (docs/superpowers/specs/
# 2026-09-17-consistency-checks-design.md, section 3.3).
SALVAGE_SHARE = 0.005

_TRUST_OF = {"pass": "proven", "nototal": "consistent", "fail": "doubtful"}


def section_results(block: dict) -> list:
    """
    One entry per section of a vertical statement, or one for a whole
    T-account: {"name", "items", "total", "sum", "gap", "status"}, status
    "pass" | "fail" | "nototal". Same rules and tolerance as _check_sections
    (a section needs a total and at least two items to be checked), so the
    two never disagree. Order and items align with all_items(block["sides"]).
    """
    sides = block["sides"]
    if sides.get("sections"):
        out = []
        for s in sides["sections"]:
            got = sum(a for _l, a in s["items"])
            if s["total"] is None or len(s["items"]) < 2:
                out.append({"name": s["name"], "items": s["items"], "total": s["total"],
                            "sum": got, "gap": 0, "status": "nototal"})
                continue
            gap = abs(s["total"] - got)
            ok = gap <= _tol(block, len(s["items"]) + 1)
            out.append({"name": s["name"], "items": s["items"], "total": s["total"],
                        "sum": got, "gap": 0 if ok else gap,
                        "status": "pass" if ok else "fail"})
        return out
    chk = block.get("check") or check_block(block)
    status = status_of(chk)
    left, right, printed = chk["left_total"], chk["right_total"], chk["printed_total"]
    if printed is not None:
        gap = abs(left - printed)
        if sides.get("right"):
            gap = max(gap, abs(right - printed))
    else:
        gap = abs(left - right)
    return [{"name": block["kind"], "items": all_items(sides), "total": printed,
             "sum": left, "gap": gap if status == FAILED else 0,
             "status": {VERIFIED: "pass", UNVERIFIED: "nototal"}.get(status, "fail")}]


def salvageable(block: dict) -> bool:
    """
    A FAILED statement worth keeping: every failing section misses its own
    total by at most SALVAGE_SHARE, and - with those small gaps closed - the
    statement passes every STRUCTURAL check too (both sides of a P&L present,
    a balance sheet's grand totals agreeing). A one-sided or badly broken
    statement is never salvaged.
    """
    chk = block.get("check") or check_block(block)
    if status_of(chk) != FAILED:
        return False
    results = section_results(block)
    failing = [r for r in results if r["status"] == "fail"]
    if not failing:
        return False
    for r in failing:
        base = abs(r["total"]) if r["total"] else max(abs(r["sum"]), 1)
        if r["gap"] > SALVAGE_SHARE * base:
            return False
    sides = block["sides"]
    if sides.get("sections"):
        failing_items = {id(r["items"]) for r in failing}
        closed = [dict(s, total=sum(a for _l, a in s["items"]))
                  if id(s["items"]) in failing_items else s
                  for s in sides["sections"]]
        return check_block(dict(block, sides=dict(sides, sections=closed)))["balanced"]
    return bool(sides.get("left")) and bool(sides.get("right"))


def item_statuses(block: dict) -> list:
    """Trust of every line - "proven" | "consistent" | "doubtful" - aligned
    with all_items(block["sides"]). A statement that is only UNVERIFIED as a
    whole (e.g. no grand total) never makes a line more than consistent."""
    capped = block.get("status") == UNVERIFIED
    out = []
    for r in section_results(block):
        level = _TRUST_OF[r["status"]]
        if capped and level == "proven":
            level = "consistent"
        out.extend([level] * len(r["items"]))
    return out


def check_block(block: dict) -> dict:
    """
    Returns {balanced, left_total, right_total, printed_total, shortfall, reason}.
    `shortfall` is what the weaker side is missing - the figure to go looking
    for on the page when a block fails.
    """
    if block["sides"].get("sections"):
        return _check_sections(block)

    left  = sum(a for _l, a in block["sides"]["left"])
    right = sum(a for _l, a in block["sides"]["right"])
    printed = block.get("printed_total")

    out = {
        "balanced":      False,
        "left_total":    left,
        "right_total":   right,
        "printed_total": printed,
        "shortfall":     0,
        "reason":        "",
    }

    if not block["sides"]["left"] and not block["sides"]["right"]:
        out["reason"] = "no line items harvested from this block"
        return out

    # A vertical (single-column) statement has nothing to cross-check against
    # except its own printed total.
    if not block["sides"]["right"]:
        if printed is None:
            out["reason"] = "single-column statement with no printed total to check"
            return out
        # A P&L whose items sum to its printed total on ONE side can still
        # have lost its whole income side: a T-account's debit side (expenses
        # plus the closing profit line) sums to the account total on its own.
        # Confirmed on a real filing (docs/SHORTCOMINGS.md Case 12): the
        # credit-side total was unreadable, the expense side matched the
        # printed total, and the block VERIFIED with income silently zero.
        # A genuine single-column P&L (a Vision read of a vertical statement)
        # still passes - its revenue lines sit in the same list.
        if (block.get("kind") == PROFIT_LOSS
                and not any(_INCOME_LABEL_RE.search(l)
                            for l, _a in block["sides"]["left"])):
            out["reason"] = ("only one side of the account was found - the "
                             "items match the printed total but no income "
                             "line was read, so the revenue side is missing")
            return out
        out["shortfall"] = printed - left
        out["balanced"]  = abs(out["shortfall"]) <= _tol(block, len(block["sides"]["left"]))
        if not out["balanced"]:
            out["reason"] = (f"items sum to {left:,} against a printed total of "
                             f"{printed:,} (short by {out['shortfall']:,})")
        return out

    n_lr = len(block["sides"]["left"]) + len(block["sides"]["right"])
    if abs(left - right) > _tol(block, n_lr):
        out["shortfall"] = right - left
        side = "left" if left < right else "right"
        out["reason"] = (f"{side} side sums to {min(left, right):,} against "
                         f"{max(left, right):,} on the other side "
                         f"(short by {abs(out['shortfall']):,})")
        return out

    if printed is not None and abs(left - printed) > _tol(block, n_lr):
        out["shortfall"] = printed - left
        out["reason"] = (f"both sides sum to {left:,} but the statement prints "
                         f"a total of {printed:,} (short by {out['shortfall']:,})")
        return out

    out["balanced"] = True
    return out


def _named_total(sections: list, pattern: str):
    """The total of the section whose anchor name matches `pattern`."""
    rx = re.compile(pattern, re.I)
    for s in sections:
        if s["total"] is not None and rx.search(s["name"] or ""):
            return s["total"]
    return None


def _check_sections(block: dict) -> dict:
    """
    Validate a vertical statement: every section's items must sum to the TOTAL
    the statement prints for that section.

    Same proof as a T-account's two sides agreeing, applied to a layout that
    has no two sides - a Schedule III balance sheet prints its total twice
    (once under Equity & Liabilities, once under Assets) and each must be
    reproduced by the items above it.
    """
    sections = block["sides"]["sections"]
    # A section is only checkable when its total could plausibly BE a sum of
    # what precedes it. A single-item section is usually an outcome line
    # rather than an aggregate - a P&L's tail prints "Total comprehensive
    # income" under one tax entry, and that total is the period's profit, not
    # the sum of the tax rows. Checking it reports a failure on a statement
    # that was read perfectly.
    checked = [s for s in sections
               if s["total"] is not None and len(s["items"]) >= 2]

    out = {
        "balanced":      False,
        "left_total":    sum(a for s in sections for _l, a in s["items"]),
        "right_total":   0,
        "printed_total": checked[0]["total"] if checked else None,
        "shortfall":     0,
        "reason":        "",
    }

    if not checked:
        out["reason"] = ("no section totals found to check against"
                         if sections else "no line items harvested from this block")
        return out

    failures = []
    for s in checked:
        got  = sum(a for _l, a in s["items"])
        diff = s["total"] - got
        if abs(diff) > _tol(block, len(s["items"]) + 1):
            failures.append(f"{s['name'] or 'section'}: items sum to {got:,} "
                            f"against a printed {s['total']:,} "
                            f"(short by {diff:,})")
            out["shortfall"] = out["shortfall"] or diff

    if failures:
        out["reason"] = "; ".join(failures)
        return out

    # A balance sheet's two GRAND totals must equal each other. Matched by
    # name, not by position: a Schedule III / Ind AS balance sheet prints
    # several sub-totals ("Total non-current assets", "Total equity", "Total
    # current liabilities") before either grand total, so comparing the first
    # two sections just pits two sub-totals against each other and reports a
    # mismatch on a statement that balances perfectly.
    if block["kind"] == BALANCE_SHEET:
        assets = _named_total(sections, r"total\s+assets\b")
        eqliab = _named_total(sections, r"total\s+equity\s+(?:and|&)\s+liabilit"
                                        r"|total\s+liabilit\w*\s+(?:and|&)\s+equity")
        if assets is not None and eqliab is not None:
            if abs(assets - eqliab) > _tol(block, 2):
                out["reason"] = (f"Assets total {assets:,} but Equity & "
                                 f"Liabilities total {eqliab:,}")
                return out
            out["left_total"], out["right_total"] = eqliab, assets
        else:
            # No grand total to check against. Sub-sections reconciling is NOT
            # enough for a balance sheet: OCR can drop an entire section (on a
            # real filing every current-asset line vanished) and every
            # surviving section still adds up perfectly. Without a grand total
            # there is nothing that would notice the gap, so this is reported
            # as unverified rather than passed.
            totals = sorted((s["total"] for s in sections if s["total"] is not None),
                            reverse=True)
            paired = any(abs(a - b) <= _tol(block, 2)
                         for a, b in zip(totals, totals[1:]))
            if not paired:
                out["reason"] = ("no grand total found to check the balance "
                                 "sheet against - sections reconcile but the "
                                 "statement may be incomplete")
                return out
            out["left_total"] = out["right_total"] = totals[0]

    # A vertical P&L needs evidence of BOTH sides, the same way a balance
    # sheet needs both its grand totals - a section reconciling against its
    # own printed total proves nothing about whether the OTHER side was
    # captured at all. Confirmed on a real filing (poor phone-scan): OCR
    # dropped the entire Revenue section while "Total expenses" harvested
    # and balanced perfectly, and the block still came back VERIFIED with
    # the year's income silently reading as zero downstream.
    if block["kind"] == PROFIT_LOSS:
        # A side's total may also be printed UNLABELLED under its heading;
        # _harvest_vertical then names that section after the heading ("IV
        # EXPENSES"). Accepted here too: Borrower M FY2025 printed its
        # expense total that way and the P&L - read correctly - was
        # rejected as "missing its expense side".
        revenue = _named_total(sections, r"total\s+revenue|total\s+income\b|"
                                         r"^\s*(?:[A-Z]{1,4}\s*[\].):]\s*|[IVX]{1,4}\s+)?"
                                         r"(?:income|revenue)\s*[:\-\s]*$")
        expense = _named_total(sections, r"total\s+expen(?:ses?|diture)\b|"
                                         r"^\s*(?:[A-Z]{1,4}\s*[\].):]\s*|[IVX]{1,4}\s+)?"
                                         r"expen(?:ses?|diture)\s*[:\-\s]*$")
        if revenue is None or expense is None:
            out["reason"] = ("only one side of the account was found - "
                             "sections reconcile but the statement may be "
                             "missing its revenue or expense side entirely")
            return out

    out["balanced"] = True
    return out
