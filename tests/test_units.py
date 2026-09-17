"""
Pure-function unit tests. No sample ITRs needed - these always run.

    pytest tests/test_units.py

Each test here pins a behaviour that a real filing broke at least once. The
comments say which, because the failure modes are not obvious from the code.
"""

import os
import re
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


# ── A genuine two-digit amount in the period column ──────────────
#    Borrower D FY2025 (docs/SHORTCOMINGS.md Case 28): the P&L
#    prints "(f) Finance costs | 30 | 63 | 1,25,03,233" - a real Rs 63
#    finance cost beside its Note No. 30. A bare 1-2 digit figure was
#    unreadable by design (a guard against harvesting note numbers), so
#    the row carried no amount, was taken for a heading and dropped; the
#    expense section then came up Rs 62 short of its printed total and
#    the whole P&L failed - taking Revenue, Purchases, Employee Costs and
#    every P&L row with it.
#
#    Figures are RIGHT-ALIGNED under their column: the real amount's right
#    edge sits with the other amounts of its period, the note number's does
#    not. That is what tells them apart - not the digit count.

def _sample_d_rows():
    """The sample_d layout: label | note | this year (right-aligned at 560) |
    last year (right-aligned at 760)."""
    return [rows_with_cells(r, 900)[0] for r in [
        [_w(10, 0, 200, "Particulars"), _w(300, 0, 340, "Note"),
         _w(400, 0, 520, "For Year Ended 31.03.2025"),
         _w(620, 0, 740, "31.03.2024")],
        [_w(10, 20, 200, "(a) Cost of materials consumed"), _w(300, 20, 330, "24")],
        [_w(10, 40, 200, "(b) Purchases of Stock In Trade"), _w(300, 40, 330, "25"),
         _w(430, 40, 560, "8,84,09,944"), _w(630, 40, 760, "13,70,17,989")],
        [_w(10, 60, 200, "(f) Finance costs"), _w(300, 60, 330, "30"),
         _w(540, 60, 560, "63"), _w(640, 60, 760, "1,25,03,233")],
        [_w(10, 80, 200, "Total expenses"),
         _w(430, 80, 560, "8,84,10,007"), _w(630, 80, 760, "14,95,21,222")],
    ]]


def test_two_digit_amount_in_the_period_column_is_read():
    sides = F._harvest_vertical(_sample_d_rows())
    items = {l: a for s in sides["sections"] for l, a in s["items"]}
    assert items.get("(f) Finance costs") == 63
    assert items.get("(b) Purchases of Stock In Trade") == 88409944
    section = sides["sections"][0]
    assert sum(a for _l, a in section["items"]) == section["total"] == 88410007


def test_note_number_is_still_never_an_amount():
    """The guard this loosens must still hold: a row whose only figure is
    its Note No. ('Cost of materials consumed | 24 | - | -') contributes
    nothing - reading 24 as Rs 24 is the failure the 3-digit rule exists
    to prevent."""
    sides = F._harvest_vertical(_sample_d_rows())
    labels = [l for s in sides["sections"] for l, _a in s["items"]]
    assert not any("Cost of materials" in l for l in labels)
    assert 24 not in [a for s in sides["sections"] for _l, a in s["items"]]


# ── "Total outstanding dues ..." is a line item, not a section total ──
#    Schedule III spells trade payables out longhand as two lines, each
#    OPENING with the word "total":
#        (a) Trade payables
#            total outstanding dues of micro enterprises ...
#            total outstanding dues of creditors other than micro ...  4,32,24,860
#    Read as a section anchor (Borrower D FY2025), each closed the
#    Current liabilities group: the payables figure became a section total
#    with no items and vanished from the sheet, and the lines BELOW it lost
#    their [CL] tag - so "(c) Current tax liabilities (net)" was mapped as a
#    P&L tax expense. Total of Liabilities came up Rs 499 lakh short of
#    Total of Assets, and PAT was overstated as a loss by Rs 66.75 lakh.

