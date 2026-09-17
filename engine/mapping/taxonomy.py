"""
taxonomy.py  -  map harvested statement labels onto the spreading template.

financials.py extracts what a statement says, without interpreting it. This
module decides what each line MEANS: "Pagar Bhatta Exp." and "Employee Benefits
Expenses" are both Employee Costs; "Trade Payables" and "Sundry Creditors" are
both Current Liabilities.

This is where format variance actually lives. A CA package's layout can be
handled structurally (see financials.py), but the words it chooses cannot -
they vary per software, per firm, and per business. So the design is:

  1. a SYNONYM table, which is deterministic, free, and covers the labels that
     recur across filings;
  2. Gemini for the leftovers ONLY - and it is sent the label STRINGS, not page
     images and not the document, so the call is small and cheap;
  3. every mapping the model returns is written back to a local dictionary, so
     the same wording is free next time. Cost falls with use rather than
     recurring per document.

Mapping is verified, not trusted: the mapped buckets must still add up to the
same total the statement itself printed (`check_mapping`). A label the model
drops, duplicates, or invents breaks that identity and is caught.
"""

import json
import os
import re

from .template_config import (
    PL_INCOME, PL_EXPENSE, PL_TAX, BS_LIABILITY, BS_ASSET, MAPPABLE, DERIVED,
    LABELS, SYNONYMS, CONVENTIONS,
)

_COMPILED = [(key, re.compile(pat, re.I)) for key, pat in SYNONYMS]

# Sentinel for rows that are RESULTS, not inputs.
IGNORE = "__derived__"

# Subtotal and outcome lines. These must never enter a bucket: they restate
# figures already captured by the items above them, so counting them adds the
# same money twice - and their wording collides with real categories.
#
# "V Profit before Exceptional and Extraordinary Items and Tax (III-IV)" was
# matching the Extraordinary Item rule on a real filing. Two such lines put
# -30.65 lakhs of "extraordinary items" into expenses, which turned a
# -15.32 lakh loss into a +15.32 lakh profit - a sign flip on the single figure
# a credit analyst cares most about.
_IGNORE_RE = re.compile(
    r"^\s*(?:\[[A-Z]{2,3}\]\s*)?"          # optional section-context token
    # Stray punctuation a scan leaves before the text - the ditto marks under
    # "To"/"By" OCR as "#", '"' or "|". "# Net profit trf to Capital A/c"
    # (Borrower B FY24) missed this whole pattern and the year's Rs 14.46
    # lakh profit was counted as an expense, reading profit as nil.
    r"(?:[#\"'|*~`]+\s*)?"
    r"(?:(?:to|by)\s+)?"                   # T-account prefix: "To Net Profit"
    r"(?:[IVX]{1,4}[\s.)]+|\(?\d{1,2}\)?[\s.)]+)?"
    # Statements introduce their movement lines with "Add:" / "Less:"
    # ("Add: Profit/(Loss) for the year"), which must not hide the subtotal.
    r"(?:add\s*:?\s*|less\s*:?\s*)?"
    # "Total" glued to the next word by OCR ("TotalAssets") is still a total.
    # "total outstanding dues of micro ... / of creditors other than micro
    # and small enterprises" is Schedule III's spelling of TRADE PAYABLES -
    # a line item that merely opens with "total". Set aside here as a
    # subtotal, Rs 432.25 lakh of creditors never reached the sheet and
    # Total of Liabilities came up short by exactly that amount
    # (Borrower D FY2025). A real "Total ..." row is still ignored.
    r"(?:total(?!\s+outstanding\s+dues)(?:\b|(?=[A-Z]))|gross\s+total\b|profit\s+(?:before|after|for\s+the|available)"
    r"|(?:nett?\s+)?profit\s*/?\s*\(?loss\)?\b|loss\s+for\s+the"
    # A T-account P&L closes with the period's result on whichever side
    # balances it: "Net Loss", "Net Profit c/f", "Loss transferred to Capital".
    # "Nett" is Tally's own spelling ("Nett Profit").
    r"|nett?\s+(?:profit|loss)\b|(?:profit|loss)\s+(?:carried|transferred|c/?f)\b"
    # A Trading Account and P&L Account sharing one combined heading close
    # the Trading half by carrying its own result down as "To Gross Profit"
    # (an expense-side plug) and reopening the P&L half with "By Gross
    # Profit" (the same figure, now income). That is an internal transfer
    # between the two halves of ONE statement, not a real expense or
    # income - counted as either it doubles the true revenue and cost by
    # exactly that amount, on a real filing by Rs 66.13 lakh each way.
    # "pr\w{1,4}t", not "profit": a scanned "Gross prfit trf to P & L A/c"
    # (Borrower B, both years) went unrecognised, was counted as an
    # EXPENSE, and turned a Rs 10.70 lakh profit into a Rs 31.17 lakh loss.
    r"|gross\s+(?:pr\w{1,4}t|loss)\b"
    r"|earnings?\s+per|\bE\.?P\.?S\.?\b|basic\s*\(|basic\s*(?:&|and)\s*diluted|diluted"
    r"|carried\s+(?:to|forward)|balance\s+(?:c/?f|b/?f)"
    r"|as\s+per\s+last\s+balance\s+sheet)", re.I)

