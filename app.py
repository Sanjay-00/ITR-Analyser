"""
ITR Extractor  -  ITR filing bundles → the ITR Validation spreading sheet.

Upload a borrower's ITR PDFs (any number, any order). The app finds the
financial statements inside, verifies them against their own printed totals,
maps their line items onto the analyst's template, and produces the
spreading workbook.
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

import html

import pandas as pd
import streamlit as st

from engine import (
    spread, financials as F, taxonomy as T, analysis as A,
    generate_excel, get_filename, borrower_name, ROWS, LAKH,
)

st.set_page_config(page_title="ITR Extractor", page_icon="📄",
                   layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  html, body, [class*="css"], .stMarkdown, button, input { font-family: 'Inter', sans-serif; }
  .block-container { max-width: 100%; padding: 1.6rem 2.6rem 3rem 2.6rem; }
  .advice { border: 1px solid #DCE3F0; border-left: 5px solid #3B5BDB; border-radius: 12px;
            padding: 14px 18px; margin: 0 0 1rem 0; background: #F3F6FF; font-size: 0.92rem; }
  .advice.ok   { border-left-color: #16A34A; background: #EEFBF3; border-color: #CDEFD9; }
  .advice.warn { border-left-color: #D97706; background: #FFF7EB; border-color: #F8E1BD; }
  .advice.bad  { border-left-color: #DC2626; background: #FEF1F1; border-color: #F6CACA; }
  .advice .h { font-weight: 600; color: #1B1F24; margin-bottom: 4px; }
  .advice ul { margin: 6px 0 0 18px; padding: 0; color: #374151; }
  #MainMenu, footer, header [data-testid="stDecoration"] { visibility: hidden; }
  h1, h2, h3 { letter-spacing: -0.01em; }

  /* Header band */
  .hero { background: linear-gradient(120deg, #1F3864 0%, #3B5BDB 55%, #0EA5A4 100%);
          border-radius: 16px; padding: 22px 28px; color: #FFF; margin-bottom: 1.1rem;
          box-shadow: 0 8px 24px rgba(31, 56, 100, 0.18); }
  .hero .t { font-size: 1.55rem; font-weight: 700; margin: 0; letter-spacing: -0.01em; }
  .hero .s { font-size: 0.93rem; opacity: 0.88; margin: 4px 0 12px 0; }
  .chip { display: inline-block; background: rgba(255,255,255,0.16); border: 1px solid rgba(255,255,255,0.28);
          border-radius: 999px; padding: 3px 11px; font-size: 0.76rem; margin: 0 6px 0 0; }

  /* Cards: the upload box and every bordered container */
  [data-testid="stVerticalBlockBorderWrapper"] { background: #FFF; border-radius: 14px !important;
          box-shadow: 0 2px 10px rgba(16, 24, 40, 0.05); }
  [data-testid="stFileUploaderDropzone"] { background: #F3F6FF !important;
          border: 1.5px dashed #9DB0F0 !important; }

  /* KPI tiles */
  .kpis { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr; gap: 14px; margin: 0.2rem 0 1rem 0; }
  .kpi  { border-radius: 14px; padding: 16px 18px; background: #FFF; border: 1px solid #DCE3F0;
          border-top: 4px solid var(--c); box-shadow: 0 2px 10px rgba(16,24,40,0.05); }
  .kpi .k { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.07em; color: #5B6475;
            display: flex; gap: 6px; align-items: center; }
  .kpi .i { width: 22px; height: 22px; border-radius: 7px; background: var(--bg); color: var(--c);
            display: inline-flex; align-items: center; justify-content: center; font-size: 0.8rem; }
  .kpi .v { font-size: 1.2rem; font-weight: 700; color: #1B1F24; margin-top: 8px;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .kpi.blue   { --c: #3B5BDB; --bg: #E8EDFF; }
  .kpi.violet { --c: #7C3AED; --bg: #F1E8FF; }
  .kpi.green  { --c: #16A34A; --bg: #E7F8EE; }
  .kpi.amber  { --c: #D97706; --bg: #FFF3E0; }
  .kpi.red    { --c: #DC2626; --bg: #FDECEC; }
  .kpi.teal   { --c: #0EA5A4; --bg: #E3F7F6; }
  .ok   { color: #16A34A !important; }
  .warn { color: #B45309 !important; }
  .bad  { color: #B42318 !important; }

  .empty { border: 1.5px dashed #9DB0F0; border-radius: 14px; padding: 30px; color: #4B5563;
           text-align: center; font-size: 0.94rem; margin-top: 0.6rem;
           background: linear-gradient(180deg, #F7F9FF 0%, #FFFFFF 100%); }
  .empty b { color: #1F3864; font-size: 1.02rem; }
  .steps { display: flex; justify-content: center; gap: 26px; margin-top: 16px; flex-wrap: wrap; }
  .step  { display: flex; gap: 8px; align-items: center; font-size: 0.86rem; color: #374151; }
  .step span { width: 24px; height: 24px; border-radius: 50%; background: #3B5BDB; color: #FFF;
               display: inline-flex; align-items: center; justify-content: center; font-weight: 600; font-size: 0.78rem; }

  /* Tabs as pills */
  .stTabs [data-baseweb="tab-list"] { gap: 6px; border-bottom: none !important;
          background: #EAF0FB; padding: 5px; border-radius: 12px; width: fit-content; }
  .stTabs [data-baseweb="tab"] { padding: 7px 16px !important; border-radius: 9px; font-weight: 500; }
  .stTabs [aria-selected="true"] { background: #FFF !important; color: #1F3864 !important;
          box-shadow: 0 1px 4px rgba(16,24,40,0.10); }
  .stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display: none; }
  h4.sec { font-size: 0.98rem; font-weight: 600; color: #1F3864; margin: 1.1rem 0 0.4rem 0;
           padding-left: 10px; border-left: 3px solid #3B5BDB; }

  .flag { display: flex; gap: 10px; align-items: baseline; padding: 9px 0;
          border-bottom: 1px solid #F0F2F4; font-size: 0.9rem; }
  .dot  { width: 8px; height: 8px; border-radius: 50%; flex: none; transform: translateY(-1px); }
  .dot.red { background: #B42318; } .dot.amber { background: #D97706; }
  .flag .y { color: #6B7280; min-width: 44px; }
  .muted { color: #6B7280; font-size: 0.84rem; }

  .stTabs [data-baseweb="tab-list"] { gap: 20px; border-bottom: 1px solid #E4E7EB; }
  .stTabs [data-baseweb="tab"] { padding: 8px 2px; font-weight: 500; }
  [data-testid="stFileUploaderDropzone"] { background: #F9FAFB; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

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
    return f"FY {col['year']}" if col.get("year") \
        else (col.get("source_name") or "Unknown year")


UNREAD = "Check ITR"


def _lakhs(v):
    return None if v is None else round(v / LAKH, 2)


def _spread_df(columns):
    """
    The sheet's own rows, in lakhs. Kept NUMERIC - an unread figure is NaN,
    not the text "Check ITR": a column mixing floats and strings cannot be
    serialised to Arrow. The wording is applied at display time instead.
    """
    names = [_col_name(c) for c in columns]
    index, data = [], []
    for kind, key, _label in ROWS:
        if kind in ("blank", "section"):
            continue
        index.append(T.LABELS.get(key, key))
        data.append([_lakhs(c["values"].get(key)) for c in columns])
    return pd.DataFrame(data, index=index, columns=names, dtype="float64")


def _ratio_df(columns):
    names = [_col_name(c) for c in columns]
    keys = list(columns[0]["ratios"].keys()) if columns else []
    data = [[(c.get("ratios") or {}).get(k) for c in columns] for k in keys]
    return pd.DataFrame(data, index=keys, columns=names, dtype="float64")


def _signature(uploads, use_vision):
    """Identifies a (files, settings) combination, so a stale result left in
    session_state from a previous run can be told apart from a fresh one."""
    return tuple(sorted((f.name, f.size) for f in uploads)) + (use_vision,)


_TONE_COLOUR = {"ok": "green", "warn": "amber", "bad": "red"}


def _kpis(items):
    """(label, value, tone, icon, colour) tiles. A tone (ok/warn/bad) overrides
    the tile's own colour, so a problem is never shown in a calm blue."""
    cells = "".join(
        f'<div class="kpi {_TONE_COLOUR.get(tone, colour)}">'
        f'<div class="k"><span class="i">{icon}</span>{html.escape(k)}</div>'
        f'<div class="v {tone}">{html.escape(str(v))}</div></div>'
        for k, v, tone, icon, colour in items)
    st.markdown(f'<div class="kpis">{cells}</div>', unsafe_allow_html=True)


