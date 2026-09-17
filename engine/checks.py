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


CHECKS = [section_totals, sheet_balances, profit_vs_statement, profit_vs_computation]


def run_checks(cols: list) -> None:
    """Attach col["checks"] and col["trust"] to every column."""
    for i, col in enumerate(cols):
        trust = base_trust(col)
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
