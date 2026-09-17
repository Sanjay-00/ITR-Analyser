"""
checks.py  -  consistency checks and the trust of every figure.

Stage 1 of docs/superpowers/specs/2026-09-17-consistency-checks-design.md.

A statement reconciling proves its numbers ADD UP; it cannot prove a figure
sits in the right row, side or year - five of one week's bugs balanced
perfectly. So every sheet row gets a trust level, and a registry of small,
independent checks disputes rows the evidence does not support:

    proven      its lines come from sections that reconcile
    consistent  nothing to prove it against, and nothing disputes it
    doubtful    a failing check could be explained by this row
    unread      no usable reading

Two rules keep this strictly additive. A check that lacks its inputs SKIPS -
it never fails for want of data. And a failing check never downgrades a
proven row: it may only dispute what was not already proven.
"""

import re
from dataclasses import asdict, dataclass, field

from .extract import financials as F
from .mapping import taxonomy
from .mapping.template_config import (BS_ASSET, BS_LIABILITY, PL_EXPENSE,
                                      PL_INCOME, PL_TAX)

LEVELS = ("unread", "doubtful", "consistent", "proven")
_RANK = {level: n for n, level in enumerate(LEVELS)}

PL_ITEMS = list(PL_INCOME + PL_EXPENSE + PL_TAX)
BS_ITEMS = list(BS_LIABILITY + BS_ASSET)

# Derived rows and what they are computed from - dependencies first, so one
# pass in this order settles them all.
DERIVED_INPUTS = {
    "gross_receipts":    ["sales_other_income"],
    "gross_expenses":    list(PL_EXPENSE),
    "profit_before_tax": ["gross_receipts", "gross_expenses"],
    "profit_after_tax":  ["profit_before_tax", "provision_tax", "provision_deferred_tax"],
    "profit_available":  ["profit_after_tax", "preference_dividend"],
    "cash_profit":       ["profit_available", "depreciation"],
    "total_liabilities": list(BS_LIABILITY),
    "total_assets":      list(BS_ASSET),
    "networth":          ["equity_capital", "preference_shares", "reserves"],
}

# Cross-statement comparisons are between figures read from DIFFERENT places,
# so they get more room than a statement's own rupee-exact total: 0.1%, and
# never less than Rs 1,000.
_SHARE = 0.001
_MIN_TOLERANCE = 1000


@dataclass
class CheckResult:
    name: str
    status: str                        # "pass" | "fail" | "skip"
    gap: float = 0
    implicates: list = field(default_factory=list)
    detail: str = ""


def _tolerance(amount) -> float:
    return max(_MIN_TOLERANCE, _SHARE * abs(amount or 0))


def _lowest(levels: list) -> str:
    return min(levels, key=_RANK.__getitem__) if levels else "consistent"


def _entry(level: str) -> dict:
    return {"level": level, "reasons": [], "evidence": []}


def base_trust(col: dict) -> dict:
    """Every row's level from the lines it was built from, before any check."""
    values, sources = col.get("values") or {}, col.get("sources") or {}
    trust = {}
    for key in PL_ITEMS + BS_ITEMS:
        if values.get(key) is None:
            trust[key] = _entry("unread")
        elif sources.get(key):
            trust[key] = _entry(_lowest([s.get("trust", "consistent")
                                         for s in sources[key]]))
        else:
            trust[key] = _entry("consistent")     # read statement, no such line
    _derive(trust, values)
    return trust


def _derive(trust: dict, values: dict) -> None:
    """Derived rows take the lowest level of their inputs, and carry the
    reasons of the inputs that put them there."""
    for key, inputs in DERIVED_INPUTS.items():
        if values.get(key) is None:
            trust[key] = _entry("unread")
            continue
        known = [k for k in inputs if k in trust]
        level = _lowest([trust[k]["level"] for k in known])
        entry = _entry(level)
        if level == "doubtful":
            for k in known:
                if trust[k]["level"] == "doubtful":
                    entry["reasons"] += [r for r in trust[k]["reasons"]
                                         if r not in entry["reasons"]]
        trust[key] = entry


def _open_rows(col: dict, trust: dict, keys: list) -> list:
    """Rows a failing check may blame: read, non-zero, and not proven."""
    values = col.get("values") or {}
    return [k for k in keys
            if values.get(k) and trust[k]["level"] in ("consistent", "doubtful")]


