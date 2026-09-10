# Extraction shortcomings — case log and mitigations

A living record of concrete gaps found in `engine/`, each verified against the
**original PDF page** (never taken on a reference report's word alone — see
"Why ground-truth, not Perfios" below) and, where available, cross-checked
against a Perfios FSA report for the same borrower. Add a new dated case
below the template whenever a real filing exposes a new failure mode; don't
delete old ones even after they're fixed — note the fix and the date instead.

## Why ground-truth, not Perfios

A third-party report is a second opinion, not an oracle - it can be wrong,
mis-mapped, or (as here) contain broken formulas of its own. Every finding
below was confirmed by rendering the actual ITR page as an image and reading
the printed figures directly; Perfios is cited only as a second, independent
data point once the ground truth is already known from the source document.

---

## Case: FC6 / Borrower C (Borrower S), AY 2023-24 & 2024-25

Source: `ITR 2023-2024.pdf`, `ITR 2024-2025.pdf`. Reference: Perfios FSA report
(`FSA report/0_fsa_consolidated...xlsx`) — note its `P&L`/`Balance Sheet`/
`Ratios` sheets are formula-driven and were never recalculated before saving,
so their **totals cache as 0**; only the `Input Template` sheet's raw,
already-bucketed line items are usable as a comparison, not its totals.

### 1. FY2023-24 P&L — revenue correct, one expense line dropped by OCR

**Ground truth** (page 7 image): Revenue (Transportation charges received)
₹14,22,91,953; 28 expense lines; Net Profit ₹48,23,981; both sides total
₹14,22,91,953.

**What we got right:** Rule-based OCR (no Vision) harvested the revenue
figure exactly: ₹14,22,91,953, matching both the printed page and Perfios's
own `Input Template` row for the same year to the rupee.

**What needs improvement:** The expense side summed to only ₹12,15,51,652 —
short by ₹2,07,40,301. That gap is, to within ₹1 of rounding, exactly the
printed "To Driver Charges 2,07,40,302" line. OCR silently dropped that one
row. Because `financials.check_block` requires both sides to reconcile before
a block is trusted, the **entire block was marked FAILED** and the whole
year's P&L was reported as "Check ITR" — discarding the correct revenue
figure along with the one bad line.

**Mitigation:**
- Root cause is an OCR row-detection miss on one line among 28 visually
  similar rows (same font, same column). Re-running OCR at a higher DPI or
  re-attempting a second OCR pass with different page segmentation on blocks
  that fail by a single-line-sized margin is worth trying before falling back
  to Vision.
- Separately: `_looks_nil` / the FAILED-block path could report *which specific
  rows were captured* even when the block doesn't verify, instead of an
  all-or-nothing "Check ITR" — an analyst could visually confirm the one
  missing line in seconds rather than re-keying the whole statement by hand.

### 2. FY2023-24 P&L — Vision escalation is confirmed to hallucinate a self-consistent but wrong statement — FIXED 2026-09-08

**Ground truth:** as above — revenue ₹14,22,91,953, net profit ₹48,23,981.

**What Vision produced when re-reading the same page image:** revenue
₹15,84,68,556, expenses ₹29,50,92,692, derived profit **-₹13,66,24,136** — a
sign flip and two orders of magnitude off the real ₹48.24 lakh profit. The
block was marked **VERIFIED** because Vision's own two sides balanced each
other.

**This is the most serious finding.** `columns.py`'s stated safety argument —
"a hallucinated number does not balance, so it cannot reach the sheet" —
assumes a re-read only replaces one side against an *independently sourced*
total. It does not hold here: Vision is asked to produce both sides of the
statement (and the total) in one response, so a hallucinated pair of sides
that agree with each other sails through `check_block` untouched. The
existing `_profit_crosscheck` (comparing derived profit to the statement's
*own* printed result) would have caught this instantly — ₹48.24 lakh printed
vs -₹13.66 crore derived is not a rounding gap — but that check only runs on
the OCR path's `ignored` subtotal rows, not on a Vision-sourced block, because
Vision's JSON schema doesn't carry the statement's own printed "Net Profit"
line as a separate, ignored field the way the OCR harvest does.

