"""
Consistency checks, stage 1 (docs/superpowers/specs/
2026-09-17-consistency-checks-design.md). Pure functions and a spread over
synthetic documents - always run.
"""

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import columns as COL                                     # noqa: E402
from engine.extract import financials as F                            # noqa: E402


def _sec(name, items, total):
    return {"name": name, "items": items, "total": total}


def _block(kind, sections, year=2025, entity="Test Traders", page=0, src="t.pdf"):
    b = {"kind": kind, "year": year, "entity": entity, "page": page, "source": src,
         "title": "", "unit_scale": 1, "printed_total": None,
         "sides": {"sections": sections}}
    b["check"] = F.check_block(b)
    b["status"] = F.status_of(b["check"])
    return b


INCOME = [("I. Revenue from operations", 95243661), ("II. Other income", 50856)]
# sample_d FY2025: four expense lines read, the Rs 62 finance cost lost.
EXPENSES = [("[EXP] (b) Purchases of Stock In Trade", 88409944),
            ("[EXP] (e) Employee benefits expenses", 4435992),
            ("[EXP] (f) Depreciation and amortisation expenses", 11963397),
            ("[EXP] (g) Other expenses", 20945366)]


def _pl(expense_total=125754761, year=2025):
    return _block(F.PROFIT_LOSS, [_sec("Total Income", list(INCOME), 95294517),
                                  _sec("Total expenses", list(EXPENSES), expense_total)],
                  year=year)


def test_section_results_report_each_section():
    rows = F.section_results(_pl())
    assert [(r["name"], r["status"]) for r in rows] == [
        ("Total Income", "pass"), ("Total expenses", "fail")]
    assert rows[1]["gap"] == 62


def test_a_tiny_gap_is_salvageable_a_large_one_is_not():
    small = _pl()                              # 62 of 12.58 crore
    large = _pl(expense_total=200000000)       # 7.4 crore of 20 crore
    assert small["status"] == F.FAILED and large["status"] == F.FAILED
    assert F.salvageable(small)
    assert not F.salvageable(large)


def test_salvage_boundary_is_half_a_percent():
    """The gap is measured against the PRINTED total, which already includes
    the gap - 0.4% of the items is inside the limit, 0.6% is outside."""
    total = sum(a for _l, a in EXPENSES)
    inside = _pl(expense_total=total + int(total * 0.004))
    outside = _pl(expense_total=total + int(total * 0.006))
    assert F.salvageable(inside)
    assert not F.salvageable(outside)


def test_structural_failures_are_never_salvaged():
    """A P&L missing its whole revenue side fails structurally, whatever the
    gaps - salvage must not rescue it."""
    one_sided = _block(F.PROFIT_LOSS, [_sec("Total expenses", list(EXPENSES), 125754761)])
    assert not F.salvageable(one_sided)


def test_verified_blocks_are_not_salvage_candidates():
    good = _pl(expense_total=sum(a for _l, a in EXPENSES))
    assert good["status"] == F.VERIFIED
    assert not F.salvageable(good)


def test_item_statuses_follow_their_sections():
    statuses = F.item_statuses(_pl())
    assert statuses == ["proven", "proven"] + ["doubtful"] * 4
    unchecked = _block(F.PROFIT_LOSS, [_sec("", [("Depreciation", 10)], None)])
    assert F.item_statuses(unchecked) == ["consistent"]


def _doc(name, year, blocks, book_profit=None):
    return {"source_name": name, "identity": {}, "scanned": False,
            "book_profit": book_profit, "book_profit_page": 3 if book_profit else None,
            "pages_total": 3, "pages_used": 2, "page_summary": {"statement": 1},
            "blocks": blocks, "vision_pages": [], "year": year}


def _spread(monkeypatch, docs):
    monkeypatch.setattr(COL, "_read_document", lambda src, *a, **k: docs[src])
    return COL.spread_many(list(docs), extract_fn=None)


def test_a_salvaged_pl_reaches_the_sheet(monkeypatch):
    """sample_d FY2025: before, the Rs 62 gap dropped the whole P&L and every
    P&L row read 'Check ITR'."""
    col = _spread(monkeypatch, {"s.pdf": _doc("s.pdf", 2025, [_pl()])})[0]
    assert col["values"]["sales_other_income"] == 95294517
    assert col["values"]["purchases"] == 88409944
    assert col["blocks_used"][0].get("salvaged") is True


def test_a_badly_broken_pl_is_still_excluded(monkeypatch):
    col = _spread(monkeypatch, {"s.pdf": _doc("s.pdf", 2025, [_pl(expense_total=200000000)])})[0]
    assert col["values"].get("sales_other_income") is None      # unread, as today


