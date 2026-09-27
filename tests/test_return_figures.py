"""Figures read off the ITR-V acknowledgement, and a file holding only the
acknowledgement never being called a NIL return."""
from engine.columns import _looks_nil, _warnings
from engine.extract.itr_parser import extract_return_figures

# Text as a digital acknowledgement extracts: label, row number, value.
ACK = """INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT
Current Year business loss, if any
1
48,25,888
Total Income
1A
0
Book Profit under MAT, where applicable
2
0
Adjusted Total Income under AMT, where applicable
3
0
Net tax payable
4
0
Taxes Paid
7
3,19,862
(+) Tax Payable /(-) Refundable (6-7)
8
(-) 3,19,860
"""

OLD_ACK = "ITR-V INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT Gross Total Income 1 6,20,000 Deductions under Chapter-VI-A 2 1,50,000 Total Income 3 4,70,000 Taxes Paid 7 12,000"


def test_reads_the_acknowledgement_table():
    figs = extract_return_figures(["computation page", ACK])
    assert figs == {
        "current_year_business_loss": 4825888,
        "total_income": 0,
        "net_tax_payable": 0,
        "taxes_paid": 319862,
        "tax_payable_or_refund": -319860,
    }


def test_total_income_is_not_gross_total_income():
    figs = extract_return_figures([OLD_ACK])
    assert figs["gross_total_income"] == 620000
    assert figs["total_income"] == 470000


def test_no_acknowledgement_no_figures():
    assert extract_return_figures(["Balance Sheet as at 31 March 2025"]) == {}


def test_acknowledgement_only_file_is_not_nil():
    assert _looks_nil([]) is False
    out = _warnings([], [], [], {"ok": True, "diff": 0}, return_figures={"total_income": 0})
    assert "acknowledgement and computation of income only" in out[0]
    assert not any("NIL" in w for w in out)
