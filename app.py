"""
ITR Extractor  -  ITR filing bundles → the ITR Validation spreading sheet.

Upload one ITR PDF per financial year. The app finds the financial statements
inside each bundle, verifies them against their own printed totals, maps their
line items onto the analyst's template, and produces the spreading workbook.
"""

import os
try:
    from dotenv import load_dotenv
    # Explicit path, not the bare load_dotenv(). Its default search walks up
    # from the CALLING FILE's directory, so whether .env is found depends on
    # where the process was launched from - it silently found nothing when the
    # app was started from another directory, leaving the key empty and Vision
    # quietly unavailable with no error anywhere.
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import pandas as pd
import streamlit as st

from engine import (
    spread, financials as F, taxonomy as T, analysis as A,
    generate_excel, get_filename, ROWS, SNAP_ROWS, LAKH,
)

st.set_page_config(page_title="ITR Extractor", page_icon="🧾",
                   layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
    [data-testid="stMetricValue"] { font-size: 1.6rem !important; font-weight: 700 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.78rem !important; color: #666; }
    hr { margin: 0.8rem 0 !important; }
    div[data-testid="stStatusWidget"] { border-radius: 0.6rem; }
    .stTabs [data-baseweb="tab-list"] { gap: 4px; }
</style>
""", unsafe_allow_html=True)


def _load_api_key():
    try:
        key = st.secrets.get("GEMINI_API_KEY", "")
        if key and key not in ("", "your_key_here"):
            return key.strip()
    except Exception:
        pass
    key = os.getenv("GEMINI_API_KEY", "").strip()
    return key if key and key not in ("", "your_key_here") else None


def _col_name(col):
    return f"FY ending 31-03-{col['year']}" if col.get("year") \
        else (col.get("source_name") or "Unknown year")


UNREAD = "Check ITR"


def _lakh(v):
    """None means the statement could not be read - never render that as 0."""
    return UNREAD if v is None else round(v / LAKH, 2)


def _fmt_lakh(v):
    return f"{v / LAKH:,.2f}"


def _spread_df(columns):
    """The sheet's own rows, in lakhs, for on-screen review."""
    names = [_col_name(c) for c in columns]
    index, data = [], []
    for kind, key, label in ROWS:
        if kind in ("blank", "section"):
            continue
        index.append(T.LABELS.get(key, key))
        data.append([_lakh(c["values"].get(key)) for c in columns])
    return pd.DataFrame(data, index=index, columns=names)


def _ratio_df(columns):
    names = [_col_name(c) for c in columns]
    keys = list(columns[0]["ratios"].keys()) if columns else []
    data = [[(c.get("ratios") or {}).get(k) for c in columns] for k in keys]
    return pd.DataFrame(data, index=keys, columns=names)


def _signature(uploads, use_vision):
    """Identifies a (files, settings) combination, so a stale result left in
    session_state from a previous run can be told apart from a fresh one."""
    return tuple(sorted((f.name, f.size) for f in uploads)) + (use_vision,)


# ─────────────────────────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────────────────────────

st.markdown("## 🧾 ITR Extractor")
st.caption("Upload one ITR filing bundle per financial year  ·  "
           "Reads the attached Balance Sheet and P&L  ·  "
           "Verifies every statement against its own printed totals  ·  "
           "Outputs the ITR Validation spreading sheet")
st.divider()

api_key = _load_api_key()

# ─────────────────────────────────────────────────────────────────
# INPUTS
# ─────────────────────────────────────────────────────────────────

with st.container(border=True):
    uploads = st.file_uploader(
        "ITR PDFs  -  one per financial year",
        type=["pdf"], accept_multiple_files=True,
        help="The full filing bundle, including the financial statements. Pages "
             "that carry nothing needed (audit-report clauses, TDS listings, "
             "schedules) are skipped automatically.",
    )

    opt_col, key_col = st.columns([3, 2])
    with opt_col:
        use_vision = st.checkbox(
            "Use Gemini Vision to re-read statements OCR could not reconcile",
            value=False, disabled=not api_key,
            help="Off by default. Rule-based OCR always runs first. With this "
                 "on, any statement that fails its own balance check is "
                 "re-read from the page image, and the re-read is accepted "
                 "ONLY if it then reconciles. Without it, those figures are "
                 "reported as 'Check ITR' rather than guessed. Costs an API "
                 "call per failed page.",
        )
    with key_col:
        if api_key:
            st.caption("🔑 Gemini API key detected - Vision re-reading is available.")
        else:
            st.caption("🔒 No GEMINI_API_KEY set - Vision re-reading is unavailable. "
                       "Rule-based extraction still works; unreadable statements "
                       "will show as 'Check ITR'.")

    run_col, clear_col = st.columns([1, 1])
    run_clicked = run_col.button(
        "▶️  Extract", type="primary", use_container_width=True,
        disabled=not uploads,
        help="Runs rule-based extraction (and OCR for scanned pages) over "
             "every uploaded PDF." if uploads else "Upload at least one PDF first.",
    )
    clear_clicked = clear_col.button(
        "🗑️  Clear results", use_container_width=True,
        disabled="columns" not in st.session_state,
    )

if clear_clicked:
    st.session_state.pop("columns", None)
    st.session_state.pop("signature", None)
    st.rerun()

if run_clicked:
    status = st.status(f"Reading {len(uploads)} document(s)…", expanded=True)
    progress = st.progress(0.0)

    def _on_progress(file_i, file_total, page, page_total):
        progress.progress(min((file_i + page / max(page_total, 1)) / max(file_total, 1), 1.0))
        status.write(f"OCR  ·  {uploads[file_i].name}  ·  page {page}/{page_total}")

    try:
        columns = spread(uploads, api_key=api_key, on_progress=_on_progress,
                         use_vision=use_vision)
    except Exception as e:
        progress.empty()
        status.update(label="Extraction failed", state="error", expanded=True)
        st.error(f"Could not process this upload: {e}")
        st.stop()

    progress.empty()
    status.update(label=f"Read {len(columns)} document(s)", state="complete", expanded=False)
    st.session_state["columns"]   = columns
    st.session_state["signature"] = _signature(uploads, use_vision)

# ─────────────────────────────────────────────────────────────────
# RESULTS  (persisted in session_state, so toggling a widget elsewhere
# on the page doesn't force a re-extraction)
# ─────────────────────────────────────────────────────────────────

columns = st.session_state.get("columns")

if columns is None:
    st.info("Upload your ITR PDFs above, then press **Extract**.")
    st.stop()

if uploads and _signature(uploads, use_vision) != st.session_state.get("signature"):
    st.warning("The uploaded files or settings have changed since this result was "
               "generated. Press **Extract** again to refresh it.")

st.divider()

entity = next((c["entity"] for c in columns if c.get("entity")), "") or "Borrower"
verified = sum(1 for c in columns for b in c["blocks_used"] if b["status"] == F.VERIFIED)
used = sum(len(c["blocks_used"]) for c in columns)

c1, c2, c3 = st.columns([2, 1, 1])
c1.metric("Borrower", entity)
c2.metric("Years", " · ".join(str(c["year"] or "?") for c in columns))
c3.metric("Statements reconciled", f"{verified} of {used}")

st.download_button(
    "⬇️  Download ITR Validation Excel",
    data=generate_excel(columns),
    file_name=get_filename(entity, columns),
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    type="primary",
    use_container_width=True,
)

st.divider()

tab_docs, tab_sheet, tab_ratios, tab_analysis = st.tabs(
    ["📄  Documents", "📊  Spreading Sheet", "📐  Ratios", "🔎  Analysis"])

with tab_docs:
    for col in columns:
        bad = any(b["status"] == F.FAILED for b in col["blocks"]) or not col["blocks_used"]
        icon = "⚠️" if (bad or col["warnings"]) else "✅"
        with st.expander(f"{icon}  {_col_name(col)}  ·  {col.get('source_name', '')}",
                         expanded=bad):
            a, b, c, d = st.columns(4)
            a.metric("Pages read", f"{col['pages_used']} of {col['pages_total']}")
            b.metric("Statements used", len(col["blocks_used"]))
            c.metric("Source", "Scanned (OCR)" if col["scanned"] else "Digital")
            vp = col.get("vision_pages") or []
            d.metric("Vision calls", len(vp),
                     help=("Pages sent to Gemini: "
                           + ", ".join(str(p + 1) for p in vp)) if vp else
                          "No page needed re-reading")

            rows = []
            for blk in col["blocks"]:
                rows.append({
                    "Status":    blk["status"],
                    "Statement": blk["kind"],
                    "Entity":    blk.get("entity", ""),
                    "Page":      blk["page"] + 1,
                    "Detail":    blk["check"]["reason"] or "reconciles to its printed total",
                })
            if rows:
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            # A block that didn't verify is excluded from every figure above -
            # but "didn't verify" usually means one row went missing, not that
            # nothing was read. Showing what WAS harvested lets an analyst spot
            # the one dropped line in seconds instead of re-keying the whole
            # statement by hand (see docs/SHORTCOMINGS.md Case 1).
            for blk in col["blocks"]:
                if blk["status"] == F.VERIFIED:
                    continue
                items = F.all_items(blk["sides"])
                if not items:
                    continue
                with st.expander(
                        f"What was harvested from the {blk['kind']} on page "
                        f"{blk['page'] + 1} ({blk['status']}, not used above)"):
                    st.dataframe(pd.DataFrame(
                        [{"Line item": l, "Amount (lakhs)": round(a / LAKH, 2)}
                         for l, a in items]),
                        use_container_width=True, hide_index=True)

            for w in col["warnings"]:
                st.warning(w)
            if not col["warnings"]:
                st.success("Every statement used reconciled against its own printed totals.")

            # Anything that did not fit the template, or that required a
            # judgement call, is shown here rather than left implicit in the
            # numbers.
            unmapped = col.get("unmapped", [])
            if unmapped:
                total = sum(a for _l, a in unmapped)
                st.error(f"**Excluded from the sheet — fits no template row: "
                         f"{_fmt_lakh(total)} lakhs across {len(unmapped)} line(s).** "
                         f"These figures are in the accounts but in none of the "
                         f"numbers above.")
                st.dataframe(pd.DataFrame(
                    [{"Line item": l, "Amount (lakhs)": round(a / LAKH, 2)}
                     for l, a in unmapped]),
                    use_container_width=True, hide_index=True)

            notes = col.get("assumptions", [])
            if notes:
                st.info(f"**{len(notes)} assumption(s) made** — these are judgement "
                        f"calls, not facts stated in the document.")
                st.dataframe(pd.DataFrame(
                    [{"Line item": str(n["label"]),
                      "Placed in": n["target"],
                      "Amount (lakhs)": (None if n["amount"] is None
                                         else round(n["amount"] / LAKH, 2)),
                      "Assumption": n["note"]} for n in notes]),
                    use_container_width=True, hide_index=True)

            if col.get("ignored"):
                st.caption(f"{len(col['ignored'])} subtotal/outcome row(s) "
                           f"(\"Total …\", \"Profit before tax\", EPS) were read but "
                           f"deliberately not counted — they restate figures already "
                           f"included, and are re-derived instead.")

with tab_sheet:
    st.dataframe(_spread_df(columns), use_container_width=True)

with tab_ratios:
    st.dataframe(_ratio_df(columns), use_container_width=True)

with tab_analysis:
    summ = A.summary(columns)
    a1, a2, a3 = st.columns(3)
    a1.metric("Revenue CAGR", "n/a" if summ["revenue_cagr"] is None
              else f"{summ['revenue_cagr']:.1%}")
    a2.metric("Red flags", summ["red"])
    a3.metric("Amber flags", summ["amber"])

    tr = A.trends(columns)
    st.caption("Year-on-year growth (%) - blank where a year is unread or not "
               "consecutive with the one before it")
    st.dataframe(pd.DataFrame(
        {_col_name(c): [None if tr[k][i] is None else round(tr[k][i] * 100, 1)
                        for k in tr] for i, c in enumerate(columns)},
        index=[A.LABELS[k] for k in tr]), use_container_width=True)

    fl = A.flags(columns)
    if not fl:
        st.success("No red or amber signals raised.")
    for f in fl:
        (st.error if f["severity"] == A.RED else st.warning)(
            f"**{f['year'] or '?'}  ·  {f['flag']}** — {f['detail']}")
    st.caption("Thresholds are conventional MSME comfort levels "
               "(engine/analysis.py THRESHOLDS) - adjust to your credit policy.")
