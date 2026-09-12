"""
template_config.py  -  the single place that says what goes where.

Edit THIS file when the analyst's template changes shape, or when a line
item should be bucketed differently. Nothing else in the codebase should
need to change for either kind of edit.

Three things live here:

  1. ROWS / LABELS / SNAP_ROWS / SNAP_LABELS - the spreading sheet's shape:
     which rows exist, in what order, under what heading.
  2. PL_INCOME / PL_EXPENSE / ... - which taxonomy keys are P&L rows vs
     balance-sheet rows, used to total and to gate "Check ITR" vs 0.
  3. SYNONYMS - the wording -> bucket rules. This is where format variance
     actually lives (see taxonomy.py's own docstring for why). Order
     matters: the FIRST pattern that matches a label wins, so more specific
     wording must come before the general case.
  4. CONVENTIONS - the judgement calls worth telling the analyst about,
     shown in both the UI and the Audit Trail sheet.

The sheet is the COMBINED MASTER format (agreed with the user 2026-09-12):
the union of the analysts' two reference layouts, "Format.xlsx" and
"Validation Format- check.xlsx". Every row either has - all eight expense
rows, separate Sundry Creditors and Debtors / Receivables rows, Debtor and
Creditor Days, and the DSCR Calculation block - so the output is always the
same shape, with 0 in any row a borrower's accounts do not use. Tax is
entered POSITIVE and subtracted (PAT = PBT - tax - deferred tax), as in
"Validation Format- check" and the analysts' filled sheets. Totals, ratios,
DSCR and the Financial Snap are LIVE Excel formulas, defined once below
(TOTAL_FORMULAS, RATIOS, SNAP, SNAP_RATIO_NAMES) with `{key}` placeholders
that excel_generator resolves to cell addresses.
"""

import re

# ─────────────────────────────────────────────────────────────────
# BUCKET CATALOGUES
# ─────────────────────────────────────────────────────────────────
# Keys are the rows of the analyst's spreading sheet. DERIVED rows are
# computed from the others (see taxonomy.compute()), never mapped to - a
# statement's own subtotal is a cross-check, not an input.

PL_INCOME = ["sales_other_income"]
PL_EXPENSE = [
    "purchases", "transport_admin", "electricity", "employee_costs",
    "other_expenses", "interest_finance", "depreciation", "extraordinary",
]
PL_TAX = ["provision_tax", "provision_deferred_tax", "preference_dividend"]

BS_LIABILITY = [
    "equity_capital", "preference_shares", "reserves",
    "secured_loan_asset_financed", "secured_loan_maturity_1yr",
    "secured_loan_ccod", "other_lt_liabilities", "unsecured_loans",
    "deferred_tax_liability", "sundry_creditors", "current_liabilities",
]
# Trade receivables and trade payables have rows of their own in the master
# format (for Debtor and Creditor Days); "Current assets" / "Current
# Liabilities and Provision" hold everything else current.
BS_ASSET = [
    "fixed_assets", "intangible_assets", "investments", "deferred_tax_assets",
    "debtors", "current_assets", "lt_loans_advances", "other_non_current_assets",
]

MAPPABLE = PL_INCOME + PL_EXPENSE + PL_TAX + BS_LIABILITY + BS_ASSET

DERIVED = [
    "gross_receipts", "gross_expenses", "profit_before_tax", "profit_after_tax",
    "profit_available", "cash_profit", "total_liabilities", "total_assets",
    "networth", "total_current_liabilities", "total_current_assets",
]

