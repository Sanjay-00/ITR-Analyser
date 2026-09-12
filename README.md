# ITR Extractor

ITR filing bundles → the **ITR Validation** spreading sheet: Profit & Loss,
Balance Sheet, ratios and Financial Snap, one column per financial year, in
lakhs.

Companion to the AutoCAM CIBIL project. Same shape (rule-based first, OCR for
scans, LLM only as a gated fallback, formatted Excel out), applied to the
income side of a loan file.

## Running

```bash
run_app.bat                                    # Windows
.itr\Scripts\python.exe -m streamlit run app.py
```

Upload one PDF per financial year. An optional `GEMINI_API_KEY` in `.env` (or
Streamlit Secrets) enables the label-classification fallback; extraction itself
never needs it. Scanned PDFs need Tesseract (`packages.txt` covers Streamlit
Cloud; a standard Windows install is auto-detected, or set `TESSERACT_CMD`).

## The design, in one page

**Don't recognise ITR formats.** Every CA package lays these out differently and
chasing layouts is a treadmill. The problem splits somewhere better: *layout and
structure are universal; only the wording varies.*

**1 — Structural harvest** (`engine/extract/financials.py`, `engine/ingest/layout.py`). A financial statement
is always a titled block of `label … amount` pairs that balances. Two families
appear — the T-account (proprietor/partnership: liabilities left, assets right)
and the vertical Schedule III statement (company: labels down the left, one
column per period). Both are read from word-box geometry, never from
`page.get_text()`, whose reading order interleaves the two sides of a T-account
and welds one side's amount onto the other's label.

**2 — Arithmetic proof** (`financials.check_block`). Both sides must agree with
each other and with the printed total; a vertical statement's sections must each
reproduce their own TOTAL. This is a proof, not a heuristic, and it is the thing
the CIBIL project never had an equivalent of. Three states are distinguished:
`verified`, `unverified` (the statement printed no total — unproven, not wrong)
and `failed`.

The same identity does more than check. Where the layout is ambiguous — which
amount column belongs to the account, where the two sides divide, whether a page
is a T-account or a vertical statement — every candidate reading is scored
against the statement's own totals and the one that reconciles wins. **The
arithmetic selects the extraction, not just validates it.**

**3 — Label mapping** (`engine/mapping/taxonomy.py`, rules in
`engine/mapping/template_config.py`). `Pagar Bhatta Exp.` → Employee Costs.
This is short-string classification, not layout parsing, so: a synonym table
handles the recurring 80% deterministically; Gemini sees *only the leftover
label strings* — no figures, no images, no document — and buckets them into the
fixed template; every answer is cached to `data/label_map.json` so the same
wording is free next time. Mapped buckets must still sum to the harvested
total, so a dropped, duplicated or invented label is caught.

**Adding a new ITR format costs zero code** — at most a few synonym rows in
`template_config.py`, which holds every "what goes where" rule in one place:
the sheet's row schema, the label→bucket synonyms, and the judgement-call
notes shown in the Audit Trail.

**Independent cross-check.** The computation of income restates the P&L's
bottom line ("Net Profit as per P&L a/c ..."), typed separately and usually on
a digital page. `itr_parser.extract_book_profit` reads it and a signed
mismatch with the derived profit is warned about - a second source that a
Vision re-read of the same image can never be.

**5 — Analysis** (`engine/analysis.py`). Year-on-year growth, revenue CAGR,
and explainable red/amber flags (losses, eroded net worth, TOL/TNW, liquidity,
interest cover, falling turnover, margin compression, debt outpacing
turnover, profit contradicting the tax computation). Thresholds live in one
`THRESHOLDS` dict. An unread year is reported as not assessable, never as 0.

**4 — Page relevance** (`engine/ingest/relevance.py`). A real bundle ran to 101
pages, of which 19 carried anything needed. Audit clauses, TDS listings and
schedules are dropped before any expensive work.

## Output: the ITR Validation workbook

Sheet 1 is always the same **combined master format** - the union of the
analysts' two reference layouts (`Format.xlsx` and `Validation Format-
check.xlsx`), agreed 2026-09-12:

- **P&L:** Sales, Gross Receipts; Purchases, Transport, Electricity, Employee,
  Other Expenses, Interest, Depreciation, Extra Ordinary; Gross Expenses; PBT;
  tax and deferred tax (entered **positive and subtracted**); PAT; Cash Profit.
- **Balance Sheet:** the ten liability rows plus **Sundry Creditors**, the
  seven asset rows plus **Debtors / Receivables**, with totals.
- **Ratios:** ROCE, ROE, PAT/Income, PAT/Assets, LT Debt/Equity and TOL/TNW
  (both inclusive of quasi-equity), Interest Coverage, Current Ratio, DSCR,
  **Debtor Days, Creditor Days**.
- **DSCR Calculation** (the analyst keys the EMIs) and the **Financial Snap**.

Figures read from the accounts are written as plain lakhs; every total,
ratio, DSCR and Snap figure is a **live Excel formula**, defined once in
`engine/mapping/template_config.py`. A row the borrower does not use shows 0.
A row that could not be *read* also shows 0 so the formulas keep working, but
is **filled red with a note** - "no income" and "income not read" must never
look alike. Column headings say *Audited* or *Provisional* (detected from
"PROV." headings). Sheets 2 and 3 are the Analysis and the Audit Trail.

`tests/test_master_excel.py` evaluates the workbook's own formulas
(`tests/xlsx_eval.py`) and checks them against the Python ratios.

## Layout

```
app.py                          Streamlit UI - upload, Extract button, results, Excel download
engine/
  __init__.py                   Public API re-exported for app.py: spread, generate_excel, ...
  parser.py                     Document I/O, OCR dispatch, Gemini model cascade, debug harness
  columns.py                     Pipeline: pages -> statements -> verified blocks -> template rows
  analysis.py                   Year-on-year trends and red/amber flags over the finished columns
  excel_generator.py            The ITR Validation workbook + Analysis + Audit Trail sheets
  ingest/                       PDF pages -> structured text
    layout.py                   Word boxes -> rows with column boundaries (OCR + digital, shared)
    ocr_extractor.py            Scanned-PDF OCR (parallel Tesseract) + Gemini Vision fallback
    relevance.py                Which pages are worth reading
  extract/                      What a document says, unverified vs verified
    itr_parser.py                Indian-number parsing (to_int) + identity extraction (PAN/AY/...)
    financials.py                Locate, harvest and VERIFY statements. The core.
  mapping/                      What a harvested line MEANS
    template_config.py           Row schema, label->bucket SYNONYMS, judgement-call notes
    taxonomy.py                   Applies template_config's rules; derived rows and ratios
