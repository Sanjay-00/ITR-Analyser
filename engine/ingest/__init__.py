"""
engine.ingest  -  turn a PDF's pages into structured text a parser can read.

    layout          word-box geometry -> rows with column boundaries, shared
                     by the OCR and digital-text paths
    ocr_extractor    Tesseract OCR for scanned pages, and the Gemini Vision
                     fallback for a single page's image
    relevance        classifies pages (statement / schedule / noise / ...) so
                     only the ones worth reading get OCR'd or sent to the model
"""