# ─────────────────────────────────────────────────────────────────
# THE CHECKS  -  each (col, cols, i, trust) -> CheckResult
# ─────────────────────────────────────────────────────────────────

def section_totals(col, cols, i, trust) -> CheckResult:
    """1. Each section against its own printed total - reported per section,
    from the statements kept despite a small gap."""
    name = "Section totals"
    used = col.get("blocks_used") or []
    if not used:
        return CheckResult(name, "skip", detail="no statements read")
    parts, gap = [], 0
    for b in used:
        if not b.get("salvaged"):
            continue
        for r in F.section_results(b):
            if r["status"] == "fail":
                gap += r["gap"]
                parts.append(f"{b['kind'].replace('_', ' ')} section "
                             f"'{r['name'] or 'unnamed'}' is off by {r['gap']:,.0f} "
                             f"against its printed {r['total'] or 0:,.0f}")
    if not parts:
        return CheckResult(name, "pass")
    blamed = [k for k in PL_ITEMS + BS_ITEMS if trust[k]["level"] == "doubtful"]
    return CheckResult(name, "fail", gap, blamed, "; ".join(parts))


def sheet_balances(col, cols, i, trust) -> CheckResult:
    """2. Total Liabilities = Total Assets AFTER mapping - catches a line put
    on the wrong side, or left out, on a page that balanced perfectly."""
    name = "Sheet balances"
    v = col.get("values") or {}
    tl, ta = v.get("total_liabilities"), v.get("total_assets")
    if tl is None or ta is None or not (tl or ta):
        return CheckResult(name, "skip", detail="no balance sheet read")
    gap = abs(tl - ta)
    if gap <= _tolerance(max(abs(tl), abs(ta))):
        return CheckResult(name, "pass")
    heavier = "liabilities" if tl > ta else "assets"
    return CheckResult(name, "fail", gap, _open_rows(col, trust, BS_ITEMS),
                       f"total {heavier} exceed the other side by {gap:,.0f} after mapping")


def profit_vs_statement(col, cols, i, trust) -> CheckResult:
    """3. Our derived profit against the profit line the statement prints."""
    from .columns import _profit_crosscheck
    name = "Profit vs statement"
    v, ignored = col.get("values") or {}, col.get("ignored") or []
    stated = taxonomy.stated_result(ignored)
    pbt = v.get("profit_before_tax")
    if stated is None or pbt is None:
        return CheckResult(name, "skip", detail="the statement prints no profit line")
    messages = _profit_crosscheck(v, ignored)
    if not messages:
        return CheckResult(name, "pass")
    return CheckResult(name, "fail", abs(abs(stated) - abs(pbt)),
                       _open_rows(col, trust, PL_ITEMS), messages[0])


def profit_vs_computation(col, cols, i, trust) -> CheckResult:
    """4. Our derived profit against the one the ITR's computation of income
    restates - an independent source, typed separately."""
    from .columns import _BOOK_PROFIT_TOLERANCE
    name = "Profit vs ITR computation"
    v = col.get("values") or {}
    book, pbt = col.get("book_profit"), v.get("profit_before_tax")
    if book is None or pbt is None:
        return CheckResult(name, "skip", detail="no computation of income read")
    gap = abs(book - pbt)
    if gap <= _BOOK_PROFIT_TOLERANCE:
        return CheckResult(name, "pass")
    page = col.get("book_profit_page")
    where = f" (page {page + 1})" if page is not None else ""
    return CheckResult(name, "fail", gap, _open_rows(col, trust, PL_ITEMS),
                       f"profit before tax {pbt:,.0f} but the computation of "
                       f"income{where} restates {book:,.0f}")


# The year's result, as a capital account credits it. An Income & Expenditure
# account calls it a SURPLUS or an excess of income over expenditure - J K
# Petroleum's "Surplus from I & E A/c" equals its P&L profit to the rupee.
# "Agri income" and "SB Interest" on the same side are income, not the result.
_PROFIT_LINE = re.compile(
    r"\bprofit\b|\bsurplus\b|\bexcess\s+of\s+income\b", re.I)


