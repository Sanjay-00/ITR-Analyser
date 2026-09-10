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
_PERIOD_RE = re.compile(
    r"(?:as\s*on|for\s*the\s*year\s*end\w*|as\s*at)\D{0,20}"
    r"(?:(\d{1,2})\s*(?:st|nd|rd|th)?\s*[-/\s]?\s*([A-Za-z]{3,9})[,\s]*)?(\d{4})",
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
    r"(?:(?:net\s+)?profit\s+(?:before|after|for\s+the)"
    r"|(?:net\s+)?profit\s*/?\s*\(?loss\)?\s+(?:before|after|for"
    # "...Carried over to Balance Sheet" restates the bottom-line figure
    # rather than adding a new one - on a real filing it was harvested as a
    # plain expense line and folded into Other Expenses, corrupting every
    # profit figure derived downstream.
    r"|carried\s+(?:over\s+)?to\s+(?:the\s+)?balance\s+sheet)"
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
    r"total|as\s+per\s+our\s+report|place|date|udin|for[,\s]|"
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


def _match_title(line: str) -> bool:
    flat = re.sub(r"\s+", " ", line).strip()
    if len(flat) > _MAX_TITLE_LEN:
        return False
    # A heading carries no MONEY of its own, which rejects the line items that
    # would otherwise read as headings. Tested for grouping or paise rather
    # than with _AMOUNT_RE: most headings end in their own year, and a bare
    # "2026" is indistinguishable from an amount to a general number pattern.
    if _MONEY_RE.search(flat):
        return False
    return any(rx.search(flat) for rx in _TITLES)


# An address line sits between the entity name and the heading in most layouts.
_ADDRESS_RE = re.compile(
    r"\b(road|nagar|marg|street|chowk|complex|shop\s*no|near|dist|"
    r"pin|opp\.?|building|plot|floor)\b", re.I)

# An address keyword alone is not enough: "road", "nagar" and "complex" are
# common words in a genuine Indian BUSINESS name too ("Borrower A Road
# Carrier" is a real filing's actual borrower). A genuine address line
# carries some OTHER tell an address has and a bare business name does not -
# a house/shop/plot number or a pincode, or comma-separated locality parts.
_ADDRESS_DETAIL_RE = re.compile(r"\d|,")


def _looks_like_address(cand: str) -> bool:
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


def _find_period(lines: list, idx: int):
    """
    (ending_year, raw_text) for the statement's period.

    Checked on the heading line first: layouts split roughly evenly between
    "BALANCE SHEET / as on 31st March 2024" and the single-line
    "BALANCE SHEET AS ON 31 ST MARCH 2026", and only looking below the heading
    misses every instance of the latter.
    """
    for j in range(idx, min(len(lines), idx + 1 + _PERIOD_LOOKAHEAD)):
        m = _PERIOD_RE.search(lines[j])
        if m:
            return int(m.group(3)), re.sub(r"\s+", " ", lines[j]).strip()
    return None, ""


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
    for page_no, rows in enumerate(page_rows or []):
        if not rows or (wanted is not None and page_no not in wanted):
            continue
        lines = [" ".join(t for _a, _b, t in cells) for _y, cells in rows]
        page_width = max((c[1] for _y, cells in rows for c in cells), default=1) or 1

        titles = [i for i, ln in enumerate(lines) if _match_title(ln)]
        for n, idx in enumerate(titles):
            end   = titles[n + 1] if n + 1 < len(titles) else len(lines)
            body  = rows[idx + 1:end]
            year, period = _find_period(lines, idx)
            foot = _footer_at(body)
            if foot is not None:
                body = body[:foot]
            total, total_at = _printed_total(body)
            sides = _harvest(body, page_width, total, total_at,
                            title_cells=rows[idx][1])
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

            # Everything downstream works in whole rupees.
            scale = _unit_scale(lines, idx)
            _rescale(sides, scale)
            if total is not None:
                total = int(round(total * scale))
            if comparative:
                comparative = {k: int(round(v * scale)) for k, v in comparative.items()}

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
    return blocks


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


def _split_cells(cells: list) -> list:
    out = []
    for x0, x1, txt in cells:
        m = _WELDED_RE.match(txt.strip())
        if m and re.search(r"[A-Za-z]{3}", m.group(2)):
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
            title_cells=None) -> dict:
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
    if _verifies(preferred):
        return preferred
    if other is not None and _verifies(other):
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


def _verifies(sides: dict) -> bool:
    """Does this reading reconcile against the statement's own totals?"""
    if not sides or not _has_items(sides):
        return False
    return check_block({"sides": sides, "kind": OTHER})["balanced"]


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
    ("EXP", re.compile(r"^\s*(?:[IVX]{1,4}[\s.)]+)?expenses?\s*:?\s*$", re.I)),
    ("INC", re.compile(r"^\s*(?:[IVX]{1,4}[\s.)]+)?(?:income|revenue)\s*:?\s*$", re.I)),
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
    ("LIAB", re.compile(r"\bequity\s*(?:&|and)\s*liabilit", re.I)),
]


def _section_token(label: str):
    for token, rx in _SECTIONS:
        if rx.search(label):
            return token
    return None