**Fix applied** (`engine/columns.py`: `_vision_disagrees_with_ocr`, wired into
`_escalate`): comparing a Vision re-read against its OWN printed subtotal
(the originally-proposed mitigation) turned out to be self-referential —
Vision writes the subtotal and the line items in the same hallucinated
response, so asking it to also report the bottom line a second time doesn't
add independent evidence; a model that fabricates a consistent P&L fabricates
a consistent bottom line too. The only genuinely independent signal
available is the **original OCR attempt** - a different pipeline (RapidOCR's
geometry parse) reading the same pixels. Since a dropped row can only shrink
a side's sum, never inflate one, whichever side of the *original* OCR block
summed larger is trustworthy-ish and used as a reference; a Vision re-read is
now accepted only if it both (a) balances against itself AND (b) lands within
20% of that reference. Rejected re-reads fall back to the honest FAILED/
UNVERIFIED original instead of a confident wrong number.

Re-run against this exact filing after the fix: FY2023-24 P&L now reads
revenue ₹14,22,91,953 (exact) and profit ₹48,23,980 (off by ₹1 of rounding
from the printed ₹48,23,981) — both within a rupee of ground truth, up from
the previous -₹13,66,24,136. Balance sheet total_assets came out
₹9,20,75,896 against a true ₹9,20,75,895 — also within a rupee. See
`tests/test_vision_verification.py` for the regression tests (one reproduces
the exact hallucination that shipped, one guards that a genuine Vision
correction still gets accepted).

**New, smaller gap surfaced by this fix:** with the bad Vision block no
longer masking things, FY2023-24 now reports one unmapped line item of
₹6,01,78,819 - which is exactly the Secured Loans figure from the ground
truth page, just differently spaced (6,01,78,819 vs the digits 60178819).
Vision read the number correctly; its group-heading label for that section
didn't match anything in `template_config.SYNONYMS`, so it fell through to
`unmapped` rather than `secured_loan_asset_financed`. This is a taxonomy
synonym gap, not a wrong-number risk (the amount is visible and excluded, not
silently mis-bucketed) - worth a follow-up synonym rule, not urgent.

### 3. FY2023-24 Balance Sheet — OCR dropped the cash sub-schedule; Vision also wrong, differently — PARTIALLY FIXED 2026-09-08

**Ground truth** (page 8 image): Liabilities total ₹9,20,75,895 (Capital
₹1,45,75,929 + Secured Loans ₹6,01,78,819 + Current Liabilities
₹1,73,21,147); Assets total ₹9,20,75,895 (Fixed Assets ₹5,52,81,148 +
Investment ₹19,36,506 + Current Assets ₹3,29,53,179 + Other Current Assets
₹19,05,062), where Current Assets = Loans & Advances 1,66,02,131 + Sundry
Debtors 1,52,71,032 + Cash in Hand 4,41,330 + Cash at Bank 6,38,687.

**What we got right (both paths):** The Capital Account figure
(₹1,45,75,929, i.e. networth) came out **exactly correct** whether or not
Vision was used, and matches Perfios's own `Input Template` "Partner A"
capital row for this year to the rupee.

**What needs improvement — OCR path:** Assets summed to ₹9,09,95,879, short
by ₹10,80,016 — to the rupee, the sum of "Cash in Hand 4,41,330" + "Cash at
Bank 6,38,687" (₹10,80,017, off by ₹1 of rounding). Both lines sit under a
"(As per Schedule E)" sub-caption in a small two-line box distinct from the
main current-assets list — layout that the column-divider / section-heading
logic in `financials._harvest_at` evidently treats as detached from the
current-assets group above it. Because the statement prints no total on this
side that OCR could re-derive against, it was reported `unverified` rather
than outright `failed`, which is at least honestly labeled but still meant
the year's Balance Sheet did not enter the sheet.

