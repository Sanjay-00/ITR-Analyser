"""
columns.py  -  one ITR bundle → one year column of the spreading sheet.

(Named columns.py, not spread.py, deliberately: parser.spread() is the
public function every caller uses, and a submodule sharing that exact name
previously collided with it on the `engine` package namespace - the first
call to parser.spread() imports this submodule, which Python's import
machinery then binds as the `engine.spread` attribute, silently replacing
the function every later `from engine import spread` picks up in the same
process. Streamlit's rerun-the-whole-script model hit this on literally the
second run. Distinct names avoid the collision outright.)

Ties the layers together:

    parser._extract        PDF → text + cell geometry (OCR if scanned)
    relevance.select       drop the ~80% of pages that carry nothing we need
    financials.find_blocks locate and harvest the statements, and verify them
    taxonomy.map_items     raw labels → the analyst's template rows
    taxonomy.compute       derived rows and ratios

Each year column keeps the evidence behind it: which blocks fed it, whether
each verified, and every label that could not be mapped. Nothing enters the
sheet without a trail back to the statement it came from.
"""

import os
import re

from .extract import financials as F
from .extract.borrowings import GENERIC_LT_BORROWINGS, owner_loan_lines, owners_loan_note
from .extract.itr_parser import extract_identity, extract_book_profit, extract_return_figures
from .ingest import relevance
from .mapping import taxonomy

# Statement kinds that feed the spread. A capital account reconciles the
# proprietor's capital movement and would double-count against the balance
# sheet's capital figure, so it is located and verified but not spread.
SPREAD_KINDS = (F.BALANCE_SHEET, F.PROFIT_LOSS)


def _fy_end_year(blocks: list, identity: dict):
    """
    The financial year this bundle reports on, as the calendar year it ends in
    (FY 2023-24 → 2024), matching the analyst's column headings.

    Taken from the statements' own period lines where possible. Otherwise
    derived from the Assessment Year: AY 2024-25 assesses FY 2023-24, so the
    year the accounts end in is the AY's FIRST year, not its second.
    """
    ay = identity.get("ay") or ""
    m = re.match(r"(\d{4})", ay)
    ay_year = int(m.group(1)) if m else None
    years = [b["year"] for b in blocks if b.get("year")]
    if years:
        # A tie is broken by the return's own Assessment Year, then the
        # earliest - not arbitrarily. "ITR 2022-2023.pdf" (AY 2023-24) held
        # one FY2023 and one FY2024 Balance Sheet and was dated 2024 by
        # set-iteration order.
        top = max(years.count(y) for y in set(years))
        tied = sorted(y for y in set(years) if years.count(y) == top)
        return ay_year if ay_year in tied else tied[0]
    return ay_year


# A block whose figures are mostly figures we already have is a restatement,
# not a second statement. Above this share of repeated amounts, drop it.
_DUPLICATE_SHARE = 0.5


def _rank(b: dict) -> tuple:
    return (b["status"] == F.VERIFIED, len(F.all_items(b["sides"])))


