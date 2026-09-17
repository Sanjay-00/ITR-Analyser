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