def profit_into_capital(col, cols, i, trust) -> CheckResult:
    """
    5. The year's profit flows into net worth: the profit a proprietor's
    verified capital account credits must equal the P&L's profit. A P&L line
    misread, or retained profit filed as a liability, breaks it even when
    both statements balance. Skips unless there is exactly ONE profit line to
    compare - a second ("Profit on sale of car") makes the match a guess.
    """
    name = "Profit into capital account"
    pat = (col.get("values") or {}).get("profit_after_tax")
    lines = [(label, amount)
             for b in col.get("blocks") or []
             if b.get("kind") == F.CAPITAL_ACCOUNT and b.get("status") == F.VERIFIED
             for label, amount in F.all_items(b["sides"])
             if _PROFIT_LINE.search(label)]
    if pat is None or len(lines) != 1:
        return CheckResult(name, "skip",
                           detail="no verified capital account with a single profit line")
    credited = abs(lines[0][1])
    gap = abs(credited - abs(pat))
    if gap <= _tolerance(pat):
        return CheckResult(name, "pass")
    return CheckResult(name, "fail", gap, _open_rows(col, trust, PL_ITEMS),
                       f"the capital account credits a profit of {credited:,.0f}; "
                       f"the P&L gives {abs(pat):,.0f}")


# ─────────────────────────────────────────────────────────────────
# THE SAME YEAR PRINTED TWICE
# ─────────────────────────────────────────────────────────────────
# "ITR 2025-26" prints FY2026 with FY2025 beside it; "ITR 2024-25" prints
# FY2025 itself. The second printing is kept as col["alternate"]
# (columns.spread_many). The two must agree, and a field one printing lost is
# taken from the other - under three rules: a proven reading is never
# replaced; only a PROVEN row of the other printing is borrowed; and only when
# both printings agree on their grand totals, which is what shows they are the
# same statement rather than a restated or reclassified one.

_GRAND_TOTALS = {F.PROFIT_LOSS:   ["gross_receipts", "gross_expenses"],
                 F.BALANCE_SHEET: ["total_liabilities", "total_assets"]}
_SAME_FIGURE = 1       # rupees: rounding between two printings of one figure

# Which grand total each input row feeds, and in which direction. A fill must
# move THAT total closer to the other printing's: two printings can agree on
# their totals while grouping lines differently - Borrower E's own FY2024 balance
# sheet is a summary whose "Current Assets" already holds the debtors, while
# FY2025's comparative column breaks them out. Filling Debtors from the
# breakdown counted Rs 1.89 crore twice. A fill that CLOSES a gap is a lost
# line recovered; one that OPENS a gap is the same money in another row.
_TOTAL_OF = {
    **{k: ("gross_receipts", 1) for k in PL_INCOME},
    **{k: ("gross_expenses", 1) for k in PL_EXPENSE},
    "provision_tax":          ("profit_after_tax", -1),
    "provision_deferred_tax": ("profit_after_tax", -1),
    "preference_dividend":    ("profit_available", -1),
    **{k: ("total_liabilities", 1) for k in BS_LIABILITY},
    **{k: ("total_assets", 1) for k in BS_ASSET},
}


def _kind_keys(kinds) -> list:
    return ((PL_ITEMS if F.PROFIT_LOSS in kinds else [])
            + (BS_ITEMS if F.BALANCE_SHEET in kinds else []))


def _compare_printings(col: dict, other: dict) -> tuple:
    """(totals compared, disagreements, statement kinds both printings hold)."""
    kinds = ({b["kind"] for b in other.get("blocks_used") or []}
             & {b["kind"] for b in col.get("blocks_used") or []})
    mine, theirs = col.get("values") or {}, other.get("values") or {}
    compared, disagreements = 0, []
    for kind in sorted(kinds):
        for key in _GRAND_TOTALS.get(kind, []):
            a, b = mine.get(key), theirs.get(key)
            if a is None or b is None:
                continue
            compared += 1
            if abs(a - b) > _tolerance(b):
                disagreements.append((kind, key, a, b))
    return compared, disagreements, kinds


def _where(other: dict, key: str) -> str:
    spots = sorted({f"{s.get('file') or 'file'} page {s.get('page')}"
                    for s in (other.get("sources") or {}).get(key, [])})
    return ", ".join(spots) or "its comparative column"


