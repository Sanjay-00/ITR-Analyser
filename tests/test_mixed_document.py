"""
A "digital" ITR bundle can still carry pages with NOTHING for PyMuPDF's text
layer to return - the CA's signed financial statements were photocopied and
scanned into a physical page, then inserted as a raster image into an
otherwise digitally-generated ITR PDF. The document-wide scanned/digital
decision only ever asks "is there enough text SOMEWHERE in this document" -
it says nothing about which pages have it.

Confirmed on a real filing: 13 of a 20-page bundle - including the entire
Balance Sheet - were pure images, while the tax-computation pages alone
carried enough digital text to route the WHOLE document past OCR entirely.
The tool concluded "NIL return" on a company with real financials it had
never actually looked at.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import parser as P                                      # noqa: E402
from engine.ingest import ocr_extractor as O                        # noqa: E402


def test_extract_ocrs_only_the_blank_pages_in_a_mostly_digital_document(monkeypatch):
    digital = {
        0: ("Computation sheet with plenty of digital text here " * 5, [("row0",)]),
        1: ("", []),   # the scanned Balance Sheet - no digital text at all
        2: ("More digital text carries on for this page too " * 5, [("row2",)]),
    }
    monkeypatch.setattr(P, "_digital_page", lambda page: digital[page])

    calls = {}

    def fake_ocr_pages(doc, indices, on_progress=None):
        calls["indices"] = list(indices)
        return {1: ("Balance Sheet as at 31 March 2025 ...", [("ocr_row",)], 0.95)}

    monkeypatch.setattr(O, "ocr_pages", fake_ocr_pages)

    text, scanned, page_texts, page_rows, page_conf = P._extract([0, 1, 2])

    assert calls["indices"] == [1]
    assert "Balance Sheet" in page_texts[1]
    assert page_rows[1] == [("ocr_row",)]
    assert page_conf[1] == 0.95
    # Untouched digital pages keep full confidence and their own text/rows.
    assert page_conf[0] == 1.0 and page_conf[2] == 1.0
    assert page_rows[0] == [("row0",)]
    assert scanned is True


def test_extract_skips_ocr_entirely_when_no_page_is_blank(monkeypatch):
    """Regression guard: a genuinely all-digital document must not pay for
    OCR at all."""
    digital = {
        0: ("Plenty of digital text on this page " * 5, [("row0",)]),
        1: ("Plenty of digital text on this page too " * 5, [("row1",)]),
    }
    monkeypatch.setattr(P, "_digital_page", lambda page: digital[page])

    def fail_ocr_pages(doc, indices, on_progress=None):
        raise AssertionError("ocr_pages should not be called")

    monkeypatch.setattr(O, "ocr_pages", fail_ocr_pages)

    text, scanned, page_texts, page_rows, page_conf = P._extract([0, 1])

    assert scanned is False
    assert page_conf == [1.0, 1.0]