# Inside a Balance Sheet's CAPITAL section ([EQ], see financials._SECTIONS),
# "Add: Profit for the Year" is not a restated subtotal - it is one of the
# additions that make up closing capital, and the harvest that proved the
# Balance Sheet counted it. Ignoring it (as the rule below does everywhere
# else) silently cut Net Worth by the whole year's profit on a real filing
# (Borrower L: Rs 54.71 lakh). A LOSS line is deliberately not included: a
# CA prints "Less: Net Loss" as a positive figure to subtract, which the
# harvest could not have summed correctly in the first place.
_EQ_PROFIT_RE = re.compile(
    r"^\[EQ\]\s*(?:(?:add|by|to)\s*:?\s*)?(?:nett?\s+)?profit\b", re.I)

# Opening/Closing Stock inside a P&L or Trading Account (tagged [INC]/[EXP] by
# financials.tag_pl_sides) is a Trading-Account computation input, not a
# genuine revenue or expense line - and this template has no Opening/Closing
# Stock row of its own. Left alone, the blanket "credit side of a P&L is
# always income" rule below claims it: on a real filing, Closing Stock
# inflated Sales by its own Rs 27.54 lakh.
#
# It is netted into Purchases - opening stock added, closing stock deducted -
# which is the analyst's own convention, confirmed to the rupee against a
# reference sheet (Borrower B FY24: Purchases 1985.23 lakh = opening 44.16
# + purchases 1960.09 - closing 19.02). The earlier choice of leaving both
# lines unmapped excluded them from every figure, so a trading business's
# profit came out wrong by the stock movement (Rs 37.6 lakh on that filing).
# Profit is unchanged by the netting: closing stock leaves the income side
# and the expense side by the same amount. A BALANCE SHEET's own Closing
# Stock line is untouched: it carries no [INC]/[EXP] tag.
_STOCK_MOVEMENT_RE = re.compile(
    r"^\[(?:INC|EXP)\].*\b(?:opening|closing)\s+stock\b", re.I)
_CLOSING_STOCK_RE = re.compile(r"\bclosing\s+stock\b", re.I)


def _mapped_amount(label: str, amount):
    """The amount a line contributes to its bucket: a P&L's closing stock is
    DEDUCTED from Purchases, everything else counts as printed."""
    if _STOCK_MOVEMENT_RE.match(label.strip()) and _CLOSING_STOCK_RE.search(label):
        return -abs(amount)
    return amount


# ─────────────────────────────────────────────────────────────────
# LEARNED DICTIONARY
# ─────────────────────────────────────────────────────────────────
# Mappings confirmed by the LLM are cached here so the same wording never costs
# a second API call. Plain JSON, in data/, safe to inspect, edit or delete by
# hand. Lives outside engine/ - it's runtime state the app writes to, not
# source the app reads only.

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_LEARNED_PATH = os.path.join(_PROJECT_ROOT, "data", "label_map.json")


def _norm(label: str) -> str:
    """Canonical form of a label for dictionary lookup."""
    s = re.sub(r"[^a-z0-9 ]+", " ", str(label).lower())
    return re.sub(r"\s+", " ", s).strip()