def _payables_rows():
    return [rows_with_cells(r, 900)[0] for r in [
        [_w(10, 0, 200, "Particulars"), _w(300, 0, 340, "Note"),
         _w(400, 0, 520, "For Year Ended 31.03.2025"), _w(620, 0, 740, "31.03.2024")],
        [_w(10, 20, 200, "3 Current liabilities")],
        [_w(10, 40, 200, "(a) Trade payables")],
        [_w(10, 60, 360, "total outstanding dues of micro enterprises and small enterprises"),
         _w(545, 60, 560, "-")],
        [_w(10, 80, 360, "total outstanding dues of creditors other than micro enterprises"),
         _w(430, 80, 560, "4,32,24,860")],
        [_w(10, 100, 200, "(c) Current tax liabilities (net)"), _w(300, 100, 330, "7"),
         _w(440, 100, 560, "66,75,420")],
        [_w(10, 120, 200, "Total current liabilities"), _w(430, 120, 560, "4,99,00,280")],
    ]]


def test_trade_payables_longhand_is_an_item_not_a_section_total():
    sides = F._harvest_vertical(_payables_rows())
    items = {l: a for s in sides["sections"] for l, a in s["items"]}
    payables = [a for l, a in items.items() if "outstanding dues" in l]
    assert payables == [43224860]
    section = next(s for s in sides["sections"] if "current liabilities" in s["name"].lower())
    assert section["total"] == 49900280
    assert sum(a for _l, a in section["items"]) == 49900280


def test_lines_below_trade_payables_keep_their_section_tag():
    """The tag decides the row: untagged, 'Current tax liabilities' reads as
    the P&L's current tax expense instead of a balance-sheet liability."""
    from engine.mapping import taxonomy as T
    sides = F._harvest_vertical(_payables_rows())
    tax = next(l for s in sides["sections"] for l, _a in s["items"]
               if "Current tax liabilities" in l)
    assert tax.startswith("[CL]")
    assert T.map_label(tax, {}) == "current_liabilities"


def test_trade_payables_longhand_is_not_ignored_as_a_subtotal():
    """The mapping layer drops "Total ..." rows as restatements. Schedule
    III's trade-payables wording opens with the same word, so Rs 432.25 lakh
    of creditors was set aside as a subtotal and never reached the sheet -
    Total of Liabilities was short by exactly that (sample_d FY2025). A real
    subtotal must still be ignored."""
    from engine.mapping import taxonomy as T
    payables = "[CL] total outstanding dues of creditors other than micro and small enterprises"
    micro = "[CL] total outstanding dues of micro and small enterprises"
    assert T.map_label(payables, {}) == "sundry_creditors"
    assert T.map_label(micro, {}) == "sundry_creditors"
    assert T.map_label("[CL] Total current liabilities", {}) is T.IGNORE
    assert T.map_label("Total expenses", {}) is T.IGNORE


# ── A two-digit year in the heading ───────────────────────────────
#    "PROVISIONAL PROFIT AND LOSS ACCOUNT FOR THE YEAR ENDED 31.03.26"
#    (Borrower D provisional set). _PERIOD_RE wanted four digits, so
#    the statement had no period: it could not join the Balance Sheet dated
#    31st March 2026 from the other file and became its own undated column -
#    the year showed a P&L with no balance sheet and a balance sheet with no
#    P&L, side by side.

def test_two_digit_year_in_a_heading_is_understood():
    lines = ["PROVISIONAL PROFIT AND LOSS ACCOUNT FOR THE YEAR ENDED 31.03.26"]
    assert F._find_period(lines, 0)[0] == 2026
    assert F._find_period(["Balance Sheet as at 31/03/25"], 0)[0] == 2025
    # Four-digit years and month-name spellings keep working.
    assert F._find_period(["Balance Sheet as at 31st March, 2026"], 0)[0] == 2026
    assert F._find_period(["for the year ended 31.03.2026"], 0)[0] == 2026
    # A bare two-digit number that is not a date must not become a year.
    assert F._find_period(["Total expenses 26"], 0)[0] is None


# ── The comparative column as a year of its own ───────────────────
#    A Schedule III statement prints last year beside this year, line for
#    line. Borrower D' provisional set (FY2026) carries the whole of
#    FY2025 that way, and a case can arrive with no separate FY2025 file at
#    all. The comparative column is emitted as a SECOND statement for the
#    prior year, so it is checked against its own printed totals like any
#    other - and a real statement for that year always wins over it
#    (see columns.spread_many).