**What needs improvement — Vision path:** total_assets ₹5,91,22,716 and
total_liabilities ₹3,18,97,075 — neither figure resembles the true
₹9,20,75,895 total on either side, and (unlike a real statement) the two
don't even equal each other once networth is added back
(₹3,18,97,075 + ₹1,45,75,929 = ₹4,64,73,004 ≠ ₹5,91,22,716). Vision reproduced
the single, salient, easy-to-read Capital line correctly but fabricated
different, mutually-inconsistent figures for the rest of the statement.

**Root cause, found by dumping the actual OCR word-boxes for this page**
(not guessed): RapidOCR merged two visually-close printed rows - "Cash in
Hand 4,41,330" and "Cash at Bank 6,38,687 [outer total] 3,29,53,179" - into
ONE detected row, and "Cash at Bank"'s amount landed in the SAME cell as
Cash in Hand's amount AND the group's own subtotal, concatenated with no
label text between them: one cell containing the text
`"4,41,330 6,38,687 3,29,53,179"`. `_AMOUNT_RE` anchors to the end of a
cell, so `parse_line` only ever saw the LAST number and treated everything
before it - two real rupee amounts - as the label. The result was worse than
simply dropping the line: a nonsense label like `"4,41,330 6,38,687"` entered
the harvest, carrying the group's OUTER SUBTOTAL (Rs 3,29,53,179) as if it
were that garbage label's own amount.

**Fix applied** (`financials.parse_line`): a real label always contains at
least one letter; when the text before the matched trailing amount has none,
it's leftover digits from an earlier amount in the same OCR-merged cell, not
a label, so the cell is now rejected outright rather than harvested with a
garbage label and the wrong figure. See
`test_parse_line_rejects_a_cell_of_only_concatenated_amounts` in
`tests/test_units.py`.

**What this does NOT fix, and is still open:** rejecting the cell stops the
wrong-value-under-a-garbage-label failure mode, but it does not recover
"Cash in Hand" (Rs 4,41,330) or "Cash at Bank" (Rs 6,38,687) as correctly
labelled, correctly valued line items - that Balance Sheet still won't
verify. Doing that properly needs either a fix at the row-clustering level
in `layout.py` (so the two printed rows are never merged in the first place)
or a smarter split of a same-cell amount-run into separate candidate columns
- attempted and abandoned this session because a same-cell proportional
split produces touching (zero-gap) sub-spans that `_cluster_columns` merges
straight back together, so it can't actually feed `_pick_columns`' own
combinatorial search the separate columns it would need to pick correctly.
A genuine fix belongs in the OCR row-segmentation step, not here.
- Same Vision-agreement fix as Case 2 applies to Balance Sheets too, and is
  already in place (`_vision_disagrees_with_ocr` is not P&L-specific) - see
  the FY2023-24 total_assets result (₹9,20,75,896 vs a true ₹9,20,75,895)
  in Case 2's write-up above, which is this same page.

### 4. FY2024-25 P&L — good match, small unexplained gap already flagged

**What we got right:** Revenue ₹14,30,70,909 vs Perfios's reference
₹14,30,42,459 for the same year — a ₹28,450 gap on a ₹14.3 crore figure
(0.02%). This is the one block in this bundle that verified cleanly on OCR
alone, no Vision needed.