# This cache is ONLY reached by labels the SYNONYMS table failed to
# recognise (llm_map is called on the leftovers) - which skews it toward
# exactly the wording least likely to be generic: a related-party lender's
# own name on a capital-account line, a driver's name, a specific vehicle or
# route. That happened on a real filing - "a relative", "a lender",
# "Fasttag a payee" all ended up cached before this filter existed.
#
# A cached entry persists on disk indefinitely and is reused for every
# future borrower, with no natural expiry - so the cost of wrongly caching a
# name (a real person's identity sitting in a file) is much higher than the
# cost of wrongly declining to cache a genuine generic term (one repeat LLM
# call, next time the same document is processed). When unsure, don't cache.
# The label is still mapped for the CURRENT document either way - this only
# controls what gets written to disk for next time.
_GENERIC_TERM_RE = re.compile(
    r"expense|expenditure|income|receipt|revenue|\bsales?\b|turnover|"
    r"purchase|\bcost\b|charge|\bduty\b|dues|\btax|\bgst\b|\btds\b|\btcs\b|"
    r"cess|\bfee|fine|penalty|"
    r"stock|inventor|\bcash\b|\bbank\b|deposit|investment|\bloan|borrowing|"
    r"advance|capital|reserve|surplus|provision|payable|receivable|"
    r"creditor|debtor|\binterest\b|"
    r"salary|salaries|\bwages?\b|bonus|gratuity|provident|\bstaff\b|employee|"
    r"\brent\b|insurance|repair|maintenance|freight|transport|fuel|diesel|"
    r"\btoll\b|\btyre|vehicle|\bstores?\b|spares?|"
    r"audit|\blegal\b|profession|stationer|printing|postage|telephone|"
    r"internet|electricity|\bpower\b|\bwater\b|"
    r"depreciation|amorti[sz]ation|"
    r"\btotal\b|balance|\baccount\b|liabilit|\basset|equity|\bshare|"
    r"debenture|commission|discount|royalty|"
    r"miscellaneous|\bsundry\b|\bgeneral\b|\boffice\b|administrat|"
    r"opening|closing|written[\s-]*off|bad\s*debt", re.I)


# A run of 4+ digits in a label is an account, loan, vehicle or reference
# number ("Axis Bank A/c 17194", "Shriram Finance Loan 7001") - identifying
# data that must never be written to disk, and useless for the next borrower
# anyway, whose numbers differ.
_REFERENCE_NO_RE = re.compile(r"\d{4,}")

# A T-account P&L tags its lines [INC]/[EXP] (financials.tag_pl_sides), and
# _norm keeps those tags as a leading "inc"/"exp" word. Such a line is income
# or expense BY POSITION, so a balance-sheet answer for it is wrong for every
# future borrower too - one cached "inc by closing stock" -> current_assets.
_PL_SIDE_RE = re.compile(r"^\[(?:INC|EXP)\]", re.I)


def _looks_cacheable(label: str, key: str = None) -> bool:
    """Generic accounting/functional wording, worth remembering for the next
    borrower - as opposed to a name, a business, or a route specific to this
    one filing, which is worth nothing to cache and unsafe to keep."""
    if _REFERENCE_NO_RE.search(label):
        return False
    if key in BS_LIABILITY + BS_ASSET and _PL_SIDE_RE.match(label.strip()):
        return False
    return bool(_GENERIC_TERM_RE.search(label))


def load_learned() -> dict:
    try:
        with open(_LEARNED_PATH, encoding="utf-8") as f:
            data = json.load(f)
        # Entries written before save_learned's filters existed are dropped on
        # read as well, so a stale copy of the file can't reintroduce them: a
        # reference number is identifying data, and a normalised "inc ..." /
        # "exp ..." key is a P&L line that no balance-sheet bucket can be
        # right for.
        return {k: v for k, v in data.items()
                if v in MAPPABLE and not _REFERENCE_NO_RE.search(k)
                and not (v in BS_LIABILITY + BS_ASSET
                         and re.match(r"(?:inc|exp)\b", k))}
    except (OSError, ValueError):
        return {}


def save_learned(mapping: dict) -> None:
    """Merge `mapping` into the on-disk dictionary. Never raises - a cache that
    cannot be written is a lost optimisation, not a failed extraction."""
    try:
        current = load_learned()
        current.update({_norm(k): v for k, v in mapping.items()
                        if v in MAPPABLE and _looks_cacheable(k, v)})
        os.makedirs(os.path.dirname(_LEARNED_PATH), exist_ok=True)
        with open(_LEARNED_PATH, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(current.items())), f, indent=1)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────