def _two_period_rows():
    """sample_d's provisional P&L: FY2026 beside FY2025, line for line."""
    return [rows_with_cells(r, 900)[0] for r in [
        [_w(10, 0, 200, "Particulars"), _w(300, 0, 340, "Note"),
         _w(400, 0, 520, "For Year Ended 31.03.2026"),
         _w(600, 0, 740, "For Year Ended 31.03.2025")],
        [_w(10, 20, 200, "I. Revenue from operations"), _w(300, 20, 330, "22"),
         _w(430, 20, 560, "9,01,43,224"), _w(630, 20, 760, "9,52,43,661")],
        [_w(10, 40, 200, "II. Other income"), _w(300, 40, 330, "23"),
         _w(470, 40, 560, "1,45,306"), _w(680, 40, 760, "50,856")],
        [_w(10, 60, 200, "III. Total income"),
         _w(430, 60, 560, "9,02,88,530"), _w(630, 60, 760, "9,52,94,517")],
        [_w(10, 80, 200, "(b) Purchases of Stock In Trade"), _w(300, 80, 330, "25"),
         _w(430, 80, 560, "6,08,35,520"), _w(630, 80, 760, "8,84,09,944")],
        [_w(10, 100, 200, "(e) Employee benefits expenses"), _w(300, 100, 330, "28"),
         _w(460, 100, 560, "30,55,266"), _w(660, 100, 760, "44,35,992")],
        [_w(10, 120, 200, "Total expenses"),
         _w(430, 120, 560, "6,38,90,786"), _w(630, 120, 760, "9,28,45,936")],
        # Nil this year, a real figure last year - the row carries no
        # current-period amount at all, so it is a heading for FY2026 and a
        # LINE ITEM for FY2025.
        [_w(10, 140, 200, "(b) Deferred tax"), _w(545, 140, 560, "-"),
         _w(650, 140, 760, "(13,69,133)")],
        [_w(10, 160, 200, "Total tax expenses"), _w(545, 160, 560, "-"),
         _w(650, 160, 760, "(13,69,133)")],
    ]]


def test_comparative_column_is_harvested_line_by_line():
    sides = F._harvest_vertical(_two_period_rows())
    comp = sides.get("comparative_sections")
    assert comp, "no comparative sections harvested"
    items = {l: a for s in comp for l, a in s["items"]}
    assert items.get("I. Revenue from operations") == 95243661
    assert items.get("II. Other income") == 50856
    assert items.get("(e) Employee benefits expenses") == 4435992
    assert [s["total"] for s in comp] == [95294517, 92845936, -1369133]
    assert items.get("(b) Deferred tax") == -1369133


def test_comparative_statement_is_emitted_for_the_prior_year():
    rows = _two_period_rows()
    title = rows_with_cells(
        [_w(10, -20, 400, "Statement of Profit and Loss for the year ended 31.03.2026")], 900)[0]
    blocks = F.find_blocks([[title] + rows])
    years = {(b["year"], b.get("from_comparative", False)) for b in blocks}
    assert (2026, False) in years
    assert (2025, True) in years
    prior = next(b for b in blocks if b.get("from_comparative"))
    assert F.status_of(F.check_block(prior)) == F.VERIFIED


# ── The entity is the business, not its address ──────────────────
#    Borrower D' statements head:
#        Borrower D
#        (CIN: U35120MH2008PTC182130 )
#        6-A, H & G House, Sector 11, C B D Belapur, Navi Mumbai, ... 400614
#        Provisional Balance Sheet as at 31st March, 2026
#    _ADDRESS_RE knew "road/nagar/marg/building" but not "House"/"Sector",
#    so the address line was taken for the business and the workbook titled
#    itself after it. An address is recognised by SHAPE - a PIN code, or
#    several comma-separated parts - not only by its keywords.

_sample_d_HEAD = [
    "Borrower D",
    "(CIN: U35120MH2008PTC182130 )",
    "6-A, H & G House, Sector 11, C B D Belapur, Navi Mumbai, Maharashtra, India, 400614",
    "Provisional Balance Sheet as at 31st March, 2026",
]