def _as_text(df, digits=2, na=UNREAD):
    """
    Numbers as display text. Streamlit's grid ignores a Styler's na_rep and
    prints a missing figure as "None" - which reads as "no such line" when it
    means "could not be read". All-text columns also serialise to Arrow
    cleanly, unlike a column mixing numbers and words.
    """
    return df.map(lambda v: na if pd.isna(v) else f"{v:,.{digits}f}")


from engine.mapping.template_config import HIGHLIGHT as _HIGHLIGHT   # noqa: E402
from engine import cache as CACHE                                    # noqa: E402

# The rows the Excel paints yellow, plus the bottom line - tinted on screen too.
_KEY_ROWS = {T.LABELS.get(k, k) for k in _HIGHLIGHT | {"profit_after_tax"}}


def _styled(text_df):
    """Colour for the on-screen grid: unread figures red-tinted, key totals
    blue, negative figures in red. Works on _as_text output."""
    def cell(v):
        if v in (UNREAD,):
            return "background-color: #FDECEC; color: #B42318;"
        if isinstance(v, str) and v.startswith("-") and v != "–":
            return "color: #B42318;"
        return ""

    def row(r):
        return (["background-color: #E8EDFF; color: #1F3864; font-weight: 600;"] * len(r)
                if r.name in _KEY_ROWS else [""] * len(r))

    return text_df.style.apply(row, axis=1).map(cell)


