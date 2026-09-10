"""
Vision escalation safety: a re-read must agree with the ORIGINAL OCR attempt,
not just with itself.

Confirmed on a real filing (FC6 / Borrower C, AY 2023-24):
Gemini Vision re-read a P&L page whose OCR harvest had failed by one dropped
line (revenue correct at Rs 14,22,91,953, expenses short by exactly one
row), and came back with a DIFFERENT, internally-balancing but wildly wrong
statement - revenue Rs 15,84,68,556, expenses Rs 29,50,92,692, implying a
Rs -13.66 crore loss against the Rs 48,23,981 profit the page actually
prints. It was accepted as VERIFIED because check_block only requires a
block's own two sides to agree with each other - which a single hallucinated
JSON response trivially does, since Vision generates both sides in the same
response. That is not independent evidence of correctness.

The fix: a Vision re-read is compared against whichever side of the
ORIGINAL OCR attempt is LARGER (a dropped row can only shrink a sum, never
inflate one, so the larger side is the one least likely to already be
wrong). A re-read landing far outside that reference is rejected even though
it reconciles with itself, and the original (honestly-labelled FAILED /
UNVERIFIED) block is kept instead of a confidently wrong number.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import columns as CL                                    # noqa: E402
from engine.extract import financials as F                          # noqa: E402


# ── Pure function: _vision_disagrees_with_ocr ──────────────────────

def test_flags_the_real_hallucination_case():
    original = {"left_total": 121_551_652, "right_total": 142_291_953}
    vision   = {"left_total": 295_092_692, "right_total": 158_468_556}
    assert CL._vision_disagrees_with_ocr(original, vision)


def test_agrees_within_tolerance_is_not_flagged():
    original = {"left_total": 100_000, "right_total": 100_000}
    vision   = {"left_total": 108_000, "right_total": 108_000}
    assert not CL._vision_disagrees_with_ocr(original, vision)


def test_no_ocr_baseline_means_nothing_to_disagree_with():
    """A block that harvested nothing at all (both sides zero) gives Vision
    no reference to be checked against - trust it as before, don't invent a
    false rejection."""
    original = {"left_total": 0, "right_total": 0}
    vision   = {"left_total": 500_000, "right_total": 500_000}
    assert not CL._vision_disagrees_with_ocr(original, vision)


# ── Integration: _escalate rejects a self-consistent-but-wrong re-read ──

def _failed_pl_block(page=0):
    return {
        "kind": F.PROFIT_LOSS, "title": "PROFIT & LOSS ACCOUNT",
        "entity": "Borrower C", "year": 2024, "period": "",
        "page": page,
        "sides": {"left": [("[EXP] Diesel", 121_551_652)],
                 "right": [("[INC] Transportation charges received", 142_291_953)]},
        "printed_total": 142_291_953,
        "check": {"balanced": False, "left_total": 121_551_652,
                 "right_total": 142_291_953, "printed_total": 142_291_953,
                 "shortfall": 20_740_301,
                 "reason": "left side sums to 121,551,652 against 142,291,953 "
                          "on the other side (short by 20,740,301)"},
        "status": F.FAILED,
    }


def _hallucinated_vision_obj():
    """Shaped exactly like the real bad Gemini response: internally
    balanced, nowhere near the real figures."""
    return {
        "kind": "profit_loss", "entity": "Borrower C",
        "year": 2024, "printed_total": 295_092_692,
        "left": [["Various expenses", 295_092_692, ""]],
        "right": [["Revenue", 158_468_556, ""],
                 ["Other income", 136_624_136, ""]],
    }


def test_escalate_rejects_a_hallucinated_but_self_balancing_reread():
    blocks = [_failed_pl_block(page=6)]

    def fake_vision_fn(source, page, api_key):
        return _hallucinated_vision_obj()

    out, sent = CL._escalate(blocks, source="doc", api_key="key",
                             vision_fn=fake_vision_fn,
                             main_entity="Borrower C",
                             statement_pages={6})

    assert sent == [6]
    assert len(out) == 1
    # The ORIGINAL block is kept - not silently replaced by the wrong one.
    assert out[0]["status"] == F.FAILED
    assert out[0]["sides"]["right"][0][1] == 142_291_953
    assert "disagrees" in out[0]["check"]["reason"]


def test_escalate_still_accepts_a_reread_that_agrees_with_ocr():
    """Regression guard: a genuine correction (Vision fixing a real OCR
    misread, landing close to what OCR already had) must still go through."""
    blocks = [_failed_pl_block(page=6)]

    def fake_vision_fn(source, page, api_key):
        return {
            "kind": "profit_loss", "entity": "Borrower C",
            "year": 2024, "printed_total": 142_291_953,
            "left": [["Diesel and Petrol Charges", 44_195_706, ""],
                    ["Driver Charges", 20_740_302, ""],
                    ["Other expenses", 72_531_964, ""],
                    ["Net Profit", 4_823_981, ""]],
            "right": [["Transportation charges received", 142_291_953, ""]],
        }

    out, sent = CL._escalate(blocks, source="doc", api_key="key",
                             vision_fn=fake_vision_fn,
                             main_entity="Borrower C",
                             statement_pages={6})

    assert sent == [6]
    assert len(out) == 1
    assert out[0]["status"] == F.VERIFIED
    assert out[0].get("via_vision") is True


# ── Comparative-column recovery across years ────────────────────────
# A column whose own statement failed can sometimes be rescued from the
# FOLLOWING year's own comparative column - confirmed valuable on a real
# filing (Borrower G): AY2023-24's Balance Sheet and
# P&L were too badly scanned to verify on their own, but AY2024-25's
# statements printed AY2023-24's grand totals as their comparative column.
# Scoped to grand totals only (Total Revenue, Total Expenses, Total
# Assets) - never individual line items, which a real filing showed a new
# auditor legitimately reclassifying between filings while the totals held.

def _bare_column(year, entity="Acme Pvt Ltd", values=None, comparative=None):
    return {
        "year": year, "entity": entity, "identity": {}, "scanned": True,
        "pages_total": 1, "pages_used": 1, "page_summary": {},
        "blocks": [], "blocks_used": [], "values": values or {}, "ratios": {},
        "vision_pages": [], "unmapped": [], "ignored": [],
        "assumptions": [], "mapping_check": {"ok": True, "diff": 0},
        "comparative": comparative or {}, "source_name": f"{year}.pdf",
        "warnings": [],
    }


def test_recovers_pl_totals_from_next_years_comparative_column():
    cols = [
        _bare_column(2023, values={"sales_other_income": None, "gross_expenses": None,
                                   "profit_before_tax": None}),
        _bare_column(2024, values={"sales_other_income": 99534864},
                    comparative={F.PROFIT_LOSS: {"year": 2023,
                                                 "totals": {"total_revenue": 70003,
                                                           "total_expenses": 1132360}}}),
    ]
    CL._recover_from_comparative(cols)
    v = cols[0]["values"]
    assert v["sales_other_income"] == 70003
    assert v["gross_expenses"] == 1132360
    assert v["profit_before_tax"] == 70003 - 1132360
    assert cols[0]["assumptions"]
    assert cols[0]["warnings"]


def test_recovers_total_assets_from_next_years_comparative_column():
    cols = [
        _bare_column(2023, values={"total_assets": None}),
        _bare_column(2024, values={"total_assets": 114340054},
                    comparative={F.BALANCE_SHEET: {"year": 2023,
                                                    "totals": {"total_assets": 19880660}}}),
    ]
    CL._recover_from_comparative(cols)
    assert cols[0]["values"]["total_assets"] == 19880660


def test_does_not_recover_across_different_entities():
    """A comparative column from a DIFFERENT company's filing must never be
    used, even if the years happen to line up."""
    cols = [
        _bare_column(2023, entity="Acme Pvt Ltd", values={"sales_other_income": None}),
        _bare_column(2024, entity="Widgets Pvt Ltd", values={"sales_other_income": 1},
                    comparative={F.PROFIT_LOSS: {"year": 2023,
                                                 "totals": {"total_revenue": 70003,
                                                           "total_expenses": 1132360}}}),
    ]
    CL._recover_from_comparative(cols)
    assert cols[0]["values"]["sales_other_income"] is None


def test_does_not_overwrite_a_year_that_already_has_real_figures():
    cols = [
        _bare_column(2023, values={"sales_other_income": 555}),
        _bare_column(2024, values={"sales_other_income": 99534864},
                    comparative={F.PROFIT_LOSS: {"year": 2023,
                                                 "totals": {"total_revenue": 70003,
                                                           "total_expenses": 1132360}}}),
    ]
    CL._recover_from_comparative(cols)
    assert cols[0]["values"]["sales_other_income"] == 555