**What needs improvement:** The engine's own `_profit_crosscheck` already
flags a ₹56,900 gap between the derived profit (₹51,02,007) and the
statement's own printed result (₹50,45,107) — a real, acknowledged residual
error, most likely one misread income or expense line hiding inside an
otherwise-balancing statement (the classic case this cross-check exists to
catch — it just hasn't been chased down to the specific line yet).

**Mitigation:** Not yet investigated — next step is the same ground-truth
image check applied to Cases 1-3, to find which specific line accounts for
the ₹56,900 and ₹28,450 gaps.

### 5. Entity name resolution degrades without Vision — FIXED 2026-09-08

**What we got right:** With Vision on, the borrower resolves correctly to
"Borrower C" for both years.

**What needs improvement:** Without Vision, `_main_entity`/`_belongs_to`
picked only the parenthetical "(Proprietor : Borrower S)" line rather
than the business name printed above it on the same block. `_find_entity`'s
lookback presumably hit the proprietor line first and `_belongs_to` accepted
it (it passes the capitalisation/word-count heuristic on its own), never
trying the line above it.

**Fix applied** (`financials._find_entity`, `_PROPRIETOR_LINE_RE`): a line
matching `(Proprietor|Partner|Owner) : ...` is no longer accepted on sight -
it's kept as a fallback while the lookback continues past it, so a genuine
business name one line further up wins when one exists. Confirmed against
this exact filing: the OCR-only (no Vision) run now resolves to "Borrower C" instead of "(Proprietor : Borrower S)". See
`test_find_entity_prefers_business_name_over_proprietor_line` and
`test_find_entity_falls_back_to_proprietor_line_when_nothing_else` in
`tests/test_units.py`.

---

### 6. FY2024-25 Balance Sheet — misread as a vertical statement, hiding a working T-account reading — FIXED 2026-09-08

**Ground truth:** the page prints a plain two-column T-account (Liabilities |
Assets) with a clear closing `TOTAL 11,13,07,366` on both sides - no
Schedule III period-column header anywhere on it.

**What was happening:** `_orientation`'s primary signal (period-column
headers) correctly found none, so it fell through to its weaker geometry
heuristic - which guessed VERTICAL. The vertical harvester, given a page
with no real per-period columns, concatenated fragments from BOTH sides of
the account into one meaningless merged section
(`"Sundry Creditors (As per Schedule E)"`, an impossible label mixing a
liabilities-side name with an assets-side schedule reference) with nothing
checkable. Meanwhile a completely separate, correct T-account reading of the
exact same rows existed - left ₹11,13,07,365, right ₹11,31,44,244, off by
only ₹18,36,879 (one duplicated Other-Current-Assets subtotal) against a
₹11,13,07,366 target - but `_harvest` discarded it because neither reading
had verified and the orientation guess (vertical) was returned as the
default fallback regardless of which candidate was actually closer to
correct. The result: `check_block` saw an empty `right` side and reported
"prints no total, so its figures could not be checked" - masking a
near-miss T-account reading that was one duplicate line away from
reconciling outright.

**Fix applied** (`financials._prefer_t_account_over_empty_vertical`, wired
into `_harvest`): when the vertical candidate has literally nothing
checkable (no section prints a total over 2+ items) and the T-account
candidate has real data on both sides, the T-account reading is preferred as
the fallback - not because T-account is assumed correct, but because a
vertical guess with zero checkable structure can never be a better answer
than one that is. This doesn't force a verify; it just stops discarding the
better of two unverified readings. See
`test_prefers_t_account_when_vertical_found_nothing_checkable` and its
companions in `tests/test_units.py`.

**Result after the fix:** this page now correctly FAILS with a precise,
actionable reason ("left side sums to 111,307,365 against 113,144,244 on the
other side, short by 1,836,879") instead of a useless "no total" - a genuine
improvement even though this specific page still doesn't verify (the
duplicated-subtotal issue itself, the same nested-outer-total pattern as
Case 3, is unfixed - see below). But this filing carries the identical
Balance Sheet TWICE (this page, and an unstamped earlier copy at page 8 whose
OCR read cleanly without the corruption that caused the duplicate on this
page) - `_dedupe` already picks the verified copy over the failed one, so
the column's final figures now come from the clean copy: total_assets
₹11,13,07,367 and total_liabilities ₹11,13,07,365 (both within ₹2 of the true
₹11,13,07,366) and networth ₹1,72,38,637 (exact). FY2024-25's Balance Sheet
went from `total_assets: 0, networth: 0` to matching ground truth almost to
the rupee.