_PL_KEY, _BS_KEY = "sales_other_income", "equity_capital"


def _vision_advice(columns, api_key, used_vision):
    """
    After a run: should this case be re-read with Gemini Vision?

    Vision only helps a statement that was FOUND but did not reconcile (a
    bad scan dropping a digit). It cannot help a year whose statements are
    simply not in the files, and it is wasted on a case that reconciled.
    Returns (tone, heading, [points], offer_rerun).
    """
    failed, missing = [], []
    for c in columns:
        fails = [b for b in c["blocks"]
                 if b["kind"] in (F.BALANCE_SHEET, F.PROFIT_LOSS) and b["status"] == F.FAILED]
        kinds = {b["kind"] for b in c["blocks_used"]}
        for b in fails:
            failed.append(f"{_col_name(c)} · {b['kind'].replace('_', ' ')} on page "
                          f"{b['page'] + 1} ({b.get('source') or c.get('source_name', '')})")
        for kind, key in ((F.PROFIT_LOSS, "P&L"), (F.BALANCE_SHEET, "Balance Sheet")):
            if kind not in kinds and not any(b["kind"] == kind for b in fails):
                missing.append(f"{_col_name(c)} · no {key} found in the files")

    if used_vision:
        if not failed:
            return ("ok", "Gemini Vision was used and everything now reconciles.",
                    [f"{sum(len(c.get('vision_pages') or []) for c in columns)} page(s) "
                     "were re-read from their images."] + missing, False)
        return ("bad", "Still unreadable after Gemini Vision - check these pages by hand.",
                failed + missing, False)
    if not failed and not missing:
        return ("ok", "Gemini not needed.",
                ["Every statement reconciled with rule-based reading - leave Vision off."],
                False)
    if not failed:
        return ("warn", "Gemini will not help here - statements are missing, not unreadable.",
                missing + ["Upload the financial statements for these years instead."], False)
    points = failed + [f"Re-reading costs about {len(failed)} Gemini call(s); a re-read "
                       "is accepted only if it balances."]
    if not api_key:
        return ("warn", "Gemini could recover these - but no API key is set.",
                failed + ["Add GEMINI_API_KEY to the .env file, then Extract again."]
                + missing, False)
    return ("warn", f"Use Gemini Vision: {len(failed)} statement(s) did not reconcile.",
            points + missing, True)


def _items_df(pairs):
    return pd.DataFrame([{"Line item": l, "Lakhs": _lakhs(a)} for l, a in pairs])


# ─────────────────────────────────────────────────────────────────
# HEADER + INPUT
# ─────────────────────────────────────────────────────────────────

api_key = _load_api_key()

