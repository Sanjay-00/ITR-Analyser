"""
Pure-function unit tests. No sample ITRs needed - these always run.

    pytest tests/test_units.py

Each test here pins a behaviour that a real filing broke at least once. The
comments say which, because the failure modes are not obvious from the code.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.extract import financials as F                          # noqa: E402
from engine.ingest import relevance as R                            # noqa: E402
from engine.mapping import taxonomy as T                            # noqa: E402
from engine.mapping import template_config as C                     # noqa: E402
from engine.extract.itr_parser import to_int, extract_identity      # noqa: E402
from engine.ingest.layout import rows_with_cells, COL_SEP, rows_from_words  # noqa: E402


# ── Number parsing ────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("12,34,567",    1234567),
    ("12,34,567.00", 1234567),
    ("₹ 1,000",      1000),
    ("Rs. 1,000",    1000),      # the '.' must not become a decimal point
    ("(1,234)",      -1234),
    ("1 23 456",     123456),
    ("0",            0),
    ("",             None),
    ("-",            None),
    (None,           None),
    ("NA",           None),
])
def test_to_int(raw, expected):
    assert to_int(raw) == expected


def test_to_int_distinguishes_zero_from_missing():
    assert to_int("0") == 0 and to_int("") is None


def test_clean_amount_strips_paise_before_grouping():
    """
    '1,35,24,153.00' is 1.35 crore, not 135 crore. Stripping the thousands
    separators first welds the paise onto the rupees and inflates it 100x -
    which silently passed every balance check by scaling both sides.
    """
    assert F._clean_amount("1,35,24,153.00") == 13524153
    assert F._clean_amount("3,12,25,193.00") == 31225193


# ── Layout ────────────────────────────────────────────────────────

def _w(x0, y, x1, text):
    return (x0, y, x1, y + 10, text)


def test_columns_separated_by_wide_gaps():
    words = [_w(10, 0, 60, "Salary"), _w(62, 0, 90, "Paid"),
             _w(300, 0, 350, "72,000")]
    rows = rows_with_cells(words, 600)
    assert len(rows) == 1
    assert [c[2] for c in rows[0][1]] == ["Salary Paid", "72,000"]


def test_row_text_uses_tab_so_label_regexes_still_work():
    """The TAB must be whitespace, or every `\\s+` in the label engine breaks."""
    words = [_w(10, 0, 80, "Gross"), _w(82, 0, 140, "Total"),
             _w(142, 0, 200, "Income"), _w(400, 0, 460, "20,15,000")]
    text = rows_from_words(words, 600)[0][1]
    assert COL_SEP in text and COL_SEP.isspace()


# ── Statement harvesting ──────────────────────────────────────────

def _t_account_rows():
    """A minimal balancing T-account: two liabilities, two assets."""
    rows = [
        [_w(10, 0, 90, "Liabilities"), _w(300, 0, 380, "Assets")],
        [_w(10, 20, 90, "Capital"), _w(200, 20, 260, "1,00,000"),
         _w(300, 20, 380, "Fixed Assets"), _w(500, 20, 560, "60,000")],
        [_w(10, 40, 90, "Creditors"), _w(200, 40, 260, "50,000"),
         _w(300, 40, 380, "Cash"), _w(500, 40, 560, "90,000")],
    ]
    return [rows_with_cells(r, 600)[0] for r in rows]


def test_t_account_balances():
    rows = _t_account_rows()
    sides = F._harvest(rows, 600, 150000)
    chk = F.check_block({"sides": sides, "kind": F.BALANCE_SHEET})
    assert chk["balanced"], chk["reason"]
    assert chk["left_total"] == chk["right_total"] == 150000


def test_amount_takes_its_row_labels_side():
    """
    A liability's amount column can sit right of centre. Side must follow the
    row's LABEL, not the amount's own x - otherwise "Sundry Creditors 16,950"
    files under assets and the sheet is wrong by that amount.
    """
    rows = [rows_with_cells(r, 600)[0] for r in [
        [_w(10, 0, 120, "Sundry Creditors"), _w(320, 0, 380, "16,950")],
        [_w(330, 20, 460, "Sundry Debtors"), _w(520, 20, 580, "16,950")],
    ]]
    sides = F._harvest(rows, 600, None)
    assert [l for l, _a in sides["left"]] == ["Sundry Creditors"]
    assert [l for l, _a in sides["right"]] == ["Sundry Debtors"]


def test_check_catches_a_dropped_line():
    """The whole design rests on this: a missing row must not pass."""
    sides = {"left": [("Capital", 100000)], "right": [("Cash", 150000)]}
    chk = F.check_block({"sides": sides, "kind": F.BALANCE_SHEET})
    assert not chk["balanced"]
    assert chk["shortfall"] == 50000


def test_unverifiable_is_not_the_same_as_failed():
    """A statement printing no total is unproven, not contradicted."""
    sides = {"left": [("Capital", 100000)], "right": []}
    chk = F.check_block({"sides": sides, "kind": F.BALANCE_SHEET})
    assert F.status_of(chk) == F.UNVERIFIED
    # Well outside TOLERANCE - a couple of rupees of rounding slack is allowed.
    bad = F.check_block({"sides": {"left": [("A", 100)], "right": [("B", 900)]},
                         "kind": F.BALANCE_SHEET})
    assert F.status_of(bad) == F.FAILED


def test_heading_with_a_year_is_still_a_heading():
    """'BALANCE SHEET AS ON 31 ST MARCH 2026' must open a block; the trailing
    year is not an amount."""
    assert F._match_title("BALANCE SHEET AS ON 31 ST MARCH 2026")
    assert not F._match_title("Net Income (Trf to Capital A/c) 4,98,650")


def test_classified_by_content_not_by_heading():
    """A real filing labelled its Income & Expenditure account
    'RECEIPT & PAYMENTS ACCOUNT'."""
    sides = {"left": [("Salary Paid", 72000), ("Shop Rent", 60000)],
             "right": [("Gross Receipt", 985600)]}
    assert F._classify("RECEIPT & PAYMENTS ACCOUNT", sides) == F.PROFIT_LOSS


# ── Taxonomy ──────────────────────────────────────────────────────

@pytest.mark.parametrize("label,expected", [
    ("(a) Share Capital",                 "equity_capital"),
    ("Proprietor's Capital Account",      "equity_capital"),
    ("(b) Reserves and Surplus",          "reserves"),
    # The master format keeps trade payables / receivables on rows of their
    # own (for Creditor and Debtor Days).
    ("(b) Trade Payables",                "sundry_creditors"),
    ("Sundry Creditors",                  "sundry_creditors"),
    ("[CL] Sundry Creditors",             "sundry_creditors"),
    ("(i) Tangible Assets",               "fixed_assets"),
    ("(d) Cash and Cash equivalents",     "current_assets"),
    ("Sundry Debtors & Other Receivable", "debtors"),
    ("(c) Trade Receivables",             "debtors"),
    ("[NCA] (b) Right-of-use assets",     "fixed_assets"),
    ("(ii) Lease liabilities",            "unsecured_loans"),
    ("Depreciation and Amortization Expense", "depreciation"),
    ("Employee Benefits Expenses",        "employee_costs"),
    ("Finance Costs",                     "interest_finance"),
    ("I Revenue from Operations",         "sales_other_income"),
    # Deferred tax liability keeps its own row (confirmed against the
    # analyst's own Borrower K template), not folded into Other Long Term
    # Liabilities.
    ("Deferred Tax Liabilities (Net)",    "deferred_tax_liability"),
    ("(e) Income-tax assets (net)",       "deferred_tax_assets"),
])
def test_synonyms(label, expected):
    assert T.map_label(label) == expected


@pytest.mark.parametrize("label,expected", [
    # Both contain "Term Borrowings"/"Term Loans" and were being claimed by the
    # long-term rule, moving money across the balance sheet.
    ("(a) Short - Term Borrowings",          "current_liabilities"),
    ("(e) Short - Term Loans and Advances",  "current_assets"),
])
def test_short_term_beats_term_loan(label, expected):
    assert T.map_label(label) == expected


@pytest.mark.parametrize("label", [
    "III Total Revenue (I + II)",
    "Total Expense",
    "V Profit before Exceptional and Extraordinary Items and Tax (III-IV)",
    "IX Profit Before Tax (VII-VIII)",
    "Add: Profit/(Loss) for the year",
    "Basic (Rs.)",
])
def test_subtotals_are_ignored_not_mapped(label):
    """
    These restate figures already captured. Two 'Profit before ...
    Extraordinary Items' lines once mapped to Extraordinary Item and flipped a
    -15.32 lakh loss into a +15.32 lakh profit.
    """
    assert T.map_label(label) is T.IGNORE


# ── Learned-dictionary cache / PII ──────────────────────────────────
# The cache is only ever reached by labels the SYNONYMS table failed to
# recognise, which skews it toward exactly the wording least likely to be
# generic - a related-party lender's own name on a capital-account line, a
# driver's name. On a real filing, names like "a relative" and "Ashok
# a lender" ended up cached before this filter existed. A cached entry has no
# expiry and is reused for every future borrower, so a wrongly-cached name
# is a real person's identity sitting in a file indefinitely.

@pytest.mark.parametrize("label", [
    "a relative",
    "a lender",
    "Fasttag a payee",
    "Borrower B supplier",
    "R S Auto Center",
])
def test_names_and_businesses_are_not_cacheable(label):
    assert not T._looks_cacheable(label)


@pytest.mark.parametrize("label", [
    "Employee Welfare Expenses",
    "Interest on IT Refund",
    "Stores & Spares",
    "RTO Road Taxes Paid",
    "Outstanding dues of creditors other than micro and small enterprises",
])
def test_generic_accounting_wording_is_cacheable(label):
    assert T._looks_cacheable(label)


def test_save_learned_never_writes_an_uncacheable_label(tmp_path, monkeypatch):
    """End-to-end: even a caller that hands save_learned a name does not get
    it written to disk."""
    fake_path = tmp_path / "label_map.json"
    monkeypatch.setattr(T, "_LEARNED_PATH", str(fake_path))
    T.save_learned({"A PERSON": "equity_capital",
                    "Employee Welfare Expenses": "employee_costs"})
    import json
    on_disk = json.loads(fake_path.read_text(encoding="utf-8"))
    assert "A PERSON" not in on_disk
    assert "employee welfare expenses" in on_disk


def test_mapping_conserves_every_rupee():
    items = [("Share Capital", 100000), ("Trade Payables", 50000),
             ("Total", 150000), ("Mystery Item", 7000)]
    buckets, unmapped, ignored = T.map_items(items, learned={})
    chk = T.check_mapping(items, buckets, unmapped, ignored)
    assert chk["ok"], chk
    assert unmapped == [("Mystery Item", 7000)]
    assert ignored == [("Total", 150000)]


def test_derived_rows_and_cash_profit():
    v = T.compute({"sales_other_income": 716104, "employee_costs": 260000,
                   "other_expenses": 175687, "depreciation": 1812836})
    assert v["gross_expenses"]    == 2248523
    assert v["profit_before_tax"] == -1532419
    assert v["profit_after_tax"]  == -1532419
    # Depreciation is a book charge, so it is added back.
    assert v["cash_profit"]       == 280417


def test_ratio_is_none_not_zero_when_undefined():
    """No interest to cover is not 'coverage of zero'."""
    assert T.ratios(T.compute({"sales_other_income": 100}))["Interest Coverage"] is None


# ── Relevance ─────────────────────────────────────────────────────

def test_relevance_keeps_statements_and_drops_boilerplate():
    pages = [
        "INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT  Assessment Year 2024-25",
        "BALANCE SHEET as at March 31, 2024\nShare Capital 1,00,000.00\n"
        "Trade Payables 1,52,32,796.00\nTOTAL 1,38,00,377.00",
        "FORM NO. 3CD  Statement of particulars  Clause 12  Clause 13",
        "",
    ]
    kinds = R.classify(pages)
    assert kinds[0] == R.ACK
    assert kinds[1] == R.STATEMENT
    assert kinds[2] == R.AUDIT
    assert kinds[3] == R.NOISE
    assert 2 not in R.select(pages)


def test_relevance_falls_back_to_everything_when_nothing_matches():
    """An unclassifiable bundle should still be attempted, not skipped."""
    assert R.select(["mystery", "pages"]) == [0, 1]


# ── Identity ──────────────────────────────────────────────────────

def test_extract_identity():
    ident = extract_identity(
        "INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT\nAssessment Year 2023-24\n"
        "Name  RAMESH KUMAR TRADERS\nPAN  ABCDE1234F\nStatus  Firm\n"
        "Form Number  ITR-3\n")
    assert ident["ay"] == "2023-24"
    assert ident["pan"] == "ABCDE1234F"
    assert ident["form"] == "ITR-3"


# ── Config consistency ──────────────────────────────────────────────
# template_config.RATIO_NAMES / SNAP_RATIO_NAMES and taxonomy.ratios() have
# to agree on names by hand across two files - excel_generator reads ratio
# values by looking a name up in the dict ratios() returns, and a silent
# mismatch renders as "n/a" rather than an error. These tests are the
# guardrail that catches the two files drifting apart.

def test_ratio_names_match_what_ratios_actually_returns():
    computed = set(T.ratios({}).keys())
    for name in C.RATIO_NAMES:
        assert name in computed, f"RATIO_NAMES has {name!r}, ratios() does not"
    assert computed == set(C.RATIO_NAMES), (
        f"ratios() returns names RATIO_NAMES does not list: "
        f"{computed - set(C.RATIO_NAMES)}")


def test_snap_ratio_names_are_known_ratios_or_the_dscr_sentinel():
    computed = set(T.ratios({}).keys())
    for label, key in C.SNAP_RATIO_NAMES:
        assert key == "DSCR" or key in computed, (
            f"SNAP_RATIO_NAMES entry {label!r} points at {key!r}, which "
            f"ratios() does not return")


def test_every_row_key_is_labelled_or_a_section_row():
    """Every ROWS entry with a value (item/total) must resolve to a display
    label - a missing LABELS entry would otherwise print the raw dict key."""
    for kind, key, label in C.ROWS:
        if kind in ("item", "total"):
            assert label or key in C.LABELS, f"{key!r} has no label"


def test_every_formula_refers_only_to_real_rows():
    """A {key} placeholder that names no ROWS entry would crash the export."""
    import re
    row_keys = {key for kind, key, _l in C.ROWS if kind in ("item", "total")}
    templates = (list(C.TOTAL_FORMULAS.values()) + [f for _n, f, _fmt in C.RATIOS]
                 + [f for _l, f in C.SNAP])
    for tmpl in templates:
        for key in re.findall(r"\{(\w+)\}", tmpl):
            assert key in row_keys, f"{tmpl!r} refers to {key!r}, not a sheet row"


# ── Entity resolution ─────────────────────────────────────────────
# Confirmed on a real filing (FC6 / Borrower C): without
# Vision, the resolved entity degraded to just "(Proprietor : Borrower S
# Borrower S)" because _find_entity returned on the FIRST non-skipped line
# above the heading, never looking further up at the real business name
# printed one line above that.

def test_find_entity_prefers_business_name_over_proprietor_line():
    lines = [
        "Borrower C",
        "(Proprietor : Borrower S)",
        "Balance Sheet as on 31st March, 2024",
    ]
    assert F._find_entity(lines, 2) == "Borrower C"


def test_find_entity_falls_back_to_proprietor_line_when_nothing_else():
    lines = [
        "(Proprietor : Borrower S)",
        "Balance Sheet as on 31st March, 2024",
    ]
    assert F._find_entity(lines, 1) == "(Proprietor : Borrower S)"


def test_parse_line_rejects_a_cell_of_only_concatenated_amounts():
    """
    OCR can merge two visually-close printed rows into one detected cell,
    concatenating their amounts with no label text between them at all
    ("4,41,330 6,38,687 3,29,53,179" - Cash in Hand, Cash at Bank, and their
    group's own subtotal, all read as one line). _AMOUNT_RE anchors to the
    END of the cell, so without this guard the leading digits become a
    nonsense numeric "label" paired with the LAST (usually largest, often a
    subtotal) figure - worse than dropping the cell outright.
    """
    assert F.parse_line("4,41,330 6,38,687 3,29,53,179") is None
    # A genuine label that happens to end in digits must still parse.
    assert F.parse_line("Cash at Bank 6,38,687") == ("Cash at Bank", 638687)


def test_capital_and_assets_caption_line_is_not_a_title():
    """
    A bank-format Balance Sheet's own two column captions ('CAPITAL ACCOUNT:'
    and 'Fixed Assets:') can be OCR'd onto one joined line with no amount of
    its own. _match_title's capital-a/c pattern otherwise treats this as a
    FRESH statement heading, splitting the real Balance Sheet into an empty
    phantom block (dropped for having no items) and a second block that
    opens at the WRONG line - losing the real heading's period (so the
    column gets no year) and pushing the real entity name out of
    _find_entity's lookback window. Confirmed on a real filing (FC6,
    Borrower C AY 2024-25): this is why that year's Balance
    Sheet never verified even though every figure, including its own
    printed TOTAL, was read correctly.
    """
    assert not F._match_title("CAPITAL ACCOUNT: Fixed Assets:")
    # A genuine standalone capital-account heading must still open a block.
    assert F._match_title("CAPITAL ACCOUNT OF A PERSON")


# ── Orientation fallback: don't prefer a vertical reading over a working
#    T-account one when the vertical guess found nothing checkable ─────

def test_prefers_t_account_when_vertical_found_nothing_checkable():
    """
    Confirmed on a real filing (FC6, Borrower C AY 2024-25):
    a genuine two-column T-account Balance Sheet, with no period-column
    header to settle orientation outright, fell through to the weaker
    label-position heuristic and was misread as vertical. The vertical
    harvester then concatenated text from BOTH sides of the account into
    ONE meaningless merged section with no printed total to check - while a
    T-account reading of the SAME rows had real figures on both sides and
    was off by only one duplicated subtotal. Preferring the T-account
    reading here is strictly better: a vertical guess with nothing
    checkable at all can never beat one that at least harvested plausible,
    checkable data.
    """
    t_best = {"left": [("Capital", 100000)], "right": [("Cash", 100000)]}
    vert = {"sections": [{"name": "TOTAL TOTAL",
                          "items": [("Sundry Creditors (As per Schedule E)", 16644481)],
                          "total": 111307366}]}
    assert F._prefer_t_account_over_empty_vertical(t_best, vert)


def test_does_not_override_when_vertical_has_a_checkable_section():
    t_best = {"left": [("Capital", 100000)], "right": [("Cash", 100000)]}
    vert = {"sections": [{"name": "Current Assets",
                          "items": [("a", 1), ("b", 2)], "total": 3}]}
    assert not F._prefer_t_account_over_empty_vertical(t_best, vert)


def test_does_not_override_when_t_account_side_is_empty():
    t_best = {"left": [], "right": [("Cash", 100000)]}
    vert = {"sections": []}
    assert not F._prefer_t_account_over_empty_vertical(t_best, vert)


def test_interest_on_a_loan_is_a_finance_expense_not_the_loan_itself():
    """
    'Interest on Vehicle Loan' is a P&L finance charge, but it CONTAINS
    'vehicle loan' wording that the secured-loan-principal rule also
    matches - and being a balance-sheet rule, it was checked first. On a
    real filing (FC4, Borrower A AY 2023-24) this moved
    Rs 43,24,133 of interest expense into a phantom Secured Loan bucket:
    expenses were understated (so profit was overstated) by exactly that
    amount, and a balance-sheet liability was fabricated that was never on
    the P&L at all. Same class of bug the file's very first SYNONYMS rule
    already documents fixing for 'Interest On FD' vs income.
    """
    assert T.map_label("TO INTEREST ON VEHICLE LOAN") == "interest_finance"
    assert T.map_label("Interest on Term Loan") == "interest_finance"
    # A genuine loan-principal line must still map as a liability.
    assert T.map_label("Secured Loan") == "secured_loan_asset_financed"
    assert T.map_label("Vehicle Loan") == "secured_loan_asset_financed"


def test_find_entity_recognises_prop_abbreviation():
    """
    'PROP.' (not spelled out as 'Proprietor') is the exact wording used on a
    real filing (FC4, Borrower A AY 2023-24) - without
    recognising the abbreviation, the fix for the spelled-out form doesn't
    help this document at all.
    """
    lines = [
        "Borrower A",
        "PROP. the proprietor",
        "Balance Sheet as on 31st Mar 2023",
    ]
    assert F._find_entity(lines, 2) == "Borrower A"


def test_find_entity_not_fooled_by_a_road_in_the_business_name():
    """
    _ADDRESS_RE exists to skip a genuine street-address line between the
    entity name and the heading, but 'road' (and 'nagar', 'complex', ...)
    are common words in a genuine Indian BUSINESS name too - a real filing's
    borrower is literally named 'Borrower A'. A bare business
    name carries none of an address's other tells (a house/shop/plot number,
    a pincode, comma-separated locality parts), so requiring one of those
    alongside the keyword keeps genuine address lines skipped without
    rejecting a business name that merely contains the same word.
    """
    lines = [
        "Borrower A",
        "PROP. the proprietor",
        "Balance Sheet as on 31st Mar 2023",
    ]
    assert F._find_entity(lines, 2) == "Borrower A"
    # A genuine address line must still be skipped.
    lines2 = [
        "Some Business Name",
        "Shop No 4, Sample Address, Near Bus Stand",
        "Balance Sheet as on 31st Mar 2023",
    ]
    assert F._find_entity(lines2, 2) == "Some Business Name"


def test_parse_line_recognises_a_leading_minus_sign_as_negative():
    """
    Accounts almost always print a negative in brackets ('(4,391,651)'), but
    not always: a real filing (FC4, Borrower A AY 2024-25)
    printed its loss as 'TO NET PROFIT -4,391,651' with a plain leading
    minus sign. _is_negative only ever checked for brackets, and the "-"
    sat outside the amount match's own capture group entirely, so the loss
    was silently read as a positive profit of the same magnitude - the same
    failure mode (a lost sign flipping a loss into a profit) the
    Extraordinary Item bug in taxonomy.py already documents, just a
    different way to lose the sign.
    """
    assert F.parse_line("TO NET PROFIT -4,391,651") == ("TO NET PROFIT", -4391651)
    # A hyphen inside the LABEL itself must not be mistaken for a sign.
    assert F.parse_line("Short - Term Borrowings 50,000") == ("Short - Term Borrowings", 50000)


def test_bare_unformatted_amount_is_still_harvested():
    """
    Confirmed on a real filing (FC4, Borrower A AY 2024-25): the
    credit side's own total was printed as a bare '145644604' with none of
    the thousands separators every OTHER figure on the same page used.
    _AMOUNT_RE's comma/decimal/exact-3-digit grammar matches none of that,
    so the entire income figure was silently dropped from the harvest - the
    block still came back "verified" only by accident, via check_block's
    single-column fallback path, because the (correctly-read) expense side
    alone happened to equal the printed total with the income side sitting
    completely empty.
    """
    rows = [rows_with_cells(r, 600)[0] for r in [
        [_w(10, 0, 120, "TO Hiring Charges Paid"), _w(200, 0, 260, "51,336,100"),
         _w(300, 0, 380, "BY Hiring charges received"), _w(500, 0, 560, "145644604")],
    ]]
    sides = F._harvest_at(rows, 290, 145644604)
    assert sides["right"] == [("BY Hiring charges received", 145644604)]


# ── Statement-type coverage, not just STATEMENT-page count ──────────
# Confirmed on a real filing (Borrower G Pvt Ltd, AY
# 2023-24, poor-quality phone scan): a Chartered Accountant's letterhead
# page and a "Reserves and Surplus" Notes/Schedule page each independently
# matched enough of the combined STATEMENT pattern to be counted as one -
# satisfying the old ">= 2 STATEMENT pages found" safety bar - while the
# real Balance Sheet and Profit & Loss sat on OTHER pages that the cheap
# OCR classify pass had misread badly enough to miss entirely. Counting
# STATEMENT pages says nothing about whether we actually found ONE OF EACH
# kind; `has_both_statement_kinds` does.

def test_has_both_statement_kinds_ignores_an_audit_reports_prose_mention():
    """
    A real Independent Auditor's Report reads (nine lines into the page,
    wrapped across two OCR lines): '...which comprise the Balance Sheet as
    at March 31, 2023, the Statement of Profit and Loss for the year ended
    on that date...'. A plain substring search matches both BS and P&L
    wording on this ONE page alone - it is prose ABOUT the statements, not
    either statement's own heading, and a genuine heading is never nine
    lines into a page or 90+ characters long.
    """
    audit_report = (
        "K V B & ASSOCIATES\nCHARTERED ACCOUNTANTS\n9 Office No. 608, Sample Address, "
        "Sample Address, Sample Address, Mumbai - 400088.\n"
        "ca@example.com +91 97692 57012 | +91 89285 01505\n"
        "INDEPENDENT AUDITOR'S REPORT\n"
        "TO THE MEMBERS OF Borrower G\n"
        "Report on the Audit of the financial Statements\nOpinion\n"
        "We have audited the accompanying financial statements of Borrower G &\n"
        "LOGISTICS PRIVATE LIMITED (\"the Company\"), which comprise the Balance Sheet as at March\n"
        "31,2023, the Statement of Profit and Loss for the year ended on that date, and a summary of the\n"
        "significant accounting policies and other explanatory information (hereinafter referred to as \"the\n"
        "Financial Statement\")."
    )
    assert not R.has_both_statement_kinds([audit_report])


def test_has_both_statement_kinds_ignores_a_notes_page_sub_caption():
    """
    A real Notes/Schedule page's own heading is 'Notes forming part of the
    Financial Statements' - but note 4 (Reserves and Surplus) cross-refers
    to 'Statement of Profit and loss' as a short sub-caption, six lines into
    the page. Short enough to pass a length check alone, so this needs the
    near-top-of-page requirement too: the page's OWN heading, not a
    cross-reference buried in its body, is what should count.
    """
    notes_page = (
        "Borrower G\n"
        "(CIN: U63040MH2022PTC392155)\n"
        "Notes forming part of the Financial Statements\n"
        "4Reserves and Surplus\t( in '000)\n"
        "Particulars\t31 March 2023 31 March 2022\n"
        "Statement of Profit and loss\n"
        "Balance at the beginning of the year\n"
        "Add: Profit/(loss) during the year\t(1,062)\n"
        "Balance at the end of the year\t(1,062)\n"
        "Total\t(1,062)\n"
        "5 Long term borrowings\t( in '000)\n"
        "Particulars\t31 March 2023 31 March 2022\n"
        "Secured Term loans from other parties\t3,369\n"
        "Total\t3,369"
    )
    # Paired with a genuine Balance Sheet page elsewhere in the bundle - if
    # the notes page's sub-caption counted as a real P&L heading, this
    # combination would wrongly satisfy "found one of each kind."
    bs_page = "BALANCE SHEET as at 31 March 2023\nShare Capital 1,00,000.00 Total 1,38,00,377.00"
    assert not R.has_both_statement_kinds([bs_page, notes_page])


def test_has_both_statement_kinds_true_when_both_present():
    pages = [
        "BALANCE SHEET as at 31 March 2024\nShare Capital 1,00,000.00 Total 1,38,00,377.00",
        "PROFIT AND LOSS ACCOUNT for the year ended 31 March 2024\nSales 50,00,000.00 Total 50,00,000.00",
    ]
    assert R.has_both_statement_kinds(pages)


def test_has_both_statement_kinds_false_with_only_one_kind():
    pages = ["BALANCE SHEET as at 31 March 2024\nShare Capital 1,00,000.00 Total 1,38,00,377.00"]
    assert not R.has_both_statement_kinds(pages)


def test_classify_page_trusts_a_genuine_heading_over_a_low_money_count():
    """
    Confirmed on a real filing (Borrower G Pvt Ltd, AY
    2023-24, poor phone-scan): the Balance Sheet page's own heading read
    perfectly ("Balance Sheet as at 31 March 2023") while the entire number
    table below it OCR'd to noise, leaving only 1 money-like pattern on the
    whole page - below _MIN_MONEY_HITS. The money gate exists to reject
    PROSE that merely mentions a statement; it should not also reject a
    page whose own genuine heading proves it IS one, just badly OCR'd. A
    low money count on a headed page is evidence of an OCR failure, not
    evidence there's no statement here.
    """
    page = ("Borrower G\n"
           "(CIN: u6so40mHz022PTC392155)\n"
           "(Address: garbled address line)\n"
           "Balance Sheet as at 31 March 2023\n\n"
           "cue ao usomrrs\ned shraaer ue\nshee ta : 1500\n")
    assert R.classify_page(page) == R.STATEMENT


def test_classify_page_still_rejects_low_money_prose_without_a_heading():
    """Regression guard: the money gate must still reject genuine prose -
    only a real, positioned heading overrides it."""
    prose = "This document discusses balance sheet matters in general terms."
    assert R.classify_page(prose) == R.NOISE


def test_vertical_pl_requires_both_revenue_and_expense_totals():
    """
    A vertical (Schedule III) P&L whose harvest captured ONLY an expenses
    section - Revenue never made it into any section at all, most likely
    because OCR failed on that part of the page - must not be reported
    VERIFIED just because the one section it DID find balances against
    itself. Confirmed on a real filing (Borrower G,
    poor phone-scan): a badly-scanned P&L page harvested exactly one
    section ("Total expenses", items summing to its own printed total),
    and the missing Revenue side silently zeroed the whole year's income
    figure downstream - the block still came back VERIFIED.
    """
    sections = [{"name": "Total expenses",
                "items": [("[EXP] Employee Benefit Expenses", 875000),
                         ("[EXP] Other Expenses", 257000)],
                "total": 1132000}]
    chk = F.check_block({"sides": {"sections": sections}, "kind": F.PROFIT_LOSS})
    assert not chk["balanced"]


def test_vertical_pl_verifies_when_both_sides_present():
    sections = [
        {"name": "Total Revenue", "items": [("[INC] Sales", 2000000)], "total": 2000000},
        {"name": "Total expenses",
         "items": [("[EXP] Employee Benefit Expenses", 875000),
                  ("[EXP] Other Expenses", 257000)],
         "total": 1132000},
    ]
    chk = F.check_block({"sides": {"sections": sections}, "kind": F.PROFIT_LOSS})
    assert chk["balanced"], chk["reason"]


# ── Comparative-column recovery ──────────────────────────────────────
# A Schedule III statement prints last year's figures right next to this
# year's, as its own comparative column - a free second reading of the prior
# year that the harvester used to throw away outright. Confirmed valuable on
# a real filing (Borrower G): AY2023-24's own Balance
# Sheet was too badly scanned to verify, but AY2024-25's statement printed
# AY2023-24's grand totals as its comparative column, matching a manual
# read of the original page almost to the rupee. Scoped to GRAND TOTALS
# only (Total Revenue/Income, Total Expenses, Total Assets, Total Equity
# and Liabilities) - a real filing showed a new auditor reclassifying prior
# year borrowings between long-term and short-term while the totals held,
# so individual line items are not safe to recover this way, only anchors.

def test_harvest_vertical_captures_comparative_grand_totals():
    rows = [rows_with_cells(r, 900)[0] for r in [
        [_w(10, 0, 150, "Particulars"), _w(300, 0, 400, "31 March 2024"),
         _w(500, 0, 600, "31 March 2023")],
        [_w(10, 20, 150, "Revenue from Operations"),
         _w(300, 20, 400, "9,91,858"), _w(500, 20, 600, "62,000")],
        [_w(10, 40, 150, "Total Revenue"),
         _w(300, 40, 400, "9,91,858"), _w(500, 40, 600, "62,000")],
    ]]
    result = F._harvest_vertical(rows)
    assert result["comparative"].get("total_revenue") == 62000


def test_harvest_vertical_comparative_empty_without_a_second_column():
    """A single-period statement (no comparative column at all) must not
    error or fabricate a comparative entry."""
    rows = [rows_with_cells(r, 900)[0] for r in [
        [_w(10, 0, 150, "Particulars"), _w(300, 0, 400, "31 March 2024")],
        [_w(10, 20, 150, "Total Revenue"), _w(300, 20, 400, "9,91,858")],
    ]]
    result = F._harvest_vertical(rows)
    assert result["comparative"] == {}