def test_entity_is_the_business_not_the_address():
    assert F._find_entity(_sample_d_HEAD, 3) == "Borrower D"


def test_address_shapes_are_recognised():
    for addr in (
        "6-A, H & G House, Sector 11, C B D Belapur, Navi Mumbai, Maharashtra, India, 400614",
        "Shop No 4, Sample Address, Wakad, Pune 411057",
        "Plot 12, MIDC Industrial Area, Nashik",
    ):
        assert F._looks_like_address(addr), addr


def test_a_business_name_is_not_mistaken_for_an_address():
    """These are real borrowers' names - two carry address WORDS."""
    for name in ("Borrower A", "M/S. Borrower E",
                 "Borrower D", "Borrower I",
                 "Borrower M"):
        assert not F._looks_like_address(name), name


# ── A statement continued on the next page ───────────────────────
#    Tally prints a long Balance Sheet across pages, bridged by its own
#    running subtotal (A CUSTOMER FY2026):
#        page 3   ... Carried Over 14,98,54,659.78 | Carried Over 6,06,57,088.80
#        page 4   Brought Forward 14,98,54,659.78 | Brought Forward 6,06,57,088.80
#                 Current Assets ... Sundry Debtors 8,35,17,640.61 ...
#                 Total 14,98,54,659.78 | Total 14,98,54,659.78
#    Read a page at a time, the first half could never balance (its assets
#    continue overleaf) and the second half looked like a second statement
#    whose two "Brought Forward" lines - Rs 21.05 crore - fitted no row.
#    Neither is an OCR fault, so Vision cannot fix it either.

def _continued_pages():
    page3 = [rows_with_cells(r, 900)[0] for r in [
        [_w(10, 0, 200, "A CUSTOMER")],
        [_w(10, 20, 200, "Balance Sheet")],
        [_w(10, 40, 200, "1-Apr-25 to 31-Mar-26")],
        [_w(10, 60, 150, "Liabilities"), _w(300, 60, 420, "as at 31-Mar-26"),
         _w(500, 60, 600, "Assets"), _w(700, 60, 820, "as at 31-Mar-26")],
        [_w(10, 80, 200, "Capital Account"), _w(300, 80, 420, "4,90,42,016.76"),
         _w(500, 80, 650, "Fixed Assets"), _w(700, 80, 820, "2,17,97,750.80")],
        [_w(10, 100, 200, "Sundry Creditors"), _w(300, 100, 420, "10,08,12,643.02"),
         _w(500, 100, 650, "Investments"), _w(700, 100, 820, "3,88,59,338.00")],
        [_w(10, 120, 200, "Carried Over"), _w(300, 120, 420, "14,98,54,659.78"),
         _w(500, 120, 650, "Carried Over"), _w(700, 120, 820, "6,06,57,088.80")],
        [_w(10, 140, 200, "continued ...")],
    ]]
    page4 = [rows_with_cells(r, 900)[0] for r in [
        [_w(10, 0, 200, "A CUSTOMER")],
        [_w(10, 20, 300, "Balance Sheet : 1-Apr-25 to 31-Mar-26")],
        [_w(10, 40, 150, "Liabilities"), _w(300, 40, 420, "as at 31-Mar-26"),
         _w(500, 40, 600, "Assets"), _w(700, 40, 820, "as at 31-Mar-26")],
        [_w(10, 60, 200, "Brought Forward"), _w(300, 60, 420, "14,98,54,659.78"),
         _w(500, 60, 650, "Brought Forward"), _w(700, 60, 820, "6,06,57,088.80")],
        [_w(500, 80, 650, "Sundry Debtors"), _w(700, 80, 820, "8,35,17,640.61")],
        [_w(500, 100, 650, "Cash-in-hand"), _w(700, 100, 820, "56,79,930.37")],
        [_w(10, 120, 200, "Total"), _w(300, 120, 420, "14,98,54,659.78"),
         _w(500, 120, 650, "Total"), _w(700, 120, 820, "14,98,54,659.78")],
    ]]
    return [page3, page4]


