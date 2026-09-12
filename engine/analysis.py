"""
analysis.py  -  what the spread SAYS about the borrower, across years.

Everything upstream answers "what do the accounts contain". This module
answers the analyst's next question: which way is the business moving, and
what should worry a lender. It reads only the finished year columns
(columns.spread_many's output), so it never touches extraction and can't
change a figure.

Two outputs:
  trends(columns) -> year-on-year growth rows
  flags(columns)  -> [{year, severity, flag, detail}] - red and amber signals

Deliberately rule-based and explainable: every flag names the figures and the
threshold that raised it, so an analyst can agree or overrule it in seconds.
A figure that could not be read (None) never raises or clears a flag - it
is reported as unassessable instead, not treated as zero.
"""

from .mapping import taxonomy as T

RED   = "red"
AMBER = "amber"

# Thresholds - conventional credit-appraisal comfort levels for Indian MSME
# lending. Kept here, together, so a lender's own policy is a one-place edit.
THRESHOLDS = {
    "tol_tnw_max":         3.0,    # total outside liabilities / tangible net worth
    "current_ratio_min":   1.0,
    "interest_cover_min":  1.5,
    "revenue_drop_amber":  -0.10,  # a 10% fall in turnover
    "revenue_drop_red":    -0.25,
    "margin_drop_pts":     0.03,   # PAT margin down 3 percentage points
}

# Rows worth a growth figure. Order is display order.
TREND_KEYS = [
    "sales_other_income", "gross_expenses", "profit_before_tax",
    "profit_after_tax", "cash_profit", "networth", "total_assets",
    "current_liabilities",
]


def _growth(prev, cur):
    """Fractional change, or None when either side is unread or prev is 0.
    A move from a loss to a smaller loss is still reported against the
    magnitude of the base, so the sign reads as 'better' or 'worse'."""
    if prev is None or cur is None or not prev:
        return None
    return (cur - prev) / abs(prev)


def trends(columns: list) -> dict:
    """
    {key: [None, g1, g2, ...]} - growth versus the previous column, aligned
    with `columns` (the first column has nothing to compare against). Only
    consecutive financial years are compared; a gap in the years uploaded
    gives None rather than a misleading multi-year jump.
    """
    out = {}
    for key in TREND_KEYS:
        row = [None]
        for prev, cur in zip(columns, columns[1:]):
            consecutive = (prev.get("year") and cur.get("year")
                           and cur["year"] - prev["year"] == 1)
            row.append(_growth(prev["values"].get(key), cur["values"].get(key))
                       if consecutive else None)
        out[key] = row
    return out


def cagr(columns: list, key: str = "sales_other_income"):
    """Compound annual growth between the first and last readable year."""
    pts = [(c["year"], c["values"].get(key)) for c in columns
           if c.get("year") and c["values"].get(key)]
    if len(pts) < 2:
        return None
    (y0, v0), (y1, v1) = pts[0], pts[-1]
    if y1 <= y0 or v0 <= 0 or v1 <= 0:
        return None
    return (v1 / v0) ** (1 / (y1 - y0)) - 1


def _flag(out, col, severity, flag, detail):
    out.append({"year": col.get("year"), "severity": severity,
                "flag": flag, "detail": detail})


def _lakh(v):
    return f"{v / 100000:,.2f} L"


