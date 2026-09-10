"""
excel_generator.py  -  the ITR Validation spreading sheet.

Reproduces the analyst's existing workbook: Profit & Loss, Balance Sheet,
Ratios, a DSCR working and a Financial Snap, one column per financial year,
amounts in lakhs. A second sheet carries the audit trail - which statement fed
each column, whether it reconciled, and any label the mapping could not place.

Figures are written in lakhs as REAL NUMBERS, not strings, so the sheet stays
sortable and chartable and the analyst can keep working in it.
"""

import io
import re
import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from .extract import financials as F
from .mapping import taxonomy as T
from .mapping import template_config as C

# ─────────────────────────────────────────────
# PALETTE
# ─────────────────────────────────────────────

NAVY       = "1F3864"
WHITE      = "FFFFFF"
SECTION_BG = "D6E4F0"
TOTAL_BG   = "FFF2CC"
BORDER_CLR = "BFBFBF"
GOOD_GREEN = "375623"
BAD_RED    = "C00000"
WARN_BG    = "FFF2F2"

LAKH = 100000.0


def _b(style="thin"):
    s = Side(style=style, color=BORDER_CLR)
    return Border(left=s, right=s, top=s, bottom=s)


def _f(size=10, bold=False, color="000000", italic=False):
    return Font(name="Arial", size=size, bold=bold, color=color, italic=italic)


def _fill(hex_color):
    return PatternFill("solid", fgColor=hex_color)


def _a(h="left", v="center", wrap=False, indent=0):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap, indent=indent)


LAKH_FMT  = "#,##0.00;-#,##0.00;0"
RATIO_FMT = "0.00;-0.00;0.00"
DAYS_FMT  = "0.0"


# ─────────────────────────────────────────────
# SHEET LAYOUT
# ─────────────────────────────────────────────
# The row schema, labels and taxonomy-bucket catalogues all live in
# template_config.py now - the one file to edit when the analyst's template
# changes shape or a line item should be bucketed differently.

ROWS        = C.ROWS
SNAP_ROWS   = C.SNAP_ROWS
SNAP_LABELS = C.SNAP_LABELS


def borrower_name(columns: list) -> str:
    """
    The borrower's name for the title and filename.

    Taken from a column that actually produced statements. A year with none -
    a NIL return, or a document that failed to read - has no reliable name to
    offer, and letting it supply one titled a whole workbook after a fragment
    of ITR form boilerplate.
    """
    for cols in (
        [c for c in columns if c.get("blocks_used") and c.get("entity")],
        [c for c in columns if c.get("entity")],
    ):
        if cols:
            return cols[0]["entity"]
    return "Borrower"


def generate_excel(columns: list) -> bytes:
    """`columns` is spread.spread_many()'s output, oldest year first."""
    wb = Workbook()
    _build_sheet(wb, columns)
    _build_audit_sheet(wb, columns)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _col_header(col: dict) -> str:
    y = col.get("year")
    return f"31-03-{y}" if y else (col.get("source_name") or "Unknown year")


# Shown where a statement could not be read at all. Deliberately NOT "-" or 0:
# an analyst must be able to tell "the accounts say nil" from "we could not
# read this - go and look at the ITR yourself".
UNREAD = "Check ITR"


def _write_value(ws, row, c, value, kind):
    cell = ws.cell(row=row, column=c)
    if value is None:
        cell.value     = UNREAD
        cell.font      = _f(italic=True, color=BAD_RED)
        cell.fill      = _fill(WARN_BG)
        cell.alignment = _a(h="right")
        cell.border    = _b()
        return cell
    else:
        cell.value = value / LAKH
        cell.number_format = LAKH_FMT
        cell.alignment = _a(h="right")
    cell.font   = _f(bold=(kind == "total"))
    cell.border = _b()
    if kind == "total":
        cell.fill = _fill(TOTAL_BG)
    return cell


def _write_ratio_row(ws, row, label, columns, ratio_key, bold=False):
    """
    One row of a ratios block - the main RATIOS section and the Financial
    Snap's condensed ratios sub-block both call this, so a formatting change
    only has to happen once.
    """
    lbl = ws.cell(row=row, column=1, value=label)
    lbl.font, lbl.alignment, lbl.border = _f(bold=bold), _a(indent=1), _b()
    for i, col in enumerate(columns):
        v = (col.get("ratios") or {}).get(ratio_key)
        cell = ws.cell(row=row, column=2 + i)
        if v is None:
            # The denominator was zero - no interest to cover, no equity to
            # gear against. Shown as "n/a" rather than 0, which would read as
            # a measured value.
            cell.value, cell.font = "n/a", _f(italic=True, color="808080")
        else:
            cell.value = v
            cell.number_format = RATIO_FMT
        cell.alignment, cell.border = _a(h="right"), _b()