# Display names, in sheet order. Mirrors the analyst's existing workbook so
# the output drops straight into their process.
LABELS = {
    "sales_other_income":          "Sales and other Income",
    "gross_receipts":              "Gross Receipts",
    "purchases":                   "Purchases & Raw Material",
    "transport_admin":             "Transport operation and admin charges",
    "electricity":                 "Electricity",
    "employee_costs":              "Employee Costs",
    "other_expenses":              "Other Expenses",
    "interest_finance":            "Interest and Finance Expenses",
    "depreciation":                "Depreciation",
    "extraordinary":               "Extra Ordinary Item",
    "gross_expenses":              "Gross Expenses",
    "profit_before_tax":           "Profit before tax",
    "provision_tax":               "Provision for tax",
    "provision_deferred_tax":      "Provision for Deferred Tax",
    "profit_after_tax":            "Profit after tax",
    "preference_dividend":         "Preference dividend (including tax)",
    "profit_available":            "Profit available for equity shareholders",
    "cash_profit":                 "Cash Profit",
    "equity_capital":              "Equity Capital",
    "preference_shares":           "Preference Shares",
    "reserves":                    "Reserves & Surplus",
    "secured_loan_asset_financed": "Secured loan - Asset Financed",
    "secured_loan_maturity_1yr":   "Secured loan maturity within one year",
    "secured_loan_ccod":           "Secured loan - CC/OD",
    "other_lt_liabilities":        "Other Long term liabilities",
    "unsecured_loans":             "Unsecured loans",
    "deferred_tax_liability":      "Deffered tax liability",
    "sundry_creditors":            "Sundry Creditors",
    "current_liabilities":         "Current Liabilities and Provision",
    "total_liabilities":           "Total of Liabilities",
    "fixed_assets":                "Fixed Assets",
    "intangible_assets":           "Intangible Assets",
    "investments":                 "Investments",
    "deferred_tax_assets":         "Deffered Tax Assets",
    "debtors":                     "Debtors / Receivables",
    "current_assets":              "Current assets, Loans and Advances",
    "lt_loans_advances":           "Long Term Loan & Advance",
    "other_non_current_assets":    "Other Non Current Assets",
    "total_assets":                "Total of Assets",
    "networth":                    "Networth",
    # Not sheet rows - used by the Analysis tab.
    "total_current_liabilities":   "Current Liabilities incl. Sundry Creditors",
    "total_current_assets":        "Current Assets incl. Debtors",
}

# ─────────────────────────────────────────────────────────────────
# SHEET LAYOUT
# ─────────────────────────────────────────────────────────────────
# (kind, key, label)
#   section - banded heading
#   item    - a mapped or derived value, in lakhs
#   total   - same, highlighted
#   blank   - spacer

ROWS = [
    ("section", "",                            "PROFIT AND LOSS ACCOUNT"),
    ("section", "",                            "Income"),
    ("item",  "sales_other_income",            None),
    ("total", "gross_receipts",                None),
    ("section", "",                            "Expenses"),
    ("item",  "purchases",                     None),
    ("item",  "transport_admin",               None),
    ("item",  "electricity",                   None),
    ("item",  "employee_costs",                None),
    ("item",  "other_expenses",                None),
    ("item",  "interest_finance",              None),
    ("item",  "depreciation",                  None),
    ("item",  "extraordinary",                 None),
    ("total", "gross_expenses",                None),
    ("blank", "",                              ""),
    ("total", "profit_before_tax",             None),
    ("item",  "provision_tax",                 None),
    ("item",  "provision_deferred_tax",        None),
    ("total", "profit_after_tax",              None),
    ("item",  "preference_dividend",           None),
    ("total", "profit_available",              None),
    ("total", "cash_profit",                   None),

    ("section", "",                            "BALANCE SHEET"),
    ("section", "",                            "Liabilities"),
    ("item",  "equity_capital",                None),
    ("item",  "preference_shares",             None),
    ("item",  "reserves",                      None),
    ("item",  "secured_loan_asset_financed",   None),
    ("item",  "secured_loan_maturity_1yr",     None),
    ("item",  "secured_loan_ccod",             None),
    ("item",  "other_lt_liabilities",          None),
    ("item",  "unsecured_loans",               None),
    ("item",  "deferred_tax_liability",        None),
    ("item",  "sundry_creditors",              None),
    ("item",  "current_liabilities",           None),
    ("total", "total_liabilities",             None),
    ("section", "",                            "Assets"),
    ("item",  "fixed_assets",                  None),
    ("item",  "intangible_assets",             None),
    ("item",  "investments",                   None),
    ("item",  "deferred_tax_assets",           None),
    ("item",  "debtors",                       None),
    ("item",  "current_assets",                None),
    ("item",  "lt_loans_advances",             None),
    ("item",  "other_non_current_assets",      None),
    ("total", "total_assets",                  None),
]