def flags(columns: list) -> list:
    """Red/amber signals, oldest year first. See THRESHOLDS."""
    th, out = THRESHOLDS, []
    for i, col in enumerate(columns):
        v, r = col.get("values") or {}, col.get("ratios") or {}

        if not col.get("blocks_used"):
            _flag(out, col, AMBER, "Not assessable",
                  "No reconciled statement for this year - every signal below "
                  "is blind to it")
            continue

        pbt, pat = v.get("profit_before_tax"), v.get("profit_after_tax")
        if pbt is not None and pbt < 0:
            _flag(out, col, RED, "Loss-making",
                  f"Profit before tax is {_lakh(pbt)}")

        nw = v.get("networth")
        if nw is not None and v.get("total_assets") and nw <= 0:
            _flag(out, col, RED, "Net worth eroded",
                  f"Net worth is {_lakh(nw)} - liabilities exceed assets")

        tol = r.get("Total Debt / Equity(inclusive q/e)")
        if tol is not None and nw and nw > 0 and tol > th["tol_tnw_max"]:
            _flag(out, col, RED, "High leverage",
                  f"TOL/TNW {tol:.2f} is above {th['tol_tnw_max']:.1f}")

        cr = r.get("Current Ratio")
        if cr is not None and cr < th["current_ratio_min"]:
            _flag(out, col, AMBER, "Weak liquidity",
                  f"Current ratio {cr:.2f} is below {th['current_ratio_min']:.1f}"
                  f" - current liabilities exceed current assets")

        ic = r.get("Interest Coverage")
        if ic is not None and v.get("interest_finance") and ic < th["interest_cover_min"]:
            _flag(out, col, RED if ic < 1 else AMBER, "Thin interest cover",
                  f"Interest coverage {ic:.2f}x is below "
                  f"{th['interest_cover_min']:.1f}x")

        if col.get("book_profit") is not None and pbt is not None \
                and (col["book_profit"] < 0) != (pbt < 0) and abs(pbt) > 1000:
            _flag(out, col, RED, "Profit contradicts tax computation",
                  f"Statement gives {_lakh(pbt)} but the computation of "
                  f"income restates {_lakh(col['book_profit'])}")

        if i == 0:
            continue
        prev = columns[i - 1]
        if not (prev.get("year") and col.get("year")
                and col["year"] - prev["year"] == 1):
            continue
        pv = prev.get("values") or {}

        g = _growth(pv.get("sales_other_income"), v.get("sales_other_income"))
        if g is not None and g <= th["revenue_drop_red"]:
            _flag(out, col, RED, "Turnover falling sharply",
                  f"Revenue down {abs(g):.0%} year on year")
        elif g is not None and g <= th["revenue_drop_amber"]:
            _flag(out, col, AMBER, "Turnover falling",
                  f"Revenue down {abs(g):.0%} year on year")

        m0 = (prev.get("ratios") or {}).get("PAT / Income (%) (PAT Margin)")
        m1 = r.get("PAT / Income (%) (PAT Margin)")
        if m0 is not None and m1 is not None and m0 - m1 >= th["margin_drop_pts"]:
            _flag(out, col, AMBER, "Margin compressing",
                  f"PAT margin {m0:.1%} -> {m1:.1%}")

        # Debt growing faster than the business: the classic early sign of
        # borrowing to fund losses or a working-capital squeeze.
        debt0 = (pv.get("total_liabilities") or 0) - (pv.get("networth") or 0)
        debt1 = (v.get("total_liabilities") or 0) - (v.get("networth") or 0)
        gd = _growth(debt0 or None, debt1 or None)
        if gd is not None and g is not None and gd - g > 0.25 and gd > 0.25:
            _flag(out, col, AMBER, "Debt outpacing turnover",
                  f"Outside liabilities up {gd:.0%} against revenue "
                  f"{'up' if g >= 0 else 'down'} {abs(g):.0%}")

        cl0, cl1 = pv.get("current_liabilities"), v.get("current_liabilities")
        gcl = _growth(cl0, cl1)
        if gcl is not None and g is not None and gcl > 0.5 and gcl - g > 0.4:
            _flag(out, col, AMBER, "Payables stretching",
                  f"Current liabilities up {gcl:.0%} against revenue "
                  f"{'up' if g >= 0 else 'down'} {abs(g):.0%} - possible "
                  f"working-capital squeeze")
    return out


def summary(columns: list) -> dict:
    """Headline numbers for the UI: revenue CAGR and flag counts."""
    fl = flags(columns)
    return {
        "revenue_cagr": cagr(columns),
        "red":   sum(1 for f in fl if f["severity"] == RED),
        "amber": sum(1 for f in fl if f["severity"] == AMBER),
    }


LABELS = {k: T.LABELS.get(k, k) for k in TREND_KEYS}