def _reserve_dscr_row(ws, row, label, columns):
    """
    "DSCR" appears as a row in both the main RATIOS block and the Financial
    Snap's ratios sub-block, but it is not in taxonomy.ratios() - it depends
    on EMI figures no statement carries. Write the label now and leave the
    values for `_link_dscr_row` once the DSCR Calculation block exists.
    """
    lbl = ws.cell(row=row, column=1, value=label)
    lbl.alignment, lbl.border = _a(indent=1), _b()
    for i in range(len(columns)):
        ws.cell(row=row, column=2 + i).border = _b()


def _link_dscr_row(ws, row, columns, dscr_calc_row):
    """Point a DSCR row at the live DSCR Calculation cell for each column."""
    for i in range(len(columns)):
        L = get_column_letter(2 + i)
        cell = ws.cell(row=row, column=2 + i, value=f"={L}{dscr_calc_row}")
        cell.number_format, cell.font = RATIO_FMT, _f(bold=True)
        cell.alignment, cell.border = _a(h="right"), _b()


def _build_sheet(wb, columns: list) -> None:
    ws = wb.active
    ws.title = "ITR Validation"
    ws.column_dimensions["A"].width = 42
    n = max(len(columns), 1)
    for i in range(n):
        ws.column_dimensions[get_column_letter(2 + i)].width = 16
    last = get_column_letter(1 + n)

    entity = borrower_name(columns)

    row = 1
    ws.merge_cells(f"A{row}:{last}{row}")
    t = ws.cell(row=row, column=1, value=f"{entity}  (Amt in Lakhs)")
    t.font, t.fill, t.alignment = _f(14, True, WHITE), _fill(NAVY), _a(indent=1)
    ws.row_dimensions[row].height = 26
    row += 1

    # Period header
    ws.cell(row=row, column=1, value="Audited").font = _f(bold=True)
    for i, col in enumerate(columns):
        c = ws.cell(row=row, column=2 + i, value=_col_header(col))
        c.font, c.fill, c.alignment = _f(bold=True, color=WHITE), _fill(NAVY), _a(h="center")
        c.border = _b()
    ws.row_dimensions[row].height = 20
    ws.freeze_panes = f"B{row + 1}"
    row += 1

    for kind, key, label in ROWS:
        if kind == "blank":
            row += 1
            continue
        if kind == "section":
            ws.merge_cells(f"A{row}:{last}{row}")
            c = ws.cell(row=row, column=1, value=label)
            c.font, c.fill, c.alignment = _f(bold=True, color=NAVY), _fill(SECTION_BG), _a(indent=1)
            row += 1
            continue

        lbl = ws.cell(row=row, column=1, value=label or T.LABELS.get(key, key))
        lbl.font      = _f(bold=(kind == "total"))
        lbl.alignment = _a(indent=1)
        lbl.border    = _b()
        if kind == "total":
            lbl.fill = _fill(TOTAL_BG)
        for i, col in enumerate(columns):
            _write_value(ws, row, 2 + i, col["values"].get(key), kind)
        row += 1

    # ── Ratios ───────────────────────────────────────────────
    row += 1
    ws.merge_cells(f"A{row}:{last}{row}")
    c = ws.cell(row=row, column=1, value="RATIOS")
    c.font, c.fill, c.alignment = _f(bold=True, color=NAVY), _fill(SECTION_BG), _a(indent=1)
    row += 1

    names = list(columns[0]["ratios"].keys()) if columns and columns[0]["ratios"] \
        else list(T.ratios({}).keys())
    for name in names:
        _write_ratio_row(ws, row, name, columns, name)
        row += 1

    # DSCR is reserved here and linked below, once the DSCR Calculation block
    # (further down this same sheet) exists to point at.
    dscr_ratio_row = row
    _reserve_dscr_row(ws, row, "DSCR", columns)
    row += 1

    # ── DSCR working ─────────────────────────────────────────
    # Part A (cash available) is derived from the accounts. Part B needs the
    # borrower's existing and proposed EMI, which appears in no ITR or
    # financial statement, so those cells are left EMPTY for the analyst
    # rather than filled with a guess. The DSCR formula is written live, so
    # the ratio computes itself the moment the EMI is typed in.
    row += 2
    ws.merge_cells(f"A{row}:{last}{row}")
    c = ws.cell(row=row, column=1, value="DSCR Calculation")
    c.font, c.fill, c.alignment = _f(bold=True, color=NAVY), _fill(SECTION_BG), _a(indent=1)
    row += 1

    dscr_src = [("Net Profit", "profit_after_tax"),
                ("Depreciation", "depreciation"),
                ("Interest (Including CC/OD interest)", "interest_finance")]
    first_a = row
    for label, key in dscr_src:
        lbl = ws.cell(row=row, column=1, value=label)
        lbl.alignment, lbl.border = _a(indent=1), _b()
        for i, col in enumerate(columns):
            _write_value(ws, row, 2 + i, col["values"].get(key), "item")
        row += 1

    total_a = row
    lbl = ws.cell(row=row, column=1, value="Total (A)")
    lbl.font, lbl.alignment, lbl.border = _f(bold=True), _a(indent=1), _b()
    for i in range(len(columns)):
        L = get_column_letter(2 + i)
        cell = ws.cell(row=row, column=2 + i,
                       value=f"=SUM({L}{first_a}:{L}{total_a - 1})")
        cell.number_format, cell.font = LAKH_FMT, _f(bold=True)
        cell.fill, cell.alignment, cell.border = _fill(TOTAL_BG), _a(h="right"), _b()
    row += 1

    emi_rows = {}
    for label in ("Existing Monthly EMI", "Proposed Loan EMI"):
        lbl = ws.cell(row=row, column=1, value=label)
        lbl.alignment, lbl.border = _a(indent=1), _b()
        for i in range(len(columns)):
            cell = ws.cell(row=row, column=2 + i)
            cell.number_format = LAKH_FMT
            cell.fill, cell.alignment, cell.border = _fill(WARN_BG), _a(h="right"), _b()
        emi_rows[label] = row
        row += 1

    total_b = row
    lbl = ws.cell(row=row, column=1, value="Yearly Obligation (B)")
    lbl.font, lbl.alignment, lbl.border = _f(bold=True), _a(indent=1), _b()
    e, p = emi_rows["Existing Monthly EMI"], emi_rows["Proposed Loan EMI"]
    for i in range(len(columns)):
        L = get_column_letter(2 + i)
        cell = ws.cell(row=row, column=2 + i, value=f"=({L}{e}+{L}{p})*12")
        cell.number_format, cell.font = LAKH_FMT, _f(bold=True)
        cell.fill, cell.alignment, cell.border = _fill(TOTAL_BG), _a(h="right"), _b()
    row += 1

    lbl = ws.cell(row=row, column=1, value="DSCR (A/B)")
    lbl.font, lbl.alignment, lbl.border = _f(bold=True), _a(indent=1), _b()
    dscr_calc_row = row
    for i in range(len(columns)):
        L = get_column_letter(2 + i)
        cell = ws.cell(row=row, column=2 + i,
                       value=f'=IF({L}{total_b}=0,"",{L}{total_a}/{L}{total_b})')
        cell.number_format, cell.font = RATIO_FMT, _f(bold=True)
        cell.alignment, cell.border = _a(h="right"), _b()
    row += 1

    # Backfill the "DSCR" row reserved earlier in the RATIOS block: a live
    # link to this same computed cell, so it updates the moment EMI is typed.
    _link_dscr_row(ws, dscr_ratio_row, columns, dscr_calc_row)

    note = ws.cell(row=row, column=1,
                   value="Enter monthly EMI in the highlighted cells - DSCR computes automatically")
    note.font = _f(size=9, italic=True, color="808080")
    ws.merge_cells(f"A{row}:{last}{row}")
    row += 1

    # ── Financial snap ───────────────────────────────────────
    row += 2
    ws.merge_cells(f"A{row}:{last}{row}")
    c = ws.cell(row=row, column=1, value="Financial Snap  (Amt in Lakhs)")
    c.font, c.fill, c.alignment = _f(bold=True, color=WHITE), _fill(NAVY), _a(indent=1)
    row += 1
    for key in SNAP_ROWS:
        lbl = ws.cell(row=row, column=1,
                      value=SNAP_LABELS.get(key, T.LABELS.get(key, key)))
        lbl.alignment, lbl.border = _a(indent=1), _b()
        for i, col in enumerate(columns):
            _write_value(ws, row, 2 + i, col["values"].get(key), "item")
        row += 1

    # Financial Snap's own condensed ratios sub-block - same underlying
    # figures as the main RATIOS block above, under the Financial Snap's own
    # labels (see template_config.SNAP_RATIO_NAMES).
    lbl = ws.cell(row=row, column=1, value="Ratios")
    lbl.font, lbl.alignment, lbl.border = _f(bold=True), _a(indent=1), _b()
    row += 1
    for label, ratio_key in C.SNAP_RATIO_NAMES:
        if ratio_key == "DSCR":
            _reserve_dscr_row(ws, row, label, columns)
            _link_dscr_row(ws, row, columns, dscr_calc_row)
        else:
            _write_ratio_row(ws, row, label, columns, ratio_key)
        row += 1

    # ── Extraction status ────────────────────────────────────
    row += 1
    ws.merge_cells(f"A{row}:{last}{row}")
    c = ws.cell(row=row, column=1, value="Extraction Status")
    c.font, c.fill, c.alignment = _f(bold=True, color=NAVY), _fill(SECTION_BG), _a(indent=1)
    row += 1

    lbl = ws.cell(row=row, column=1, value="Statements reconciled")
    lbl.font, lbl.alignment, lbl.border = _f(bold=True), _a(indent=1), _b()
    for i, col in enumerate(columns):
        used = col.get("blocks_used") or []
        ok   = sum(1 for b in used if b["status"] == F.VERIFIED)
        cell = ws.cell(row=row, column=2 + i, value=f"{ok} of {len(used)}")
        cell.font = _f(color=GOOD_GREEN if used and ok == len(used) else BAD_RED,
                       bold=True)
        cell.alignment, cell.border = _a(h="center"), _b()
        if not used or ok < len(used):
            cell.fill = _fill(WARN_BG)
    row += 1

    lbl = ws.cell(row=row, column=1, value="Pages read / in document")
    lbl.font, lbl.alignment, lbl.border = _f(bold=True), _a(indent=1), _b()
    for i, col in enumerate(columns):
        cell = ws.cell(row=row, column=2 + i,
                       value=f"{col.get('pages_used', 0)} / {col.get('pages_total', 0)}")
        cell.alignment, cell.border = _a(h="center"), _b()
    row += 1

    # Lines that fit no template row are money that is REAL but absent from
    # every figure above. That has to be visible on the sheet an analyst
    # actually reads, not only in the audit tab.
    lbl = ws.cell(row=row, column=1, value="Amount excluded (fits no row)")
    lbl.font, lbl.alignment, lbl.border = _f(bold=True), _a(indent=1), _b()
    for i, col in enumerate(columns):
        excluded = sum(a for _l, a in col.get("unmapped", []))
        cell = ws.cell(row=row, column=2 + i)
        cell.value = excluded / LAKH if excluded else 0
        cell.number_format = LAKH_FMT
        cell.alignment, cell.border = _a(h="right"), _b()
        if excluded:
            cell.font, cell.fill = _f(bold=True, color=BAD_RED), _fill(WARN_BG)
    row += 1

    lbl = ws.cell(row=row, column=1, value="Assumptions made")
    lbl.font, lbl.alignment, lbl.border = _f(bold=True), _a(indent=1), _b()
    for i, col in enumerate(columns):
        n = len(col.get("assumptions", []))
        cell = ws.cell(row=row, column=2 + i, value=n)
        cell.alignment, cell.border = _a(h="center"), _b()
        if n:
            cell.font = _f(bold=True, color="9C6500")
    row += 2

    note = ws.cell(row=row, column=1,
                   value="See the Audit Trail sheet for every excluded line, "
                         "every assumption, and which statement each figure came from")
    note.font = _f(size=9, italic=True, color="808080")
    ws.merge_cells(f"A{row}:{last}{row}")