**Still open:** the ₹18,36,879 duplicated-subtotal shortfall on the OCR'd
copy is the same "amount-run in one cell, no gap for `_cluster_columns` to
separate on" pattern documented in Case 3, and remains unfixed for the same
reason - it needs a fix at the OCR row-segmentation level, not a safe
post-hoc parser patch.

## Open items (not yet root-caused)

- The ₹56,900 profit gap in FY2024-25 (Case 4) and one unclassified
  ₹60,17,8819 line item in the FY2023-24 Vision read have not been traced to
  a specific source line yet.
- A `profit_loss`-kind block on FY2023-24 page 5, entity misread as
  "Borrower S," was not investigated - likely a capital
  account or a different Borrower S family entity in the same bundle;
  needs a ground-truth check before drawing any conclusion.
- The same-cell amount-run pattern behind Case 3 and Case 6's residual
  shortfall (OCR concatenating multiple printed amounts into one cell with
  no usable gap between them) needs a fix in `layout.py`'s row/cell
  reconstruction, not in `financials.py` - flagged twice now on two
  different pages of the same bundle, so it is worth prioritising.

---

## Case: FC4 / Borrower A (the proprietor), AY 2023-24 to 2025-26

Source: `ITR 2023-2024.pdf`, `ITR 2024-2025.pdf`, `ITR 2025-2026.pdf` (a mix of
scanned and digital-native PDFs in the same bundle set). Reference: Perfios
FSA report (same broken-formula caveat as the FC6 case - only `Input
Template`'s raw rows are usable, not its totals).

### 7. Entity resolution collapsed on "PROP." (abbreviated) — FIXED 2026-09-08

The FC6 fix only recognised the spelled-out "Proprietor :" wording. This
filing's own CA firm writes "PROP. the proprietor" - same shape,
abbreviated. `_PROPRIETOR_LINE_RE` now matches `prop` as well as
`proprietors?`/`partners?`/`owners?`. See
`test_find_entity_recognises_prop_abbreviation`.

### 8. A business's own name defeated the address-line filter — FIXED 2026-09-08

Fixing Case 7 immediately exposed a second, compounding bug: `_find_entity`
still didn't return "Borrower A", because `_ADDRESS_RE` (meant to
skip a genuine street-address line between the entity and the heading)
matches the word "road" - and this business's own name contains it. A
generic address keyword is not enough evidence on its own: a real address
line also carries a house/shop/plot number, a pincode, or comma-separated
locality parts, none of which a bare business name has. `_find_entity` now
requires one of those alongside the keyword before treating a line as an
address. See `test_find_entity_not_fooled_by_a_road_in_the_business_name`.

### 9. A sideways-scanned page makes an entire statement invisible — OPEN, not fixed

FY2023-24's Balance Sheet page is a genuine, correctly-drawn statement (its
own printed TOTAL balances, confirmed by eye) - but the underlying scanned
image is rotated 90° with **no PDF-level rotation flag** (`page.rotation`
reads `0`; the image bytes themselves are sideways). `_render_pil` renders
the page as-is, so every line of text comes back with its words in the wrong
reading order relative to each other, and `_match_title` never recognises
the heading at all - the whole statement is silently invisible to
`find_blocks`, not merely unverified. This is a different, more fundamental
class of gap than anything fixed so far: everything upstream of orientation
detection assumes upright text.