def test_statement_continued_on_the_next_page_is_one_statement():
    blocks = F.find_blocks(_continued_pages())
    assert len(blocks) == 1, [b["kind"] for b in blocks]
    b = blocks[0]
    b["check"] = F.check_block(b)
    assert F.status_of(b["check"]) == F.VERIFIED, b["check"]["reason"]
    labels = [re.sub(r"^\[[A-Z]+\]\s*", "", l) for l, _a in F.all_items(b["sides"])]
    assert "Sundry Debtors" in labels          # the overleaf half is in
    assert not any("arried" in l or "rought" in l for l in labels)  # bridges are not items
    assert b["printed_total"] == 149854660


def test_pages_are_only_joined_across_a_real_bridge():
    """Two statements that merely follow one another stay separate. A join
    needs evidence of a break: a Carried Over / Brought Forward pair, or
    "continued ..." with the heading reprinted overleaf. With neither, these
    are two statements."""
    pages = _continued_pages()
    pages[0] = [r for r in pages[0]
                if not {"Carried Over", "continued ..."}
                & {" ".join(c[2] for c in r[1]).strip()}
                and "Carried Over" not in " ".join(c[2] for c in r[1])]
    assert len(F.find_blocks(pages)) == 2


def _continued_pl_pages():
    """Tally's P&L runs onto the next page with no Carried Over row at all -
    just "continued ..." at the foot, and the real Total overleaf."""
    page1 = [rows_with_cells(r, 900)[0] for r in [
        [_w(10, 0, 200, "A CUSTOMER")],
        [_w(10, 20, 200, "Profit & Loss A/c")],
        [_w(10, 40, 220, "1-Apr-25 to 31-Mar-26")],
        [_w(10, 60, 150, "Particulars"), _w(300, 60, 420, "1-Apr-25 to 31-Mar-26"),
         _w(500, 60, 640, "Particulars"), _w(700, 60, 820, "1-Apr-25 to 31-Mar-26")],
        [_w(10, 80, 200, "Purchase Accounts"), _w(300, 80, 420, "8,44,58,255.00"),
         _w(500, 80, 650, "Sales Accounts"), _w(700, 80, 820, "18,50,17,094.19")],
        [_w(10, 100, 200, "Direct Expenses"), _w(300, 100, 420, "7,04,27,032.60")],
        [_w(10, 120, 200, "Gross Profit c/o"), _w(300, 120, 420, "3,01,31,806.59")],
        [_w(300, 140, 420, "18,50,17,094.19"), _w(700, 140, 820, "18,50,17,094.19")],
        [_w(10, 160, 200, "Indirect Expenses"), _w(300, 160, 420, "1,24,57,002.67"),
         _w(500, 160, 650, "Gross Profit b/f"), _w(700, 160, 820, "3,01,31,806.59")],
        [_w(10, 180, 200, "Employee Benifit Expenses"), _w(300, 180, 420, "86,97,964.75"),
         _w(500, 180, 650, "Indirect Incomes"), _w(700, 180, 820, "276.00")],
        [_w(10, 200, 200, "Nett Profit"), _w(300, 200, 420, "1,76,75,079.92")],
        [_w(10, 220, 200, "continued ...")],
    ]]
    page2 = [rows_with_cells(r, 900)[0] for r in [
        [_w(10, 0, 200, "A CUSTOMER")],
        [_w(10, 20, 330, "Profit & Loss A/c: 1-Apr-25 to 31-Mar-26")],
        [_w(10, 40, 150, "Particulars"), _w(300, 40, 420, "1-Apr-25 to 31-Mar-26"),
         _w(500, 40, 640, "Particulars"), _w(700, 40, 820, "1-Apr-25 to 31-Mar-26")],
        [_w(10, 60, 200, "Total"), _w(300, 60, 420, "3,01,32,082.59"),
         _w(500, 60, 650, "Total"), _w(700, 60, 820, "3,01,32,082.59")],
    ]]
    return [page1, page2]