# MAPPING
# ─────────────────────────────────────────────────────────────────

# A line's SECTION TAG outranks its wording.
#
# The harvester tags each line with the part of the statement it was printed
# in: [EQ]/[CL]/[FA]/... on a balance sheet, [EXP]/[INC] on the two sides of
# a P&L. That placement is structural evidence - where the accountant put the
# figure - while a synonym is only a guess from words. Where they disagree,
# the tag wins: a movement inside the capital account is not the P&L's
# interest just because it says "Interest Paid On Housing Loan", and a debit
# line is not revenue just because it says "Sales & Commission". Both are
# real (A CUSTOMER FY2026), and together they moved profit Rs 5.92
# lakh away from the printed figure.
_BS_TOKENS = {"EQ", "LIAB", "CL", "NCL", "CA", "NCA", "FA", "INV", "SL", "UL"}
_TOKEN_RE = re.compile(r"^\s*\[([A-Z]{2,4})\]")
_PL_TARGETS = frozenset(PL_INCOME + PL_EXPENSE + PL_TAX)


def _blocked_targets(label: str) -> frozenset:
    """Buckets this line's own section forbids."""
    m = _TOKEN_RE.match(label or "")
    if not m:
        return frozenset()
    token = m.group(1)
    if token in _BS_TOKENS:
        return _PL_TARGETS                      # a balance-sheet line
    if token == "EXP":
        return frozenset(PL_INCOME)             # a debit line is never income
    if token == "INC":
        return frozenset(PL_EXPENSE)            # a credit line is never a cost
    return frozenset()


def map_label(label: str, learned: dict = None):
    """
    One label → a template key, IGNORE for a derived/subtotal row, or None when
    nothing matches. The ignore test runs FIRST: a subtotal's wording overlaps
    real categories, so letting the synonym table see it produces a confident
    wrong answer rather than no answer.
    """
    if _EQ_PROFIT_RE.match(label.strip()):
        return "equity_capital"
    if _IGNORE_RE.match(label.strip()):
        return IGNORE
    if _STOCK_MOVEMENT_RE.match(label.strip()):
        return "purchases"
    key = _norm(label)
    if learned and key in learned:
        return learned[key]
    blocked = _blocked_targets(label)
    for target, rx in _COMPILED:
        if target not in blocked and rx.search(label):
            return target
    return None


def map_items(items: list, learned: dict = None) -> tuple:
    """
    [(label, amount)] → (buckets, unmapped, ignored)

    `buckets` maps template key → summed amount. `unmapped` is what nothing
    claimed - the only thing worth asking an LLM about. `ignored` is the
    subtotal and outcome rows, kept separately so they neither enter the sheet
    nor get reported as extraction failures.
    """
    learned = load_learned() if learned is None else learned
    buckets, unmapped, ignored = {}, [], []
    for label, amount in items:
        target = map_label(label, learned)
        if target is IGNORE:
            ignored.append((label, amount))
        elif target is None:
            unmapped.append((label, amount))
        else:
            buckets[target] = buckets.get(target, 0) + _mapped_amount(label, amount)
    return buckets, unmapped, ignored


_PROMPT = """\
You are classifying line items from an Indian financial statement (a balance \
sheet or profit & loss account attached to an Income Tax Return) into a fixed \
set of categories used for credit analysis.

Categories, with what belongs in each:
{catalogue}

Classify each of these line-item labels:
{labels}

Return ONLY a JSON object mapping each label EXACTLY as given to one category \
key from the list above. Rules:
- Use a category key verbatim. Do not invent categories.
- If a label genuinely fits none of them, map it to null.
- Classify by what the item IS, not by which side of the account it appeared on.
- Do not add, merge, drop or reword labels: return one entry per label given.
"""