# Rows filled yellow, as in the analysts' sheets.
HIGHLIGHT = {"gross_receipts", "gross_expenses", "profit_before_tax",
             "total_liabilities", "total_assets"}

# ─────────────────────────────────────────────────────────────────
# LIVE FORMULAS
# ─────────────────────────────────────────────────────────────────
# `{key}` is replaced by that row's cell in the same year column. Written
# exactly as the analysts' sheets write them; taxonomy.ratios() computes the
# same figures in Python for the app, and tests/test_master_excel.py checks
# the two agree by EVALUATING these strings.

TOTAL_FORMULAS = {
    "gross_receipts":    "IFERROR(SUM({sales_other_income}:{sales_other_income}),0)",
    "gross_expenses":    "IFERROR(SUM({purchases}:{extraordinary}),0)",
    "profit_before_tax": "IFERROR({gross_receipts}-{gross_expenses},0)",
    # Tax entered positive, subtracted ("Validation Format- check").
    "profit_after_tax":  "{profit_before_tax}-{provision_tax}-{provision_deferred_tax}",
    # As both reference sheets write it; the dividend is entered negative.
    "profit_available":  "{profit_after_tax}+{preference_dividend}",
    "cash_profit":       "IFERROR({profit_available}+{depreciation},0)",
    "total_liabilities": "SUM({equity_capital}:{current_liabilities})",
    "total_assets":      "SUM({fixed_assets}:{other_non_current_assets})",
}

_EQUITY_QE = "({equity_capital}+{reserves}+{unsecured_loans}+{preference_shares})"

# (label, formula, number format). Order is the sheet's.
RATIOS = [
    ("Return on Capital Employed",
     "IFERROR(({profit_after_tax}+{interest_finance})/({equity_capital}+{reserves}"
     "+{secured_loan_asset_financed}+{unsecured_loans}+{preference_shares}),0)", "0.00%"),
    ("Return On Share Holders Fund",
     "IFERROR({profit_after_tax}/({equity_capital}+{reserves}+{preference_shares}),0)", "0.00%"),
    ("PAT / Income (%)",
     "IFERROR({profit_after_tax}/{gross_receipts},0)", "0.00%"),
    ("PAT / Assets employed (%)",
     "IFERROR({profit_after_tax}/{total_assets},0)", "0.00%"),
    # "(inclusive q/e)": unsecured loans count as the owners' money.
    ("Long Term Debt / Equity (inclusive q/e)",
     "IFERROR(({secured_loan_asset_financed}+{secured_loan_maturity_1yr})/" + _EQUITY_QE + ",0)",
     "0.00"),
    ("Total Debt / Equity(inclusive q/e)",
     "IFERROR((SUM({secured_loan_asset_financed}:{current_liabilities})-{unsecured_loans})/"
     + _EQUITY_QE + ",0)", "0.00"),
    ("Interest Coverage",
     "IFERROR(({profit_before_tax}+{interest_finance}+{depreciation})/{interest_finance},0)", "0.00"),
    # Debtors and creditors are current items on their own rows here.
    ("Current Ratio",
     "IFERROR(({debtors}+{current_assets})/({sundry_creditors}+{current_liabilities}"
     "+{secured_loan_ccod}+{secured_loan_maturity_1yr}),0)", "0.00"),
    ("DSCR",
     "IFERROR(({profit_after_tax}+{depreciation}+{interest_finance})/"
     "({interest_finance}+({secured_loan_asset_financed}/4)),0)", "0.00"),
    ("Debtor Days",   "IFERROR({debtors}/{gross_receipts}*365,0)", "0.00"),
    ("Creditor Days", "IFERROR({sundry_creditors}/{gross_receipts}*365,0)", "0.00"),
]
RATIO_NAMES = [name for name, _f, _fmt in RATIOS]

