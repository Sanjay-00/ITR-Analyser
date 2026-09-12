"""
The ITR Validation sheet in the combined master format (2026-09-12).

These tests EVALUATE the workbook's own formulas (tests/xlsx_eval.py) - so
they check the Excel the analyst receives, not a Python copy of it - and
compare the results with taxonomy.ratios(), which the app shows.
"""

import datetime
import io
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from openpyxl import load_workbook                                   # noqa: E402

from engine import generate_excel                                    # noqa: E402
from engine.mapping import taxonomy as T                             # noqa: E402
from engine.mapping import template_config as C                      # noqa: E402
from xlsx_eval import SheetEvaluator                                 # noqa: E402

L = 100000

MASTER_LABELS = [
    "PROFIT AND LOSS ACCOUNT", "Income", "Sales and other Income", "Gross Receipts",
    "Expenses", "Purchases & Raw Material", "Transport operation and admin charges",
    "Electricity", "Employee Costs", "Other Expenses", "Interest and Finance Expenses",
    "Depreciation", "Extra Ordinary Item", "Gross Expenses", "Profit before tax",
    "Provision for tax", "Provision for Deferred Tax", "Profit after tax",
    "Preference dividend (including tax)", "Profit available for equity shareholders",
    "Cash Profit", "BALANCE SHEET", "Liabilities", "Equity Capital", "Preference Shares",
    "Reserves & Surplus", "Secured loan - Asset Financed",
    "Secured loan maturity within one year", "Secured loan - CC/OD",
    "Other Long term liabilities", "Unsecured loans", "Deffered tax liability",
    "Sundry Creditors", "Current Liabilities and Provision", "Total of Liabilities",
    "Assets", "Fixed Assets", "Intangible Assets", "Investments", "Deffered Tax Assets",
    "Debtors / Receivables", "Current assets, Loans and Advances",
    "Long Term Loan & Advance", "Other Non Current Assets", "Total of Assets",
    "RATIOS", "Return on Capital Employed", "Return On Share Holders Fund",
    "PAT / Income (%)", "PAT / Assets employed (%)",
    "Long Term Debt / Equity (inclusive q/e)", "Total Debt / Equity(inclusive q/e)",
    "Interest Coverage", "Current Ratio", "DSCR", "Debtor Days", "Creditor Days",
    "DSCR Calculation", "Particular", "Net Profit", "Depreciation",
    "Interest (Including CC/OD interest)", "Total (A)", "Existing Monthly EMI",
    "Proposed Loan EMI", "Yearly Obligation (B)", "Total (B)", "DSCR (A/B)",
    "Turnover and Other Income", "PAT", "Cash Profit", "Networth",
    "Secured Loans - Long Term", "Secured Loans – CC / OD",
    "Current Liabilities and Provisions", "Fixed Assets",
    "Current Assets , ST Loans & Advances ", "Ratios", "PAT/Income  (%)",
    "Long Term Debt / Equity (including Q/E)", "TOL/TNW (including Q/E)",
    "Interest Coverage", "Current Ratio", "DSCR", "Debtor Days", "Creditor Days",
]


def _column(year, buckets, have_pl=True, have_bs=True, provisional=False):
    v = T.compute(buckets, have_pl=have_pl, have_bs=have_bs)
    return {"year": year, "entity": "Test Traders", "values": v, "ratios": T.ratios(v),
            "provisional": provisional, "blocks": [],
            "blocks_used": [{"status": "verified", "kind": "profit_loss"}],
            "warnings": [], "unmapped": [], "assumptions": [], "ignored": [],
            "page_summary": {}, "vision_pages": [], "source_name": f"ITR {year}.pdf",
            "pages_used": 5, "pages_total": 20, "scanned": False, "book_profit": None}


BUCKETS = {
    "sales_other_income": 500 * L, "purchases": 200 * L, "transport_admin": 120 * L,
    "employee_costs": 40 * L, "other_expenses": 10 * L, "interest_finance": 30 * L,
    "depreciation": 50 * L, "provision_tax": 12 * L, "provision_deferred_tax": 3 * L,
    "equity_capital": 200 * L, "reserves": 50 * L, "secured_loan_asset_financed": 300 * L,
    "secured_loan_ccod": 40 * L, "unsecured_loans": 20 * L, "sundry_creditors": 60 * L,
    "current_liabilities": 25 * L, "fixed_assets": 450 * L, "debtors": 90 * L,
    "current_assets": 155 * L,
}