_CATALOGUE = {
    "sales_other_income":          "revenue, sales, turnover, gross receipts, other income",
    "purchases":                   "purchases, raw material, cost of material, stock movements",
    "transport_admin":             "transport/vehicle running: diesel, freight, toll, tyres, RTO tax, repairs, insurance, electricity/power",
    "employee_costs":              "salaries, wages, staff welfare, bonus, gratuity, PF",
    "other_expenses":              "any other operating or administrative expense",
    "interest_finance":            "interest paid, bank charges, finance costs",
    "depreciation":                "depreciation and amortisation",
    "extraordinary":               "exceptional or extraordinary items",
    "provision_tax":               "current tax / income-tax provision",
    "provision_deferred_tax":      "deferred tax expense",
    "preference_dividend":         "preference dividend",
    "equity_capital":              "share capital, proprietor's or partners' capital account",
    "preference_shares":           "preference share capital",
    "reserves":                    "reserves and surplus, accumulated profit or loss",
    "secured_loan_asset_financed": "secured term loans, vehicle/machinery finance, bank term borrowings",
    "secured_loan_maturity_1yr":   "current maturities of long-term secured debt",
    "secured_loan_ccod":           "cash credit, overdraft, working-capital limits",
    "other_lt_liabilities":        "other long-term or non-current liabilities and provisions",
    "unsecured_loans":             "unsecured loans, loans from directors/partners/relatives",
    "deferred_tax_liability":      "deferred tax liability",
    "sundry_creditors":            "trade payables, sundry creditors, creditors for goods or services",
    "current_liabilities":         "other current liabilities: duties and taxes, short-term provisions, advances received",
    "fixed_assets":                "tangible fixed assets, plant, machinery, vehicles, furniture, CWIP",
    "intangible_assets":           "intangible assets, goodwill",
    "investments":                 "investments, shares, fixed deposits, gold",
    "deferred_tax_assets":         "deferred tax asset",
    "debtors":                     "trade receivables, sundry debtors, bills receivable",
    "current_assets":              "stock, cash, bank balances, short-term advances, other current assets",
    "lt_loans_advances":           "long-term loans and advances",
    "other_non_current_assets":    "other non-current assets",
}


def llm_map(labels: list, api_key: str, invoke_fn) -> dict:
    """
    Classify labels the synonym table could not, using the model cascade
    injected as `invoke_fn(api_key, prompt)`. Returns {label: key}; anything
    the model declines or garbles is simply absent.

    Only the label STRINGS are sent - not amounts, not the statement, not page
    images. That keeps the call small, keeps the borrower's figures out of the
    request, and makes the result cacheable: the same wording never needs a
    second call.
    """
    labels = sorted({l for l in labels if l and l.strip()})
    if not labels or not api_key:
        return {}

    catalogue = "\n".join(f"  {k} - {v}" for k, v in _CATALOGUE.items())
    prompt = _PROMPT.format(catalogue=catalogue,
                            labels="\n".join(f"- {l}" for l in labels))
    try:
        raw = invoke_fn(api_key, prompt)
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}

    out = {}
    for label, key in parsed.items():
        if key in MAPPABLE and label in labels:
            out[label] = key
    if out:
        save_learned(out)
    return out


# ─────────────────────────────────────────────────────────────────
# VERIFICATION
# ─────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────
# ASSUMPTIONS
# ─────────────────────────────────────────────────────────────────
# CONVENTIONS itself now lives in template_config.py, next to the SYNONYMS
# table it annotates - both describe "what goes where" and belong in the one
# file an analyst edits to change that.


def assumptions(items: list, learned: dict = None) -> list:
    """
    Every judgement call made while mapping `items`.
    Returns [{label, amount, target, note}] - one entry per affected line.
    """
    learned = load_learned() if learned is None else learned
    out = []
    for label, amount in items:
        target = map_label(label, learned)
        if target in (None, IGNORE):
            continue
        for rx, note in CONVENTIONS:
            if rx.search(label):
                out.append({"label": label, "amount": amount,
                            "target": LABELS.get(target, target), "note": note})
                break
    return out


_RESULT_RE = re.compile(
    r"(?:nett?\s+(?:profit|loss)|profit\s+(?:before\s+tax|for\s+the)"
    r"|(?:profit|loss)\s+(?:carried|transferred))", re.I)


def stated_result(ignored: list):
    """
    The profit or loss the statement itself prints, if it printed one.

    Set aside as a subtotal during mapping (it restates the lines above it),
    but it is an independent statement of the answer - so it can be checked
    against the profit derived from the harvested items. On a real filing that
    comparison caught a single revenue line misread by Rs 8,055: the derived
    profit and the printed one disagreed by exactly that amount, on a document
    whose two sides otherwise balanced perfectly.

    Returns the magnitude only; the sign convention of a T-account's closing
    line depends on which side it sits, so the caller compares absolute values.
    """
    for label, amount in ignored or []:
        if _RESULT_RE.search(label):
            return amount
    return None