# Financial Snap: (label, formula) - amounts, then its own ratio sub-block.
SNAP = [
    ("Turnover and Other Income",           "{gross_receipts}"),
    ("PAT",                                 "{profit_after_tax}"),
    ("Cash Profit",                         "{cash_profit}"),
    ("Networth",                            "{equity_capital}+{preference_shares}+{reserves}"),
    ("Secured Loans - Long Term",           "{secured_loan_asset_financed}+{secured_loan_maturity_1yr}"),
    ("Secured Loans – CC / OD",             "{secured_loan_ccod}"),
    ("Current Liabilities and Provisions",
     "IFERROR({sundry_creditors}+{current_liabilities}+{secured_loan_maturity_1yr},0)"),
    ("Fixed Assets",                        "{fixed_assets}"),
    ("Current Assets , ST Loans & Advances ", "{debtors}+{current_assets}"),
]
# (display label, RATIOS name it repeats).
SNAP_RATIO_NAMES = [
    ("PAT/Income  (%)",                         "PAT / Income (%)"),
    ("Long Term Debt / Equity (including Q/E)", "Long Term Debt / Equity (inclusive q/e)"),
    ("TOL/TNW (including Q/E)",                 "Total Debt / Equity(inclusive q/e)"),
    ("Interest Coverage",                       "Interest Coverage"),
    ("Current Ratio",                           "Current Ratio"),
    ("DSCR",                                    "DSCR"),
    ("Debtor Days",                             "Debtor Days"),
    ("Creditor Days",                           "Creditor Days"),
]

# DSCR Calculation: the analyst keys the EMIs; everything else is linked.
# "Yearly Obligation (B)" multiplies the proposed EMI by 4 in an audited
# column and by 1 in a provisional one, as in "Format.xlsx".
DSCR_PROPOSED_MULTIPLIER = {"Audited": 4, "Provisional": 1}

# ─────────────────────────────────────────────────────────────────
# SYNONYMS
# ─────────────────────────────────────────────────────────────────
# Grown from real filings. Adding a CA package means adding rows here, not
# code.