def test_every_source_line_carries_its_trust(monkeypatch):
    col = _spread(monkeypatch, {"s.pdf": _doc("s.pdf", 2025, [_pl()])})[0]
    assert {s["trust"] for s in col["sources"]["sales_other_income"]} == {"proven"}
    assert {s["trust"] for s in col["sources"]["purchases"]} == {"doubtful"}



def test_a_block_carrying_only_its_verdict_is_understood():
    """A statement's own status is authoritative. A block whose check dict is
    partial - only a reason - must not crash the trust code, and must be read
    by its status (seven pooling tests build blocks exactly like this)."""
    b = {"kind": F.BALANCE_SHEET, "status": F.VERIFIED, "check": {"reason": ""},
         "sides": {"left": [("Share Capital", 100)], "right": [("Fixed Assets", 100)]}}
    assert F.item_statuses(b) == ["proven", "proven"]
    assert not F.salvageable(b)



from engine import checks as CK                                       # noqa: E402


def _bs(capital=1000000, loans=2000000, fixed=1500000, debtors=1500000, year=2025):
    """A verified Schedule III balance sheet."""
    return _block(F.BALANCE_SHEET, [
        _sec("Total Equity And Liabilities",
             [("[LIAB] (a) Equity share capital", capital), ("[NCL] (i) Borrowings", loans)],
             capital + loans),
        _sec("Total Assets",
             [("[NCA] (a) Property, plant and equipment", fixed),
              ("[CA] (ii) Trade receivables", debtors)],
             fixed + debtors)], year=year)


def _check(col, name):
    return next(c for c in col["checks"] if c["name"] == name)


def test_run_checks_attaches_checks_and_trust(monkeypatch):
    col = _spread(monkeypatch, {"s.pdf": _doc("s.pdf", 2025, [_pl(), _bs()])})[0]
    assert {"checks", "trust"} <= set(col)
    assert col["trust"]["sales_other_income"]["level"] == "proven"
    assert col["trust"]["purchases"]["level"] == "doubtful"
    assert col["trust"]["equity_capital"]["level"] == "proven"


def test_derived_rows_take_the_lowest_input_level(monkeypatch):
    col = _spread(monkeypatch, {"s.pdf": _doc("s.pdf", 2025, [_pl(), _bs()])})[0]
    assert col["trust"]["gross_receipts"]["level"] == "proven"
    assert col["trust"]["gross_expenses"]["level"] == "doubtful"
    assert col["trust"]["profit_before_tax"]["level"] == "doubtful"


def test_section_totals_check_names_the_gap(monkeypatch):
    col = _spread(monkeypatch, {"s.pdf": _doc("s.pdf", 2025, [_pl(), _bs()])})[0]
    r = _check(col, "Section totals")
    assert r["status"] == "fail" and r["gap"] == 62
    assert "purchases" in r["implicates"] and "sales_other_income" not in r["implicates"]


def test_clean_statements_pass_every_check_they_can_run(monkeypatch):
    good = _pl(expense_total=sum(a for _l, a in EXPENSES))
    col = _spread(monkeypatch, {"s.pdf": _doc("s.pdf", 2025, [good, _bs()])})[0]
    assert {c["status"] for c in col["checks"]} <= {"pass", "skip"}
    assert not [k for k, t in col["trust"].items() if t["level"] == "doubtful"]


def test_sheet_balances_fails_without_blaming_proven_rows():
    col = {"values": {"total_liabilities": 5000000, "total_assets": 4000000,
                      "equity_capital": 5000000, "fixed_assets": 4000000},
           "sources": {"equity_capital": [{"trust": "proven"}],
                       "fixed_assets": [{"trust": "proven"}]}}
    trust = CK.base_trust(col)
    r = CK.sheet_balances(col, [col], 0, trust)
    assert r.status == "fail" and r.gap == 1000000 and r.implicates == []


def test_sheet_balances_blames_open_rows():
    col = {"values": {"total_liabilities": 5000000, "total_assets": 4000000,
                      "equity_capital": 5000000, "fixed_assets": 4000000},
           "sources": {"equity_capital": [{"trust": "consistent"}],
                       "fixed_assets": [{"trust": "proven"}]}}
    trust = CK.base_trust(col)
    r = CK.sheet_balances(col, [col], 0, trust)
    assert r.implicates == ["equity_capital"]


def test_checks_skip_without_their_inputs():
    col = {"values": {}, "sources": {}, "blocks_used": []}
    trust = CK.base_trust(col)
    assert CK.section_totals(col, [col], 0, trust).status == "skip"
    assert CK.sheet_balances(col, [col], 0, trust).status == "skip"