st.markdown('<div class="hero"><p class="t">ITR Extractor</p>'
            '<p class="s">Turn a borrower\'s ITR filings into the ITR Validation '
            'spreading sheet.</p>'
            '<span class="chip">✓ Verified against printed totals</span>'
            '<span class="chip">Scanned &amp; digital PDFs</span>'
            '<span class="chip">Every figure linked to its source line</span></div>',
            unsafe_allow_html=True)

# "Re-run with Gemini" (further down) cannot flip the toggle directly:
# Streamlit refuses to write a widget's key once that widget exists. It sets
# this flag instead, consumed HERE - before the toggle is created.
if st.session_state.pop("_want_vision", False):
    st.session_state["use_vision"] = True

with st.container(border=True):
    uploads = st.file_uploader(
        "ITR PDFs", type=["pdf"], accept_multiple_files=True,
        label_visibility="collapsed",
        help="Drop all of a borrower's files at once - any order. Statements are "
             "grouped by the financial year they print, and reprints are removed.",
    )
    left, mid, right = st.columns([5, 1.4, 1.2], vertical_alignment="center")
    with left:
        with st.popover("Settings", icon=":material/tune:"):
            use_vision = st.toggle(
                "Re-read unreconciled statements with Gemini Vision",
                key="use_vision", disabled=not api_key,
                help="Rule-based OCR always runs first. A statement that fails its "
                     "balance check is re-read from the page image and accepted only "
                     "if the re-read reconciles. One API call per failed page.")
            st.caption("Gemini key found." if api_key else
                       "No GEMINI_API_KEY set - unreadable statements show as "
                       "'Check ITR' instead of being re-read.")
            st.divider()
            cs = CACHE.stats()
            st.caption(f"**Cache** · {cs['files']} file(s) and {cs['pages']} Gemini "
                       f"page(s) saved · {cs['bytes'] / 1e6:,.1f} MB. Files read "
                       f"before load instantly; entries expire after "
                       f"{CACHE.MAX_AGE_DAYS} days.")
            if st.button("Clear cache", icon=":material/delete:",
                         disabled=not (cs["files"] or cs["pages"])):
                n = CACHE.clear()
                st.toast(f"Cache cleared · {n} entr{'y' if n == 1 else 'ies'} removed")
    clear_clicked = mid.button("Clear", width="stretch",
                               disabled="columns" not in st.session_state)
    run_clicked = right.button("Extract", type="primary", width="stretch",
                               disabled=not uploads, icon=":material/play_arrow:")

if clear_clicked:
    st.session_state.pop("columns", None)
    st.session_state.pop("signature", None)
    st.rerun()

# "Re-run with Gemini" (see the advice card) switches Vision on and re-runs.
run_clicked = (run_clicked or st.session_state.pop("auto_run", False)) and bool(uploads)

if run_clicked:
    status = st.status(f"Reading {len(uploads)} file(s)…", expanded=False)
    progress = st.progress(0.0)

    def _on_progress(file_i, file_total, page, page_total):
        progress.progress(min((file_i + page / max(page_total, 1)) / max(file_total, 1), 1.0),
                          text=f"{uploads[file_i].name} · page {page} of {page_total}")

    try:
        columns = spread(uploads, api_key=api_key, on_progress=_on_progress,
                         use_vision=use_vision)
    except Exception as e:
        progress.empty()
        status.update(label="Extraction failed", state="error", expanded=True)
        st.error(f"Could not process this upload: {e}")
        st.stop()

    progress.empty()
    reused = CACHE.reused()
    fresh = len(uploads) - len(reused)
    status.update(label=f"Done · {len(columns)} year(s) · {fresh} file(s) read"
                        + (f", {len(reused)} reused from cache" if reused else ""),
                  state="complete")
    st.session_state["columns"]   = columns
    st.session_state["signature"] = _signature(uploads, use_vision)
    st.session_state["used_vision"] = bool(use_vision and api_key)

columns = st.session_state.get("columns")

if columns is None:
    st.markdown('<div class="empty"><b>Start a new case</b><br>'
                'Scanned and digital files both work - drop all of a borrower\'s PDFs at once.'
                '<div class="steps">'
                '<div class="step"><span>1</span>Upload the ITR PDFs</div>'
                '<div class="step"><span>2</span>Press Extract</div>'
                '<div class="step"><span>3</span>Review checks &amp; download the Excel</div>'
                '</div></div>', unsafe_allow_html=True)
    st.stop()