def _norm_entity(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _main_entity(blocks: list, identity: dict) -> str:
    """
    Whose accounts these are - from the statements that proved themselves,
    falling back to the return's own name.

    Candidates are filtered through the same plausibility test used to reject
    note tables. A document with no real statements in it (a NIL return is 101
    pages of empty ITR schedules) otherwise yields whatever prose sat above the
    nearest table, and that name then titles the workbook: one produced a file
    called "of_district_along_with_pin_code_in_which_Whether_the...".
    """
    for pool in (
        [b for b in blocks if b["status"] == F.VERIFIED],
        list(blocks),
    ):
        names = [b.get("entity", "") for b in pool
                 if b.get("entity") and _belongs_to(b["entity"])]
        if names:
            return max(set(names), key=names.count)
    name = identity.get("name") or ""
    return name if _belongs_to(name) else ""


# Prose that turns up where an entity name should be, when the "statement" is
# really a note or a disclosure table.
_PROSE_RE = re.compile(
    r"^\s*(?:note\s*\d|schedule\b|annexure\b|as\s+at\b|as\s+on\b|"
    r"based\s+on|present\s+value|additional\s+regulatory|in\s+terms\s+of|"
    r"the\s+\w+|this\s+\w+|have\s+\w+|period\s+to|\(?amount\b|particulars\b)",
    re.I)


def _belongs_to(entity: str, main: str = "") -> bool:
    """
    Does this read like a business's own statement rather than a note table?

    An unproven block has to earn its place. A bundle's notes and disclosures
    look structurally like statements and cannot be checked - "Note 35 : Lease
    related disclosures", "Based on the actuarial valuation obtained in this
    respect", "Additional regulatory information" - and on a real filing one of
    them merged into the P&L and turned Other Expenses into a
    nine-hundred-billion-rupee figure.

    Judged on the SHAPE of the heading, NOT by matching the borrower's own
    name, because one ITR routinely covers several businesses. Borrower R's
    bundle carries three - A CUSTOMER, Borrower B supplier and
    the proprietor's own accounts - and all three must survive while none of
    the note tables does. `main` is accepted for callers that want it but is
    deliberately not used as a filter.
    """
    name = (entity or "").strip()
    if not (4 <= len(name) <= 70) or _PROSE_RE.match(name):
        return False
    words = [w for w in re.split(r"[\s,]+", name) if w]
    if not words or len(words) > 9:
        return False
    # A business name is titled or capitalised throughout; a sentence is not.
    strong = sum(1 for w in words if w[:1].isupper() or w.isupper())
    return strong >= max(2, len(words) // 2)


def _dedupe(blocks: list, main_entity: str = "") -> list:
    """
    Keep one statement per (entity, kind, year), then drop restatements.

    The second pass matters more than the first. A bundle prints the same
    figures twice - once in the Balance Sheet, once in the Notes that support
    it ("Reserves and Surplus: as per last balance sheet ... add profit for the
    year"; "Trade Payables: sundry payable ..."). Those notes pages carry their
    own headings and no entity name, so keying on (entity, kind, year) does not
    catch them, and keeping both DOUBLES real balances. Confirmed on a real
    filing: current liabilities came out at 304.66 lakhs against an actual
    152.33, and profit before tax flipped sign.

    So a block is also dropped when most of its amounts already appear in the
    blocks kept ahead of it. Verified and larger blocks are ranked first, so
    what survives is the statement and what goes is the restatement.
    """
    # Same key, DIFFERENT figures: two complementary statements, not two
    # readings of one. A petrol pump prints its Trading ("PUMP ACCOUNT") and
    # its Profit & Loss on one page, both profit_loss and neither naming an
    # entity, so both land on the same key - and keeping only the "best" threw
    # the trading account away with the year's whole turnover (Borrower B
    # FY2025: Rs 21.71 lakh of income against Rs 1,659.38 printed). Only a
    # block whose figures OVERLAP the incumbent is a rival reading of it; the
    # rest are kept and judged by the restatement pass below.
    best = {}
    for b in blocks:
        key = (b.get("entity", "").strip().lower(), b["kind"], b.get("year"))
        rivals = best.setdefault(key, [])
        mine = {a for _l, a in F.all_items(b["sides"]) if a}
        for i, other in enumerate(rivals):
            theirs = {a for _l, a in F.all_items(other["sides"]) if a}
            if mine and theirs and len(mine & theirs) > _DUPLICATE_SHARE * len(mine):
                if _rank(b) > _rank(other):
                    rivals[i] = b
                break
        else:
            rivals.append(b)

    # A verified block has proved itself and is taken as-is. An unverified one
    # is admitted only if it is plainly part of the borrower's own accounts.
    candidates = [b for rivals in best.values() for b in rivals
                  if b["status"] == F.VERIFIED or b.get("salvaged")
                  or _belongs_to(b.get("entity", ""), main_entity)]

    kept, seen = [], set()
    for b in sorted(candidates, key=_rank, reverse=True):
        amounts = [a for _l, a in F.all_items(b["sides"]) if a]
        if amounts:
            repeated = sum(1 for a in amounts if a in seen)
            if repeated / len(amounts) > _DUPLICATE_SHARE:
                continue
        kept.append(b)
        seen.update(amounts)
    return kept


# The owner's own capital account. On the LIABILITIES side it is equity; on
# the ASSETS side of a two-sided Balance Sheet it is a debit balance - the
# proprietor has drawn out more than they put in.
_CAPITAL_LINE_RE = re.compile(
    r"\bcapital\s+a/?c(?:count)?\b|(?:proprietor|partners?|owner)'?s?\s+capital",
    re.I)


def _spread_items(block: dict) -> list:
    """
    A block's (label, amount) lines as they should enter the spread.

    One adjustment: a capital account printed on the ASSETS side of a
    two-sided Balance Sheet is negative equity, not an asset and not positive
    equity. Confirmed against the analyst's reference sheet (Borrower A FY2024): the proprietor's capital of Rs 36.59 lakh sits among the
    assets, and the sheet shows Equity -36.59 with both totals reduced by the
    same amount (750.46 printed -> 713.87). Read by label alone it became
    +36.59 of equity - the wrong sign on net worth, the one figure every
    gearing ratio divides by. The block's own balance proof is untouched;
    only how its lines are bucketed changes.
    """
    sides = block["sides"]
    if block["kind"] != F.BALANCE_SHEET or sides.get("sections"):
        return F.all_items(sides)
    right = [(f"[EQ] {l} (debit balance)", -a) if _CAPITAL_LINE_RE.search(l) else (l, a)
             for l, a in sides.get("right", [])]
    return list(sides.get("left", [])) + right


def _block_from_vision(obj: dict, page: int) -> dict:
    """A Vision reading shaped like a harvested block, so the SAME check applies."""
    def _items(rows, default_token=None):
        out = []
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            label, amount = row[0], row[1]
            section = row[2] if len(row) > 2 else ""
            try:
                value = int(round(float(amount)))
            except (TypeError, ValueError):
                continue
            label = str(label).strip()
            # Tag with the same section token the OCR path uses, so one set of
            # taxonomy rules serves both readers. Without it a balance sheet's
            # two "Borrowings" lines - one non-current, one current - are
            # indistinguishable and collapse into a single bucket.
            token = F._section_token(str(section or "")) or default_token
            if token:
                label = f"[{token}] {label}"
            out.append((label, value))
        return out

    kind = obj.get("kind")
    kind = kind if kind in (F.BALANCE_SHEET, F.PROFIT_LOSS,
                            F.CAPITAL_ACCOUNT) else F.OTHER
    # For a balance sheet, WHICH ARRAY an item is in ("left" vs "right") is
    # itself a reliable liabilities-vs-assets signal - STATEMENT_PROMPT's own
    # schema guarantees it - independent of whatever free-text "group
    # heading" Vision also filled in for that row. That heading is NOT
    # reliable: asked to re-read the identical page five times, Gemini
    # returned "Equity and liabilities" three times and a blank string
    # twice, and a blank heading left "Loans" - a label with no vocabulary
    # of its own - completely unmapped and missing from the sheet, purely by
    # chance of which attempt happened to run. Falling back to the side
    # itself when the heading is blank/unrecognised means a bare left-side
    # label with no group heading at all still gets SOME liabilities-side
    # context, rather than the mapping depending on a coin flip.
    default_left = "LIAB" if kind == F.BALANCE_SHEET else None
    sides = {"left": _items(obj.get("left"), default_left),
             "right": _items(obj.get("right"))}
    # Same debit/credit tagging the OCR path applies, so a Vision-read P&L
    # cannot file its revenue as expenses either.
    F.tag_pl_sides(kind, sides)

    return {
        "kind":          kind,
        "title":         "(read from page image)",
        "entity":        str(obj.get("entity") or "").strip(),
        "year":          obj.get("year") if isinstance(obj.get("year"), int) else None,
        "period":        "",
        "page":          page,
        "sides":         sides,
        "printed_total": obj.get("printed_total"),
        "via_vision":    True,
    }


# Hard ceiling on Vision calls per document. A bundle can throw up a dozen
# note tables that look like statements; without a cap one unreadable file
# could quietly cost more than the rest of a batch put together.
MAX_VISION_PAGES = 6


_LOW_CONF_THRESHOLD = 0.80


def _needs_vision(blocks: list, main_entity: str, statement_pages: set,
                  page_confidence: list = None) -> list:
    """
    The MINIMUM set of pages worth re-reading, most promising first.

    Three filters, each removing a class of pointless spend:
      - the kind must still be missing, UNLESS this page's own OCR confidence
        was low - a low-confidence read can still coincidentally balance
        (matching sides and a printed total by chance is rare but not
        impossible), and arithmetic agreement is the only thing that ever
        marks a block VERIFIED, so it is the one failure mode the balance
        check itself cannot see. Re-reading is cheap (a handful of pages per
        document, never more than MAX_VISION_PAGES), and a re-read is only
        ever adopted if IT also reconciles, so this adds a safety net without
        weakening the guarantee that a wrong number never reaches the sheet.
      - the block must carry the borrower's name. Notes and disclosure tables
        look like statements and would otherwise soak up most of the budget.
      - the page must be one relevance called a STATEMENT, not a schedule or
        an audit annexure that merely mentions one.

    On a 41-page scanned bundle this is the difference between six calls and
    forty-one.
    """
    proven = {b["kind"] for b in blocks if b["status"] == F.VERIFIED}
    cands = []
    for b in blocks:
        low_conf = (page_confidence is not None and b["page"] < len(page_confidence)
                    and page_confidence[b["page"]] < _LOW_CONF_THRESHOLD)
        if b["kind"] not in SPREAD_KINDS:
            continue
        if b["status"] == F.VERIFIED and not low_conf:
            continue
        if b["kind"] in proven and not low_conf:
            continue
        if not _belongs_to(b.get("entity", ""), main_entity):
            continue
        if statement_pages and b["page"] not in statement_pages:
            continue
        cands.append(b)

    # A contradicted block is a better bet than an unprovable one, and a
    # bigger block is more likely to be the real statement.
    cands.sort(key=lambda b: (b["status"] != F.FAILED,
                              -len(F.all_items(b["sides"]))))
    return cands[:MAX_VISION_PAGES]


# A Vision re-read balancing against itself is NOT independent evidence of
# correctness: Vision generates both sides of the statement in the same
# response, so a hallucinated pair of sides that agree with each other sails
# through check_block untouched. Confirmed on a real filing - Gemini re-read
# a P&L whose OCR harvest had failed by one dropped line (revenue correct at
# Rs 14,22,91,953, expenses short by exactly that one row) and came back
# with a DIFFERENT, internally-balanced but wildly wrong statement: revenue
# Rs 15,84,68,556, expenses Rs 29,50,92,692, implying a Rs -13.66 crore loss
# against the Rs 48,23,981 profit the page actually prints. It was accepted
# as VERIFIED under the old rule.
#
# The only genuinely independent signal available is the ORIGINAL OCR
# attempt: a different pipeline (RapidOCR's geometry parse) reading the same
# pixels. A dropped row can only shrink a side's sum, never inflate one, so
# whichever side of the ORIGINAL block summed larger is the one least likely
# to already be wrong, and is used as the reference. 20% is generous - wide
# enough to admit a genuine correction (Vision fixing a real OCR misread that
# swung a side by a normal amount) while still catching an order-of-magnitude
# fabrication like the one above (a 107% swing in the confirmed case).
_VISION_AGREEMENT_TOLERANCE = 0.20


def _vision_disagrees_with_ocr(original_check: dict, vision_check: dict) -> bool:
    """Does a Vision re-read's own total land far outside what the original
    OCR attempt already read on the same page? `original_check` / `vision_check`
    are check_block()-shaped dicts (only left_total/right_total are used)."""
    ocr_ref = max(original_check.get("left_total", 0),
                  original_check.get("right_total", 0))
    if ocr_ref <= 0:
        return False   # no OCR baseline to disagree with - trust Vision as before
    vision_ref = max(vision_check.get("left_total", 0),
                     vision_check.get("right_total", 0))
    return abs(vision_ref - ocr_ref) > ocr_ref * _VISION_AGREEMENT_TOLERANCE


def _escalate(blocks: list, source, api_key: str, vision_fn,
              main_entity: str, statement_pages: set,
              page_confidence: list = None) -> tuple:
    """
    Re-read selected statement pages from their images.

    A replacement is adopted only if it (a) comes back VERIFIED - both its
    own sides reconcile - AND (b) roughly agrees with what the original OCR
    attempt already read on that page (see _vision_disagrees_with_ocr). (a)
    alone is not enough: it proves Vision's own reading is self-consistent,
    not that it is correct, since Vision writes both sides of the statement
    in the same response. (b) is what makes it safe to let a language model
    read financial figures at all - a hallucination has to additionally land
    within reach of an independent read of the same pixels, not just balance
    against itself. This holds even for a low-confidence page that already
    verified (see _needs_vision) - the re-read only replaces it if it clears
    both bars, so a lucky wrong coincidence is never swapped for an unlucky
    wrong guess.

    Returns (blocks, pages_sent).
    """
    targets = {id(b) for b in _needs_vision(blocks, main_entity, statement_pages,
                                            page_confidence)}
    if not targets:
        return blocks, []

    out, sent = [], []
    for b in blocks:
        if id(b) not in targets:
            out.append(b)
            continue
        sent.append(b["page"])
        obj = vision_fn(source, b["page"], api_key) or {}
        cand = _block_from_vision(obj, b["page"]) if obj else None
        if cand and F._has_items(cand["sides"]):
            check = F.check_block(cand)
            if check["balanced"] and not _vision_disagrees_with_ocr(b["check"], check):
                cand["check"], cand["status"] = check, F.status_of(check)
                cand["entity"] = cand["entity"] or b.get("entity", "")
                cand["year"]   = cand["year"] or b.get("year")
                out.append(cand)
                continue
            if check["balanced"]:
                b["check"]["reason"] += ("; re-read from the page image "
                                         "reconciled but disagrees sharply "
                                         "with the original OCR read, so it "
                                         "was not trusted either")
            else:
                b["check"]["reason"] += ("; re-read from the page image did not "
                                         "reconcile either")
        out.append(b)
    return out, sent


# How far (in pages) a dated statement can lend its period to an undated one.
_YEAR_NEIGHBOUR_PAGES = 2


def _infer_missing_years(blocks: list) -> None:
    """
    A statement that prints no period of its own takes the period of the
    nearest DATED statement in the same file - preferring the same business,
    within _YEAR_NEIGHBOUR_PAGES - before anything falls back to the file's
    ITR year.

    A proprietor's Balance Sheet and P&L are printed as a pair, and often
    only one of them carries the date. The ITR it sits in is weaker evidence:
    on a real case (Borrower O) each bundle held the OTHER year's
    accounts - "ITR 23-24.pdf" (AY 2024-25) carried the Balance Sheet "as on
    31.03.2023" and an undated P&L beside it - and the P&L, filed under its
    ITR's year, landed a whole year off the analyst's sheet. Marked
    `year_inferred`; mutates `blocks` in place.
    """
    dated = [b for b in blocks if b.get("year") and b["kind"] in SPREAD_KINDS]
    for b in blocks:
        if b.get("year") or b["kind"] not in SPREAD_KINDS or not dated:
            continue
        ent = _norm_entity(b.get("entity", ""))
        near = min(dated, key=lambda o: (_norm_entity(o.get("entity", "")) != ent,
                                         abs(o["page"] - b["page"])))
        if abs(near["page"] - b["page"]) <= _YEAR_NEIGHBOUR_PAGES:
            b["year"] = near["year"]
            b["year_inferred"] = True


def _read_document(source, extract_fn, api_key: str = None,
                   on_progress=None, vision_fn=None) -> dict:
    """
    Read ONE uploaded PDF: locate, harvest and check its statements (with the
    Vision re-read where enabled), plus its identity and page facts. Nothing
    is mapped or totalled here - `_assemble` does that per FINANCIAL YEAR,
    from the statements of every uploaded file that belong to that year.

    `extract_fn(source, on_progress)` returns (text, scanned, page_texts,
    page_rows, page_confidence); it is injected so this module stays
    independent of how the document was read. `vision_fn(source, page,
    api_key)` is optional and is called for blocks that failed their
    arithmetic check, or that balanced on a low-confidence OCR read.
    """
    text, scanned, page_texts, page_rows, page_confidence = extract_fn(source, on_progress)

    identity = extract_identity(text)
    book_profit, book_profit_page = extract_book_profit(page_texts)
    return_figures = extract_return_figures(page_texts)
    pages    = relevance.select(page_texts)

    def _find_and_check(only_pages):
        out = []
        for b in F.find_blocks(page_rows, only_pages=only_pages):
            check = F.check_block(b)
            b["check"]  = check
            b["status"] = F.status_of(check)
            out.append(b)
        return out

    blocks = _find_and_check(pages)

    # relevance.select() is a heuristic over each page's OWN text - keyword
    # hits and money density. A badly degraded scan can OCR a genuine
    # Balance Sheet or P&L page into something that no longer clears either
    # bar, which drops exactly the page that mattered before find_blocks
    # ever sees it - and the column then reports "NIL return" on a return
    # that plainly is not, because the only evidence left standing is a
    # stray small figure from an unrelated page. Confirmed on a real filing:
    # a scanned ITR-3 whose Trading/P&L and Balance Sheet pages both
    # classified as noise, leaving only a Chapter VI-A deduction table
    # (values a few thousand rupees) to judge "nil" against.
    #
    # This costs nothing extra when it doesn't fire: page_rows already holds
    # every page's OCR'd geometry regardless of relevance, so re-running
    # find_blocks over the whole document is local, cheap re-parsing, not a
    # second OCR or API pass. Only retried when the filtered pages produced
    # no usable candidate at all, and only trusted if the retry actually
    # finds one - otherwise the original (correct) "nothing here" result
    # stands.
    recovered_pages = None
    if (len(pages) < len(page_texts)
            and not any(b["kind"] in SPREAD_KINDS and b["status"] != F.FAILED
                       for b in blocks)):
        retry = _find_and_check(None)
        if any(b["kind"] in SPREAD_KINDS and b["status"] != F.FAILED for b in retry):
            blocks = retry
            recovered_pages = {b["page"] for b in blocks if b["kind"] in SPREAD_KINDS}

    main = _main_entity(blocks, identity)
    vision_pages = []
    low_conf_block = any(
        b["kind"] in SPREAD_KINDS and b["status"] == F.VERIFIED
        and b["page"] < len(page_confidence)
        and page_confidence[b["page"]] < _LOW_CONF_THRESHOLD
        for b in blocks)
    if vision_fn and api_key and (
            any(b["status"] != F.VERIFIED for b in blocks) or low_conf_block):
        statement_pages = {i for i, k in enumerate(relevance.classify(page_texts))
                           if k == relevance.STATEMENT}
        if recovered_pages:
            # These pages already proved themselves by producing a titled,
            # find_blocks-recognised statement - stronger evidence than the
            # classifier's keyword-density guess, which is exactly what
            # failed on them.
            statement_pages |= recovered_pages
        blocks, vision_pages = _escalate(blocks, source, api_key, vision_fn,
                                         main, statement_pages, page_confidence)

    # A P&L face that prints only "Direct / Indirect Expenses" takes its
    # breakdown from the schedule pages of the SAME file (see
    # financials.find_schedules). page_rows holds every page, so schedules
    # that relevance dropped are still seen.
    schedules = F.find_schedules(page_rows)
    for b in blocks:
        if b["status"] != F.FAILED:
            b["schedules_used"] = F.expand_with_schedules(b, schedules)

    name = getattr(source, "name", source if isinstance(source, str) else "")
    for b in blocks:
        b["source"] = os.path.basename(str(name)) if name else ""
    _infer_missing_years(blocks)
    candidates = [b for b in blocks
                  if b["kind"] in SPREAD_KINDS and b["status"] != F.FAILED]
    return {
        "source_name":      name,
        "identity":         identity,
        "scanned":          scanned,
        "book_profit":      book_profit,
        "book_profit_page": book_profit_page,
        "return_figures":   return_figures,
        "pages_total":      len(page_texts),
        "pages_used":       len(pages),
        "page_summary":     relevance.summary(page_texts),
        "blocks":           blocks,
        "vision_pages":     vision_pages,
        # Note lines naming unsecured loans from the owners (extract/
        # borrowings.py): what a generic "Long Term Borrowings" line is.
        "owner_loan_lines": owner_loan_lines(page_texts),
        # The year this FILE is about, for statements that print no period
        # of their own and for the computation sheet's restated profit.
        "year":             _fy_end_year(candidates, identity),
    }


def _assemble(year, blocks: list, docs: list, api_key: str = None,
              invoke_fn=None) -> dict:
    """
    One financial year's column, from `blocks` - every statement, from any of
    the uploaded files in `docs`, that belongs to that year.

    A borrower's case rarely arrives one-PDF-per-year: the ITR acknowledgement
    and the financial statements are often separate files, an audit report
    reprints the same statements, and one bundle can carry two years (on a
    real case, "ITR 2022-2023.pdf" held the FY2023 AND the FY2024 Balance
    Sheet). Read file by file, each PDF became its own column - duplicates,
    a wrongly-dated column, and an empty column for a loan offer letter.
    Pooled by each statement's OWN period instead, `_dedupe` keeps the best
    reading of each statement (verified first) and drops the reprints.
    """
    identity = next((d["identity"] for d in docs if d["year"] == year),
                    docs[0]["identity"] if docs else {})
    main = _main_entity(blocks, identity)
    # A statement that misses its own total by a hair is kept, its failing
    # section's lines doubtful - not dropped whole (F.salvageable).
    for b in blocks:
        if b["kind"] in SPREAD_KINDS and b["status"] == F.FAILED and F.salvageable(b):
            b["salvaged"] = True
    usable = _dedupe([b for b in blocks
                      if b["kind"] in SPREAD_KINDS
                      and (b["status"] != F.FAILED or b.get("salvaged"))],
                     main_entity=main)

    # Each line keeps where it came from, so every sheet figure can show the
    # lines it adds up (see `sources` below).
    located = [(it[0], it[1], b.get("page"), b.get("source", ""), level)
               for b in usable
               for it, level in zip(_spread_items(b), F.item_statuses(b))]
    items = [(l, a) for l, a, _p, _s, _t in located]
    learned = taxonomy.load_learned()
    buckets, unmapped, ignored = taxonomy.map_items(items, learned)
    resolved = {}

    # Only the labels nothing claimed go to the model, and only their text.
    if unmapped and api_key and invoke_fn:
        resolved = taxonomy.llm_map([l for l, _a in unmapped], api_key, invoke_fn) or {}
        if resolved:
            still = []
            for label, amount in unmapped:
                key = resolved.get(label)
                if key:
                    buckets[key] = buckets.get(key, 0) + amount
                else:
                    still.append((label, amount))
            unmapped = still

    mapping_check = taxonomy.check_mapping(items, buckets, unmapped, ignored)

    # Transparency: row key -> every source line that was added into it, as
    # {label, amount, page, file}. The workbook writes each figure as the sum
    # of these lines, so an analyst can see what makes up "CC/OD 1.35".
    sources = {}
    for label, amount, page, src, level in located:
        key = taxonomy.map_label(label, learned)
        if key is None:
            key = resolved.get(label)
        if not key or key is taxonomy.IGNORE:
            continue
        sources.setdefault(key, []).append({
            "label": re.sub(r"^\[[A-Z]+\]\s*", "", label),
            "amount": taxonomy._mapped_amount(label, amount),
            "page": None if page is None else page + 1,
            "file": src,
            "trust": level})

    # A generic "Long Term Borrowings" face line is mapped to secured loans by
    # default. When a note in the same filing says that amount is unsecured
    # loans from the directors, promoters or relatives, it is quasi equity,
    # and is moved to unsecured loans, with the note cited.
    owner_note = None
    generic = [src for src in sources.get("secured_loan_asset_financed", [])
               if GENERIC_LT_BORROWINGS.match(src["label"] or "")]
    if generic:
        amount = sum(src["amount"] for src in generic)
        lines = [line for d in docs for line in d.get("owner_loan_lines") or []]
        owner_note = owners_loan_note(lines, amount)
        if owner_note is not None:
            buckets["secured_loan_asset_financed"] = buckets.get("secured_loan_asset_financed", 0) - amount
            buckets["unsecured_loans"] = buckets.get("unsecured_loans", 0) + amount
            sources["secured_loan_asset_financed"] = [x for x in sources["secured_loan_asset_financed"] if x not in generic]
            if not sources["secured_loan_asset_financed"]:
                del sources["secured_loan_asset_financed"]
            sources.setdefault("unsecured_loans", []).extend(generic)

    # A statement we could not read must not render as a column of zeros. If
    # the P&L failed and the balance sheet passed, the P&L rows are UNKNOWN
    # ("Check ITR"), not nil - the difference between "this business earned
    # nothing" and "we could not read what it earned" is the whole decision.
    have = {b["kind"] for b in usable}
    values = taxonomy.compute(buckets,
                              have_pl=F.PROFIT_LOSS in have,
                              have_bs=F.BALANCE_SHEET in have)

    # The tax lines of a Schedule III P&L sit under a "Tax expense" heading
    # that OCR can separate from them, leaving an untagged "Current Year" no
    # rule claims (a real filing: Rs 21.11 lakh of tax lost, profit after tax
    # read as profit before tax). In a P&L that reconciled, such a trailing
    # line named current or deferred tax, smaller than the profit, is the tax.
    tax_note = None
    if F.PROFIT_LOSS in have and not buckets.get("provision_tax") and not buckets.get("provision_deferred_tax"):
        found = _untagged_tax_lines(usable, values.get("profit_before_tax"))
        for key, label, amount, page in found:
            buckets[key] = buckets.get(key, 0) + amount
            sources.setdefault(key, []).append({"label": label, "amount": amount, "page": page, "file": "",
                                               "trust": "consistent"})
        if found:
            values = taxonomy.compute(buckets, have_pl=True, have_bs=F.BALANCE_SHEET in have)
            tax_note = ", ".join(label for _k, label, _a, _p in found)

    # A company, LLP or firm that made a profit pays tax, and its P&L shows
    # it. With no tax line read, profit after tax is not known: it is left
    # unknown rather than shown equal to profit before tax. (A proprietor's
    # tax is personal and never in the P&L, so this does not apply there.)
    untaxed = False
    if (F.PROFIT_LOSS in have and not buckets.get("provision_tax") and not buckets.get("provision_deferred_tax")
            and (values.get("profit_before_tax") or 0) > 0 and _taxed_entity(identity, main)):
        untaxed = True
        for k in ("profit_after_tax", "profit_available", "cash_profit"):
            values[k] = None

    # Screened the same way _main_entity screens its candidates: a line
    # already rejected as a name (an address, a sentence from a note page)
    # must not title the column just because it was the first one going.
    entity = main or next((b["entity"] for b in usable
                           if _belongs_to(b.get("entity", ""))), "")

    notes = taxonomy.assumptions(items, learned)
    if tax_note:
        notes.insert(0, {"label": tax_note, "amount": None, "target": "Provision for tax",
                         "note": "An untagged line under the P&L named as tax, read as the tax provision."})
    if untaxed:
        notes.insert(0, {"label": "(tax)", "amount": None, "target": "Profit after tax",
                         "note": ("No tax line was read on this P&L, and this taxpayer pays tax on its profit: "
                                  "profit after tax, profit available and cash profit are left unknown.")})
    if owner_note is not None:
        notes.insert(0, {
            "label": "Long Term Borrowings", "amount": None, "target": "Unsecured loans (quasi equity)",
            "note": (f"Moved from secured loans: the note to accounts on page {owner_note[0]} says these are "
                     f"unsecured loans from the owners or their relatives, which count as the owners' money."),
        })
    from_comp = sorted({b["kind"].replace("_", " ") for b in usable
                        if b.get("from_comparative")})
    if from_comp:
        notes.insert(0, {
            "label": "(whole statement)", "amount": None,
            "target": ", ".join(from_comp),
            "note": ("Read from the comparative column printed beside a later "
                     "year's statement, not from this year's own filing. The "
                     "column balances against its own printed totals, but a "
                     "later auditor can reclassify figures between buckets."),
        })
    scales = {b.get("unit_scale", 1) for b in usable}
    for scale in sorted(s for s in scales if s and s != 1):
        name = {1000: "thousands", 100000: "lakhs",
                1000000: "millions", 10000000: "crores"}.get(scale, f"x{scale}")
        notes.insert(0, {
            "label": "(whole statement)", "amount": None, "target": "all rows",
            "note": f"Statement presented in {name}; every figure multiplied "
                    f"by {scale:,} to convert to rupees",
        })
    for b in usable:
        if b.get("via_vision"):
            notes.append({
                "label": f"page {b['page'] + 1}", "amount": None,
                "target": b["kind"],
                "note": "OCR could not reconcile this statement; it was re-read "
                        "from the page image by Gemini Vision and accepted only "
                        "because the re-read balanced",
            })

    # A vertical statement's comparative column (see financials._harvest_vertical)
    # is a free second reading of the PRIOR year's own grand totals - taken
    # only from a block that itself VERIFIED, so a page whose current-period
    # OCR is already suspect doesn't also donate an untrustworthy comparative
    # reading. Used by spread_many's cross-year recovery pass, not here: this
    # column's own figures are never touched by another year's comparative.
    comparative = {b["kind"]: b["comparative"] for b in blocks
                  if b["kind"] in SPREAD_KINDS and b["status"] == F.VERIFIED
                  and b.get("comparative")}

    # The computation sheet restates the profit of the year its FILE is about.
    book, book_page = next(((d["book_profit"], d["book_profit_page"]) for d in docs
                            if d["year"] == year and d["book_profit"] is not None),
                           (None, None))
    # The acknowledgement's own figures, from the file for this year.
    return_figures = next((d["return_figures"] for d in docs
                           if d["year"] == year and d.get("return_figures")), {})
    summary = {}
    for d in docs:
        for k, v in (d.get("page_summary") or {}).items():
            summary[k] = summary.get(k, 0) + v

    return {
        "year":           year,
        # "PROV. BALANCE SHEET", "Provisional P&L" - the sheet's column
        # heading then says "Provisional" instead of "Audited".
        "provisional":    any(re.search(r"\bprov(?:isional)?\b\.?", b.get("title") or "", re.I)
                              for b in usable),
        "entity":         entity,
        "identity":       identity,
        "scanned":        any(d["scanned"] for d in docs),
        "pages_total":    sum(d["pages_total"] for d in docs),
        "pages_used":     sum(d["pages_used"] for d in docs),
        "page_summary":   summary,
        "blocks":         blocks,
        "blocks_used":    usable,
        "values":         values,
        "sources":        sources,
        "ratios":         taxonomy.ratios(values),
        "vision_pages":   [p for d in docs for p in d["vision_pages"]],
        "unmapped":       unmapped,
        "ignored":        ignored,
        "assumptions":    notes,
        "mapping_check":  mapping_check,
        "comparative":    comparative,
        "book_profit":    book,
        "book_profit_page": book_page,
        "return_figures": return_figures,
        "source_name":    " + ".join(os.path.basename(str(d["source_name"]))
                                     for d in docs),
        "warnings":       ([f"{' and '.join(from_comp)} taken from the comparative "
                            f"column of a later year's statement - no filing for "
                            f"this year was uploaded"] if from_comp else [])
                          + _warnings(blocks, usable, unmapped, mapping_check,
                                    multi=len(docs) > 1, return_figures=return_figures)
                          + _profit_crosscheck(values, ignored)
                          + _book_profit_crosscheck(values, book, book_page),
    }


def spread_document(source, extract_fn, api_key: str = None,
                    invoke_fn=None, on_progress=None, vision_fn=None) -> dict:
    """One ITR PDF → one year column, read on its own. spread_many pools
    several files by year instead."""
    doc = _read_document(source, extract_fn, api_key, on_progress, vision_fn)
    return _assemble(doc["year"], doc["blocks"], [doc], api_key, invoke_fn)


# The computation sheet restates profit to the rupee, but it can be the
# profit BEFORE or AFTER a few adjustments depending on the CA's layout, and
# it is read from whole-page text rather than a proven block. A wider
# tolerance than _PROFIT_TOLERANCE keeps it from crying wolf on rounding.
_BOOK_PROFIT_TOLERANCE = 1000


def _book_profit_crosscheck(values: dict, book, page) -> list:
    """
    Compare our derived profit with the one the computation of income
    restates - an independent source (typed separately, usually digital),
    unlike a re-read of the statement image. Signed, so a loss read as a
    profit is caught. Warns only; never changes a figure.
    """
    derived = values.get("profit_before_tax")
    if book is None or derived is None:
        return []
    if abs(book - derived) <= _BOOK_PROFIT_TOLERANCE:
        return []
    return [f"Derived profit before tax ({derived:,.0f}) does not match the "
            f"{book:,.0f} restated on the computation of income (page "
            f"{page + 1}) - check the P&L's income and expense lines against "
            f"the statement"]


# The statement's printed result and our derived one should agree to the rupee;
# a few hundred is rounding, more is a misread line.
_PROFIT_TOLERANCE = 500


def _profit_crosscheck(values: dict, ignored: list) -> list:
    """
    Compare the profit we derived against the one the statement printed.

    A second, independent check on the P&L: the two sides balancing proves the
    figures were read consistently, but not that each was read CORRECTLY - a
    revenue line misread low is absorbed by the closing profit line and the
    account still balances. Comparing against the printed result catches that.
    """
    stated = taxonomy.stated_result(ignored)
    derived = values.get("profit_before_tax")
    if stated is None or derived is None:
        return []
    gap = abs(abs(stated) - abs(derived))
    if gap > _PROFIT_TOLERANCE:
        return [f"Derived profit ({derived:,.0f}) differs by {gap:,.0f} from the "
                f"{abs(stated):,.0f} the statement itself prints - a line on the "
                f"income or expense side has probably been misread"]
    # Magnitudes agree, but that alone cannot see a sign flip - a Rs 43.91 lakh
    # loss read as a Rs 43.91 lakh profit has zero magnitude gap (see
    # docs/SHORTCOMINGS.md Case 11). The printed line's own sign is not
    # reliable either (a T-account's closing "To Net Profit" sits on the debit
    # side), but its WORDING is: a line that says "loss" and not "profit"
    # names a loss outright.
    says_loss = taxonomy.stated_is_loss(ignored)
    if says_loss is not None and abs(derived) > _PROFIT_TOLERANCE \
            and says_loss != (derived < 0):
        return [f"Derived {'profit' if derived >= 0 else 'loss'} of "
                f"{abs(derived):,.0f} has the opposite sign to the "
                f"{'loss' if says_loss else 'profit'} the statement itself "
                f"prints - a sign has probably been misread"]
    return []


# Below this, a document's "financial statements" carry no money worth
# spreading - the schedules are present but filled with nil.
_NIL_RETURN_MAX = 100000


def _looks_nil(blocks: list) -> bool:
    """
    Is this a NIL return rather than a failed extraction?

    A company's first assessment year often precedes any trading, so the ITR
    is filed with every financial schedule at zero. That is a finding about the
    borrower - no accounts to spread - not a fault in the reading, and saying
    "no statements found" would send an analyst hunting for a problem that
    isn't there. Confirmed on a real file: Borrower K's own audited balance sheet
    prints a dash in every cell of that year's comparative column.
    """
    if not blocks:
        # Nothing statement shaped at all is not evidence of a nil return:
        # a proprietor's filing is often just the acknowledgement and the
        # computation of income (a real one declared a 48 lakh business
        # loss and was once labelled NIL here). _warnings says what it is.
        return False
    biggest = max((abs(a) for b in blocks for _l, a in F.all_items(b["sides"])),
                  default=0)
    return biggest < _NIL_RETURN_MAX


def _where(b: dict, multi: bool) -> str:
    """'page 19' - or 'page 19 of Audit Report.pdf' when a year's column was
    built from several files."""
    src = b.get("source") if multi else ""
    return f"page {b['page'] + 1}" + (f" of {src}" if src else "")


def _warnings(blocks, usable, unmapped, mapping_check, multi: bool = False,
              return_figures: dict = None) -> list:
    out = []
    for b in blocks:
        # A note/disclosure table (Reserves & Surplus, Share Capital, ...)
        # often reprints P&L-shaped figures and can fail its own arithmetic
        # without ever being a candidate statement - it was never going to be
        # used, so flagging it as a reconciliation failure only alarms the
        # analyst about nothing. Only warn about blocks that look like a
        # real statement in the first place.
        if b["status"] == F.FAILED and _belongs_to(b.get("entity", "")):
            out.append(f"{b['kind']} on {_where(b, multi)} "
                       f"({b.get('entity') or 'unnamed'}) did not reconcile: "
                       f"{b['check']['reason']}")
    for b in usable:
        if b["status"] == F.UNVERIFIED:
            out.append(f"{b['kind']} on {_where(b, multi)} "
                       f"({b.get('entity') or 'unnamed'}) prints no total, so "
                       f"its figures could not be checked")
    if not usable:
        if not blocks and return_figures:
            out.append("This file holds the return acknowledgement and computation "
                       "of income only, no balance sheet or profit and loss "
                       "account, so there are no financial statements to spread")
        elif _looks_nil(blocks):
            out.append("This appears to be a NIL return - the financial "
                       "schedules are present but carry no figures. Common for "
                       "a company's first assessment year, before it began "
                       "trading. Nothing to spread from this document.")
        else:
            out.append("No usable financial statements were found in this "
                       "document")
    if unmapped:
        total = sum(a for _l, a in unmapped)
        out.append(f"{len(unmapped)} line item(s) totalling {total:,} could not "
                   f"be classified and are excluded")
    if not mapping_check["ok"]:
        out.append(f"Mapping lost {mapping_check['diff']:,} against the "
                   f"harvested total")
    return out


def _recover_from_comparative(cols: list) -> None:
    """
    A column whose own statement failed can sometimes be rescued from the
    FOLLOWING year's own comparative column - a Schedule III statement
    prints the prior year's grand totals right next to its own (see
    financials._harvest_vertical), and spread_document only ever captures
    that reading from a block that itself VERIFIED. Confirmed valuable on a
    real filing (Borrower G): AY2023-24's Balance Sheet
    and P&L were too badly scanned to verify on their own, but AY2024-25's
    statements printed AY2023-24's grand totals as their comparative
    column, matching a manual read of the original page almost to the
    rupee.

    Deliberately scoped to grand totals only (Total Revenue, Total
    Expenses, Total Assets) - never Profit After Tax (tax is not shown in
    a comparative column) and never individual line items or Net Worth,
    which a real filing showed a new auditor legitimately reclassifying
    between long-term and short-term borrowings across filings while the
    totals held. A recovered figure is flagged in both `assumptions` and
    `warnings`, never silently blended in as if it were this year's own
    verified reading.

    Mutates `cols` in place. Called after every column has already been
    built and sorted by year, since recovery always looks at the
    FOLLOWING year's column.
    """
    for i, col in enumerate(cols):
        if i + 1 >= len(cols) or col.get("year") is None:
            continue
        nxt = cols[i + 1]
        source = nxt.get("source_name") or "the following year"
        for kind, comp in (nxt.get("comparative") or {}).items():
            if comp.get("year") != col["year"]:
                continue
            if col.get("entity") and nxt.get("entity") and (
                    _norm_entity(col["entity"]) != _norm_entity(nxt["entity"])):
                continue
            totals = comp.get("totals") or {}

            if kind == F.PROFIT_LOSS and col["values"].get("sales_other_income") is None:
                rev, exp = totals.get("total_revenue"), totals.get("total_expenses")
                if rev is not None and exp is not None:
                    col["values"]["sales_other_income"] = rev
                    col["values"]["gross_expenses"] = exp
                    col["values"]["profit_before_tax"] = rev - exp
                    col["assumptions"].append({
                        "label": "(whole P&L)", "amount": None, "target": "P&L rows",
                        "note": (f"This year's own P&L could not be read; Total "
                                f"Revenue and Total Expenses recovered from "
                                f"{source}'s own comparative column instead. "
                                f"Profit after tax is NOT recovered this way "
                                f"(tax is not shown in a comparative column) "
                                f"and stays unknown."),
                    })
                    col["warnings"].append(
                        "P&L recovered from next year's comparative column, not "
                        "this document's own statement - treat with more "
                        "caution than a directly-verified figure")
                elif rev is not None:
                    # Revenue alone (the comparative printed no expenses total,
                    # confirmed on a real filing): turnover is still the
                    # figure a sales trend needs, so it is recovered, and
                    # expenses and profit stay unknown rather than guessed.
                    col["values"]["sales_other_income"] = rev
                    col["assumptions"].append({
                        "label": "(P&L revenue)", "amount": None, "target": "Turnover",
                        "note": (f"This year's own P&L could not be read; Total "
                                f"Revenue recovered from {source}'s own "
                                f"comparative column instead. Expenses and "
                                f"profit are NOT recovered and stay unknown."),
                    })
                    col["warnings"].append(
                        "Turnover recovered from next year's comparative column, "
                        "not this document's own statement - treat with more "
                        "caution than a directly-verified figure")

            if kind == F.BALANCE_SHEET and col["values"].get("total_assets") is None:
                assets = totals.get("total_assets")
                if assets is not None:
                    col["values"]["total_assets"] = assets
                    col["assumptions"].append({
                        "label": "(whole Balance Sheet)", "amount": None,
                        "target": "Total Assets",
                        "note": (f"This year's own Balance Sheet could not be "
                                f"read; Total Assets recovered from {source}'s "
                                f"own comparative column instead. Individual "
                                f"liability/asset buckets and Net Worth are NOT "
                                f"recovered this way and stay unknown."),
                    })
                    col["warnings"].append(
                        "Total Assets recovered from next year's comparative "
                        "column, not this document's own statement - treat "
                        "with more caution than a directly-verified figure")


_TAX_LINE = re.compile(r"(?i)^\s*(?:\(\d\)\s*)?(current\s+(?:tax|year)|deferred\s+tax|tax\s+expenses?|income\s+tax)\s*$")
_TAXED_ENTITY = re.compile(r"(?i)\b(limited|ltd|llp|private|pvt)\b")


def _untagged_tax_lines(blocks: list, profit_before_tax) -> list:
    """[(key, label, amount, page)] for untagged P&L lines named as tax,
    each smaller than the profit before tax."""
    out = []
    if not profit_before_tax or profit_before_tax <= 0:
        return out
    for b in blocks:
        if b.get("kind") != F.PROFIT_LOSS:
            continue
        for section in (b.get("sides") or {}).get("sections") or []:
            for label, amount in section.get("items") or []:
                if str(label).startswith("[") or not isinstance(amount, (int, float)):
                    continue
                if _TAX_LINE.match(str(label)) and 0 < amount < profit_before_tax:
                    key = "provision_deferred_tax" if "deferred" in str(label).lower() else "provision_tax"
                    page = None if b.get("page") is None else b["page"] + 1
                    out.append((key, str(label).strip(), amount, page))
    return out


def _taxed_entity(identity: dict, entity: str) -> bool:
    """A company, LLP or firm: its P&L carries its own tax. Read from the
    ITR's assessee status, else the entity's name."""
    status = str((identity or {}).get("assessee_status") or "").lower()
    if status:
        return status in ("company", "firm", "llp", "aop", "boi")
    return bool(_TAXED_ENTITY.search(entity or ""))


def _empty_column(source_name: str, warnings: list) -> dict:
    return {
        "year": None, "entity": "", "identity": {}, "scanned": False,
        "pages_total": 0, "pages_used": 0, "page_summary": {},
        "blocks": [], "blocks_used": [], "values": {}, "ratios": {},
        "unmapped": [], "ignored": [], "assumptions": [],
        "mapping_check": {"ok": False, "diff": 0},
        "comparative": {}, "book_profit": None, "provisional": False,
        "return_figures": {}, "vision_pages": [], "source_name": source_name, "warnings": warnings,
    }


def spread_many(sources, extract_fn, api_key: str = None,
                invoke_fn=None, on_progress=None, vision_fn=None) -> list:
    """
    All of one borrower's uploaded files → one column per financial year,
    oldest first.

    Every file is read, then every statement is placed in the year its own
    period says ("as at 31st March 2024" - or, when it prints none, the year
    of the file it came from) and each year is assembled from whatever files
    hold its statements (see `_assemble`). A file with no statements at all -
    a loan offer letter, a covering page - gets no column of its own, only a
    note; so does a file that could not be read at all, which must not cost
    the others their columns.
    """
    docs, notes, total = [], [], len(sources)
    for i, src in enumerate(sources):
        cb = (lambda p, t, _i=i: on_progress(_i, total, p, t)) if on_progress else None
        name = getattr(src, "name", src if isinstance(src, str) else "")
        try:
            docs.append(_read_document(src, extract_fn, api_key, cb, vision_fn))
        except Exception as e:
            notes.append(f"Could not read {os.path.basename(str(name))}: {e}")

    def _amounts(b):
        return {a for _l, a in F.all_items(b["sides"]) if a}

    # Each year's statements from files that are ABOUT that year (the
    # statement's own period matches its file's): (business, kind) -> the
    # figures of each such statement.
    claimed = {}
    for d in docs:
        for b in d["blocks"]:
            if (b["kind"] in SPREAD_KINDS and not b.get("from_comparative")
                    and (b.get("year") or d["year"]) == d["year"]):
                claimed.setdefault(d["year"], {}).setdefault(
                    (_norm_entity(b.get("entity", "")), b["kind"]), []).append(_amounts(b))

    def _year_of(b, d):
        """
        The year a statement goes in: its own period, with one exception.

        When that period differs from its file's and the other year already
        holds this business's same statement from a file about that year,
        the FIGURES decide:
          - the same figures: a REPRINT (an audit report or a later filing
            repeating the statement) - it moves, and _dedupe drops it there.
            Borrower H's FY2025 Balance Sheet reprinted inside "ITR 2023-2024.pdf"
            is one; kept in 2024 instead, it was added to FY2024's own and
            doubled that year's totals.
          - different figures: a MISREAD date - it stays with its file. On a
            real case (Borrower R) page 2 of the FY2025 P&L read as
            2024; moved, FY2025 lost its depreciation and interest (PBT
            134.81 -> 353.43 lakh) and the page sat unused beside FY2024's
            genuine, differently-figured P&L.
        """
        y = b.get("year") or d["year"]
        # A comparative column is last year's figures printed inside this
        # year's statement: its year is not in doubt, and the reprint/misread
        # reasoning below (which is about a page whose DATE may be wrong)
        # would otherwise drag it into the year it was printed in and add it
        # to that year's own figures.
        if b.get("from_comparative"):
            return y
        if y == d["year"] or d["year"] is None or b["kind"] not in SPREAD_KINDS:
            return y
        rivals = claimed.get(y, {}).get((_norm_entity(b.get("entity", "")), b["kind"]))
        if not rivals:
            return y
        mine = _amounts(b)
        if mine and any(len(mine & r) > _DUPLICATE_SHARE * len(mine) for r in rivals):
            return y                 # a reprint - moves, then deduped
        return d["year"]             # a misread date - stays with its file

    groups = {}          # year -> (blocks, docs)
    for d in docs:
        for b in d["blocks"]:
            y = _year_of(b, d)
            blocks, members = groups.setdefault(y, ([], []))
            blocks.append(b)
            if d not in members:
                members.append(d)

    # The weakest reading of a year, kept only where that year has no
    # statement of its own of that kind: a comparative column is one column
    # of a statement about ANOTHER year. Where the borrower's own filing for
    # the year is in the upload, that filing decides - and the comparative
    # must not be pooled ALONGSIDE it, which would add the two together.
    #
    # Only a READABLE own statement outranks it, though: a year whose own P&L
    # is broken beyond salvage takes the other printing for the whole
    # statement instead of reading "Check ITR". And the other printing is not
    # thrown away - it is kept apart as the year's `alternate`, so the
    # consistency checks can compare the two printings and fill a field one of
    # them lost from the other (engine/checks.py).
    alternates = {}
    for y, (blocks, _members) in groups.items():
        own = {b["kind"] for b in blocks
               if not b.get("from_comparative") and b["kind"] in SPREAD_KINDS
               and (b["status"] != F.FAILED or F.salvageable(b))}
        other = [b for b in blocks if b.get("from_comparative") and b["kind"] in own]
        if other:
            alternates[y] = other
            kept_apart = {id(b) for b in other}
            blocks[:] = [b for b in blocks if id(b) not in kept_apart]

    for d in docs:
        if d["blocks"]:
            continue
        label = os.path.basename(str(d["source_name"]))
        if d["year"] is not None and d["year"] not in groups:
            # A year with no statements anywhere still gets its column - a NIL
            # return is a finding about the borrower, not a reading failure.
            groups[d["year"]] = ([], [d])
        elif d["year"] in groups:
            groups[d["year"]][1].append(d)
        else:
            notes.append(f"Not used: {label} - no financial statements found in it")

    # Statements that printed no period, from a file with no year either, can
    # only form an undated column - and if none of them was usable it would
    # be a column of unread zeros headed by a file name. On a real case
    # ("ITR 2025-26.pdf", a 2-page P&L that failed its check) that column's
    # heading also read as "2025" and shadowed the real FY2025 column. Such a
    # file gets a note instead.
    undated = groups.get(None)
    if undated and not any(b["kind"] in SPREAD_KINDS and b["status"] != F.FAILED
                           for b in undated[0]):
        del groups[None]
        for d in undated[1]:
            notes.append(f"Not used: {os.path.basename(str(d['source_name']))} - its "
                         f"statements could not be read or dated")

    cols = [_assemble(y, blocks, members, api_key, invoke_fn)
            for y, (blocks, members) in groups.items()]
    if not cols:
        cols = [_empty_column(" + ".join(os.path.basename(str(getattr(s, "name", s)))
                                         for s in sources), [])]
    for col in cols:
        other = alternates.get(col["year"]) if col["year"] is not None else None
        if other:
            col["alternate"] = _assemble(col["year"], other, groups[col["year"]][1])
    cols.sort(key=lambda c: (c["year"] is None, c["year"] or 0))
    cols[0]["warnings"] = notes + cols[0]["warnings"]
    _recover_from_comparative(cols)
    # Imported here, not at the top: checks.py reuses this module's
    # cross-checks, and a top-level import would be circular.
    from . import checks
    checks.run_checks(cols)
    return cols