SYNONYMS = [
    # ── Credit side of a P&L: income, whatever it is called ──
    # FIRST in the table, deliberately. Placed lower it lost to balance-sheet
    # rules that happen to match the wording: "Interest On FD" was claimed by
    # the Investments rule (\bFDR?\b) and dropped out of revenue, understating
    # income by exactly Rs 8,055 on a real filing. Nothing on the credit side
    # of a profit and loss account is an asset.
    ("sales_other_income",          r"^\[INC\]"),

    # Same class of bug as the rule above, mirror-imaged on the expense side:
    # "Interest on Vehicle Loan" / "Interest on Term Loan" is a P&L finance
    # charge, but it CONTAINS "vehicle loan" / "term loan" wording that the
    # secured-loan-PRINCIPAL rule below also matches - and being a
    # balance-sheet rule, it would otherwise be checked first. On a real
    # filing (FC4, Borrower A AY 2023-24) this moved
    # Rs 43,24,133 of interest expense into a phantom Secured Loan bucket:
    # expenses were understated (profit overstated by the same amount), and
    # a balance-sheet liability was fabricated that was never on the P&L at
    # all. Placed here, ahead of every loan-principal rule, for the same
    # reason "sales_other_income" is placed first.
    ("interest_finance",            r"interest\s+(?:paid\s+)?on\s+\w[\w\s]*?\bloan"),

    # ── Balance sheet: liabilities ───────────────────────────
    # "Deferred Tax Liabilities (Net)" gets its own row - confirmed against
    # the analyst's own Borrower K template (2026-07-29), not folded into Other Long
    # Term Liabilities.
    ("deferred_tax_liability",      r"deferred\s+tax\s+liabilit"),
    # Analyst convention: an income-tax asset is bucketed with deferred tax
    # assets rather than getting a row of its own.
    ("deferred_tax_assets",         r"deferred\s+tax\s+asset|income[\s-]*tax\s+asset"),
    ("equity_capital",              r"\b(?:share\s+capital|equity\s+(?:share\s+)?capital"
                                    r"|capital\s+account|proprietor'?s?\s+capital"
                                    r"|partners?'?\s+capital|owner'?s?\s+capital)\b"),
    ("preference_shares",           r"preference\s+(?:share|capital)"),
    # "Other equity" is Ind AS wording for reserves and surplus.
    ("reserves",                    r"\breserves?\b|surplus\b|other\s+equity|"
                                    r"profit\s*(?:&|and)\s*loss\s+(?:a/?c|account)\s+balance"),
    ("secured_loan_ccod",           r"\b(?:cash\s+credit|\bcc\b|overdraft|\bod\b|"
                                    r"working\s+capital\s+loan)\b"),
    ("secured_loan_maturity_1yr",   r"current\s+maturit"),
    # Schedule III's "Short - Term Borrowings" / "Short - Term Loans and
    # Advances" both contain "Term Borrowings"/"Term Loans" and would otherwise
    # be claimed by the long-term rule below. Short-term borrowings are a
    # current liability and short-term advances a current asset - misfiling
    # either moves real money between the two halves of the balance sheet.
    ("current_liabilities",         r"short[\s-]*term\s+borrowing"),
    ("current_assets",              r"short[\s-]*term\s+loans?\s*(?:&|and)?\s*advance"),
    # Trade receivables and trade payables have rows of their own in the
    # master format. Both must come before the [CL]/[CA] section-context rules
    # below, or "[CL] Sundry Creditors" would be claimed by the section.
    ("debtors",                     r"trade\s+receivable|sundry\s+debtor|"
                                    r"\bdebtors?\b|bills?\s+receivable"),
    ("sundry_creditors",            r"sundry\s+creditor|trade\s+payable|sundry\s+payable|"
                                    r"\bcreditors?\b|outstanding\s+dues\s+of\s+(?:micro|creditors)|"
                                    r"micro\s+and\s+small\s+enterprise"),
    # Section context from the statement's own group headings (see
    # financials._SECTIONS). Anything printed under "Current Liabilities" or
    # "Current Assets" belongs to that bucket whatever the CA called the line -
    # one real filing lists "(a) Term Borrowings" under Current Liabilities,
    # having dropped the word "Short".
    ("current_liabilities",         r"^\[CL\]"),
    ("current_assets",              r"^\[CA\]"),
    # Same section-context principle, for a proprietorship's own bare bank-
    # format captions ("SECURED LOAN :", "FIXED ASSETS :" - see
    # financials._SECTIONS) rather than Schedule III wording. The lines under
    # them are lender/debtor NAMES or a bare "As per Schedule" placeholder,
    # neither of which carries any vocabulary of its own to map by - without
    # this, a real filing's entire liabilities side (every line a proper
    # noun) went unmapped despite the Balance Sheet itself verifying cleanly.
    # [EQ] specifically was already being attached to a proprietor's own
    # capital-account line ("CAPITAL ACCOUNT OF: <name>") but had no
    # consuming rule at all until now, so it never actually did anything.
    ("equity_capital",              r"^\[EQ\]"),
    # A line that NAMES a current asset outright is a current asset, whatever
    # caption a T-account's side last carried. A bare "Investment" caption's
    # [INV] context ran on down the assets side of a real filing (Borrower F
    # Roadways FY2023) and filed Loans & Advances, Sundry Debtors and Cash &
    # Bank as investments. Placed before the [FA]/[INV] context rules, which
    # exist for lines with no vocabulary of their own (an asset's or a
    # lender's bare name).
    ("current_assets",              r"^\[(?:FA|INV)\].*\b(?:loans?\s*(?:&|and)\s*advances?|"
                                    r"cash\s*(?:&|and)\s*bank|bank\s+balances?|"
                                    r"cash\s+(?:in|on)\s+hand|cash-in-hand|"
                                    r"sundry\s+debtors?|debtors?|receivables?|"
                                    r"closing\s+stock|stock\s+in\s+hand|"
                                    r"other\s+current\s+assets?)\b"),
    ("secured_loan_asset_financed", r"^\[SL\]"),
    ("unsecured_loans",             r"^\[UL\]"),
    ("fixed_assets",                r"^\[FA\]"),
    ("investments",                 r"^\[INV\]"),
    # "Other financial liabilities/assets" is Ind AS wording that carries no
    # meaning of its own - which bucket it belongs to depends entirely on
    # whether it sits in the current or non-current section, handled by the
    # [CL]/[CA]/[NCL]/[NCA] rules above and below. These are the fallbacks for
    # when no section heading was captured.
    ("other_lt_liabilities",        r"^\[NCL\].*financial\s+liabilit"),
    ("current_liabilities",         r"other\s+financial\s+liabilit"),
    ("current_assets",              r"^\[CA\].*financial\s+asset"),
    # An asset-side "...loans and advances" line (money the company has lent
    # out) must be claimed before the liability rule below - "term loan" as a
    # bare substring of "Long-term loans and advances" was matching there
    # first, booking a real asset as if it were a secured borrowing and
    # leaving the actual liability-side figure it displaced overstated by the
    # same amount.
    ("lt_loans_advances",           r"long[\s-]*term\s+loans?\s*(?:&|and)?\s*advance"),
    # A bare "Borrowings" reaches here only from a NON-current section - the
    # [CL] rule above has already claimed the current-liability one.
    # (?<!un): "Unsecured Loans" CONTAINS "secured loan", and this rule is
    # checked before the unsecured one below - every unsecured loan line was
    # being filed as secured.
    ("secured_loan_asset_financed", r"(?<!un)secured\s+loan|term\s+borrowing|term\s+loan|"
                                    r"\bborrowings?\b|"
                                    r"vehicle\s+loan|hypothecat|loan\s+from\s+bank"),
    # A lease liability carries no charge on assets, so the existing workbooks
    # group it with unsecured borrowings rather than secured loans.
    ("unsecured_loans",             r"unsecured\s+loan|lease\s+liabilit|"
                                    r"loans?\s+from\s+(?:director|partner|"
                                    r"relative|friend)"),
    # A bare "Loans" line with no further qualifier - only reached under the
    # [LIAB] "somewhere on the liabilities side" tag (financials._SECTIONS),
    # which a bare "loans?" NEVER gets on the asset side (an asset-side
    # "Loans and Advances" is claimed by the lt_loans_advances/current_assets
    # rules above before this table is ever consulted with no qualifier at
    # all). Defaults to secured, matching the common real-world case for an
    # unqualified liabilities "Loans" line in a small business's accounts -
    # on a real filing this line's schedule was entirely bank/NBFC term
    # loans - rather than leaving it unmapped and excluded from the sheet.
    ("secured_loan_asset_financed", r"^\[LIAB\].*\bloans?\b"),
    # Tally's borrowings group printed as a line WITH its own total, in a
    # summary Balance Sheet ("Loans (Liability) 2,04,37,748", Borrower E
    # Enterprises FY2024 and FY2023) - it matches no other rule, and the whole
    # of the year's borrowings dropped out of the sheet. The analyst files it
    # as Secured loan - Asset Financed (204.38 / 35.89 lakh, to the rupee).
    ("secured_loan_asset_financed", r"\bloans?\s*\(\s*liabilit"),
    # Same idea for a lender listed by NAME under the liabilities side's
    # "Loans (Liability)" caption ("MUTHOOT FINANCE LTD", "IDFC FIRST BANK").
    # On a real filing (Borrower J / Borrower J FY2023) these two were
    # unmapped; with them, Secured Loans equals the analyst's 57.94 lakh.
    ("secured_loan_asset_financed", r"^\[LIAB\].*\b(?:finance|financial|bank|"
                                    r"nbfc|capital\s+(?:ltd|limited))\b"),
    ("other_lt_liabilities",        r"(?:other\s+)?long[\s-]*term\s+(?:liabilit|"
                                    r"borrowing|provision)|non[\s-]*current\s+liabilit"),
    # Schedule III spells trade creditors out longhand: "Total outstanding dues
    # of micro and small enterprises" / "... of creditors other than micro and
    # small enterprises". Neither contains the words "creditor" in a form the
    # generic rule below catches on its own.
    ("current_liabilities",         r"outstanding\s+dues|micro\s+and\s+small\s+enterprise"),
    ("current_liabilities",         r"sundry\s+creditor|trade\s+payable|sundry\s+payable|"
                                    r"current\s+liabilit|short[\s-]*term\s+(?:provision|"
                                    r"borrowing)|duties\s*&?\s*taxes|outstanding\s+"
                                    r"(?:expense|liabilit)|provision|payable\b"),

    # LAST of the liability rules: a genuine fallback for anything under a
    # capital/equity heading that nothing more specific has claimed - notably a
    # proprietorship whose capital line is nothing but the owner's name. It has
    # to come after every named liability, because a T-account keeps the
    # heading in force down the whole side: placed earlier it swallowed the
    # Sundry Creditors printed below the capital line into equity.
    ("equity_capital",              r"^\[EQ\]"),

    # ── Balance sheet: assets ────────────────────────────────
    ("intangible_assets",           r"intangible"),
    # A right-of-use asset is the leased asset itself, so it sits with fixed
    # assets (matching the existing workbooks, which add it to PPE).
    ("fixed_assets",                r"fixed\s+asset|tangible\s+asset|property,?\s*plant|"
                                    r"plant\s*(?:&|and)\s*machin|furniture|"
                                    r"right[\s-]*of[\s-]*use|"
                                    r"capital\s+work[\s-]*in[\s-]*progress"),
    # Non-current "other financial assets" are deposits and similar, bucketed
    # with investments. Must be tested BEFORE the current-asset rules, and only
    # for the non-current section - the current-section namesake is a
    # receivable.
    ("investments",                 r"^\[NCA\].*financial\s+asset"),
    ("investments",                 r"\binvestment|shares?\s+(?:in|of)\b|"
                                    r"gold\s*(?:&|and)?\s*ornament|"
                                    r"fixed\s+deposit|\bFDR?\b"),
    # A proprietor's T-account often lists fixed assets by WHAT THEY ARE, with
    # no "Fixed Assets" caption above them ("COMPUTER", "HONDA CAR", "MOTOR
    # TRUEK", "AIRCONDTIONER") - unmapped on a real filing (Borrower J FY2023,
    # Rs 28 lakh of assets). Never on a P&L line ([INC]/[EXP]) and never a
    # label that reads as a running cost, so "Car Expenses" or "Vehicle
    # Insurance" stays an expense.
    ("fixed_assets",                r"^(?!\[(?:INC|EXP)\])"
                                    r"(?!.*\b(?:exp\.?|expenses?|repairs?|maint\w*|"
                                    r"insurance|running|hire|rent|fuel|loan|emi)\b)"
                                    r".*\b(?:computers?|laptops?|mobiles?|bikes?|"
                                    r"cars?|trucks?|truek|motor|tempo|lorry|"
                                    r"air\s*-?\s*condi?tione?rs?|household\s+appliances?|"
                                    r"printers?|machinery|equipments?)\b"),
    ("debtors",                     r"trade\s+receivable|sundry\s+debtor|"
                                    r"\bdebtors?\b|bills?\s+receivable"),
    ("other_non_current_assets",    r"other\s+non[\s-]*current\s+asset|"
                                    r"non[\s-]*current\s+asset"),
    ("current_assets",              r"receivable|"
                                    r"closing\s+stock|inventor|cash\s*(?:&|and|\s+in\s+)|"
                                    r"bank\s+balance|balance\s+with\s+bank|"
                                    r"current\s+asset|short[\s-]*term\s+loans?|"
                                    r"loans?\s*(?:&|and)\s*advance|current\s+investment"),

    # ── Profit & loss ────────────────────────────────────────
    # The credit side of a P&L is income, whatever the line is called. This
    # must come first: a transport operator's revenue lines ("Bus Fare - AC",
    # "Freight receipts") contain the same words as its operating COSTS, and
    # matching on wording alone moved the entire revenue line into expenses.
    ("sales_other_income",          r"^\[INC\]"),
    # "Dep." / "Dep" is the everyday abbreviation a small proprietor's T-account
    # uses ("TO DEP  26,73,141") - \bdep\b is a whole word, so it does not
    # also swallow "Deposit".
    ("depreciation",                r"depreciation|amorti[sz]ation|\bdep\.?\b"),
    ("interest_finance",            r"finance\s+cost|interest\s*(?:&|and)?\s*"
                                    r"(?:bank\s*)?charge|bank\s+(?:int|charge)|"
                                    r"\binterest\b(?!\s+(?:income|received|on\s+(?:IT|"
                                    r"income[\s-]*tax)))"),
    ("employee_costs",              r"employee|salar(?:y|ies)|wages|staff|bonus|"
                                    r"pagar|labour|gratuity|provident"),
    # "Light Bill" is the everyday small-business name for the electricity
    # bill - the analyst's own Borrower B sheet files it here.
    ("electricity",                 r"electricity|power\s*(?:&|and)?\s*fuel|"
                                    r"light\s+bill|\bmseb\b|\bmsedcl\b"),
    ("purchases",                   r"purchase|cost\s+of\s+material|raw\s+material|"
                                    r"changes?\s+in\s+inventor|stock[\s-]*in[\s-]*trade"),
    # "Direct Expenses" / "Operating costs" is the operating-cost line of a
    # transport business, which the existing workbooks spread as Transport
    # operation and admin charges rather than lumping into Other Expenses.
    ("transport_admin",             r"direct\s+expense|operating\s+(?:expense|cost)|"
                                    r"cost\s+of\s+(?:services|operations)|"
                                    r"transport|freight|diesel|fuel|toll|tyre|"
                                    r"tanker|"
                                    r"\br\.?t\.?o\.?\b|road\s+tax|"
                                    r"stores?\s*(?:&|and)?\s*spares?|hamali|"
                                    r"vehicle\s+(?:running|expense)|"
                                    r"repair\s*(?:&|and)?\s*maint|insurance"),
    ("extraordinary",               r"extra\s*[\s-]*ordinary|exceptional\s+item"),
    ("provision_deferred_tax",      r"deferred\s+tax(?:\s+expense)?\b"),
    ("provision_tax",               r"\b(?:current\s+tax|provision\s+for\s+tax|"
                                    r"income[\s-]*tax\s+(?:expense|provision)|tax\s+expense)\b"),
    ("preference_dividend",         r"preference\s+dividend"),
    ("sales_other_income",          r"revenue\s+from\s+operation|gross\s+receipt|"
                                    r"\bsales?\b|turnover|other\s+income|"
                                    r"\bincome\s+from\b|\breceipts?\b|bus\s+fare|"
                                    r"service\s+charge"),
    # Anything left that reads as an expense lands in Other Expenses. Last, so
    # every more specific rule above gets first refusal.
    ("other_expenses",              r"rates?\s*(?:&|and)?\s*taxes?|"
                                    r"expense|expenditure|charges?\b|fees?\b|rent\b|"
                                    r"stationer|travel|mobile|internet|telephone|"
                                    r"printing|postage|audit|legal|profession|"
                                    r"miscellaneous|sundry|office|advertis|"
                                    # Same reasoning as depreciation's \bdep\b:
                                    # "Exp."/"Exp" is the everyday abbreviation
                                    # for "Expense" on a proprietor's T-account
                                    # ("TO CONVEYANCES EXP.", "TO GENERAL EXP"),
                                    # and the full word is already covered
                                    # above. Last in the whole table, so it
                                    # only ever catches what nothing more
                                    # specific already claimed.
                                    r"\bexp\.?\b|entertain|\bmedical\b|"
                                    r"tea\s*(?:&|and)?\s*snack"),
]