def _current_period_amount(cells: list, cur, prior: list):
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
    for x0, x1, txt in cells:
        txt = txt.strip()
        m = _AMOUNT_RE.search(txt)
        if not m or re.search(r"[A-Za-z]{3}", txt):
            continue
        if any(x0 < p[1] and x1 > p[0] for p in prior):
            continue
        val = _clean_amount(m.group(1))
        if val is None:
            continue
        signed = -val if _is_negative(txt, txt[:m.start()]) else val
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
    prior_col = cols[1] if len(cols) > 1 else None
    _comparative_others = ([c for c in cols if c is not prior_col]
                           if prior_col else [])
    comparative = {}

    sections, current, anchors = [], [], []
    section_token = None
    section_heading = None
    for _y, cells in rows:
        cells = sorted(cells, key=lambda c: c[0])
        # Join every label fragment on the row, don't just take the first.
        # Schedule III wraps long captions across cells, and "(a) Short - |
        # Term Borrowings" read as only "(a) Short -" loses the word that
        # decides whether the figure is a current liability or a term loan.
        parts = [t for _a, _b, t in cells if re.search(r"[A-Za-z]{3}", t)]
        label = re.sub(_AMOUNT_RE.pattern, "", " ".join(parts)).strip(" .:-|")

        is_anchor = bool(label and _ANCHOR_RE.match(label))
        amount = _current_period_amount(
            cells, cur, prior_anchor if is_anchor else prior_item)

        if is_anchor and amount is not None:
            sections.append({"name": label, "items": current, "total": amount})
            anchors.append(amount)
            if prior_col:
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
            sections.append({"name": section_heading, "items": current,
                             "total": amount})
            anchors.append(amount)
            current = []
            section_token = None
            section_heading = None
            continue

        if label and amount is not None and not _SKIP_RE.match(label):
            if _DERIVED_ROW_RE.search(label):
                continue
            if section_token:
                label = f"[{section_token}] {label}"
            current.append((label, amount))

    if current:
        sections.append({"name": section_heading or "", "items": current,
                         "total": None})

    return {"sections": sections, "comparative": comparative}


def _harvest_at(rows: list, mid: float, printed_total, title_cells=None) -> dict:
    """Harvest with the sides split at `mid`."""
    raw = {"left": [], "right": []}
    carried = {"left": "", "right": ""}   # last label seen, for amount-only cells
    section = {"left": "", "right": ""}   # last group heading, per side

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
                section["left" if (x0 + x1) / 2 < mid else "right"] = token

    for _y, cells in rows:
        # A bare amount belongs to the label on ITS OWN ROW, and takes that
        # label's side - not the side its own x-position falls on. A liability
        # and an asset can print their figures in near-identical columns
        # ("Sundry Creditors  16,950" vs "Sundry Debtors  35,260"); only the
        # label says which side of the account each one is.
        row_label, row_side = "", None
        for x0, x1, txt in _split_cells(cells):
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
                continue
            m = _AMOUNT_RE.search(txt)
            bare = _BARE_LONG_INT_RE.match(txt) if not m else None
            if (m or bare) and not re.search(r"[A-Za-z]{3}", txt):
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
                use_side  = row_side or side
                use_label = row_label or carried[use_side] or "(unlabelled)"
                if section[use_side] and not use_label.startswith("["):
                    use_label = f"[{section[use_side]}] {use_label}"
                raw[use_side].append((x0, use_label, -val if neg else val))
            elif re.search(r"[A-Za-z]{3}", txt):
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
                if token:
                    section[side] = token

    return {
        side: [(lab, amt) for _x, lab, amt in
               _pick_columns(_cluster_columns(raw[side]), printed_total)]
        for side in ("left", "right")
    }


def _printed_total(rows: list):
    """
    The statement's own total, as (value, row_index) so the caller can drop the
    footer row before harvesting. In a T-account the total is the figure
    repeated under both sides ("441,784,183.00  441,784,183.00"); in a vertical
    statement it is the line labelled Total. (None, None) when neither reads.
    """
    for i in range(len(rows) - 1, -1, -1):
        texts   = [t.strip() for _a, _b, t in rows[i][1]]
        amounts = [_clean_amount(m.group(1))
                   for t in texts if (m := _AMOUNT_RE.search(t))]
        amounts = [a for a in amounts if a]
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
        amounts = [_clean_amount(m.group(1))
                   for t in texts if (m := _AMOUNT_RE.search(t))]
        amounts = [a for a in amounts if a]
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
_ANCHOR_RE = re.compile(
    r"^\s*(?:[IVX]{1,4}[\s.)]+|\(?\d{1,2}\)?[\s.)]+)?"
    r"(?:total|gross\s+total)\b", re.I)

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
        for x0, x1, txt in cells:
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

_UNVERIFIABLE = ("no printed total", "no section totals", "nothing to check",
                 "no grand total found",
                 "single-column statement with no printed total")


def status_of(check: dict) -> str:
    if check["balanced"]:
        return VERIFIED
    if any(s in check["reason"] for s in _UNVERIFIABLE):
        return UNVERIFIED
    return FAILED


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
        out["shortfall"] = printed - left
        out["balanced"]  = abs(out["shortfall"]) <= TOLERANCE
        if not out["balanced"]:
            out["reason"] = (f"items sum to {left:,} against a printed total of "
                             f"{printed:,} (short by {out['shortfall']:,})")
        return out

    if abs(left - right) > TOLERANCE:
        out["shortfall"] = right - left
        side = "left" if left < right else "right"
        out["reason"] = (f"{side} side sums to {min(left, right):,} against "
                         f"{max(left, right):,} on the other side "
                         f"(short by {abs(out['shortfall']):,})")
        return out

    if printed is not None and abs(left - printed) > TOLERANCE:
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
        if abs(diff) > TOLERANCE:
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
            if abs(assets - eqliab) > TOLERANCE:
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
            paired = any(abs(a - b) <= TOLERANCE
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
        revenue = _named_total(sections, r"total\s+revenue|total\s+income\b")
        expense = _named_total(sections, r"total\s+expenses?\b")
        if revenue is None or expense is None:
            out["reason"] = ("only one side of the account was found - "
                             "sections reconcile but the statement may be "
                             "missing its revenue or expense side entirely")
            return out

    out["balanced"] = True
    return out