def stated_is_loss(ignored: list):
    """
    Whether the statement's own result line names a LOSS (True), a PROFIT
    (False), or can't be told from its wording (None - "Net Profit/(Loss)",
    or no result line at all).

    The amount's sign cannot answer this: a T-account prints its closing
    "To Net Profit" as a plain positive figure on the debit side. The wording
    can - and it is what the sign-flip check in columns._profit_crosscheck
    needs, since comparing magnitudes alone passes a loss read as a profit.
    """
    for label, amount in ignored or []:
        if not _RESULT_RE.search(label):
            continue
        low = label.lower()
        has_profit = "profit" in low
        has_loss = bool(re.search(r"\bloss\b", low))
        if has_profit and has_loss:
            # "Net Profit/(Loss)" - wording is neutral, but a printed
            # negative is then unambiguous.
            return True if amount < 0 else None
        if has_loss:
            return True
        if has_profit:
            return amount < 0
        return None
    return None


def check_mapping(items: list, buckets: dict, unmapped: list,
                  ignored: list = (), tolerance: int = 2) -> dict:
    """
    Every rupee harvested must still be accounted for after mapping - in a
    bucket, in the unmapped list, or deliberately set aside as a subtotal.

    This is what makes it safe to let a model classify labels: if it drops a
    label, doubles one up, or answers about a label that was never sent, the
    totals stop agreeing and the discrepancy is reported rather than quietly
    shipped.
    """
    # Signed the same way map_items signs them (closing stock deducted), so
    # the netting itself is not reported as money lost.
    harvested = sum(_mapped_amount(l, a) for l, a in items)
    mapped    = (sum(buckets.values()) + sum(a for _l, a in unmapped)
                 + sum(a for _l, a in ignored))
    diff      = harvested - mapped
    return {
        "ok":        abs(diff) <= tolerance,
        "harvested": harvested,
        "mapped":    mapped,
        "diff":      diff,
        "unmapped_count":  len(unmapped),
        "unmapped_amount": sum(a for _l, a in unmapped),
    }


# ─────────────────────────────────────────────────────────────────
# DERIVED ROWS AND RATIOS
# ─────────────────────────────────────────────────────────────────

def _g(d: dict, key: str) -> float:
    return d.get(key) or 0


PL_KEYS = (PL_INCOME + PL_EXPENSE + PL_TAX
           + ["gross_receipts", "gross_expenses", "profit_before_tax",
              "profit_after_tax", "profit_available", "cash_profit"])
BS_KEYS = (BS_LIABILITY + BS_ASSET
           + ["total_liabilities", "total_assets", "networth",
              "total_current_liabilities", "total_current_assets"])


def compute(buckets: dict, have_pl: bool = True, have_bs: bool = True) -> dict:
    """
    Fill the derived rows. Computed here rather than read off the statement:
    a statement's own subtotals are used to VERIFY the harvest (see
    financials.check_block), so deriving them independently keeps the check
    meaningful instead of circular.

    `have_pl` / `have_bs` say whether each statement was actually read. When
    one was not, its rows come back as None rather than 0 - the caller renders
    that as "Check ITR". Reporting an unread P&L as a column of zeros would
    show a business with no income and no costs, which reads as a fact.
    """
    out = dict(buckets)

    income   = _g(out, "sales_other_income")
    expenses = sum(_g(out, k) for k in PL_EXPENSE)

    out["gross_receipts"]   = income
    out["gross_expenses"]   = expenses
    out["profit_before_tax"] = income - expenses
    out["profit_after_tax"] = (out["profit_before_tax"]
                               - _g(out, "provision_tax")
                               - _g(out, "provision_deferred_tax"))
    out["profit_available"] = out["profit_after_tax"] - _g(out, "preference_dividend")
    # Cash profit adds depreciation back: it is a book charge, not an outflow.
    out["cash_profit"]      = out["profit_after_tax"] + _g(out, "depreciation")

    out["total_liabilities"] = sum(_g(out, k) for k in BS_LIABILITY)
    out["total_assets"]      = sum(_g(out, k) for k in BS_ASSET)
    # As the sheet's Financial Snap: Equity + Preference Shares + Reserves.
    out["networth"]          = (_g(out, "equity_capital") + _g(out, "preference_shares")
                                + _g(out, "reserves"))
    # Not sheet rows: the Analysis tab's view of all current items.
    out["total_current_liabilities"] = (_g(out, "sundry_creditors")
                                        + _g(out, "current_liabilities"))
    out["total_current_assets"]      = _g(out, "debtors") + _g(out, "current_assets")

    # A statement that WAS read confidently reports "no such line" as 0, not
    # "Check ITR" - a logistics business genuinely has no Purchases & Raw
    # Material line, and that is a fact, not a gap. "Check ITR" is reserved
    # for a statement that could not be read at all (see below).
    if have_pl:
        for k in PL_KEYS:
            out.setdefault(k, 0)
    if have_bs:
        for k in BS_KEYS:
            out.setdefault(k, 0)

    if not have_pl:
        for k in PL_KEYS:
            out[k] = None
    if not have_bs:
        for k in BS_KEYS:
            out[k] = None
    return out