def test_continued_page_without_a_carried_over_row_still_joins():
    """"continued ..." at the foot, the same statement's heading repeated
    overleaf: one statement. Read apart, A CUSTOMER' whole Indirect
    Expenses block (Rs 1.24 crore, including Rs 86.98 lakh of employee cost)
    was lost and the year's profit read Rs 292.50 lakh against a printed
    Rs 176.75 lakh."""
    blocks = F.find_blocks(_continued_pl_pages())
    assert len(blocks) == 1, [b["title"] for b in blocks]
    labels = [re.sub(r"^\[[A-Z]+\]\s*", "", l) for l, _a in F.all_items(blocks[0]["sides"])]
    assert "Employee Benifit Expenses" in labels


# ── The section tag outranks the wording ─────────────────────────
#    A line already placed by its statement - [EQ] on the capital side of a
#    balance sheet, [EXP] on the debit side of a P&L - must not be pulled
#    into the other statement by its words alone. Both happened on one real
#    filing (A CUSTOMER FY2026):
#      "[EQ] Interest Paid On Housing Loan"  a movement INSIDE the capital
#          account, read as the P&L's interest: equity short Rs 8.82 lakh and
#          the year's profit reduced by the same.
#      "[EXP] Sales & Commission"  an expense, read as SALES: income
#          overstated Rs 1.45 lakh and the expense lost, Rs 2.90 lakh of
#          phantom profit.
#    Together they put profit Rs 5.92 lakh below the printed Rs 176.75 lakh.

def test_a_balance_sheet_line_is_not_pulled_into_the_pl():
    from engine.mapping import taxonomy as T
    assert T.map_label("[EQ] Interest Paid On Housing Loan", {}) == "equity_capital"
    assert T.map_label("[EQ] Lic of India", {}) == "equity_capital"
    assert T.map_label("[CL] Provision for tax", {}) == "current_liabilities"


def test_an_expense_is_never_income_and_income_never_an_expense():
    from engine.mapping import taxonomy as T
    assert T.map_label("[EXP] Sales & Commission", {}) == "other_expenses"
    assert T.map_label("[EXP] Sales Promotion Expenses", {}) == "other_expenses"
    assert T.map_label("[INC] Transport Movement Charges", {}) == "sales_other_income"


def test_the_guard_leaves_ordinary_lines_alone():
    from engine.mapping import taxonomy as T
    assert T.map_label("[EXP] Interest on loan", {}) == "interest_finance"
    assert T.map_label("[EXP] Diesel & Fuel Expenses", {}) == "transport_admin"
    assert T.map_label("[EXP] Employee Benifit Expenses", {}) == "employee_costs"
    assert T.map_label("[INC] Sales Accounts", {}) == "sales_other_income"
    assert T.map_label("[FA] Motor Vehicle", {}) == "fixed_assets"


def _t_account(left_rows, right_rows):
    """A two-sided Balance Sheet page: liabilities left, assets right."""
    rows = [[_w(10, 0, 160, "Liabilities"), _w(300, 0, 420, "as at 31-Mar-26"),
             _w(500, 0, 640, "Assets"), _w(700, 0, 820, "as at 31-Mar-26")]]
    for i, (l, r) in enumerate(zip(left_rows, right_rows), start=1):
        row = []
        if l:
            row.append(_w(10, i * 20, 200, l[0]))
            if l[1]:
                row.append(_w(300, i * 20, 420, l[1]))
        if r:
            row.append(_w(500, i * 20, 650, r[0]))
            if r[1]:
                row.append(_w(700, i * 20, 820, r[1]))
        rows.append(row)
    return [rows_with_cells(r, 900)[0] for r in rows]


def test_tally_profit_and_loss_group_is_equity_on_the_liabilities_side():
    """
    Tally's liabilities side runs Capital Account, Loans, Current
    Liabilities, Profit & Loss A/c. The last is retained profit; with no tag
    it inherited the Current Liabilities context above it, and Rs 1.76 crore
    of profit was spread as a current liability (A CUSTOMER FY2026).
    The Balance Sheet still balanced - only the tag catches it.
    """
    sides = F._harvest(_t_account(
        [("Capital Account", "4,90,42,016.76"), ("Cap.of a lender", "4,90,42,016.76"),
         ("Current Liabilities", "6,72,91,201.63"), ("Sundry Creditors", "6,72,91,201.63"),
         ("Profit & Loss A/c", "1,76,75,079.92"), ("Current Period", "1,76,75,079.92")],
        [("Fixed Assets", "2,17,97,750.80"), ("Akola Land", "2,17,97,750.80"),
         (None, None), (None, None), (None, None), (None, None)])[1:],
        900, None, None)
    tags = {l: l.split("]")[0] + "]" for l, _a in sides["left"] if l.startswith("[")}
    profit = [t for l, t in tags.items() if "Current Period" in l]
    assert profit == ["[EQ]"], tags


