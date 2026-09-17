"""
The reading cache (engine/cache.py): a PDF already read - same bytes, same
reading code - is never OCR'd again, and a page Gemini already read is never
paid for twice. Pure functions over a temp directory; always run.
"""

import io
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import cache as CACHE                                    # noqa: E402
from engine import parser as P                                       # noqa: E402

import fitz                                                          # noqa: E402


def _pdf(text: str) -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


class _Upload(io.BytesIO):
    def __init__(self, data, name):
        super().__init__(data)
        self.name = name


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(CACHE, "CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("ITR_CACHE", "1")
    CACHE.reset_reused()
    return tmp_path


@pytest.fixture
def counted_extract(monkeypatch):
    calls = []

    def fake(doc, on_progress=None):
        calls.append(doc.page_count)
        return ("text", True, ["text"], [[]], [1.0])
    monkeypatch.setattr(P, "_extract", fake)
    return calls


def test_same_file_is_read_once_even_renamed(tmp_cache, counted_extract):
    data = _pdf("Balance Sheet")
    first = P._extract_source(_Upload(data, "ITR 2024.pdf"))
    again = P._extract_source(_Upload(data, "renamed copy.pdf"))
    assert counted_extract == [1]                  # OCR ran once
    assert again == first
    assert CACHE.reused() == ["renamed copy.pdf"]


def test_different_file_is_read(tmp_cache, counted_extract):
    P._extract_source(_Upload(_pdf("one"), "a.pdf"))
    P._extract_source(_Upload(_pdf("two"), "b.pdf"))
    assert len(counted_extract) == 2 and CACHE.reused() == []


def test_changed_reading_code_retires_old_entries(tmp_cache, counted_extract, monkeypatch):
    data = _pdf("x")
    P._extract_source(_Upload(data, "a.pdf"))
    monkeypatch.setattr(CACHE, "_VERSION", "newcode00000")
    P._extract_source(_Upload(data, "a.pdf"))
    assert len(counted_extract) == 2
    CACHE.prune()
    assert all("newcode00000" in n for n in os.listdir(tmp_cache))


def test_cache_off(tmp_cache, counted_extract, monkeypatch):
    monkeypatch.setenv("ITR_CACHE", "0")
    data = _pdf("x")
    P._extract_source(_Upload(data, "a.pdf"))
    P._extract_source(_Upload(data, "a.pdf"))
    assert len(counted_extract) == 2 and not os.listdir(tmp_cache)


def test_gemini_page_answer_cached_only_when_usable(tmp_cache, monkeypatch):
    calls = []

    def fake_vision(doc, page, key, invoke):
        calls.append(page)
        return {} if page == 1 else {"kind": "balance_sheet"}
    monkeypatch.setattr(P.ocr_extractor, "vision_read_page", fake_vision)
    data = _pdf("x")
    for _ in range(2):
        assert P.vision_read(_Upload(data, "a.pdf"), 0, "key") == {"kind": "balance_sheet"}
        P.vision_read(_Upload(data, "a.pdf"), 1, "key")
    assert calls == [0, 1, 1]      # page 0 paid once; the failed page 1 retried


def test_prune_expires_old_and_caps_size(tmp_cache, monkeypatch):
    CACHE.put("ocr", "old", "x" * 10)
    CACHE.put("ocr", "new", "y" * 10)
    old = CACHE._path("ocr", "old")
    past = time.time() - (CACHE.MAX_AGE_DAYS + 1) * 86400
    os.utime(old, (past, past))
    assert CACHE.prune() == 1 and CACHE.get("ocr", "old") is None
    monkeypatch.setattr(CACHE, "MAX_BYTES", 0)
    CACHE.prune()
    assert CACHE.get("ocr", "new") is None


def test_clear_and_stats(tmp_cache):
    CACHE.put("ocr", "a", 1)
    CACHE.put("vision", "a-p0", 2)
    assert CACHE.stats()["files"] == 1 and CACHE.stats()["pages"] == 1
    assert CACHE.clear() == 2 and CACHE.stats()["bytes"] == 0