def _build_audit_sheet(wb, columns: list) -> None:
    """
    Where every figure came from, and what could not be verified.

    The point of the arithmetic checks is lost if their results stay inside the
    program: an analyst signing off on these numbers needs to see which
    statement fed which column and which ones proved themselves.
    """
    ws = wb.create_sheet("Audit Trail")
    for col, width in zip("ABCDE", (18, 30, 16, 16, 74)):
        ws.column_dimensions[col].width = width

    row = 1
    t = ws.cell(row=row, column=1, value="Audit Trail")
    t.font = _f(14, True, NAVY)
    row += 2

    for col in columns:
        ws.merge_cells(f"A{row}:E{row}")
        c = ws.cell(row=row, column=1,
                    value=f"{_col_header(col)}  ·  {col.get('entity', '')}  ·  "
                          f"{col.get('source_name', '')}"
                          f"{'  ·  scanned' if col.get('scanned') else ''}")
        c.font, c.fill, c.alignment = _f(bold=True, color=WHITE), _fill(NAVY), _a(indent=1)
        row += 1

        pages = col.get("page_summary") or {}
        ws.cell(row=row, column=1, value="Pages").font = _f(bold=True)
        ws.cell(row=row, column=2,
                value=", ".join(f"{k}: {v}" for k, v in sorted(pages.items()))).font = _f(size=9)
        row += 1

        vp = col.get("vision_pages") or []
        if vp:
            ws.cell(row=row, column=1, value="Vision calls").font = _f(bold=True)
            ws.merge_cells(f"B{row}:E{row}")
            ws.cell(row=row, column=2,
                    value=f"{len(vp)} page(s) re-read from their images: "
                          + ", ".join(str(p + 1) for p in vp)).font = _f(size=9)
            row += 1

        for head in ("Status", "Statement", "Page", "Total", "Detail"):
            hc = ws.cell(row=row, column=("Status", "Statement", "Page", "Total",
                                          "Detail").index(head) + 1, value=head)
            hc.font, hc.fill = _f(bold=True), _fill(SECTION_BG)
            hc.border = _b()
        row += 1

        for b in col.get("blocks", []):
            chk = b["check"]
            sc = ws.cell(row=row, column=1, value=b["status"])
            sc.font = _f(bold=True, color={F.VERIFIED: GOOD_GREEN,
                                           F.FAILED: BAD_RED}.get(b["status"], "808080"))
            ws.cell(row=row, column=2,
                    value=f"{b['kind']}  ({b.get('entity') or 'unnamed'})").font = _f(size=9)
            ws.cell(row=row, column=3, value=b["page"] + 1).alignment = _a(h="center")
            tot = ws.cell(row=row, column=4, value=chk.get("printed_total"))
            tot.number_format = "#,##0"
            ws.cell(row=row, column=5,
                    value=chk.get("reason") or "reconciles to the statement's own "
                                               "printed total").font = _f(size=9)
            for cc in range(1, 6):
                ws.cell(row=row, column=cc).border = _b()
            row += 1

        # ── Lines that did not fit the template ──────────────
        for label, amount in col.get("unmapped", []):
            ws.cell(row=row, column=1, value="EXCLUDED").font = _f(bold=True, color=BAD_RED)
            ws.cell(row=row, column=2, value=label[:60]).font = _f(size=9)
            a = ws.cell(row=row, column=4, value=amount)
            a.number_format = "#,##0"
            a.fill = _fill(WARN_BG)
            ws.cell(row=row, column=5,
                    value="fits no row in this template - NOT included in any "
                          "figure above").font = _f(size=9, color=BAD_RED)
            for cc in range(1, 6):
                ws.cell(row=row, column=cc).border = _b()
            row += 1

        # ── Judgement calls ──────────────────────────────────
        for note in col.get("assumptions", []):
            ws.cell(row=row, column=1, value="ASSUMED").font = _f(bold=True, color="9C6500")
            ws.cell(row=row, column=2, value=str(note["label"])[:60]).font = _f(size=9)
            ws.cell(row=row, column=3, value=note["target"]).font = _f(size=9)
            if note["amount"] is not None:
                a = ws.cell(row=row, column=4, value=note["amount"])
                a.number_format = "#,##0"
            ws.cell(row=row, column=5, value=note["note"]).font = _f(size=9)
            for cc in range(1, 6):
                ws.cell(row=row, column=cc).border = _b()
            row += 1

        # ── Subtotal rows deliberately not counted ───────────
        ignored = col.get("ignored", [])
        if ignored:
            ws.cell(row=row, column=1, value="not counted").font = _f(bold=True, color="808080")
            ws.merge_cells(f"B{row}:E{row}")
            ws.cell(row=row, column=2,
                    value=f"{len(ignored)} subtotal/outcome row(s) "
                          f"(“Total …”, “Profit before tax”, EPS) were read but "
                          f"deliberately excluded - they restate figures already "
                          f"counted, and are re-derived instead"
                    ).font = _f(size=9, color="808080")
            row += 1

        for w in col.get("warnings", []):
            ws.cell(row=row, column=1, value="note").font = _f(bold=True, color="9C6500")
            ws.merge_cells(f"B{row}:E{row}")
            ws.cell(row=row, column=2, value=w).font = _f(size=9)
            row += 1
        row += 1


def get_filename(entity: str, columns: list = None) -> str:
    safe = re.sub(r"[^\w\s-]", "", entity or "ITR").strip().replace(" ", "_") or "ITR"
    span = ""
    if columns:
        years = [c["year"] for c in columns if c.get("year")]
        if years:
            span = f"_{years[0]}" if len(years) == 1 else f"_{years[0]}_{years[-1]}"
    return f"{safe}_ITR_Validation{span}_{datetime.datetime.now():%d%b%Y}.xlsx"