def test_profit_and_loss_on_the_assets_side_is_not_equity():
    """The same group name, printed among the ASSETS, is an accumulated LOSS
    carried as an asset - Borrower A FY2024 (Rs 310.29 lakh),
    which its analyst's sheet keeps in current assets. Read as equity it
    moved Rs 310.29 lakh from the asset side to the liability side and put
    both totals out by that amount."""
    sides = F._harvest(_t_account(
        [("Capital Account", "36,59,100.00"), (None, None), (None, None)],
        [("FIXED ASSETS", "1,00,00,000.00"), ("PROFIT & LOSS", "3,10,29,400.00"),
         ("CASH-IN-HAND", "1,25,000.00")])[1:],
        900, None, None)
    right = dict((l, a) for l, a in sides["right"])
    pl = [l for l in right if "PROFIT" in l.upper()]
    assert pl and not any(l.startswith("[EQ]") for l in pl), right


# ── A statement's year, when its heading will not say ────────────
#    A CUSTOMER AY2025-26: "Profit & Loss A/c as on 31st, March , 2025"
#    - a comma after "31st" defeated the period pattern, so the P&L had no
#    year. Its comparative column is only emitted as the prior year's
#    statement when the year is known, so FY2024's P&L - printed in full,
#    line by line - never reached the sheet, while the Balance Sheet on the
#    next page (dated cleanly) produced its FY2024 column. The column headers
#    ("2025 | 2024") said both years all along.

def test_heading_date_with_stray_punctuation_is_read():
    for line, year in [
            ("Profit & Loss A/c as on 31st, March , 2025", 2025),
            ("Balance Sheet as at 31st March, 2025", 2025),
            ("Balance Sheet as on 31 st , March 2026", 2026),
            ("PROFIT AND LOSS ACCOUNT FOR THE YEAR ENDED 31.03.26", 2026)]:
        assert F._find_period([line], 0)[0] == year, line


def _undated_two_period_pl():
    return [
        rows_with_cells([_w(10, -40, 300, "Profit & Loss Account")], 900)[0],
        rows_with_cells([_w(10, -20, 150, "Particulars"), _w(300, -20, 340, "Note"),
                         _w(480, -20, 520, "2025"), _w(700, -20, 740, "2024")], 900)[0],
    ] + _two_period_rows()[1:]


def test_year_falls_back_to_the_statements_own_column_headers():
    """A heading with no date at all: the column headers still name both
    periods, and the prior one becomes its own statement."""
    blocks = F.find_blocks([_undated_two_period_pl()])
    years = sorted((b["year"], b.get("from_comparative", False)) for b in blocks)
    assert years == [(2024, True), (2025, False)], years


def test_a_period_range_names_the_year_it_ends_in():
    """
    Tally heads its statements with the period as a RANGE:
    "1-Apr-2023 to 31-Mar-2024". That is one period, ending in 2024. The
    column-header fallback first read the range's start year, dating
    Borrower L's FY2024 statements 2023 and his FY2025 ones 2024 - a
    borrower that had matched the analyst's sheet in full then diverged on
    69 rows. Separate cells are still separate columns.
    """
    def rows(*cells_per_row):
        return [rows_with_cells([_w(x0, i * 20, x0 + 150, t) for x0, t in r], 900)[0]
                for i, r in enumerate(cells_per_row)]
    assert F._header_years(rows([(10, "1-Apr-2023 to 31-Mar-2024")])) == [2024]
    assert F._header_years(rows([(10, "1-Apr-25 to 31-Mar-26")])) == [2026]
    assert F._header_years(rows([(400, "2025"), (650, "2024")])) == [2025, 2024]
    assert F._header_years(rows([(400, "As at 31st March, 2025"),
                                 (650, "As at 31st March, 2024")])) == [2025, 2024]