# ─────────────────────────────────────────────────────────────────
# ASSUMPTIONS
# ─────────────────────────────────────────────────────────────────
# Where a line does not map to the template one-for-one, a judgement has been
# made. Those judgements are defensible but they are NOT facts from the
# document, and an analyst signing the sheet is entitled to see every one of
# them. Each rule below records what was assumed and why.

CONVENTIONS = [
    (re.compile(r"^\[(?:INC|EXP)\].*\bopening\s+stock\b", re.I),
     "Opening stock added to Purchases (trading-account stock movement)"),
    (re.compile(r"^\[(?:INC|EXP)\].*\bclosing\s+stock\b", re.I),
     "Closing stock deducted from Purchases, not counted as income"),
    (re.compile(r"right[\s-]*of[\s-]*use", re.I),
     "Right-of-use (leased) assets grouped with Fixed Assets"),
    (re.compile(r"lease\s+liabilit", re.I),
     "Lease liabilities grouped with Unsecured loans - they carry no charge "
     "on assets"),
    (re.compile(r"income[\s-]*tax\s+asset", re.I),
     "Income-tax assets grouped with Deferred Tax Assets"),
    (re.compile(r"^\[NCA\].*financial\s+asset", re.I),
     "Non-current 'other financial assets' treated as Investments"),
    (re.compile(r"^\[CL\]", re.I),
     "Classified as a Current Liability from the statement's own section "
     "heading, not from the line's wording"),
    (re.compile(r"^\[CA\]", re.I),
     "Classified as a Current Asset from the statement's own section heading, "
     "not from the line's wording"),
]
