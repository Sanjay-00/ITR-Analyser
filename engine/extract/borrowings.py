"""
borrowings.py - what a generic "Long Term Borrowings" line actually is.

A Schedule III balance sheet prints one face line, "Long Term Borrowings",
and says in its notes what the borrowings are. Without the note the spread
maps the line to secured loans, and for a company funded by its directors
that turns owners' money into outside debt: on a real filing, TOL/TNW read
5.59 (a red flag) where the note said every rupee was an unsecured loan from
the directors and relatives, which a CAM counts as quasi equity (0.55).

This reads the notes for exactly that statement: a line naming loans from
directors, promoters, shareholders, partners, the proprietor, relatives or
related parties as unsecured, printing the same amount as the face line.
Both are required, the wording and the amount, so a note that merely
mentions related parties never moves a rupee.
"""

import re

_OWNER_LOAN = re.compile(
    r"(?i)\b(?:loans?|borrowings?|advances?|deposits?)\s+(?:taken\s+|received\s+)?from\s+(?:the\s+)?"
    r"(?:directors?|promoters?|shareholders?|partners?|proprietor|relatives?|related\s+part\w*|members?)")
_UNSECURED = re.compile(r"(?i)\bunsecured\b")
_NUMBER = re.compile(r"\d[\d,]*")

GENERIC_LT_BORROWINGS = re.compile(r"(?i)^\s*long[\s-]*term\s+borrowings?\s*$")


def owner_loan_lines(page_texts: list) -> list:
    """[(page number, line)] for every line that names unsecured loans from
    the owners or their relatives. Kept small: only those lines."""
    out = []
    for i, text in enumerate(page_texts or []):
        for line in (text or "").splitlines():
            if _OWNER_LOAN.search(line) and _UNSECURED.search(line):
                out.append((i + 1, line.strip()))
    return out


def _printed_forms(amount) -> list:
    """How a rupee amount can appear in a note: in rupees, thousands or
    lakhs, as the digits a line prints (separators and decimals dropped)."""
    rupees = int(round(abs(float(amount))))
    forms = [str(rupees)]
    for unit in (1000, 100000):
        if rupees % unit == 0:
            forms.append(str(rupees // unit))
    return [f for f in forms if len(f) >= 4]


def owners_loan_note(lines: list, amount):
    """The (page, line) of a note that says `amount` is unsecured loans from
    the owners, or None. A line's number matches when its digits are one of
    the amount's printed forms, or that form followed by its paise "00" (OCR
    often runs the decimals and the next column into the same token). A bare
    prefix is not enough: 1,20,050 must not confirm an amount of 1,200
    thousand."""
    if not amount:
        return None
    forms = _printed_forms(amount)
    for page, line in lines:
        digits = [re.sub(r"\D", "", n) for n in _NUMBER.findall(line)]
        if any(d == f or d.startswith(f + "00") for d in digits for f in forms):
            return page, line
    return None