@pytest.fixture(scope="module")
def sheet():
    cols = [_column(2024, BUCKETS),
            _column(2025, {}, have_pl=False, have_bs=False, provisional=True)]
    ws = load_workbook(io.BytesIO(generate_excel(cols)))["ITR Validation"]
    return ws, SheetEvaluator(ws), cols


def _row(ws, label, start=1):
    for r in range(start, ws.max_row + 1):
        if ws.cell(row=r, column=1).value == label:
            return r
    raise AssertionError(f"no row {label!r}")


def test_layout_is_exactly_the_master_format(sheet):
    ws, ev, _ = sheet
    labels = [ws.cell(row=r, column=1).value for r in range(4, ws.max_row + 1)]
    labels = [l for l in labels if l and l != "=A2"]
    assert labels == MASTER_LABELS
    assert ws["A2"].value == "Test Traders (Amt in Lakhs)"
    assert ws["B2"].value == "Audited" and ws["C2"].value == "Provisional"
    assert ws["B3"].value == datetime.datetime(2024, 3, 31)


def test_profit_and_totals_are_live_formulas_with_tax_subtracted(sheet):
    ws, ev, cols = sheet
    pbt, pat = _row(ws, "Profit before tax"), _row(ws, "Profit after tax")
    assert str(ws.cell(row=pat, column=2).value).startswith("=")
    assert ev.value(f"B{pbt}") == pytest.approx(500 - 450)
    assert ev.value(f"B{pat}") == pytest.approx(50 - 12 - 3)
    assert ev.value(f"B{_row(ws, 'Cash Profit')}") == pytest.approx(35 + 50)
    assert ev.value(f"B{_row(ws, 'Total of Liabilities')}") == pytest.approx(695)
    assert ev.value(f"B{_row(ws, 'Total of Assets')}") == pytest.approx(695)


def test_excel_ratios_equal_the_python_ratios(sheet):
    ws, ev, cols = sheet
    py = cols[0]["ratios"]
    for name in C.RATIO_NAMES:
        got = ev.value(f"B{_row(ws, name)}")
        want = py[name] if py[name] is not None else 0
        assert got == pytest.approx(want), name
    # Spot-check against hand arithmetic from the analysts' definitions.
    assert py["Current Ratio"] == pytest.approx((90 + 155) / (60 + 25 + 40))
    assert py["Debtor Days"] == pytest.approx(90 / 500 * 365)
    assert py["Long Term Debt / Equity (inclusive q/e)"] == pytest.approx(300 / (250 + 20))


def test_unread_year_is_zero_but_flagged_and_formulas_still_work(sheet):
    ws, ev, _ = sheet
    r = _row(ws, "Sales and other Income")
    cell = ws.cell(row=r, column=3)
    assert cell.value == 0
    assert cell.fill.fgColor.rgb.endswith("FFC7CE")
    assert cell.comment is not None and "Not read" in cell.comment.text
    # Every formula must still evaluate on an all-zero column - each ratio
    # divides by zero there and must fall back to 0 through its IFERROR.
    for label in ["Profit after tax", "Total of Assets", "DSCR (A/B)"] + C.RATIO_NAMES:
        assert ev.value(f"C{_row(ws, label)}") == pytest.approx(0), label


def test_snap_repeats_the_sheet(sheet):
    ws, ev, _ = sheet
    snap_start = _row(ws, "DSCR (A/B)")
    assert ev.value(f"B{_row(ws, 'Networth', snap_start)}") == pytest.approx(250)
    assert ev.value(f"B{_row(ws, 'Current Assets , ST Loans & Advances ', snap_start)}") \
        == pytest.approx(245)
    assert ev.value(f"B{_row(ws, 'Debtor Days', snap_start + 20)}") \
        == pytest.approx(90 / 500 * 365)


def test_dscr_block_follows_the_format(sheet):
    ws, ev, _ = sheet
    total_a = _row(ws, "Total (A)")
    assert ev.value(f"B{total_a}") == pytest.approx(35 + 50 + 30)
    oblig = _row(ws, "Yearly Obligation (B)")
    assert "*12" in ws.cell(row=oblig, column=2).value
    assert ws.cell(row=oblig, column=2).value.endswith("*4)")     # audited
    assert ws.cell(row=oblig, column=3).value.endswith("*1)")     # provisional