data/
  label_map.json                 Learned label->bucket cache (label text only, no figures/PII)
tests/
  test_units.py                  Pure-function tests, always run
  test_itr_regression.py         Internal-consistency checks against ITR_TEST_DIR
```

Each subpackage's own `__init__.py` explains its role; `app.py` only ever
imports from the top-level `engine` package, never reaches into a subpackage
directly - that boundary is what lets the internals move again without
touching the UI.

## Conventions

- **`None` vs `0`.** `None` means unreadable; `0` means the document printed a
  zero. Never conflate them.
- **Subtotals are never inputs.** `Total Revenue`, `Profit before Tax` and
  friends restate figures already captured; they verify the harvest and are
  excluded from it (`taxonomy.IGNORE`). Two `Profit before … Extraordinary
  Items` lines once mapped to Extraordinary Item and flipped a ₹15.32 lakh loss
  into a ₹15.32 lakh profit.
- **Restatements are deduped.** Notes pages reprint the statement's figures;
  keeping both doubles real balances.
- **Never commit real ITRs** — PAN, Aadhaar, addresses, bank accounts. Samples
  live outside the repo, behind `ITR_TEST_DIR`.

## Inspecting a document

```bash
.itr\Scripts\python.exe -m engine.parser <file.pdf> [<file.pdf> ...]
```

Runs the real pipeline (spread → verify → map) over one or more PDFs and
prints each column's entity, statement statuses and key figures - the same
path the app uses, without the UI.

## Testing

```bash
.itr\Scripts\python.exe -m pytest tests/                     # always runs
ITR_TEST_DIR="C:\path\to\samples" .itr\Scripts\python.exe -m pytest tests/
```

`tests/test_units.py` pins the behaviours real filings broke, with the failure
each one prevents recorded in the test. `tests/test_itr_regression.py` runs
against real bundles in an external folder and skips cleanly without it.

## Status

Verified end to end against real filings: the Borrower K bundles reproduce
the analyst's own spreading sheet line for line (FY2024 with zero warnings), and
the the CA bundle's three statements all reconcile.

**Golden suite** (16 borrowers with an analyst reference sheet, no Vision,
DSCR rows excluded). **Correction 2026-09-11:** the suite picked the first
.xlsx in each folder, which for Borrower H, Borrower O and Borrower P was a Perfios export or
a dedupe sheet - zero cells compared, so those three "passed" vacuously. With
the analyst's ITR Validation sheet selected, **1,565 cells are compared and
798 differ (about 49% cell accuracy); 2 borrowers match fully** (Borrower A,
Borrower L). Borrower H 94/133, Borrower O 108/108, Borrower P 150/158 differ - mostly unread
("Check ITR"), not wrong. On the 13 borrowers that were compared all along:
mismatched rows 827 -> 682 -> 446, no borrower worse.

**After the switch to the combined master format (2026-09-12):** 773 of
1,538 compared cells differ (**~50% cell accuracy**, from ~49%); Borrower A
and Borrower L match fully, Borrower K is off by 1 row; no borrower got worse and eight
improved slightly (Borrower Q 36->32, Borrower N 113->104, Borrower P 150->146, ...).
Digital/Tally statements: 95-100%. Poor scans and messy multi-file folders
remain the gap - see docs/SHORTCOMINGS.md.
About 14 of the drop are analyst conventions recorded as known divergences
(Borrower B's EDFS loan as CC/OD, by decision; Borrower A's placement of
hiring costs and Diwali expenses), not reading fixes. Biggest moves: Borrower A 62 -> 0, Borrower L 76 -> 0, Borrower E 56 -> 11, Borrower F 76 -> 27, Borrower J 40 -> 14.
Still weak: Borrower N (malformed source), Borrower R (dropped scan lines),
Borrower G (only one year's file), and statements whose expense breakdown lives
in the schedule pages (Borrower I, Borrower E). See docs/SHORTCOMINGS.md 15-24.

Known gaps:
- Poor scans (the Borrower R bundle) lose digits to OCR; blocks correctly fail
  their balance check rather than shipping wrong numbers, but Vision escalation
  for those blocks is not wired yet.
- Sub-classification within a matching total can differ from the analyst's
  convention (e.g. short-term borrowings placed under Secured Loans rather than
  Current Liabilities). Totals agree; the split is a one-line synonym change.