**Not attempted this session** - the safe fix (auto-detect a sideways scan
and re-render at 90°/270° before OCR, e.g. by trying multiple rotations and
keeping whichever yields the most title/keyword matches, similar in spirit
to Tesseract's own OSD) touches the shared rendering path both the scanned
and digital pipelines depend on, and deserves its own test corpus of rotated
pages before changing it blindly.

### 10. "Interest on Vehicle Loan" was bucketed as the loan itself, not the interest — FIXED 2026-09-08

**Ground truth:** FY2023-24's P&L prints "TO INTEREST ON VEHICLE LOAN
4,324,133" as an ordinary finance-cost expense line, the fourth-largest on
the page. The statement reconciled perfectly (revenue, expenses and Net
Profit all matched Perfios to the rupee) - so this was never visible as an
arithmetic failure, only as the tool's own `_profit_crosscheck` warning
flagging a Rs 43,24,133 gap between derived and printed profit, on a
statement that otherwise looked clean.

**Root cause:** `template_config.SYNONYMS`' `secured_loan_asset_financed`
rule matches `vehicle\s+loan` (meant for a genuine loan-PRINCIPAL balance)
and is checked before `interest_finance`. "Interest on Vehicle Loan" matches
that pattern purely on the overlapping words, moving the entire expense line
into a phantom Secured Loan balance-sheet bucket: expenses were understated
by Rs 43.24 lakh (profit overstated by the same amount), and a liability
that was never actually on this statement got fabricated in `buckets`.
Confirmed by direct call: `T.map_label("Interest on Vehicle Loan")` returned
`secured_loan_asset_financed`, not `interest_finance`.

**Fix applied**: a new, more specific `interest_finance` rule
(`interest\s+(?:paid\s+)?on\s+\w[\w\s]*?\bloan`) placed first in `SYNONYMS`,
same idiom as the file's very first rule (which fixes the identical
class of bug on the income side, for "Interest On FD"). See
`test_interest_on_a_loan_is_a_finance_expense_not_the_loan_itself`. Re-run
after the fix: FY2023-24 profit_before_tax reads exactly Rs 30,38,443,
matching both the printed Net Profit and Perfios to the rupee.

### 11. A leading minus sign (no brackets) silently flipped a loss into a profit — FIXED 2026-09-08

**Ground truth:** FY2024-25's P&L prints "TO NET PROFIT -4,391,651" - a
loss, written with a plain leading hyphen, not the bracket convention
("(4,391,651)") every other negative figure in this codebase's test corpus
uses.

