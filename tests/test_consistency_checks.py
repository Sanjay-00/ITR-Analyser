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



def _pl_col(pbt=4859400, ignored=(), book=None, trust="consistent"):
    col = {"values": {"sales_other_income": 142647963, "transport_admin": 137788563,
                      "profit_before_tax": pbt},
           "sources": {"sales_other_income": [{"trust": trust}],
                       "transport_admin": [{"trust": trust}]},
           "ignored": list(ignored), "book_profit": book, "book_profit_page": 3}
    return col, CK.base_trust(col)


def test_profit_vs_statement_passes_skips_and_fails():
    col, t = _pl_col(ignored=[("Profit before tax", 4859400)])
    assert CK.profit_vs_statement(col, [col], 0, t).status == "pass"
    col, t = _pl_col(ignored=[])
    assert CK.profit_vs_statement(col, [col], 0, t).status == "skip"
    col, t = _pl_col(pbt=4000000, ignored=[("Profit before tax", 4859400)])
    r = CK.profit_vs_statement(col, [col], 0, t)
    assert r.status == "fail" and r.gap == 859400
    assert set(r.implicates) == {"sales_other_income", "transport_admin"}


def test_profit_vs_computation_passes_skips_and_fails():
    col, t = _pl_col(book=4859400)
    assert CK.profit_vs_computation(col, [col], 0, t).status == "pass"
    col, t = _pl_col(book=None)
    assert CK.profit_vs_computation(col, [col], 0, t).status == "skip"
    col, t = _pl_col(book=6000000)
    r = CK.profit_vs_computation(col, [col], 0, t)
    assert r.status == "fail" and r.gap == 1140600 and "page 4" in r.detail


def test_profit_checks_never_blame_proven_rows():
    col, t = _pl_col(pbt=4000000, ignored=[("Profit before tax", 4859400)], trust="proven")
    assert CK.profit_vs_statement(col, [col], 0, t).implicates == []



def _capital_account(lines, status=F.VERIFIED):
    return {"kind": F.CAPITAL_ACCOUNT, "status": status,
            "sides": {"left": [("To Drawings", 702324)], "right": list(lines)}}


def _proprietor_col(pat, accounts):
    col = {"values": {"sales_other_income": 53581608, "transport_admin": 50342494,
                      "profit_after_tax": pat},
           "sources": {"sales_other_income": [{"trust": "consistent"}],
                       "transport_admin": [{"trust": "consistent"}]},
           "blocks": accounts}
    return col, CK.base_trust(col)


def test_profit_into_capital_passes_and_fails():
    """Borrower E FY2023: the capital account credits Rs 32,39,114 of profit."""
    ok = [_capital_account([("Net Profit / Loss from Profit & loss A/c", 3239114)])]
    col, t = _proprietor_col(3239114, ok)
    assert CK.profit_into_capital(col, [col], 0, t).status == "pass"
    col, t = _proprietor_col(2239114, ok)
    r = CK.profit_into_capital(col, [col], 0, t)
    assert r.status == "fail" and r.gap == 1000000 and r.implicates


def test_profit_into_capital_skips_when_unsure():
    col, t = _proprietor_col(3239114, [])                                   # no account
    assert CK.profit_into_capital(col, [col], 0, t).status == "skip"
    failed = [_capital_account([("Net Profit", 3239114)], status=F.FAILED)]
    col, t = _proprietor_col(3239114, failed)                               # not verified
    assert CK.profit_into_capital(col, [col], 0, t).status == "skip"
    two = [_capital_account([("Net Profit", 3239114), ("Profit on sale of car", 50000)])]
    col, t = _proprietor_col(3239114, two)                                  # ambiguous
    assert CK.profit_into_capital(col, [col], 0, t).status == "skip"



def test_profit_into_capital_reads_an_income_and_expenditure_surplus():
    """Borrower B's capital account credits the year's result as "Surplus
    from I & E A/c" - an Income & Expenditure account's word for profit. It
    equals the P&L's profit to the rupee (Rs 14,45,832 FY2024), but the check
    only knew the word "profit" and skipped. "Agri income" and "SB Interest"
    on the same side must not be mistaken for the year's result."""
    lines = [("[EQ] By Balance B/f", 5008887), ("[EQ] II Agri income", 424200),
             ("[EQ] SB Interest", 181), ("[EQ] Surplus from I & E A/c", 1445832)]
    col, t = _proprietor_col(1445832, [_capital_account(lines)])
    assert CK.profit_into_capital(col, [col], 0, t).status == "pass"
    lines = [("[EQ] Excess of income over expenditure", 1445832)]
    col, t = _proprietor_col(1445832, [_capital_account(lines)])
    assert CK.profit_into_capital(col, [col], 0, t).status == "pass"



