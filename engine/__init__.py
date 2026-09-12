"""
engine  -  the ITR Extractor's extraction/spreading pipeline.

    PDF  ->  engine.ingest    (text/rows, OCR, page relevance)
         ->  engine.extract   (statement location, arithmetic verification)
         ->  engine.mapping   (label -> template bucket, ratios)
         ->  engine.columns   (orchestrates the three above into year columns)
         ->  engine.excel_generator  (year columns -> the workbook)

engine.columns is deliberately NOT named engine.spread, even though its
public function is parser.spread() - a submodule attribute and a re-exported
function of the same name collide on this package's namespace (see
columns.py's own docstring for what that broke).

app.py talks to this package only through the names re-exported below - it
does not reach into the subpackages directly. That boundary is what lets the
internals move or split further without touching the UI.
"""

from .parser import spread, extract_text, vision_read
from .excel_generator import generate_excel, get_filename, ROWS, SNAP_ROWS, LAKH
from .extract import financials
from .mapping import taxonomy
from . import analysis

__all__ = [
    "spread", "extract_text", "vision_read",
    "generate_excel", "get_filename", "ROWS", "SNAP_ROWS", "LAKH",
    "financials", "taxonomy", "analysis",
]
