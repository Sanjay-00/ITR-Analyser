"""
Tests for the cross-checks, cache hygiene, rotation detection and the
analysis layer added 2026-09-10. Pure functions - always run.

Each pins a gap found in the 2026-09-10 review (see docs/SHORTCOMINGS.md):
a check that existed but could not see the failure it was meant to catch.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import analysis as A                                     # noqa: E402
from engine import columns as COL                                     # noqa: E402
from engine.extract import financials as F                            # noqa: E402
from engine.extract.itr_parser import extract_book_profit             # noqa: E402
from engine.ingest import ocr_extractor as O                          # noqa: E402
from engine.mapping import taxonomy as T                              # noqa: E402


# ── Profit sign ──────────────────────────────────────────────────

def test_profit_crosscheck_catches_a_loss_read_as_a_profit():
    """
    Case 11: 'TO NET LOSS 4,391,651' read as a Rs 43.9 lakh PROFIT. The old
    check compared magnitudes only, so the gap was zero and nothing warned.
    """
    ignored = [("[EXP] Net Loss", 4391651)]
    assert COL._profit_crosscheck({"profit_before_tax": 4391651}, ignored)
    assert not COL._profit_crosscheck({"profit_before_tax": -4391651}, ignored)


def test_profit_crosscheck_neutral_wording_trusts_a_printed_negative():
    ignored = [("Net Profit / (Loss)", -1532419)]
    assert COL._profit_crosscheck({"profit_before_tax": 1532419}, ignored)
    assert not COL._profit_crosscheck({"profit_before_tax": -1532419}, ignored)


def test_profit_crosscheck_t_account_profit_line_is_not_a_sign_flip():
    # A T-account prints its closing "To Net Profit" as a positive figure.
    ignored = [("[EXP] To Net Profit", 4823981)]
    assert not COL._profit_crosscheck({"profit_before_tax": 4823981}, ignored)


# ── One-sided P&L ────────────────────────────────────────────────

def test_one_sided_pl_without_income_does_not_verify():
    """
    Case 12: the credit side was unreadable, the debit side matched the
    printed total, and the P&L VERIFIED with income silently zero.
    """
    block = {"kind": F.PROFIT_LOSS, "printed_total": 300,
             "sides": {"left": [("[EXP] Salary", 100), ("[EXP] Rent", 150),
                                ("[EXP] To Net Profit", 50)], "right": []}}
    check = F.check_block(block)
    assert not check["balanced"]
    assert F.status_of(check) == F.FAILED


def test_single_column_pl_with_revenue_still_verifies():
    # A Vision read of a vertical P&L puts everything in "left" - legitimate.
    block = {"kind": F.PROFIT_LOSS, "printed_total": 300,
             "sides": {"left": [("Revenue from operations", 500),
                                ("Employee costs", -200)], "right": []}}
    assert F.check_block(block)["balanced"]


# ── Book profit from the computation of income ───────────────────
# Strings copied verbatim from real filings' computation pages.

def test_book_profit_reads_bracketed_loss():
    pages = ["COMPUTATION OF TOTAL INCOME\nNet Profit / (Loss) as per profit "
             "& loss A/c (15,32,419)\nAdd: Depreciation"]
    assert extract_book_profit(pages) == (-1532419, 0)


def test_book_profit_reads_before_tax_wording_and_plain_digits():
    assert extract_book_profit(
        ["x", "Computation of income\nNet Profit Before Tax as per P & L a/c\n"
              "19,81,650"]) == (1981650, 1)
    assert extract_book_profit(
        ["PROFITS AND GAINS OF BUSINESS\nNet Profit (Loss) 5470889"])[0] == 5470889


def test_book_profit_ignores_pages_that_are_not_a_computation():
    # A P&L's own "Net Profit" line must not be mistaken for the restatement.
    assert extract_book_profit(["PROFIT AND LOSS A/C\nTo Net Profit 48,23,981"]) \
        == (None, None)


def test_book_profit_crosscheck_is_signed():
    assert COL._book_profit_crosscheck({"profit_before_tax": 1532419}, -1532419, 1)
    assert not COL._book_profit_crosscheck({"profit_before_tax": -1532000}, -1532419, 1)
    assert not COL._book_profit_crosscheck({"profit_before_tax": None}, 5, 1)


# ── Label cache hygiene ──────────────────────────────────────────

def test_cache_refuses_account_numbers():
    """A real cache held 'axis bank a c 17194' - an account number on disk."""
    assert not T._looks_cacheable("Axis Bank A/c 17194", "current_assets")
    assert T._looks_cacheable("Cash at Bank", "current_assets")


def test_cache_refuses_a_pl_line_mapped_to_the_balance_sheet():
    """A real cache held 'inc by closing stock' -> current_assets."""
    assert not T._looks_cacheable("[INC] By Closing Stock", "current_assets")
    assert T._looks_cacheable("[EXP] Rate & Taxes Paid", "other_expenses")


# ── Rotation ─────────────────────────────────────────────────────

class _Img:
    def __init__(self, angle=0):
        self.angle = angle

    def rotate(self, angle, expand=True):
        return _Img((self.angle + angle) % 360)


def _reader(upright_at):
    words = " ".join(["Balance"] * 40)
    return lambda img: words if img.angle == upright_at else "|| ~~ 1l"


def test_best_rotation_finds_a_sideways_page():
    angle, text = O._best_rotation(_reader(90), _Img(0), "|| ~~")
    assert angle == 90 and "Balance" in text


def test_best_rotation_leaves_an_upright_page_alone():
    text = " ".join(["Balance"] * 40)
    assert O._best_rotation(_reader(90), _Img(0), text)[0] == 0


def test_best_rotation_does_not_turn_a_blank_page_on_noise():
    assert O._best_rotation(lambda img: "ab", _Img(0), "")[0] == 0


# ── Ratios ───────────────────────────────────────────────────────

def test_gearing_ratios_treat_unsecured_loans_as_quasi_equity():
    """
    Figures from the analyst's own Borrower L sheet, FY2024 (in lakhs).
    "(inclusive q/e)" adds unsecured loans to net worth and removes them
    from debt; the old version divided by plain net worth (1.143 / 1.472).
    """
    L = 100000
    v = {"equity_capital": 269.24 * L, "secured_loan_asset_financed": 304.74 * L,
         "unsecured_loans": 3.00 * L, "current_liabilities": 88.49 * L,
         "total_liabilities": 665.47 * L, "networth": 269.24 * L,
         "profit_before_tax": 54.72 * L, "profit_after_tax": 54.72 * L,
         "interest_finance": 32.69 * L}
    r = T.ratios(v)
    assert abs(r["Long Term Debt / Equity (inclusive q/e)"] - 1.1194) < 1e-3
    assert abs(r["Total Debt / Equity(inclusive q/e)"] - 1.4444) < 1e-3
    assert abs(r["Return on Capital Employed"] - 0.1515) < 1e-3
    assert abs(r["Return On Share Holders Fund"] - 0.2032) < 1e-3


# ── Tally layout ─────────────────────────────────────────────────

def test_vehicle_registration_is_label_text():
    for reg in ("MH-12-PQ-9115", "Mh 12 Nx 3915", "Mh-12-1615"):
        assert F._has_label_text(reg)
    assert not F._has_label_text("7,96,081.64")


def test_group_total_expands_only_into_a_breakdown_that_proves_it():
    grp = (226, "Direct Expenses", 20053326.48)
    kids = [(182, "Fuel", 9060500.0), (182, "Salary", 1827696.48),
            (182, "Transport", 9165130.0), (382, "stray subtotal", 36985488.0)]
    out = F._expand_groups([grp] + kids, [grp])
    assert [e[1] for e in out] == ["Fuel", "Salary", "Transport"]
    # A CA's working column that does not sum to the next figure is left alone.
    work = [(300, "Furniture", 59382.0), (200, "Computer", 10172.0),
            (200, "Less Dep", 4069.0)]
    assert F._expand_groups(work, [work[0]]) == [work[0]]


def test_unsecured_loans_are_not_filed_as_secured():
    assert T.map_label("Unsecured Loans", {}) == "unsecured_loans"
    assert T.map_label("Secured Loans", {}) == "secured_loan_asset_financed"


def test_tally_nett_profit_is_the_result_line_not_an_expense():
    assert T.map_label("[EXP] Nett Profit", {}) is T.IGNORE


def test_profit_inside_the_capital_section_is_part_of_net_worth():
    assert T.map_label("[EQ] Profit for the Year", {}) == "equity_capital"
    assert T.map_label("[EXP] Profit for the Year", {}) is T.IGNORE


def test_heading_with_a_dotted_date_is_still_a_heading():
    """Borrower B: '31.03' read as rupees-and-paise, so the Balance Sheet
    heading was rejected and the sheet merged into the capital account."""
    assert F._match_title("BALANCE SHEET AS ON 31.03.2024")
    assert F._match_title("Balance Sheet as at 31/03/2025")
    assert not F._match_title("Balance Sheet 12,34,567.00")


def test_stock_movement_is_netted_into_purchases():
    """
    Analyst convention, Borrower B FY24 (rupees): Purchases 1985.23 lakh =
    opening 44.16 + purchases 1960.09 - closing 19.02. Profit is unchanged.
    """
    items = [("[EXP] To Opening stock", 4415506), ("[EXP] Purchases", 196009252),
             ("[EXP] Gross prfit trf to P & L A/c", 4577061),
             ("[INC] By Sales", 203099742), ("[INC] Closing Stock", 1902077)]
    buckets, unmapped, ignored = T.map_items(items, {})
    assert buckets["purchases"] == 4415506 + 196009252 - 1902077
    assert buckets["sales_other_income"] == 203099742
    assert not unmapped
    assert T.check_mapping(items, buckets, unmapped, ignored)["ok"]
    v = T.compute(buckets)
    assert v["profit_before_tax"] == 203099742 - (4415506 + 196009252 - 1902077)


def test_ocr_misspelt_gross_profit_is_still_the_transfer_line():
    assert T.map_label("[EXP] Gross prfit trf to P & L A/c", {}) is T.IGNORE
    assert F._TRANSFER_RE.search("Gross prfit trf to P&I")


def test_result_line_behind_a_scanned_ditto_mark_is_still_the_result():
    assert T.map_label("[EXP] # Net profit trf to Capital A/c", {}) is T.IGNORE
    assert T.map_label('[EXP] " Net Profit', {}) is T.IGNORE


def test_ocr_mixed_separators_are_normalised():
    from engine.ingest.layout import _fix_ocr_typos
    assert _fix_ocr_typos("2.35.51,356.00") == "2,35,51,356.00"
    assert _fix_ocr_typos("1.23.456") == "1,23,456"
    assert _fix_ocr_typos("2,35,51,356.00") == "2,35,51,356.00"
    # Not amounts - must pass through untouched.
    assert _fix_ocr_typos("AS ON 31.03.2024") == "AS ON 31.03.2024"
    assert _fix_ocr_typos("12.345") == "12.345"
    # A decimal point read as a comma (Borrower Q FY2026, Rs lakhs).
    assert _fix_ocr_typos("5,106,21") == "5,106.21"
    assert _fix_ocr_typos("1,738,36") == "1,738.36"
    # Valid Indian and international grouping is untouched.
    assert _fix_ocr_typos("12,34,567") == "12,34,567"
    assert _fix_ocr_typos("1,234,567") == "1,234,567"
    assert _fix_ocr_typos("5,106.21") == "5,106.21"


def test_stacked_boxes_at_the_same_x_are_two_rows_not_one_cell():
    """
    Borrower B FY24: RapidOCR returns "45,77,061.00" and "96,250.00" as
    separate boxes ~20px apart - closer than the row tolerance. First-fit
    joined them into ONE cell "45,77,061.00 96,250.00" (Cases 3, 6, 21).
    """
    from engine.ingest.layout import rows_with_cells
    h = 34
    words = [(100, 0, 300, h, "By Gross profit"), (700, 0, 820, h, "45,77,061.00"),
             (100, 20, 300, 20 + h, "Rent received"), (705, 20, 810, 20 + h, "96,250.00")]
    rows = rows_with_cells(words, 1000)
    cells = [[t for _a, _b, t in c] for _y, c in rows]
    assert cells == [["By Gross profit", "45,77,061.00"],
                     ["Rent received", "96,250.00"]]


def test_wrapped_caption_rejoins_the_row_with_its_amount():
    """
    Borrower M FY2025: a two-line Schedule III caption must stay with
    its figure. Only the SECOND stacked line carries money here, so the two
    lines are one row - unlike two data lines, which stay apart.
    """
    from engine.ingest.layout import rows_with_cells
    h = 34
    words = [(100, 0, 500, h, "(b) Total outstanding dues of creditors other than micro"),
             (100, 20, 330, 20 + h, "and small enterprises"),
             (800, 20, 900, 20 + h, "3,44,38,977")]
    rows = rows_with_cells(words, 1000)
    assert len(rows) == 1
    texts = [t for _a, _b, t in rows[0][1]]
    assert "3,44,38,977" in texts
    assert any("small enterprises" in t for t in texts)


def test_side_by_side_words_still_share_a_row():
    from engine.ingest.layout import rows_with_cells
    words = [(100, 0, 160, 12, "Sundry"), (165, 1, 240, 13, "Creditors"),
             (600, 0, 680, 12, "16,950")]
    rows = rows_with_cells(words, 1000)
    assert len(rows) == 1


def test_vertical_misread_of_a_balance_sheet_does_not_verify_as_one():
    """
    Borrower A: one big vertical section reconciled to the page
    total and 'verified' when checked generically. Checked as the Balance
    Sheet its heading names, it must not - that is what let the correct
    T-account reading lose.
    """
    vert = {"left": [], "right": [], "sections": [
        {"name": "TOTAL", "total": 300, "items": [("Capital", 100), ("Debtors", 200)]}]}
    assert F._verifies(vert)
    assert not F._verifies(vert, F.BALANCE_SHEET)


def test_capital_on_the_assets_side_is_negative_equity():
    """Borrower A FY2024: capital 36.59 lakh printed among the assets; the
    analyst shows Equity -36.59 and both totals reduced by it."""
    block = {"kind": F.BALANCE_SHEET, "sides": {
        "left": [("[CL] Sundry Creditors", 66672152), ("Secured Loan", 7742093)],
        "right": [("the proprietor CAPITAL ACCOUNT", 3659092), ("Sundry Debtors", 70755153)]}}
    items = COL._spread_items(block)
    buckets, unmapped, ignored = T.map_items(items, {})
    v = T.compute(buckets)
    assert v["equity_capital"] == -3659092
    assert v["total_assets"] == 70755153
    assert v["total_liabilities"] == 66672152 + 7742093 - 3659092
    # A capital line on the liabilities side is untouched.
    block["sides"]["right"], block["sides"]["left"][1] = [], ("Capital Account", 5)
    assert ("Capital Account", 5) in COL._spread_items(block)


def test_printed_total_without_separators_is_found():
    """Borrower A FY2025: 'TOTAL 168012440 | TOTAL 168012440', no commas."""
    rows = [(0, [(80, 150, "Fuel"), (223, 256, "62125749")]),
            (1, [(136, 159, "TOTAL"), (261, 298, "168012440"),
                 (356, 379, "TOTAL"), (485, 523, "168012440")])]
    assert F._printed_total(rows) == (168012440.0, 1)


def test_header_years_are_not_a_printed_total():
    rows = [(0, [(300, 340, "2025"), (400, 440, "2024")]),
            (1, [(80, 150, "Fuel"), (223, 256, "62,125")])]
    assert F._printed_total(rows) == (None, None)


def test_a_balance_sheet_row_with_a_bare_figure_is_not_a_heading():
    """Borrower J AY 2023-24: 'CAPITAL ACCOUNT COMPUTER 10172' opened a phantom
    Capital Account block and split the real Balance Sheet in two."""
    assert not F._match_title("CAPITAL ACCOUNT COMPUTER 10172")
    assert F._match_title("PROV. CAPITAL A/C FOR THE YEAR ENDING 31-03-2023")
    assert F._match_title("PROV. BALANCE SHEET AS ON PERIOD ENDED 31-03-2023")
    assert F._match_title("Balance Sheet as at 31st March 2024")


def test_a_damaged_year_does_not_reject_a_heading():
    """Borrower F FY2025: the scanner's text layer read the year as
    '?025'; the bare-number rule must only reject a standalone figure."""
    assert F._match_title("Balance Sheet as at 31st March,?025")
    assert not F._match_title("CAPITAL ACCOUNT COMPUTER 10172")


def test_two_period_figures_fused_in_one_cell_are_split():
    """Borrower F FY2025: 'Long-term borrowings 24,18,44,856
    12,32,46,427' - this year then last year in ONE cell; the vertical
    reader took the last figure (last year's)."""
    cells = [(142, 218, "Long-term borrowings"), (312, 316, "4"),
             (350, 460, "24,18,44,856 12,32,46,427")]
    out = F._split_amount_runs(cells)
    assert [t for _a, _b, t in out] == ["Long-term borrowings", "4",
                                        "24,18,44,856", "12,32,46,427"]
    first, second = out[2], out[3]
    assert first[1] <= second[0] and first[0] == 350 and abs(second[1] - 460) < 1e-6
    # A label with a figure, or a single figure, is left alone.
    assert F._split_amount_runs([(0, 9, "Trade payables 57,76,235")]) == \
        [(0, 9, "Trade payables 57,76,235")]
    assert F._split_amount_runs([(0, 9, "(3,37,10,948)")]) == [(0, 9, "(3,37,10,948)")]


def test_fused_two_period_total_is_not_welded_into_one_figure():
    """Borrower F FY2025: 'Total 21,84,30,367 13,42,85,686' was recorded as the
    printed total 218430367134285696."""
    rows = [(0, [(141, 159, "Total"), (350, 460, "21,84,30,367 13,42,85,686")])]
    total, _at = F._printed_total(rows)
    assert total in (21843036.7e1, 218430367.0, 134285686.0)
    assert total < 1e10


def test_fused_depreciation_and_net_value_are_read_as_two_figures():
    """
    Borrower J FY2023 (assets side, same x-positions as the real page): the net
    value fused with its depreciation in one cell must still be picked with
    the other net values. Printed total = sum of the nets.
    """
    rows = [
        (0, [(210, 250, "COMPUTER"), (336, 360, "10172")]),
        (1, [(210, 250, "LESS: DEP"), (339, 360, "4069"), (379, 400, "6103.00")]),
        (2, [(210, 250, "FURNITURE"), (332, 360, "129732")]),
        (3, [(210, 250, "LESS : DEP"), (336, 400, "12973 116759.00")]),
        (4, [(210, 250, "CLOSING STOCK"), (369, 400, "2754100.00")]),
    ]
    target = 6103 + 116759 + 2754100
    sides = F._harvest_at(rows, 150, target)
    got = sorted(a for _l, a in sides["right"])
    assert sum(got) == target, got
    assert 12973 not in got and 4069 not in got


def test_name_only_fixed_assets_and_lenders_are_mapped():
    """Borrower J FY2023: assets listed by what they are, lenders by name."""
    for label in ("COMPUTER", "HONDA CAR", "MOTOR TRUEK", "AIRCONDTIONER",
                  "HOUSEHOLD APPLIANCE", "BIKE"):
        assert T.map_label(label, {}) == "fixed_assets", label
    assert T.map_label("GOLD & ORNAMENTS", {}) == "investments"
    assert T.map_label("[LIAB] MUTHOOT FINANCE LTD", {}) == "secured_loan_asset_financed"
    assert T.map_label("[LIAB] IDFC FIRST BANK", {}) == "secured_loan_asset_financed"
    # Running costs of the same things stay expenses.
    assert T.map_label("[EXP] Car Expenses", {}) != "fixed_assets"
    assert T.map_label("Vehicle Insurance", {}) != "fixed_assets"
    assert T.map_label("Motor Car Repairs", {}) != "fixed_assets"


def test_far_side_figure_follows_its_position_not_the_row_label():
    """
    Borrower F FY2023 (same x-positions, page ~1660 wide): 'Investment
    5,67,565' is printed one row above its caption, on a row whose only
    label is a liability. It must land on the assets side.
    """
    rows = [
        (0, [(135, 332, "a relative"), (705, 833, "38,21,283"),
             (1536, 1650, "5,67,565")]),
        (1, [(842, 997, "Investment")]),
    ]
    sides = F._harvest_at(rows, 1000, None)
    assert (567565.0 in [a for _l, a in sides["right"]])
    assert (567565.0 not in [a for _l, a in sides["left"]])
    assert [a for _l, a in sides["left"]] == [3821283.0]


def test_figure_printed_above_its_caption_takes_that_caption():
    """
    Borrower F FY2023 assets side (real x-positions): each figure sits
    one row ABOVE its caption. The totals already reconciled; the labels -
    and so the buckets - were wrong or missing.
    """
    rows = [
        (0, [(228, 371, "Liabilities"), (1498, 1646, "8,26,44,874")]),
        (1, [(838, 1008, "Fixed Assets")]),
        (2, [(135, 332, "a relative"), (705, 833, "38,21,283"),
             (1536, 1650, "5,67,565")]),
        (3, [(842, 997, "Investment")]),
        (4, [(116, 300, "Secured Loan"), (686, 836, "9,26,95,921"),
             (1561, 1652, "97,150")]),
        (5, [(843, 969, "Deposits")]),
        (6, [(1012, 1300, "Loans & Advances"), (1526, 1656, "57,35,099")]),
    ]
    # Divider between the liabilities figures (<= 836) and the assets captions
    # (>= 838), as on the real page.
    sides = F._harvest_at(rows, 837, None)
    right = {a: l for l, a in sides["right"]}
    assert right[82644874.0].endswith("Fixed Assets")
    assert right[567565.0].endswith("Investment")
    assert right[97150.0].endswith("Deposits")
    assert right[5735099.0].endswith("Loans & Advances")


def test_current_asset_wording_beats_a_carried_investment_tag():
    for label in ("[INV] Loans & Advances", "[INV] Cash & Bank Balance",
                  "[FA] Other Current Assets"):
        assert T.map_label(label, {}) == "current_assets", label
    assert T.map_label("[INV] Sundry Debtors", {}) == "debtors"
    assert T.map_label("[INV] Fdr in Ubi 1", {}) == "investments"
    assert T.map_label("[FA] Mh-12-1615", {}) == "fixed_assets"


def test_unlabelled_expense_total_under_its_heading_counts_as_the_expense_side():
    """Borrower M FY2025: '3,43,191.506' printed with no label under
    'IV EXPENSES' - the P&L was rejected as missing its expense side."""
    block = {"kind": F.PROFIT_LOSS, "sides": {"left": [], "right": [], "sections": [
        {"name": "III Total Income", "total": 378814,
         "items": [("Revenue", 372298), ("Other Income", 6516)]},
        {"name": "IV EXPENSES", "total": 343192,
         "items": [("Ops", 181913), ("Employee", 64466), ("Finance", 30021),
                   ("Depreciation", 54401), ("Other", 12391)]}]}}
    assert F.check_block(block)["balanced"]


def test_carried_to_balance_sheet_is_not_a_heading():
    assert not F._match_title("XVII Profit/(Loss) Carried over to Balance Sheet")
    assert not F._match_title("Net Profit transferred to Balance Sheet")
    assert F._match_title("Balance Sheet as at 31st March 2025")


def test_restated_result_is_not_harvested_as_an_item():
    """Borrower M FY2025: the profit after tax printed again on the next
    'Deferred Tax - Earlier Year' line must not become Rs 303 lakh of tax."""
    rows = [
        (0, [(78, 700, "V Profit before exceptional and extraordinary items and tax"),
             (1100, 1260, "35,622.534")]),
        (1, [(83, 288, "x Tax Expense"), (1148, 1262, "1,008.045")]),
        (2, [(157, 335, "C. Deferred Tax"), (1148, 1261, "4,310.081")]),
        (3, [(82, 394, "XI Profit/ (Loss) after tax"), (1129, 1263, "30,304.407")]),
        (4, [(75, 515, "XVI Deffered Tax (Liability )-Earlier Year"),
             (1135, 1263, "30,304.407")]),
    ]
    items = [l for s in F._harvest_vertical(rows)["sections"] for l, _a in s["items"]]
    assert not any("Earlier Year" in l for l in items)
    assert any("Deferred Tax" in l for l in items)


def test_tally_loans_group_with_its_own_total_is_a_secured_loan():
    """Borrower E FY2024: 'Loans (Liability) 2,04,37,748' was unmapped."""
    assert T.map_label("Loans (Liability )", {}) == "secured_loan_asset_financed"
    assert T.map_label("Loans & Advances (Asset)", {}) != "secured_loan_asset_financed"


def test_lettered_summary_pl_with_bare_totals_verifies():
    """
    Borrower E FY2024 summary P&L (real text and x-positions):
    lettered headings, bare 'Total (A)'/'Total (B)', and the result line
    wrapped over two rows. Profit is 3,269,400, as printed.
    """
    rows = [
        (0, [(269, 388, "Sr. No."), (582, 790, "Particulars"), (1139, 1560, "Sch. No. Amount (Rs.)")]),
        (1, [(302, 575, "A] Income :-")]),
        (2, [(448, 539, "Sales"), (1196, 1228, "7"), (1376, 1586, "6,36,26,453")]),
        (3, [(977, 1079, "Total"), (1180, 1247, "(A)"), (1378, 1586, "6,36,26,453")]),
        (4, [(303, 660, "B] Expenditure :-")]),
        (5, [(455, 715, "Direct Expenses"), (1199, 1229, "8"), (1378, 1588, "5,16,58,454")]),
        (6, [(450, 738, "Indirect Expenses"), (1199, 1229, "9"), (1407, 1591, "79,09,587")]),
        (7, [(977, 1082, "Total"), (1182, 1248, "(B)"), (1380, 1588, "5,95,68,041")]),
        (8, [(310, 1078, "c] Profit/ Loss before Tax & Depreciation"), (1411, 1588, "40,58,412")]),
        (9, [(307, 363, "D]"), (452, 663, "Depreciation"), (1432, 1588, "7,89,012")]),
        (10, [(412, 948, "Net Profit / Loss Transferred to")]),
        (11, [(310, 903, "E] Proprietor's Captial Account"), (1166, 1266, "(C-D)"),
              (1409, 1589, "32,69,400")]),
    ]
    sides = F._harvest_vertical(rows)
    block = {"kind": F.PROFIT_LOSS, "sides": {"left": [], "right": [], **sides}}
    assert F.check_block(block)["balanced"], F.check_block(block)["reason"]
    items = F.all_items(block["sides"])
    assert not any("Captial" in l for l, _a in items)
    buckets, _u, _i = T.map_items(items, {})
    assert T.compute(buckets)["profit_before_tax"] == 3269400


def test_statement_in_lakhs_tolerates_rounding_of_its_lines():
    """Borrower H FY2025 (Rs in lakhs): 278.85 + 434.80 + 137.01 = 850.66 against
    a printed 850.65 - Rs 1,000, pure rounding - must reconcile."""
    L = 100000
    block = {"kind": F.BALANCE_SHEET, "unit_scale": L, "sides": {
        "left": [], "right": [], "sections": [
            {"name": "(4) Current Liabilities", "total": round(850.65 * L),
             "items": [("Trade Payables", round(278.85 * L)),
                       ("Other current liabilities", round(434.80 * L)),
                       ("Short Term Provision", round(137.01 * L))]},
            {"name": "Total Equity & Liabilities", "total": 100 * L,
             "items": [("x", 50 * L), ("y", 50 * L)]},
            {"name": "Total Assets", "total": 100 * L,
             "items": [("a", 60 * L), ("b", 40 * L)]}]}}
    assert F.check_block(block)["balanced"], F.check_block(block)["reason"]
    # The same Rs 1,000 in a statement printed in rupees is a real miss.
    block["unit_scale"] = 1
    assert not F.check_block(block)["balanced"]


def test_line_printed_with_its_own_breakdown_is_counted_once():
    """Borrower H FY2025: '(b) Trade Payables 278.85' then its MSME breakdown
    '(B) ... other creditors 278.85' - both were counted."""
    items = [("(b) Trade Payables", 278.85),
             ("(B) total outstanding dues of creditors other than micro", 278.85),
             ("(c) Other current liabilities", 434.80),
             ("(d) Short Term Provision", 137.01)]
    kept = F._drop_restated_parent(items, 850.65)
    assert [l for l, _a in kept] == [l for l, _a in items[1:]]
    # A section that already reconciles is left alone.
    assert F._drop_restated_parent(items[1:], 850.65) == items[1:]


def test_total_glued_to_the_next_word_is_still_a_total():
    assert F._ANCHOR_RE.match("TotalAssets")
    assert F._ANCHOR_RE.match("Total Assets")
    assert T.map_label("[NCA] TotalAssets", {}) is T.IGNORE


def test_garbled_embedded_ocr_layer_is_detected():
    """Borrower I AY 2023-24: a scanner app's own OCR layer, trusted as
    digital text because it was not blank."""
    from engine.parser import _looks_garbled
    garbled = ("Fixed assets D 51,65,06,94g\nTotal l,06,2g,7g,ggo\n"
               "Sales G 1,,33,94,6L,760")
    assert _looks_garbled(garbled)
    clean = ("Fixed assets 51,65,06,949\nTotal 1,06,29,79,990\n"
             "Sundry Creditors 16,950\nAxis Bank A/c 17194")
    assert not _looks_garbled(clean)
    # A single typo is not the layer.
    assert not _looks_garbled("Total 51,65,06,94g and 1,06,29,79,990")


@pytest.mark.parametrize("line,year", [
    ("Balance Sheet as at March 31, 2025", 2025),          # month first (Borrower H)
    ("Statement of Profit and Loss for the year ended March 31, 2025", 2025),
    ("BALANCE SHEET AS AT 31ST MARCH, 2023", 2023),        # day first
    ("BALANCE SHEET AS ON 31.03.2024", 2024),              # numeric
    ("Balance Sheet as at 31/03/2026", 2026),
    ("PROFIT AND LOSS ACCOUNT FOR THE YEAR ENDED ON 31ST MARCH 2026", 2026),
])
def test_statement_period_in_every_date_shape(line, year):
    assert F._find_period([line], 0)[0] == year


def test_misspelt_deferred_tax_liability_stays_on_the_balance_sheet():
    """Borrower H FY2023: 'Deferred Tax Liabilty (Net)' fell through to the P&L."""
    assert T.map_label("[NCL] (b) Deferred Tax Liabilty (Net)", {}) == "deferred_tax_liability"
    assert T.map_label("Deferred Tax Liability (Net)", {}) == "deferred_tax_liability"
    assert T.map_label("C. Deferred Tax", {}) == "provision_deferred_tax"


def test_mat_credit_on_the_pl_is_part_of_the_tax_charge():
    """Borrower H FY2023: PBT 1,84,98,578 - current tax 31,08,354 - MAT credit
    32,19,297 = the printed 1,21,70,927; the analyst spreads MAT credit as
    deferred tax. A Balance Sheet 'MAT Credit Entitlement' asset is not."""
    items = [("[INC] Revenue", 18498578), ("Current tax", 3108354), ("MAT Credit", 3219297)]
    buckets, unmapped, _i = T.map_items(items, {})
    assert not unmapped
    assert T.compute(buckets)["profit_after_tax"] == 12170927
    assert T.map_label("[NCA] MAT Credit Entitlement", {}) != "provision_deferred_tax"
    assert T.map_label("Basic & Diluted", {}) is T.IGNORE


# ── Several files for one case ───────────────────────────────────

def _doc(name, year, blocks):
    return {"source_name": name, "identity": {}, "scanned": False,
            "book_profit": None, "book_profit_page": None, "pages_total": 3,
            "pages_used": 2, "page_summary": {"statement": 1}, "blocks": blocks,
            "vision_pages": [], "year": year}


def _bs(year, amount, src, status=F.VERIFIED):
    return {"kind": F.BALANCE_SHEET, "year": year, "status": status,
            "entity": "Borrower H", "page": 0, "source": src, "title": "",
            "unit_scale": 1, "check": {"reason": ""},
            "sides": {"left": [("Share Capital", amount)],
                      "right": [("Fixed Assets", amount)]}}


def test_statements_are_pooled_by_their_own_year_across_files(monkeypatch):
    """
    Borrower H: 'ITR 2022-2023.pdf' holds the FY2023 AND the FY2024 Balance Sheet,
    the audit report reprints FY2024, and a loan offer letter sits in the same
    upload. One column per YEAR - not per file - with the reprint dropped and
    the letter noted, not given a column.
    """
    docs = {
        "ITR 2022-2023.pdf": _doc("ITR 2022-2023.pdf", 2023,
                                  [_bs(2023, 100, "ITR 2022-2023.pdf"),
                                   _bs(2024, 250, "ITR 2022-2023.pdf", F.UNVERIFIED)]),
        "Audit Report.pdf": _doc("Audit Report.pdf", 2024,
                                 [_bs(2024, 250, "Audit Report.pdf")]),
        "BUS LOI.pdf": _doc("BUS LOI.pdf", None, []),
    }
    monkeypatch.setattr(COL, "_read_document", lambda src, *a, **k: docs[src])
    cols = COL.spread_many(list(docs), extract_fn=None)
    assert [c["year"] for c in cols] == [2023, 2024]
    fy24 = cols[1]
    assert len(fy24["blocks_used"]) == 1                     # reprint dropped
    assert fy24["blocks_used"][0]["status"] == F.VERIFIED    # best reading kept
    assert "Audit Report.pdf" in fy24["source_name"]
    assert fy24["values"]["equity_capital"] == 250
    assert any("BUS LOI.pdf" in w for w in cols[0]["warnings"])


def _pl(year, dep, src, entity="Borrower R"):
    return {"kind": F.PROFIT_LOSS, "year": year, "status": F.VERIFIED,
            "entity": entity, "page": 0, "source": src, "title": "",
            "unit_scale": 1, "check": {"reason": ""},
            "sides": {"left": [("[EXP] Depreciation", dep)],
                      "right": [("[INC] Sales", dep * 10)]}}


def test_a_misdated_page_stays_with_its_file_when_the_other_year_has_it(monkeypatch):
    """
    Borrower R: page 2 of the FY2025 P&L read as 2024. FY2024 already has
    this business's P&L from the FY2024 file, so the page must stay in FY2025
    - moved, FY2025 lost its depreciation and interest.
    """
    docs = {
        "ITR 2023-2024.pdf": _doc("ITR 2023-2024.pdf", 2024,
                                  [_pl(2024, 27, "ITR 2023-2024.pdf")]),
        "ITR 2024-2025.pdf": _doc("ITR 2024-2025.pdf", 2025,
                                  [_pl(2025, 0, "ITR 2024-2025.pdf"),
                                   _pl(2024, 42, "ITR 2024-2025.pdf")]),   # misdated
    }
    monkeypatch.setattr(COL, "_read_document", lambda src, *a, **k: docs[src])
    cols = COL.spread_many(list(docs), extract_fn=None)
    by_year = {c["year"]: c for c in cols}
    assert by_year[2025]["values"]["depreciation"] == 42
    assert by_year[2024]["values"]["depreciation"] == 27


def test_a_reprint_of_another_years_statement_moves_and_is_deduped(monkeypatch):
    """
    Borrower H: 'ITR 2023-2024.pdf' (about FY2024) also reprints the FY2025
    Balance Sheet - same figures as the audit report's. It must go to FY2025
    (and be dropped there as a duplicate), not be added to FY2024's own.
    """
    fy25 = _bs(2025, 2942, "Audit Report.pdf")
    reprint = _bs(2025, 2942, "ITR 2023-2024.pdf")
    reprint["entity"] = "Borrower H"
    docs = {
        "Audit Report.pdf": _doc("Audit Report.pdf", 2025, [fy25]),
        "ITR 2023-2024.pdf": _doc("ITR 2023-2024.pdf", 2024,
                                  [_pl(2024, 50, "ITR 2023-2024.pdf", "Borrower H"),
                                   _bs(2024, 2419, "ITR 2023-2024.pdf"), reprint]),
    }
    monkeypatch.setattr(COL, "_read_document", lambda src, *a, **k: docs[src])
    cols = {c["year"]: c for c in COL.spread_many(list(docs), extract_fn=None)}
    assert cols[2024]["values"]["equity_capital"] == 2419       # not 2419 + 2942
    assert cols[2025]["values"]["equity_capital"] == 2942       # counted once


def test_an_undated_statement_takes_the_date_of_its_neighbour():
    """
    Borrower O: 'ITR 23-24.pdf' (AY 2024-25) holds the Balance Sheet
    'as on 31.03.2023' on page 2 and an UNDATED P&L on page 3. The P&L
    belongs with its Balance Sheet (FY2023), not with the ITR's year.
    """
    bs = {"kind": F.BALANCE_SHEET, "year": 2023, "page": 1, "entity": "Borrower D"}
    pl = {"kind": F.PROFIT_LOSS, "year": None, "page": 2, "entity": "Borrower D"}
    far = {"kind": F.PROFIT_LOSS, "year": None, "page": 9, "entity": "Borrower D"}
    COL._infer_missing_years([bs, pl, far])
    assert pl["year"] == 2023 and pl["year_inferred"]
    assert far["year"] is None          # too far away to be its pair


def test_file_year_tie_is_broken_by_the_assessment_year():
    blocks = [{"year": 2023}, {"year": 2024}]
    assert COL._fy_end_year(blocks, {"ay": "2023-24"}) == 2023
    assert COL._fy_end_year(blocks, {"ay": "2024-25"}) == 2024
    assert COL._fy_end_year(blocks, {}) == 2023


def test_an_unreadable_file_does_not_cost_the_others_their_columns(monkeypatch):
    def read(src, *a, **k):
        if src == "broken.pdf":
            raise ValueError("encrypted")
        return _doc(src, 2024, [_bs(2024, 10, src)])
    monkeypatch.setattr(COL, "_read_document", read)
    cols = COL.spread_many(["good.pdf", "broken.pdf"], extract_fn=None)
    assert [c["year"] for c in cols] == [2024]
    assert any("broken.pdf" in w and "encrypted" in w for w in cols[0]["warnings"])


# ── Analysis ─────────────────────────────────────────────────────

def _col(year, **v):
    values = {"sales_other_income": 100_00_000, "profit_before_tax": 10_00_000,
              "profit_after_tax": 8_00_000, "networth": 50_00_000,
              "total_assets": 150_00_000, "total_liabilities": 150_00_000,
              "current_liabilities": 20_00_000, "interest_finance": 2_00_000}
    values.update(v)
    return {"year": year, "values": values, "blocks_used": [{}],
            "ratios": T.ratios(values), "book_profit": None}


def test_flags_loss_and_sharp_revenue_drop():
    cols = [_col(2024), _col(2025, sales_other_income=60_00_000,
                             profit_before_tax=-5_00_000,
                             profit_after_tax=-5_00_000)]
    names = {(f["year"], f["flag"]) for f in A.flags(cols)}
    assert (2025, "Loss-making") in names
    assert (2025, "Turnover falling sharply") in names
    assert not any(y == 2024 for y, _n in names if _n == "Loss-making")


def test_unread_year_is_reported_not_assessed():
    cols = [{"year": 2024, "values": {}, "blocks_used": [], "ratios": {}}]
    assert [f["flag"] for f in A.flags(cols)] == ["Not assessable"]


def test_trends_skip_non_consecutive_years():
    cols = [_col(2022), _col(2025, sales_other_income=200_00_000)]
    assert A.trends(cols)["sales_other_income"] == [None, None]


def test_cagr():
    cols = [_col(2023), _col(2025, sales_other_income=121_00_000)]
    assert abs(A.cagr(cols) - 0.10) < 1e-9


# ── Schedules ────────────────────────────────────────────────────

def _rows(*lines):
    """page rows from 'label || amount || amount' strings."""
    out = []
    for n, ln in enumerate(lines):
        cells = [(i * 200, i * 200 + 150, t.strip()) for i, t in enumerate(ln.split("||"))]
        out.append((n * 10, cells))
    return out


_sample_e_SCHEDULES = _rows(
    "Note Forming part of Profit & Loss Account for the Year ended 31-03-2023",
    "Sch o8 Direct Expenses",
    "Sr. No. Particulars || Amount (Rs.) Amount (Rs.)",
    "A Direct Expenses",
    "Transport Charges Paid || 1,73,42,490",
    "Disel & Oil Expenses || 8,81,593",
    "Stipend, Salary and Wages || 2,87,60,157 || 4,69,84,240",
    "Total || 4,69,84,240",
    "Sch o9 Indirect Expenses",
    "To Staff Welfare Expenses || 13,56,019",
    "To Interest on loan || 3,12,459",
    "To Other Charges || 6,01,336",
    "22,69,814",
    "Total || 22,69,814",
)


def test_schedule_read_and_proved_by_its_own_total():
    """
    Borrower E FY2023: the P&L face prints only Direct / Indirect
    Expenses. The schedules behind them are read, the running group total on
    the last line ('2,87,60,157 | 4,69,84,240') is not taken as a line, and
    each schedule verifies against the Total it prints.
    """
    sch = F.find_schedules([_sample_e_SCHEDULES])
    assert [(s["number"], s["name"], s["verified"]) for s in sch] == [
        ("08", "Direct Expenses", True), ("09", "Indirect Expenses", True)]
    assert sch[0]["items"][2] == ("Stipend, Salary and Wages", 28760157)


def test_notes_heading_is_not_a_schedule():
    assert F._schedule_heading("Notes Forming part of the Balance Sheet") is None
    assert F._schedule_heading("Note 15 : Employee benefits expense") == (
        "15", "Employee benefits expense")
    assert F._schedule_heading("Schedule E - Cash and Bank") == ("E", "Cash and Bank")


def test_pl_group_lines_replaced_by_their_schedules():
    block = {"kind": F.PROFIT_LOSS, "unit_scale": 1, "sides": {
        "left": [("[EXP] Direct Expenses", 46984240),
                 ("[EXP] Indirect Expenses", 2269814),
                 ("[EXP] Depreciation", 1088440)],
        "right": [("[INC] Sales", 53581608)]}}
    used = F.expand_with_schedules(block, F.find_schedules([_sample_e_SCHEDULES]))
    labels = [l for l, _a in block["sides"]["left"]]
    assert "[EXP] Stipend, Salary and Wages" in labels
    assert "[EXP] To Interest on loan" in labels
    assert "[EXP] Direct Expenses" not in labels
    assert sum(a for _l, a in block["sides"]["left"]) == 46984240 + 2269814 + 1088440
    assert len(used) == 2


def test_schedule_not_used_unless_it_proves_itself():
    """A schedule whose lines miss its total, or whose total is not the
    group line's amount, leaves the face line alone. So does Other expenses."""
    bad = _rows("Sch 09 Indirect Expenses", "Staff Welfare || 13,56,019",
                "Total || 22,69,814")
    for sides, schedules in [
            ({"left": [("[EXP] Indirect Expenses", 2269814)], "right": []}, bad),
            ({"left": [("[EXP] Indirect Expenses", 9999999)], "right": []},
             _sample_e_SCHEDULES),
            ({"left": [("[EXP] Other Expenses", 2269814)], "right": []},
             _sample_e_SCHEDULES)]:
        block = {"kind": F.PROFIT_LOSS, "unit_scale": 1, "sides": sides}
        before = list(sides["left"])
        assert F.expand_with_schedules(block, F.find_schedules([schedules])) == []
        assert block["sides"]["left"] == before


def test_schedule_labels_map_to_their_rows():
    """Borrower E's schedule lines: statutory contributions are employee cost,
    the plural 'Repairs' and the misspelt 'Disel' are operating cost - and a
    PF liability is not an expense."""
    learned = {}
    want = {"[EXP] To ESIC Paid": "employee_costs",
            "[EXP] To PF Paid": "employee_costs",
            "[EXP] To Repairs and Maintenance": "transport_admin",
            "[EXP] Disel & Oil Expenses": "transport_admin"}
    for label, bucket in want.items():
        buckets, _u, _i = T.map_items([(label, 100)], learned)
        assert list(buckets) == [bucket], label
    buckets, _u, _i = T.map_items([("[CL] PF Payable", 100)], learned)
    assert "employee_costs" not in buckets


_sample_i_SCHEDULE = _rows(
    "Notes to the financial statements for the year ended as on 31st March 2024",
    "Schedule H : Éxpenses",
    "Direct expenses",
    "Petrol and diesel expenses || 6,81,68,409",
    "Vehicle hiring charges || 5,08,46,237",
    "Total || 11,90,14,646",
    "Indirect expenses",
    "Interest on loan || 6,31,65,467",
    "Salary || 88,11,01,546",
    "Depreciation || 15,58,76,792",
    "Total || 1,10,01,43,805",
)


def test_one_schedule_heading_holding_two_groups():
    """Borrower I FY2024: 'Schedule H' holds Direct and Indirect expenses,
    each closed by its own Total; the accented OCR heading still reads."""
    sch = F.find_schedules([_sample_i_SCHEDULE])
    assert [(s["number"], s["caption"], s["verified"]) for s in sch] == [
        ("H", "Direct expenses", True), ("H", "Indirect expenses", True)]


def test_schedule_line_the_face_prints_separately_is_not_doubled():
    """The Indirect schedule includes Depreciation, which the P&L face also
    prints on its own line: 110.01 cr = 94.43 cr + 15.59 cr. Expanded without
    it, so depreciation is counted once and the total is unchanged."""
    items = [("[EXP] Direct expenses", 119014646),
             ("[EXP] Indirect expenses", 944267013),
             ("[EXP] Depreciation", 155876792)]
    block = {"kind": F.PROFIT_LOSS, "unit_scale": 1, "sides": {"sections": [
        {"name": "Total Expenses", "items": list(items), "total": None}]}}
    used = F.expand_with_schedules(block, F.find_schedules([_sample_i_SCHEDULE]))
    got = block["sides"]["sections"][0]["items"]
    assert len(used) == 2
    assert [l for l, _a in got].count("[EXP] Depreciation") == 1
    assert "[EXP] Salary" in [l for l, _a in got]
    assert sum(a for _l, a in got) == sum(a for _l, a in items)


# ── The comparative column as a year of its own ──────────────────

def _comp_bs(year, amount, src):
    """A prior-year Balance Sheet taken from the next year's comparative
    column (financials.find_blocks marks these)."""
    b = _bs(year, amount, src)
    b["from_comparative"] = True
    return b


def test_comparative_year_is_used_when_no_file_covers_it(monkeypatch):
    """
    sample_d's provisional set is FY2026 only, but both statements print FY2025
    beside it line for line. That year has no statement of its own anywhere
    in the upload, so the comparative column becomes its column.
    """
    docs = {"provisional.pdf": _doc("provisional.pdf", 2026,
                                    [_bs(2026, 300, "provisional.pdf"),
                                     _comp_bs(2025, 250, "provisional.pdf")])}
    monkeypatch.setattr(COL, "_read_document", lambda src, *a, **k: docs[src])
    cols = {c["year"]: c for c in COL.spread_many(list(docs), extract_fn=None)}
    assert sorted(cols) == [2025, 2026]
    assert cols[2025]["values"]["equity_capital"] == 250
    assert cols[2026]["values"]["equity_capital"] == 300
    assert any("comparative" in w.lower() for w in cols[2025]["warnings"])


def test_a_real_statement_beats_the_comparative_for_the_same_year(monkeypatch):
    """The borrower's own FY2025 filing is the better reading: the next
    year's comparative column must not be pooled alongside it (it would be
    added to it, doubling the year), nor replace it."""
    docs = {
        "FY2025.pdf": _doc("FY2025.pdf", 2025, [_bs(2025, 251, "FY2025.pdf")]),
        "FY2026.pdf": _doc("FY2026.pdf", 2026,
                           [_bs(2026, 300, "FY2026.pdf"),
                            _comp_bs(2025, 250, "FY2026.pdf")]),
    }
    monkeypatch.setattr(COL, "_read_document", lambda src, *a, **k: docs[src])
    cols = {c["year"]: c for c in COL.spread_many(list(docs), extract_fn=None)}
    assert cols[2025]["values"]["equity_capital"] == 251
    assert not any(b.get("from_comparative") for b in cols[2025]["blocks_used"])
    # Nor may it drift into the year it was PRINTED in, which would add
    # last year's figures to this year's.
    assert cols[2026]["values"]["equity_capital"] == 300


def test_column_never_titles_itself_after_an_implausible_entity(monkeypatch):
    """_main_entity screens candidates through _belongs_to, but the column's
    own fallback took the first block's entity whatever it was - so a line
    already rejected as a name (an address, a sentence) still titled the
    workbook. Confirmed on Borrower D' statements."""
    address = ("6-A, H & G House, Sector 11, C B D Belapur, Navi Mumbai, "
               "Maharashtra, India, 400614")
    b = _bs(2026, 100, "sample_d.pdf")
    b["entity"] = address
    docs = {"sample_d.pdf": _doc("sample_d.pdf", 2026, [b])}
    monkeypatch.setattr(COL, "_read_document", lambda src, *a, **k: docs[src])
    col = COL.spread_many(list(docs), extract_fn=None)[0]
    assert col["entity"] != address
    assert col["values"]["equity_capital"] == 100      # the figures still stand


def test_two_different_statements_of_one_kind_both_survive():
    """
    A petrol pump's accounts print a Trading ("PUMP ACCOUNT") and a Profit &
    Loss on the SAME page, both profit_loss, neither naming an entity - so
    both share the (entity, kind, year) key. Keeping only the "best" of them
    threw the trading account away and with it the year's whole turnover:
    Borrower B FY2025 read Rs 21.71 lakh of income against Rs 1,659.38
    printed. They are complementary statements, not two readings of one - as
    their figures show, sharing nothing.
    """
    trading = {"kind": F.PROFIT_LOSS, "year": 2025, "status": F.VERIFIED, "entity": "",
               "page": 5, "source": "fy25.pdf", "title": "PUMP ACCOUNT", "unit_scale": 1,
               "check": {"reason": ""},
               "sides": {"left": [("[EXP] Purchases", 161288000)],
                         "right": [("[INC] Sales", 163766000)]}}
    pl = {"kind": F.PROFIT_LOSS, "year": 2025, "status": F.VERIFIED, "entity": "",
          "page": 5, "source": "fy25.pdf", "title": "PROFIT AND LOSS ACCOUNT",
          "unit_scale": 1, "check": {"reason": ""},
          "sides": {"left": [("[EXP] Salary", 1224000), ("[EXP] Audit Fees", 20000)],
                    "right": [("[INC] Rent received", 97000)]}}
    kept = COL._dedupe([trading, pl])
    assert {b["title"] for b in kept} == {"PUMP ACCOUNT", "PROFIT AND LOSS ACCOUNT"}


def test_a_reprint_of_the_same_statement_is_still_dropped():
    """The guard this loosens must hold: the same figures read twice are one
    statement, and counting both doubles the year."""
    a = {"kind": F.PROFIT_LOSS, "year": 2025, "status": F.VERIFIED, "entity": "",
         "page": 1, "source": "a.pdf", "title": "P&L", "unit_scale": 1,
         "check": {"reason": ""},
         "sides": {"left": [("[EXP] Salary", 1224000), ("[EXP] Audit Fees", 20000)],
                   "right": [("[INC] Sales", 163766000)]}}
    b = dict(a, title="P&L (reprint)", page=9, status=F.UNVERIFIED)
    assert len(COL._dedupe([a, b])) == 1