# ── The same year printed twice (user scenario, 2026-09-17) ─────
#    "ITR 2025-26.pdf" prints FY2026 with FY2025 beside it; "ITR 2024-25.pdf"
#    prints FY2025 with FY2024 beside it. FY2025 is printed twice: both
#    printings must agree, and a field one printing lost is taken from the
#    other - never over a proven reading, and only when the two printings
#    agree on their grand totals (proof they are the same statement).

FINANCE = ("[EXP] (f) Finance costs", 62)


def _pl_full(year, from_comparative=False, finance=62, revenue=95243661):
    income = [("I. Revenue from operations", revenue), ("II. Other income", 50856)]
    expenses = list(EXPENSES) + [("[EXP] (f) Finance costs", finance)]
    b = _block(F.PROFIT_LOSS,
               [_sec("Total Income", income, revenue + 50856),
                _sec("Total expenses", expenses, sum(a for _l, a in expenses))],
               year=year, src="ITR 2025-26.pdf" if from_comparative or year == 2026
               else "ITR 2024-25.pdf")
    if from_comparative:
        b["from_comparative"] = True
    return b


def _two_itrs(monkeypatch, fy25_own, fy25_other):
    cols = _spread(monkeypatch, {
        "ITR 2025-26.pdf": _doc("ITR 2025-26.pdf", 2026, [_pl_full(2026), fy25_other]),
        "ITR 2024-25.pdf": _doc("ITR 2024-25.pdf", 2025, [fy25_own])})
    return {c["year"]: c for c in cols}


def test_a_field_lost_in_one_printing_is_taken_from_the_other(monkeypatch):
    """FY2025's own P&L lost its Rs 62 finance cost; FY2026's comparative
    column printed it."""
    fy25 = _two_itrs(monkeypatch, _pl(), _pl_full(2025, from_comparative=True))[2025]
    assert fy25["values"]["interest_finance"] == 62
    assert fy25["values"]["gross_expenses"] == 125754761
    filled = fy25["trust"]["interest_finance"]
    assert filled["level"] == "consistent"
    assert "ITR 2025-26.pdf" in " ".join(filled["evidence"])
    # A doubtful row the other printing confirms is doubtful no longer.
    assert fy25["trust"]["purchases"]["level"] == "consistent"
    assert _check(fy25, "Same year printed twice")["status"] == "pass"


def test_an_unreadable_statement_is_replaced_by_its_other_printing(monkeypatch):
    """Vice versa at statement level: FY2025's own P&L is broken beyond the
    0.5% guard, so the other printing stands in for all of it."""
    fy25 = _two_itrs(monkeypatch, _pl(expense_total=200000000),
                     _pl_full(2025, from_comparative=True))[2025]
    assert fy25["values"]["gross_expenses"] == 125754761
    assert any(b.get("from_comparative") for b in fy25["blocks_used"])


def test_a_proven_reading_is_never_replaced_by_the_other_printing(monkeypatch):
    own = _pl_full(2025, finance=62)
    own["source"] = "ITR 2024-25.pdf"
    fy25 = _two_itrs(monkeypatch, own, _pl_full(2025, from_comparative=True, finance=999))[2025]
    assert fy25["values"]["interest_finance"] == 62


def test_printings_that_disagree_fill_nothing_and_fail_the_check(monkeypatch):
    other = _pl_full(2025, from_comparative=True, revenue=195243661)   # a crore apart
    fy25 = _two_itrs(monkeypatch, _pl(), other)[2025]
    assert fy25["values"]["interest_finance"] == 0
    r = _check(fy25, "Same year printed twice")
    assert r["status"] == "fail"
    assert "purchases" in r["implicates"] and "sales_other_income" not in r["implicates"]


def test_same_year_twice_skips_when_a_year_is_printed_once(monkeypatch):
    col = _spread(monkeypatch, {"s.pdf": _doc("s.pdf", 2025, [_pl()])})[0]
    assert _check(col, "Same year printed twice")["status"] == "skip"



