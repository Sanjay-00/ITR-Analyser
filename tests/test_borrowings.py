"""What a generic "Long Term Borrowings" line is, read from the notes
(engine/extract/borrowings.py). Synthetic text only."""

from engine.extract.borrowings import GENERIC_LT_BORROWINGS, owner_loan_lines, owners_loan_note

PAGES = [
    "Balance Sheet\nLong Term Borrowings 5 52,919 80,295",
    "Note 5 Long term borrowings\nLoans from directors & relatives (Unsecured) 5291900] ___#0,294:5|",
    "The company has not granted any loans, secured or unsecured to companies",
]


def test_only_lines_naming_unsecured_loans_from_owners_are_kept():
    lines = owner_loan_lines(PAGES)
    assert [page for page, _ in lines] == [2]


def test_the_note_must_print_the_same_amount():
    lines = owner_loan_lines(PAGES)
    assert owners_loan_note(lines, 52919000) == (2, lines[0][1])  # printed in thousands, OCR ran on
    assert owners_loan_note(lines, 12345000) is None  # the wording alone moves nothing


def test_a_different_amount_sharing_leading_digits_does_not_confirm():
    lines = [(3, "Unsecured loans from directors 1,20,050")]
    assert owners_loan_note(lines, 1200000) is None  # 1,200 thousand is not 1,20,050
    assert owners_loan_note([(3, "Unsecured loans from directors 12,00,000.00")], 1200000) == (3, "Unsecured loans from directors 12,00,000.00")


def test_the_face_line_must_be_the_generic_one():
    assert GENERIC_LT_BORROWINGS.match("Long Term Borrowings")
    assert GENERIC_LT_BORROWINGS.match("Long-term borrowing")
    assert not GENERIC_LT_BORROWINGS.match("Term loan from bank (secured)")


def test_an_untagged_tax_line_under_a_reconciled_pl_is_the_tax():
    from engine.columns import _untagged_tax_lines
    from engine.extract import financials as F

    block = {"kind": F.PROFIT_LOSS, "page": 28, "sides": {"sections": [{"name": "Total", "items": [
        ["[INC] Revenue from Operations", 263263000], ["[EXP] Other Expenses", 33557000],
        ["Current Year", 2111000], ["Deferred Tax", 50000], ["Current Year", 999999999]]}]}}
    found = _untagged_tax_lines([block], profit_before_tax=10066000)
    assert found == [("provision_tax", "Current Year", 2111000, 29), ("provision_deferred_tax", "Deferred Tax", 50000, 29)]
    assert _untagged_tax_lines([block], profit_before_tax=-5) == []


def test_only_a_company_llp_or_firm_is_expected_to_show_its_tax():
    from engine.columns import _taxed_entity

    assert _taxed_entity({"assessee_status": "Company"}, "")
    assert not _taxed_entity({"assessee_status": "Individual"}, "Acme Traders Pvt Ltd")
    assert _taxed_entity({}, "Northwind Freight Carriers LLP")
    assert not _taxed_entity({}, "Arun Kumar Mehta")