def _recompute(col: dict) -> None:
    """Derived rows and ratios again, after an input row was filled."""
    have = {b["kind"] for b in col.get("blocks_used") or []}
    buckets = {k: v for k, v in (col.get("values") or {}).items()
               if k in PL_ITEMS + BS_ITEMS and v is not None}
    col["values"] = taxonomy.compute(buckets, have_pl=F.PROFIT_LOSS in have,
                                     have_bs=F.BALANCE_SHEET in have)
    col["ratios"] = taxonomy.ratios(col["values"])


def fill_from_other_printing(col: dict, trust: dict) -> None:
    """Take a field this printing lost from the other printing of the year."""
    other = col.get("alternate")
    if not other:
        return
    compared, disagreements, kinds = _compare_printings(col, other)
    if not compared or disagreements:
        return                          # not provably the same statement
    theirs_trust = base_trust(other)
    values = col["values"]
    sources = col.setdefault("sources", {})
    mine_totals = {tk: values.get(tk) for tk, _sign in _TOTAL_OF.values()}
    their_values = other.get("values") or {}
    filled = False
    for key in _kind_keys(kinds):
        mine = trust[key]
        if mine["level"] == "proven":
            continue                    # never replace a proven reading
        if (theirs_trust.get(key) or {}).get("level") != "proven":
            continue                    # only borrow what the other printing proves
        a, b = values.get(key), other["values"].get(key)
        if b is None:
            continue
        where = _where(other, key)
        if a is not None and abs(a - b) <= _SAME_FIGURE:
            if mine["level"] == "doubtful":
                mine["level"] = "consistent"
                mine["reasons"] = []
                mine["evidence"].append(f"confirmed by the other printing of this year ({where})")
            continue
        total_key, sign = _TOTAL_OF[key]
        current, target = mine_totals.get(total_key), their_values.get(total_key)
        if current is None or target is None:
            continue                    # nothing to prove the fill against
        after = current + sign * (b - (a or 0))
        if not (abs(after - target) < abs(current - target)
                and abs(after - target) <= _tolerance(target)):
            continue                    # would open a gap, not close one
        mine_totals[total_key] = after
        values[key] = b
        sources[key] = [dict(s, trust="consistent") for s in other["sources"].get(key, [])]
        mine["level"], mine["reasons"] = "consistent", []
        mine["evidence"].append(f"taken from the other printing of this year ({where}); "
                                f"this printing read {a or 0:,.0f}")
        filled = True
    if filled:
        _recompute(col)


def same_year_twice(col, cols, i, trust) -> CheckResult:
    """6. A year printed twice must agree on its grand totals."""
    name = "Same year printed twice"
    other = col.get("alternate")
    if not other:
        return CheckResult(name, "skip", detail="this year is printed only once")
    compared, disagreements, _kinds = _compare_printings(col, other)
    if not compared:
        return CheckResult(name, "skip", detail="no grand total printed in both places")
    if not disagreements:
        return CheckResult(name, "pass")
    kind, key, mine, theirs = disagreements[0]
    return CheckResult(
        name, "fail", abs(mine - theirs),
        _open_rows(col, trust, _kind_keys({k for k, *_ in disagreements})),
        f"{taxonomy.LABELS.get(key, key)} reads {mine:,.0f} in this year's own statement "
        f"but {theirs:,.0f} in its other printing ({_where(other, key)})")


CHECKS = [section_totals, sheet_balances, profit_vs_statement,
          profit_vs_computation, profit_into_capital, same_year_twice]


def run_checks(cols: list) -> None:
    """Attach col["checks"] and col["trust"] to every column."""
    for i, col in enumerate(cols):
        trust = base_trust(col)
        # First take anything this printing lost from the other printing of the
        # same year - the checks then judge the filled figures.
        fill_from_other_printing(col, trust)
        results = []
        for check in CHECKS:
            result = check(col, cols, i, trust)
            results.append(result)
            if result.status != "fail":
                continue
            for key in result.implicates:
                if trust[key]["level"] == "proven":
                    continue                           # never dispute a proven row
                trust[key]["level"] = "doubtful"
                reason = f"{result.name}: {result.detail}"
                if reason not in trust[key]["reasons"]:
                    trust[key]["reasons"].append(reason)
        _derive(trust, col.get("values") or {})
        col["checks"] = [asdict(r) for r in results]
        col["trust"] = trust