def test_a_summary_printing_is_not_refilled_from_a_breakdown(monkeypatch):
    """
    Borrower E FY2024: its own balance sheet is a SUMMARY - "Current Assets
    2,25,76,321" already contains the debtors - while FY2025's comparative
    column prints the same year BROKEN DOWN (trade receivables 1,89,02,362
    ...). Both agree on their totals. Filling Debtors from the breakdown while
    the summary row still held them counted Rs 1.89 crore twice (golden: 7 ->
    18 rows). A fill must CLOSE a gap between the two printings' totals, never
    open one.
    """
    summary = _block(F.BALANCE_SHEET, [
        _sec("Total Equity And Liabilities",
             [("[LIAB] Capital Account", 8987926), ("[CL] Current Liabilities", 21641395)],
             30629321),
        _sec("Total Assets",
             [("[NCA] Fixed Assets", 8053000), ("[CA] Current Assets", 22576321)],
             30629321)], year=2024)
    summary["source"] = "ITR 2024-25.pdf"
    breakdown = _block(F.BALANCE_SHEET, [
        _sec("Total Equity And Liabilities",
             [("[LIAB] Capital Account", 8987926), ("[CL] (b) Trade payables", 732563),
              ("[CL] (c) Other current liabilities", 20908832)],
             30629321),
        _sec("Total Assets",
             [("[NCA] Fixed Assets", 8053000), ("[CA] (c) Trade receivables", 18902362),
              ("[CA] (d) Cash and bank balances", 3673959)],
             30629321)], year=2024)
    breakdown["from_comparative"] = True
    breakdown["source"] = "ITR 2025-26.pdf"
    fy24 = {c["year"]: c for c in _spread(monkeypatch, {
        "ITR 2025-26.pdf": _doc("ITR 2025-26.pdf", 2025, [breakdown]),
        "ITR 2024-25.pdf": _doc("ITR 2024-25.pdf", 2024, [summary])})}[2024]
    assert fy24["values"]["debtors"] == 0
    assert fy24["values"]["sundry_creditors"] == 0
    assert fy24["values"]["total_assets"] == 30629321
    assert _check(fy24, "Same year printed twice")["status"] == "pass"



# ── The workbook ─────────────────────────────────────────────────

def _book(cols):
    from openpyxl import load_workbook
    from engine import generate_excel
    return load_workbook(io.BytesIO(generate_excel(cols)))


def _row(ws, label):
    return next(r for r in range(1, ws.max_row + 1) if ws.cell(row=r, column=1).value == label)


def test_doubtful_input_rows_are_amber_with_a_note(monkeypatch):
    cols = _spread(monkeypatch, {"s.pdf": _doc("s.pdf", 2025, [_pl(), _bs()])})
    ws = _book(cols)["ITR Validation"]
    cell = ws.cell(row=_row(ws, "Purchases & Raw Material"), column=2)
    assert cell.fill.fgColor.rgb.endswith("FFEB9C")
    assert "Section totals" in cell.comment.text
    sales = ws.cell(row=_row(ws, "Sales and other Income"), column=2)
    assert not sales.fill.fgColor.rgb.endswith("FFEB9C")


def test_totals_snap_and_growth_are_never_flagged(monkeypatch):
    """They are formulas over the input rows - flagging them too would count
    the same doubt twice (user instruction, 2026-09-17)."""
    cols = _spread(monkeypatch, {"s.pdf": _doc("s.pdf", 2025, [_pl(), _bs()])})
    ws = _book(cols)["ITR Validation"]
    for r in range(1, ws.max_row + 1):
        label = ws.cell(row=r, column=1).value
        value = ws.cell(row=r, column=2).value
        if (isinstance(value, str) and value.startswith("=IFERROR")) or label in (
                "Gross Expenses", "Profit before tax", "Profit after tax", "Cash Profit",
                "Networth", "Total of Liabilities", "Total of Assets"):
            assert not ws.cell(row=r, column=2).fill.fgColor.rgb.endswith("FFEB9C"), label


def test_a_filled_row_says_where_it_came_from(monkeypatch):
    cols = list(_two_itrs(monkeypatch, _pl(), _pl_full(2025, from_comparative=True)).values())
    ws = _book(cols)["ITR Validation"]
    fy25 = next(c for c in range(2, 2 + len(cols)) if ws.cell(row=3, column=c).value
                and getattr(ws.cell(row=3, column=c).value, "year", None) == 2025)
    cell = ws.cell(row=_row(ws, "Interest and Finance Expenses"), column=fy25)
    assert "taken from the other printing" in cell.comment.text
    assert not cell.fill.fgColor.rgb.endswith("FFEB9C")          # trusted, not amber


def test_audit_trail_lists_every_check(monkeypatch):
    cols = _spread(monkeypatch, {"s.pdf": _doc("s.pdf", 2025, [_pl(), _bs()])})
    audit = [c.value for r in _book(cols)["Audit Trail"].iter_rows() for c in r]
    for name in ("Section totals", "Sheet balances", "Profit vs statement",
                 "Profit vs ITR computation", "Profit into capital account",
                 "Same year printed twice"):
        assert name in audit