if uploads and _signature(uploads, use_vision) != st.session_state.get("signature"):
    st.caption(":material/info: Files or settings changed since this result - "
               "press Extract to refresh.")

# ─────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────

entity = borrower_name(columns)
verified = sum(1 for c in columns for b in c["blocks_used"] if b["status"] == F.VERIFIED)
used = sum(len(c["blocks_used"]) for c in columns)
issues = sum(len(c["warnings"]) for c in columns)
flags = A.flags(columns)
red = sum(1 for f in flags if f["severity"] == A.RED)

_kpis([
    ("Borrower", entity, "", "🏢", "blue"),
    ("Years", " · ".join(str(c["year"] or "?") for c in columns), "", "📅", "violet"),
    ("Statements verified", f"{verified} of {used}",
     "ok" if used and verified == used else "warn", "✓", "green"),
    ("Checks to review", issues, "ok" if not issues else "warn", "!", "teal"),
])

tone, head, points, offer = _vision_advice(columns, api_key,
                                          st.session_state.get("used_vision", False))
adv_l, adv_r = st.columns([6, 1.2], vertical_alignment="center")
adv_l.markdown(
    f'<div class="advice {tone}"><div class="h">{html.escape(head)}</div>'
    + (("<ul>" + "".join(f"<li>{html.escape(p)}</li>" for p in points) + "</ul>")
       if points else "") + "</div>", unsafe_allow_html=True)
if offer and adv_r.button("Re-run with Gemini", type="primary", width="stretch",
                          icon=":material/auto_awesome:", disabled=not uploads,
                          help=None if uploads else "Upload the files again first."):
    st.session_state["_want_vision"] = True
    st.session_state["auto_run"] = True
    st.rerun()

top_l, top_r = st.columns([6, 1.2], vertical_alignment="center")
top_l.caption(f"{sum(c['pages_used'] for c in columns)} pages read from "
              f"{sum(c['pages_total'] for c in columns)} · amounts in lakhs · "
              f"{red} red flag(s)")
top_r.download_button(
    "Download Excel", data=generate_excel(columns),
    file_name=get_filename(entity, columns),
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    type="primary", width="stretch", icon=":material/download:")

tab_sheet, tab_analysis, tab_checks = st.tabs(
    ["Spreading sheet", "Analysis", f"Checks ({issues})" if issues else "Checks"])

# ─────────────────────────────────────────────────────────────────
# SPREADING SHEET
# ─────────────────────────────────────────────────────────────────

with tab_sheet:
    st.dataframe(_styled(_as_text(_spread_df(columns))), width="stretch", height=620)

    with st.expander("Where did a figure come from?", icon=":material/search:"):
        keys = [k for kind, k, _l in ROWS
                if kind == "item" and any(k in (c.get("sources") or {}) for c in columns)]
        if not keys:
            st.caption("No source lines recorded for this result.")
        else:
            a, b = st.columns([3, 1])
            key = a.selectbox("Row", keys, format_func=lambda k: T.LABELS.get(k, k))
            year_i = b.selectbox("Year", range(len(columns)),
                                 format_func=lambda i: _col_name(columns[i]))
            lines = (columns[year_i].get("sources") or {}).get(key) or []
            if lines:
                st.dataframe(pd.DataFrame([{
                    "Line in the statement": s["label"], "Lakhs": _lakhs(s["amount"]),
                    "Page": s.get("page"), "File": s.get("file")} for s in lines]),
                    width="stretch", hide_index=True)
                st.caption(f"Total {sum(s['amount'] for s in lines) / LAKH:,.2f} lakhs · "
                           "the Excel shows the same breakdown in each cell's note.")
            else:
                st.caption("Nothing was placed in this row for that year.")

# ─────────────────────────────────────────────────────────────────
# ANALYSIS  (ratios, trends, flags)
# ─────────────────────────────────────────────────────────────────