**Root cause:** `_is_negative` only ever checked for `"(" ... ")"` in the
cell. `_AMOUNT_RE` itself has no leading-sign handling either (Indian
accounts overwhelmingly use brackets), so the "-" sat outside the matched
amount entirely and was silently discarded - the loss was read as a
**positive** profit of the same magnitude. Exactly the sign-flip failure
mode `taxonomy.py`'s own Extraordinary Item note already documents for a
different root cause ("flipped a Rs 15.32 lakh loss into a Rs 15.32 lakh
profit") - this is the same failure shape via a formatting variant nobody
had hit yet.

**Fix applied**: `_is_negative` now also accepts a `prefix` (the label text
up to the amount match) and treats a bare trailing "-" in that prefix as a
sign - deliberately keyed to the text immediately BEFORE the number, not
the whole cell, so a hyphen inside a label ("Short - Term Borrowings") is
never mistaken for one. See
`test_parse_line_recognises_a_leading_minus_sign_as_negative`.

### 12. An amount with no thousands separators at all was invisible to the harvester — FIXED 2026-09-08

**Ground truth:** the same FY2024-25 P&L's credit-side total is printed as a
bare `145644604` - no commas, no decimal - while every other figure on the
same page (including the footer TOTAL row) is properly comma-grouped.

**Root cause:** `_AMOUNT_RE` requires comma-grouping, a decimal/paise
component, or an exact 3-digit bare integer (deliberately narrow, to avoid
mistaking a reference number embedded in a label for an amount - see the
regex's own long comment). A 9-digit bare number matches none of those
alternatives, so the entire income line was dropped from the harvest. Fixing
Case 11 actually made this WORSE to detect: with the left (expense) side
now correctly summing to the true total on its own, `check_block`'s
single-column fallback path quietly marked the block "verified" with the
income side sitting **completely empty** - a coincidental pass masking a
real gap, the same shape of danger as Case 6's vertical-fallback bug.

**Fix applied** (`_BARE_LONG_INT_RE`, used in `_harvest_at`'s bare-amount
branch): a cell that is NOTHING BUT digits (the *entire* cell, not a
trailing suffix within a longer string) is now accepted as an amount even
without separators. This is deliberately narrower than loosening `_AMOUNT_RE`
itself: a whole-cell bare number carries none of the reference-number risk a
trailing suffix inside a label does. See
`test_bare_unformatted_amount_is_still_harvested`. Re-run after the fix:
FY2024-25 now reads sales_other_income Rs 14,56,44,604 and profit_before_tax
-Rs 43,91,651, both exact matches to the printed statement.

**Side effect worth noting, not a regression:** this fix makes `find_blocks`
attempt a couple of pages it previously ignored outright (computation-sheet
pages that happen to contain enough bare numbers to pass `_match_title`'s
loose heading check). Both showed up as new blocks that correctly **FAILED**
reconciliation - the arithmetic gate is doing exactly its job, rejecting a
non-statement page rather than shipping it - but it's a couple of extra rows
of audit-trail noise per such bundle worth being aware of.

---

## Case: Borrower G, AY 2023-24 to 2025-26

Source: three ITR PDFs, a genuine unrelated real-world bundle (not one of the
FC4/FC6/Full-case-1 filings used above), reported directly by the user as
"one of the cleanest ITRs" they'd seen - and it failed on all three years.

### 13. A digital ITR with scanned financial statements embedded as images was invisible past a whole-document scan/digital decision — FIXED 2026-09-08

**What was happening:** `parser._extract` decides ONCE, for the entire
document, whether to treat it as digital (read embedded text) or scanned
(OCR every page) - based on total character count summed across all pages.
AY 2024-25's bundle had 7 pages of genuine digital text (the computation
sheet) and 13 pages that were **pure images** (`page.get_text()` returns
`''`, confirmed directly) - the CA's signed Balance Sheet and P&L, scanned
and inserted as raster pages into an otherwise digitally-typed PDF. Because
the computation pages alone cleared the whole-document threshold, the entire
document was routed through the digital path and OCR never ran on ANY
page - not even the ones that were nothing but pictures. With the real
statements completely invisible, the tool concluded **"NIL return"**: a
confident, specific-sounding, and completely wrong answer, worse than an
honest failure because it gives no indication anything was missed. AY
2025-26 (a 34-page bundle, same company) lost its Balance Sheet and P&L the
same way and reported "no usable financial statements were found."

This is a materially different class of bug from every other case in this
document: not a parsing edge case inside `financials.py`, but a gap in the
page-routing decision that runs BEFORE any of that logic gets a chance to
see the page at all. A mixed bundle - digitally-typed ITR wrapper plus a
scanned, signed financial-statement attachment - is not a rare shape; it is
arguably the default shape for how a CA actually produces one of these
filings.

**Fix applied** (`parser._extract`, `ocr_extractor.ocr_pages`): the
whole-document check still decides fast-vs-OCR for a genuinely
all-digital or all-scanned document unchanged, but when the document clears
the digital threshold OVERALL, each page's own text is now checked
individually - any page whose digital text comes back essentially empty
(`_PAGE_BLANK_THRESHOLD`) is OCR'd on its own via the new `ocr_pages`
(a single-pass, specific-indices sibling of `ocr_document`, which exists
because the caller already knows exactly which few pages need it - the
cheap classify pre-pass that `ocr_document` uses for a wholly scanned
document doesn't apply here). Every other page keeps its free, exact digital
read. See `tests/test_mixed_document.py`.

**Result after the fix, same three real PDFs:** AY 2024-25 went from "NIL
return" to a fully VERIFIED Balance Sheet and P&L. AY 2025-26 went from "no
usable financial statements" to fully VERIFIED Balance Sheet and P&L, with
the derived Profit After Tax (Rs 16,72,513) landing on the exact figure the
ITR's own tax-computation page separately states as "Net profit after tax" -
strong independent confirmation the fix reads correctly, not just
plausibly.

**Still open, different root cause:** AY 2023-24 for the same company still
fails. That document was ALREADY being OCR'd in full before this fix (it's a
genuinely poor-quality scan - watermarked "Scanned with OKEN Scanner,"
consistent with a mobile photo-scan) - `relevance.classify_page` is
misclassifying the real statement pages (9-22, each one carrying the
company's name and CIN) as "noise," almost certainly because OCR quality on
this specific scan is degrading the heading text below what the keyword
classifier recognises. This needs its own investigation into
`relevance.py`'s classification against a badly-OCR'd heading, not a
continuation of this fix.

### 14. Comparative-column data was harvested then thrown away — NEW CAPABILITY, added 2026-09-09

Every Schedule III (vertical) statement prints the prior year's own grand
totals right next to this year's, as its comparative column - a free second
reading of last year that `_harvest_vertical` was reading and discarding on
every single document, on the explicit reasoning "those are last year's
numbers" (correct, but not a reason to throw them away).

**Confirmed valuable, not just theoretical:** AY2023-24's Balance Sheet and
P&L (Borrower G) were too badly scanned to verify on
their own (Cases 9/13 above). AY2024-25's own statements printed AY2023-24's
grand totals as their comparative column, and reading those instead recovers
figures matching a manual read of the original page almost to the rupee:
Total Income 70,003 (manual read: ~70,000), Total Expenses 1,132,360
(~1,132,000), Net Loss 1,062,357 (~1,062,000), Total Assets 19,880,660
(~19,881,000) - the small gaps are the original statement's own printed
'000-rounding, not extraction error.

**The one real trap, also confirmed on this same filing**: individual
line-item buckets are NOT safe to recover this way. AY2024-25's comparative
column shows Long-term Borrowings ₹1,29,97,392 against AY2023-24's own
filing stating ₹33,69,000 - not an error on either side, but a NEW auditor
(a lender & Co replacing K V B & Associates) legitimately reclassifying
prior-year Long-term/Short-term Borrowings between categories when
restating the comparative. The grand total held (both sides sum to the
same ~₹1.3 crore); the sub-classification did not.

**What was built** (`financials._harvest_vertical`, `find_blocks`,
`columns.spread_document`, `columns._recover_from_comparative`):

1. `_harvest_vertical` now captures the comparative column's value at each
  of four specific, already-checked anchor rows only - Total Revenue/Income,
  Total Expenses, Total Assets, Total Equity and Liabilities - the exact
  same anchors `check_block`'s own BS/P&L grand-total checks already key on.
  Never captured for ordinary line items.
2. `find_blocks` propagates this as `block["comparative"] = {"year": Y-1,
  "totals": {...}}`, scaled into rupees the same as everything else on the
  page.
3. `spread_document` only keeps a block's comparative reading when that
  block's OWN current-period reading VERIFIED - a page whose current-year
  OCR is already suspect doesn't get to donate a trusted comparative
  reading either.
4. `columns._recover_from_comparative`, run once across a whole bundle
  after every column is built: for a column whose own P&L or Balance Sheet
  is entirely unread, look at the FOLLOWING year's verified comparative
  totals for THIS year and use them - Total Revenue/Expenses (deriving
  Profit Before Tax as their difference) or Total Assets only. Every
  recovered figure adds both a `warnings` entry and an `assumptions` note
  naming exactly what was recovered and from where; Profit After Tax,
  individual liability/asset buckets, and Net Worth are deliberately never
  recovered this way and stay `None` ("Check ITR").

See `tests/test_units.py` (`test_harvest_vertical_captures_comparative_grand_totals`
and neighbours) and `tests/test_vision_verification.py` (`_recover_from_comparative`
tests, including the different-entity and don't-overwrite-real-figures guards).

## Template for new cases

```
### Case: <borrower/file>, <period>

**Ground truth:** <what the actual page says, with a page image reference>
**What we got right:** ...
**What needs improvement:** ...
**Mitigation:** ...
```