def _div(num, den):
    """Ratio, or None when the denominator is zero - never a spurious 0."""
    return None if not den else num / den


def ratios(v: dict) -> dict:
    """
    The sheet's RATIOS block, computed in Python for the app and the Analysis
    tab. None wherever the denominator is zero (the app shows "n/a"); the
    Excel sheet itself carries the live formulas in template_config.RATIOS,
    which show 0 there instead. Keys and order match template_config.
    RATIO_NAMES, and tests/test_master_excel.py evaluates the Excel formulas
    to prove the two give the same numbers.

    "(inclusive q/e)" - INCLUSIVE OF QUASI-EQUITY: unsecured loans (from the
    proprietor, partners, directors, relatives) count as the owners' own
    money for the gearing ratios - added to equity, taken out of debt. This
    reproduces the analyst's reference sheet exactly (Borrower L FY2024:
    304.74 / (269.24 + 3.00) = 1.119 and 393.23 / 272.24 = 1.444).
    """
    pat      = _g(v, "profit_after_tax")
    pbt      = _g(v, "profit_before_tax")
    interest = _g(v, "interest_finance")
    dep      = _g(v, "depreciation")
    income   = _g(v, "sales_other_income")
    sec_af   = _g(v, "secured_loan_asset_financed")
    mat_1yr  = _g(v, "secured_loan_maturity_1yr")
    unsec    = _g(v, "unsecured_loans")
    equity   = (_g(v, "equity_capital") + _g(v, "reserves")
                + _g(v, "preference_shares"))
    equity_qe = equity + unsec
    # SUM(Secured loan - Asset Financed : Current Liabilities) on the sheet -
    # every outside liability row, including Sundry Creditors.
    outside = sum(_g(v, k) for k in (
        "secured_loan_asset_financed", "secured_loan_maturity_1yr",
        "secured_loan_ccod", "other_lt_liabilities", "unsecured_loans",
        "deferred_tax_liability", "sundry_creditors", "current_liabilities"))
    curr_assets = _g(v, "debtors") + _g(v, "current_assets")
    curr_liab   = (_g(v, "sundry_creditors") + _g(v, "current_liabilities")
                   + _g(v, "secured_loan_ccod") + mat_1yr)

    return {
        "Return on Capital Employed":              _div(pat + interest, equity + sec_af + unsec),
        "Return On Share Holders Fund":            _div(pat, equity),
        "PAT / Income (%)":                        _div(pat, income),
        "PAT / Assets employed (%)":               _div(pat, _g(v, "total_assets")),
        "Long Term Debt / Equity (inclusive q/e)": _div(sec_af + mat_1yr, equity_qe),
        "Total Debt / Equity(inclusive q/e)":      _div(outside - unsec, equity_qe),
        "Interest Coverage":                       _div(pbt + interest + dep, interest),
        "Current Ratio":                           _div(curr_assets, curr_liab),
        "DSCR":                                    _div(pat + dep + interest,
                                                        interest + sec_af / 4),
        "Debtor Days":                             (None if not income
                                                    else _g(v, "debtors") / income * 365),
        "Creditor Days":                           (None if not income
                                                    else _g(v, "sundry_creditors") / income * 365),
    }