with tab_analysis:
    summ = A.summary(columns)
    _kpis([
        ("Revenue CAGR", "n/a" if summ["revenue_cagr"] is None
         else f"{summ['revenue_cagr']:.1%}", "", "↗", "blue"),
        ("Red flags", summ["red"], "bad" if summ["red"] else "ok", "●", "red"),
        ("Amber flags", summ["amber"], "warn" if summ["amber"] else "ok", "●", "amber"),
        ("Years", len(columns), "", "📅", "violet"),
    ])

    st.markdown('<h4 class="sec">Ratios</h4>', unsafe_allow_html=True)
    st.dataframe(_styled(_as_text(_ratio_df(columns), na="–")), width="stretch")

    st.markdown('<h4 class="sec">Year-on-year growth (%)</h4>', unsafe_allow_html=True)
    tr = A.trends(columns)
    st.dataframe(_styled(_as_text(pd.DataFrame(
        {_col_name(c): [None if tr[k][i] is None else round(tr[k][i] * 100, 1)
                        for k in tr] for i, c in enumerate(columns)},
        index=[A.LABELS[k] for k in tr], dtype="float64"), digits=1, na="–")),
        width="stretch")

    st.markdown('<h4 class="sec">Signals</h4>', unsafe_allow_html=True)
    if not flags:
        st.markdown('<p class="muted">No red or amber signals.</p>', unsafe_allow_html=True)
    else:
        st.markdown("".join(
            f'<div class="flag"><span class="dot {"red" if f["severity"] == A.RED else "amber"}">'
            f'</span><span class="y">{html.escape(str(f["year"] or "?"))}</span>'
            f'<span><b>{html.escape(f["flag"])}</b> — {html.escape(f["detail"])}</span></div>'
            for f in flags), unsafe_allow_html=True)
    st.caption("Thresholds are conventional MSME comfort levels "
               "(engine/analysis.py THRESHOLDS).")

# ─────────────────────────────────────────────────────────────────
# CHECKS  (per year: statements, warnings, exclusions, assumptions)
# ─────────────────────────────────────────────────────────────────

with tab_checks:
    for col in columns:
        bad = any(b["status"] == F.FAILED for b in col["blocks"]) or not col["blocks_used"]
        attention = bad or col["warnings"] or col.get("unmapped")
        mark = ":material/error:" if attention else ":material/check_circle:"
        with st.expander(f"{_col_name(col)} · {col.get('source_name', '')}",
                         icon=mark, expanded=bool(attention)):
            vp = col.get("vision_pages") or []
            st.caption(f"{col['pages_used']} of {col['pages_total']} pages read · "
                       f"{'scanned (OCR)' if col['scanned'] else 'digital'} · "
                       f"{len(col['blocks_used'])} statement(s) used"
                       + (f" · Vision re-read page(s) {', '.join(str(p + 1) for p in vp)}"
                          if vp else ""))

            rows = [{"Status": blk["status"], "Statement": blk["kind"],
                     "Page": blk["page"] + 1,
                     "Detail": blk["check"]["reason"] or "reconciles to its printed total"}
                    for blk in col["blocks"]]
            if rows:
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

            for w in col["warnings"]:
                st.markdown(f"- {w}")

            # A block that didn't verify is excluded from every figure, but
            # usually only one line went missing. Showing what WAS read lets an
            # analyst spot it in seconds (docs/SHORTCOMINGS.md Case 1).
            for blk in col["blocks"]:
                items = F.all_items(blk["sides"])
                if blk["status"] == F.VERIFIED or not items:
                    continue
                with st.popover(f"Lines read from the {blk['kind']} on page "
                                f"{blk['page'] + 1} (not used)"):
                    st.dataframe(_items_df(items), width="stretch", hide_index=True)

            unmapped = col.get("unmapped", [])
            if unmapped:
                total = sum(a for _l, a in unmapped)
                st.markdown(f"**Excluded — fits no row:** {total / LAKH:,.2f} lakhs "
                            f"across {len(unmapped)} line(s), in none of the figures.")
                st.dataframe(_items_df(unmapped), width="stretch", hide_index=True)

            notes = col.get("assumptions", [])
            if notes:
                st.markdown(f"**Assumptions ({len(notes)})** — judgement calls, "
                            "not stated in the document.")
                st.dataframe(pd.DataFrame(
                    [{"Line item": str(n["label"]), "Placed in": n["target"],
                      "Lakhs": _lakhs(n["amount"]), "Why": n["note"]} for n in notes]),
                    width="stretch", hide_index=True)

            if not (col["warnings"] or unmapped or bad):
                st.markdown('<p class="muted">Every statement used reconciled against '
                            'its own printed totals.</p>', unsafe_allow_html=True)
