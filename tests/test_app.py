"""
The Streamlit app itself, driven headless (streamlit.testing).

Pins the wiring that unit tests cannot see: that the page renders at all,
that the "Re-run with Gemini" button works, and that the workbook is named
after the borrower. Both were real bugs (2026-09-16).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamlit.testing.v1 import AppTest                             # noqa: E402

from engine import borrower_name, get_filename                       # noqa: E402
from test_master_excel import _column, BUCKETS                       # noqa: E402

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def _column_needing_vision():
    col = _column(2024, BUCKETS)
    col["entity"] = "Borrower D"
    col["blocks"] = [{"status": "failed", "kind": "balance_sheet", "page": 4,
                      "source": "ITR.pdf", "check": {"reason": "sides differ"},
                      "sides": {"left": [("Capital", 100)], "right": []}}]
    col["blocks_used"] = [{"status": "verified", "kind": "profit_loss"}]
    return col


def _run(columns=None, api_key=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("GEMINI_API_KEY", api_key or "")
    at = AppTest.from_file(APP, default_timeout=90)
    if columns is not None:
        at.session_state["columns"] = columns
    return at.run()


def test_empty_page_renders(monkeypatch):
    at = _run(monkeypatch=monkeypatch)
    assert not at.exception


def test_results_page_renders(monkeypatch):
    at = _run([_column(2024, BUCKETS)], monkeypatch=monkeypatch)
    assert not at.exception
    assert [t.label for t in at.tabs][:2] == ["Spreading sheet", "Analysis"]


def test_rerun_with_gemini_turns_vision_on_without_erroring(monkeypatch):
    """
    Streamlit refuses to write a widget's key after that widget exists, so
    setting st.session_state["use_vision"] from the button raised
    StreamlitAPIException and the page died. The button sets a plain flag
    instead, consumed before the toggle is created.
    """
    at = _run([_column_needing_vision()], api_key="test-key", monkeypatch=monkeypatch)
    button = [b for b in at.button if "Gemini" in b.label]
    assert button, "no 'Re-run with Gemini' button offered for a failed statement"
    at = button[0].click().run()
    assert not at.exception
    assert at.session_state["use_vision"] is True


def test_no_gemini_offer_when_everything_reconciled(monkeypatch):
    at = _run([_column(2024, BUCKETS)], api_key="test-key", monkeypatch=monkeypatch)
    assert not [b for b in at.button if "Gemini" in b.label]


def test_workbook_is_named_after_the_borrower():
    col = _column(2024, BUCKETS)
    col["entity"] = "Borrower D"
    name = get_filename(borrower_name([col]), [col])
    assert name.startswith("sample_d_LOGISTICS_PRIVATE_LIMITED")
    assert name.endswith(".xlsx")


def test_an_address_never_names_the_workbook():
    """A column whose entity was rejected as a name must not name the file."""
    col = _column(2024, BUCKETS)
    col["entity"] = ("6-A, H & G House, Sector 11, C B D Belapur, Navi Mumbai, "
                     "Maharashtra, India, 400614")
    col["blocks_used"] = []
    assert borrower_name([col]) == "Borrower"
