"""
cache.py  -  never read the same PDF (or ask Gemini about the same page) twice.

OCR is the slow, memory-hungry step: minutes per scanned bundle. Without a
cache, "Re-run with Gemini" re-OCR'd every uploaded file even though only one
page needed Vision, and re-opening a case the next day started from zero.

Entries are keyed by the file's CONTENT (sha256 of its bytes) - not its name,
so a renamed or re-uploaded file still hits - and by a fingerprint of the
reading code itself (`_CODE_FILES`). Change how pages are read and every old
entry is silently ignored: a stale reading can never be served.

What is stored is the reading of real ITRs (PAN, Aadhaar, addresses), so it
lives in data/cache/ (git-ignored), expires after MAX_AGE_DAYS and is capped
at MAX_BYTES. ITR_CACHE=0 turns it off; ITR_CACHE_DIR moves it.
"""

import gzip
import hashlib
import os
import pickle
import time

_ENGINE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_ENGINE)

CACHE_DIR = os.environ.get("ITR_CACHE_DIR") or os.path.join(_ROOT, "data", "cache")
MAX_AGE_DAYS = 30
MAX_BYTES = 500 * 1024 * 1024

# The code that turns a PDF into text and cell geometry. Editing any of it
# changes the fingerprint and so retires every cached reading.
_CODE_FILES = ("parser.py", os.path.join("ingest", "layout.py"),
               os.path.join("ingest", "ocr_extractor.py"))

# Names of the files served from the cache in the current run, for the UI.
_reused: list = []


def enabled() -> bool:
    return os.environ.get("ITR_CACHE", "1") != "0"


def _code_version() -> str:
    h = hashlib.sha256()
    for rel in _CODE_FILES:
        try:
            with open(os.path.join(_ENGINE, rel), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(rel.encode())
    return h.hexdigest()[:12]


_VERSION = _code_version()


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path(kind: str, key: str) -> str:
    return os.path.join(CACHE_DIR, f"{kind}-{_VERSION}-{key}.pkl.gz")


def get(kind: str, key: str):
    """The stored value, or None on a miss (or when caching is off)."""
    if not enabled():
        return None
    path = _path(kind, key)
    try:
        with gzip.open(path, "rb") as f:
            value = pickle.load(f)
        os.utime(path)          # recently used: keep it longest under the cap
        return value
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ValueError):
        return None


def put(kind: str, key: str, value) -> None:
    if not enabled():
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _path(kind, key)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wb") as f:
            pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)   # atomic: a crash never leaves half an entry
        prune()
    except OSError:
        pass                    # a cache that cannot write is just a miss


def _entries() -> list:
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(CACHE_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append((p, st.st_mtime, st.st_size, n))
    return out


def prune(now: float = None) -> int:
    """Drop expired entries, entries from older reading code, then the least
    recently used until under MAX_BYTES. Returns how many were removed."""
    now = time.time() if now is None else now
    removed, keep = 0, []
    for p, mtime, size, name in _entries():
        stale = (now - mtime > MAX_AGE_DAYS * 86400
                 or (name.endswith(".pkl.gz") and f"-{_VERSION}-" not in name)
                 or name.endswith(".tmp") and now - mtime > 3600)
        if stale:
            removed += _remove(p)
        else:
            keep.append((p, mtime, size))
    total = sum(s for _p, _m, s in keep)
    for p, _m, size in sorted(keep, key=lambda e: e[1]):
        if total <= MAX_BYTES:
            break
        removed += _remove(p)
        total -= size
    return removed


def _remove(path: str) -> int:
    try:
        os.remove(path)
        return 1
    except OSError:
        return 0


def clear() -> int:
    """Delete every cached entry. Returns the number removed."""
    return sum(_remove(p) for p, _m, _s, _n in _entries())


def stats() -> dict:
    entries = [e for e in _entries() if e[3].endswith(".pkl.gz")]
    return {"files": sum(1 for e in entries if e[3].startswith("ocr-")),
            "pages": sum(1 for e in entries if e[3].startswith("vision-")),
            "bytes": sum(e[2] for e in entries)}


def reset_reused() -> None:
    _reused.clear()


def mark_reused(name: str) -> None:
    if name and name not in _reused:
        _reused.append(name)


def reused() -> list:
    return list(_reused)
