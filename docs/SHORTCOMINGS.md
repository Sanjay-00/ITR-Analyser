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
  shortfall - **root cause fixed 2026-09-10 in `layout.rows_with_cells`**
  (see Case 21, item 5): the OCR engine returned the amounts as separate
  boxes; our row clustering fused them. Cases 3 and 6 should be re-checked
  against their source PDFs, which are not in the current sample set.

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

---

## Review: 2026-09-10 — checks that could not see their own failure, and the Tally layout

A whole-codebase review against the full sample corpus (16 borrowers with an
analyst reference workbook). Baseline before any change: **3 of 16 borrowers
matched their reference; 856 mismatched rows in total** (no Vision).

### 15. The profit cross-check compared magnitudes only — FIXED 2026-09-10

`_profit_crosscheck` measured `abs(abs(stated) - abs(derived))`, so a loss read
as a profit of the same size (exactly Case 11) produced a zero gap and no
warning. Now also compares the SIGN, using the result line's wording ("Net
Loss" vs "Net Profit") since a T-account prints its closing line as a positive
figure either way (`taxonomy.stated_is_loss`).

### 16. A one-sided P&L could still VERIFY — FIXED 2026-09-10

Case 12 fixed the regex that dropped an income line, not the check that let
the block pass: a T-account's debit side (expenses + closing profit) sums to
the printed total on its own, so a P&L with an EMPTY credit side verified with
income silently zero. `check_block` now refuses a single-sided P&L that holds
no income-looking line.

### 17. The learned label cache stored account numbers and a wrong-by-position mapping — FIXED 2026-09-10

`data/label_map.json` held `"axis bank a c 17194"`, `"axis bank a c 82847"`,
a proprietor's name, and `"inc by closing stock" -> current_assets` (a P&L
credit filed as a balance-sheet asset). The cache filter now refuses any
label with a 4+ digit run and any `[INC]`/`[EXP]` line mapped to a
balance-sheet bucket - on write AND on read, so a stale copy can't reintroduce
them. Existing bad entries removed.

### 18. Independent profit cross-check from the computation of income — NEW 2026-09-10

The computation sheet restates the P&L's bottom line ("Net Profit / (Loss) as
per profit & loss A/c (15,32,419)"), typed separately and usually digital - a
genuinely independent reading, unlike a Vision re-read of the same image
(the gap Case 2 kept running into). `itr_parser.extract_book_profit` reads it;
`columns._book_profit_crosscheck` warns on a signed mismatch. Found on 5 of the
digital sample bundles, matching the known figures (Borrower K 24-25's -15.32 lakh).

### 19. Sideways scans — FIXED 2026-09-10 (Case 9)

Cheap-pass OCR now retries a page at 90/180/270 degrees when it reads almost
no known words, and later full-quality renders use the winning angle
(`ocr_extractor._best_rotation`). First attempt scored by letter-runs and was
**verified not to work on real pages**: Tesseract turns sideways text into
equally "wordy" junk (115 runs sideways vs 108 upright). Scoring by known
accounting vocabulary separates cleanly (42-128 upright, at most 5 sideways).

### 20. Tally-exported statements read as nothing at all — FIXED 2026-09-10

**Borrower:** Borrower L / Borrower L, AY 2024-25 and 2025-26, both
digital. **Before:** "No usable financial statements were found" for both
years - 78 mismatched rows - while the computation sheet plainly stated profit.

Tally is the default package for small Indian businesses, so this layout is
likely common across the book. Five separate faults:

1. **Vehicle registrations are not "label text".** Every label test required a
   three-letter run; "MH-12-PQ-9115" has none, so a transport company's whole
   vehicle schedule was invisible and each vehicle's amount was handed to the
   liability label on the same row. (`financials._has_label_text`)
2. **Group totals hide the breakdown.** Tally prints "Indirect Expenses
   1,14,61,430" in the outer column with its ledgers (Depreciation 81.4 lakh,
   Bank Interest 32.7 lakh) indented beneath. The outer column is what
   reconciles, so depreciation and interest read as zero. A chosen group total
   is now replaced by the ledgers that follow it **only when they sum to it
   exactly** (`_expand_groups`), so the balance proof is unchanged.
3. **One "Total" closes only the P&L half of a combined Trading + P&L
   account**, and Tally offsets the Trading half's own closing figures by a row,
   so `_harvest_segmented` can't split it. Now the two sides are made to agree
   with each other across the whole account, accepted only when a matching
   "Gross Profit c/o" / "b/f" pair is present (`_harvest_combined`,
   `_pick_joint`). The first version picked a trivial reading (the Trading
   half's two stray closing figures balancing each other) - hence the
   transfer-pair requirement inside the search, not after it.
4. **"Nett Profit"** (Tally's spelling) wasn't recognised as the result line.
5. **"Loans (Liability)"** didn't end the capital section, so Secured Loans
   were tagged equity.

Also found on the way, affecting **every** borrower:

- **"Unsecured Loans" was filed as secured** - `secured\s+loan` matches inside
  "unsecured loans" and that rule runs first. Fixed with `(?<!un)`.
- **"Add: Profit for the Year" inside the capital section was ignored** as a
  subtotal, cutting Net Worth by the whole year's profit (Rs 54.71 lakh here).
  Now counted as equity when it sits under a capital heading.
- **Gearing ratios ignored quasi-equity.** "(inclusive q/e)" means unsecured
  loans count as owners' money: added to net worth, removed from debt. The
  analyst's sheet matches this exactly (304.74 / (269.24 + 3) = 1.119); ours
  read 1.143.

**After:** 29 mismatched rows went to 0 real ones. Both years' P&L and Balance
Sheet match the reference row for row; the remaining two rows are the
analyst's hand-typed flat 0.50 lakh Other Expenses (the statement's own lines
sum to 0.522 / 0.548) and DSCR, which depends on hand-keyed EMI.

### 21. Borrower B (proprietor, petrol pump), FY2024 & FY2025 scans — PARTLY FIXED 2026-09-10

80 mismatched rows before, **21 after** - and none of the 21 is an extraction
error (see "Still open" below). Six separate faults:

1. **A dotted date made the Balance Sheet heading invisible.** "BALANCE SHEET AS
   ON 31.03.2024" contains "31.03", which `_match_title`'s money test reads as
   rupees-and-paise. The Balance Sheet merged into the Capital Account printed
   above it on the same page and the whole block was classed a capital
   account - no Balance Sheet for either year. Dates are now stripped before
   the money test.
2. **Stock movement was excluded, distorting profit.** Opening/Closing Stock
   was left unmapped; the analyst nets it into Purchases (FY24: 1985.23 =
   44.16 + 1960.09 - 19.02, to the rupee). Now netted the same way - profit
   unchanged by construction, mapping check signs it identically.
3. **"Gross prfit trf"** (OCR) wasn't recognised as the transfer line and was
   counted as an expense: a 10.70 lakh profit read as a 31.17 lakh loss. FY25
   profit now matches the reference.
4. **"2.35.51,356.00"** - OCR read the Balance Sheet total's commas as dots and
   only the tail parsed. Mixed separators are normalised at OCR ingest.
5. **Two printed lines fused into one cell** ("45,77,061.00 96,250.00") - the
   Case 3 / Case 6 same-cell amount-run pattern, on a third borrower. **Root
   cause found and fixed, and it was not the OCR engine**: RapidOCR returns the
   two figures as separate boxes (verified at four detector settings). Our own
   `layout.rows_with_cells` put them in one row because tightly-spaced scans set
   consecutive lines closer than its row tolerance, and first-fit joined them;
   same x, so they then fused into one cell. A box now never joins a row that
   already holds a box at the same horizontal position (two boxes that overlap
   horizontally cannot be on one printed line), and it joins the NEAREST
   eligible row. Digital and Tesseract words never overlap on a real line, so
   nothing else changes. FY24's P&L went from unread to read.

   **First version regressed another borrower, caught by the full golden
   run:** Borrower M's FY2025 Schedule III Balance Sheet verified before
   and failed after (65 -> 85 mismatched rows). Isolated by re-running the same
   file with each of the day's changes switched off in turn - only the row rule
   mattered. Same geometry, different meaning: there the stacked line was the
   second line of a wrapped CAPTION with no figure of its own, which belongs
   with the figure on the other line. `layout._rejoin_wrapped` now merges
   stacked neighbours back unless BOTH carry money - two data lines stay
   apart, a wrapped caption behaves exactly as before. Borrower M back to 65,
   Borrower B's gain kept (21).
6. **"# Net profit trf to Capital A/c"** - the scan's ditto mark OCR'd as "#"
   hid the result line from the subtotal rule, so the year's 14.46 lakh profit
   was counted as an expense and profit read as nil. Leading scan punctuation
   is now skipped.

**Still open:**
- **Convention question for the analyst, not a bug:** the reference files
  "SBI EDFS loan CC" under Secured loan - Asset Financed; we follow the
  label's "CC" and file it as CC/OD. Totals agree; the split differs by
  66.3 lakh (FY24). EDFS is a dealer working-capital line, so CC/OD is
  defensible. **Decided 2026-09-11: keep CC/OD** (user's call); recorded as a
  known divergence in `tests/test_golden_values.py`.
- Smaller line placements (e.g. "Company debit" in Interest, admin items in
  Transport) are the analyst's per-borrower judgement.

### 22. Borrower A (proprietor, transport), FY2024 & FY2025, digital — FIXED 2026-09-11

62 mismatched rows before, **4 after** (the 4 are the analyst's placement
choices - hiring/fuel costs under Purchases, Diwali expenses under Employee -
with gross expenses and profit matching both years). Three faults, all on
DIGITAL pages, none of them OCR:

1. **The wrong reading of a two-sided Balance Sheet won.** `_harvest` picks
   between a T-account and a vertical reading by asking which "verifies" - but
   checked every block generically. A vertical mis-read lumped both sides into
   one big section that reconciled to the page total, so it "verified" and was
   returned; the real Balance Sheet check (needing both grand totals) then
   rejected it as unverified. The T-account reading reconciled exactly at
   every candidate divider and was never used. A heading that plainly says
   "Balance Sheet" is now passed through, and readings are judged by the
   Balance Sheet's own check (`kind_hint`).
2. **An overdrawn proprietor's capital was read as positive equity.** The
   capital account (Rs 36.59 lakh) is printed on the ASSETS side - a debit
   balance. The analyst shows Equity -36.59 with both totals reduced by it
   (750.46 -> 713.87). A capital line on a T-account Balance Sheet's assets
   side is now spread as negative equity (`columns._spread_items`); the block's
   own balance proof is untouched.
3. **A closing TOTAL printed without separators ("168012440") wasn't
   recognised** by `_printed_total` / `_subtotal_rows` (Case 12 had fixed this
   only for line items). The total and the unlabelled expense subtotal were
   harvested as items, and the FY2025 P&L failed by Rs 16.5 crore. A whole-cell
   bare integer now counts there too - except a 4-digit year, so a "2025 |
   2024" header can't pass for a matching pair of totals.

### 23. Borrower N (Pvt Ltd), FY2025 — SOURCE QUALITY, deprioritised 2026-09-11

114 mismatched rows, every statement failing - but **the OCR is not the
problem** (page confidence 0.97-0.98, figures read correctly). The typed
statement itself is misaligned: its figures sit one row away from their
captions ("(1) Shareholders' Funds 100" is Share Capital's 100; "(a) Share
Capital -214" is Reserves' -214), a "Total Expenses" caption shares a line
with "Employee Benefit Expenses", and a "21747" sits against a 172.47 lakh
current-liabilities figure. The analyst re-keyed it by judgement. A tiny
company (Rs 12.45 lakh turnover); a special rule for one malformed layout is
not worth the risk to every well-formed Schedule III page. Left as-is: the
balance check correctly refuses to ship these numbers ("Check ITR").

### 24. Scan-reading pass, 2026-09-11 — six borrowers, one fault class each

Each fault below was isolated on the real page (row dump with x-positions)
before any change, and each fix is pinned by a unit test built from that
page's own text and coordinates.

- **Borrower J / Borrower J FY2023 (digital) — Balance Sheet now matches the
  analyst on every row.** (a) "CAPITAL ACCOUNT | COMPUTER | 10172" - a row, not a
  heading - opened a phantom block and cut the Balance Sheet in two; a heading
  may now carry no standalone figure other than a year. (b) Depreciation
  workings typed without separators ("LESS : DEP | 12973 116759.00") fused the
  depreciation and the net value into one cell; the net was read at the wrong
  x and no set of columns reconciled (assets over by Rs 7,37,587). Cells of
  only figures are now split per figure. (c) Fixed assets listed by name
  (COMPUTER, HONDA CAR, MOTOR TRUEK) and lenders by name (MUTHOOT FINANCE,
  IDFC FIRST BANK) were unmapped - new rules, guarded so "Car Expenses" stays
  an expense.
- **Borrower I FY2023 — garbled embedded OCR layer.** The PDF carried a
  scanner app's own text layer ("51,65,06,94g", "1,,33,94,6L,760"), trusted as
  exact digital text because it was not blank. Such pages are now detected
  (`parser._looks_garbled`) and re-read with our OCR. **Still open:** its P&L
  face shows only Direct/Indirect expenses; the analyst took the Employee /
  Interest split from the schedule pages. Reading schedules is a new feature.
- **Borrower F FY2023-FY2025.** (a) FY2025 heading "Balance Sheet as at
  31st March,?025" - the bare-figure rule from (Borrower J a) must only reject a
  standalone number, not a damaged year. (b) This year's and last year's
  figures fused in one cell ("24,18,44,856 12,32,46,427"); the vertical reader
  took the last one (last year's). (c) FY2023/24: asset figures printed one
  row ABOVE their captions, on rows whose only label is a liability
  ("a relative ... 5,67,565" / "Investment"). Far across the divider,
  position now beats the row label, and the figure takes the caption printed
  just below it (`pending` in `_harvest_at`) - Rs 8.26 crore of fixed assets
  had been "(unlabelled)". (d) A carried "Investment" caption filed Loans &
  Advances, Debtors and Cash as investments; explicit current-asset wording
  now wins. All three Balance Sheets verify.
- **Borrower M FY2025 — P&L now verifies (PBT 356.22, PAT 303.04 lakh,
  matching the page).** (a) Expense total printed unlabelled under "IV
  EXPENSES" - accepted as the expense side. (b) "XVII Profit/(Loss) Carried
  over to Balance Sheet" opened a phantom Balance Sheet - a line referring to
  the Balance Sheet is no longer a heading. (c) OCR set the Rs 303 lakh PAT a
  second time against "XVI Deferred Tax (Liability) - Earlier Year"; counted,
  it wiped out the year's profit. A figure equal to the result row just
  skipped is a restatement.
- **Borrower E FY2023-FY2025.** (a) "Loans (Liability) 2,04,37,748" as a
  line with its own total matched nothing - Rs 2.04 crore of borrowings fell out
  of the sheet (the analyst's figure, to the rupee). (b) Summary P&L with
  lettered headings ("A] Income :-", "B] Expenditure :-") and bare "Total
  (A)/(B)": neither side could be identified; bare totals now take the heading
  they close. (c) The result line wrapped over two rows ("Net Profit / Loss
  Transferred to" / "E] Proprietor's Capital Account 32,69,400"); joined. (d)
  **An older bug found on the way:** `_DERIVED_ROW_RE`'s `(before|after|for`
  group was never closed, so "carried over to Balance Sheet" had only ever
  matched after a "Profit / Loss " prefix.
- **Borrower Q FY2026 — decimal point read as a comma** ("Total current
  assets 5,106,21" for 5,106.21 lakh; the total read 100x its items). A final
  2-digit group after a 3-digit group is never valid grouping, so it is now
  read as the decimal. **Still open:** the same year's P&L fails on a
  deferred-tax sign in the tax section.

### 25. Output switched to the combined master format — 2026-09-12

The user supplied two analyst layouts (`Format.xlsx`, `Validation Format-
check.xlsx`) that disagreed: expense rows, whether Sundry Creditors and
Debtors had rows of their own, Debtor/Creditor Days, the DSCR block, and the
sign of tax (`=PBT+tax` vs `=PBT-tax`). Agreed with the user: one combined
master format holding every row of both, tax entered positive and subtracted
(two of the three sheets seen do it that way), figures in plain lakhs, all
totals / ratios / DSCR / Snap as live formulas. See the README.

Consequences worth knowing:
- **Trade receivables and payables now have their own buckets** (`debtors`,
  `sundry_creditors`). Older analyst sheets without those rows are compared by
  folding ours back into Current Assets / Current Liabilities
  (`tests/test_golden_values.py`).
- **Ratio definitions now follow the analysts' formulas exactly** - ROCE uses
  PAT + interest, LT Debt includes maturities within a year, the Current
  Ratio divides by CC/OD and maturities too. For Borrower B the user's
  CC/OD decision (Case 21) therefore also moves its Current Ratio (5.09 ->
  1.55 for FY24); recorded as a known divergence.
- **Unread = 0, flagged.** A statement that could not be read shows 0 so every
  formula still works, filled red with a note - never a silent zero.
- **Testing formulas, not copies of them.** `tests/xlsx_eval.py` evaluates the
  workbook's own formula text. Its first version had two bugs the real
  corpus exposed at once: nested formula evaluation overwrote the outer
  parse, and an `IFERROR` catching an error around a `SUM(` miscounted
  brackets (an all-zero unread column made TOL/TNW divide by zero). Both
  fixed; the unread-column test now evaluates every ratio.

### 26. One case, many files: columns now pooled by each statement's own year — 2026-09-12

The user uploads a whole case at once - ITR acknowledgements, separate
financial-statement PDFs, audit reports. Read one-column-per-file, that
produced duplicate columns, a wrongly dated column, and a column for a loan
offer letter. `columns.spread_many` now reads every file, places each
statement in the year its OWN period names, and `_assemble`s one column per
year from whichever files hold it (`_dedupe` keeps the best reading and drops
reprints). A file with no statements, or whose statements can neither be read
nor dated, gets a note instead of a column.

Found and fixed on the way, each on a real case:
- **Borrower H (92 -> 28 rows):** "(b) Trade Payables" printed with its own
  MSME breakdown was counted twice (`_drop_restated_parent`); a statement in
  lakhs failed over Rs 1,000 of rounding (`_tol`, units-aware); "TotalAssets"
  glued by OCR wasn't a total; **month-first dates** ("as at March 31, 2025")
  were not read at all, so FY2025 filed under FY2024 once files were pooled;
  "MAT Credit" (part of the tax charge) was unmapped; "Deferred Tax
  Liabilty" (typo) fell to the P&L.
- **Borrower R (regressed 78 -> 81, fixed back to 78):** page 2 of
  the FY2025 P&L read as 2024 and moved to FY2024, losing FY2025 its
  depreciation and interest. A statement dated to another year than its
  file now moves only if that year does not already hold this business's
  same statement from a file about that year - **unless its figures match**
  (then it is a reprint, moves, and is deduped). The first version of this
  rule, without the figures test, kept Borrower H's FY2025 reprint in FY2024 and
  doubled that year (28 -> 35); both cases are unit-tested.
- **File-year ties** ("ITR 2022-2023.pdf" held one FY2023 and one FY2024
  statement) are broken by the return's Assessment Year, not set order.
- **Borrower O (108 -> 96 rows):** each bundle carried the OTHER year's
  accounts ("ITR 23-24.pdf" = AY 2024-25 acknowledgement + Balance Sheet "as
  on 31.03.2023" + undated P&L). An undated statement now takes the period of
  the nearest dated statement in the same file (`_infer_missing_years`)
  before falling back to the ITR's year.

**Still open (analyst conventions, not reading errors):** Borrower H's FY2024
reference uses the RESTATED comparatives from the FY2025 accounts; its FY2025
reference moves current maturities of long-term debt (from the notes) into
Secured Loans. Borrower H FY2024's scanned P&L sets the Operating Expenses figure
on the "II Expenses" heading line (a figure-above-its-caption offset in a
vertical statement) - totals verify, the Transport/Other split does not.

## Template for new cases

```
### Case: <borrower/file>, <period>

**Ground truth:** <what the actual page says, with a page image reference>
**What we got right:** ...
**What needs improvement:** ...
**Mitigation:** ...
```

### 27. Schedule pages were never read — ADDED 2026-09-12

**Ground truth:** a proprietorship's P&L face often prints only group lines -
Borrower E FY2023: "Direct Expenses (Sch 8) 4,69,84,240",
"Indirect Expenses (Sch 9) 22,69,814". The salary (2.88 cr), the loan
interest and the transport charges are on the schedule pages behind them.
Read from the face alone, Employee Costs and Interest were zero (no interest
cover) and every rupee of wages was spread as transport.

**What changed:** `financials.find_schedules` reads every numbered schedule
or note ("Sch 08 Direct Expenses", "Note 15 : ...", "Schedule H : Expenses")
as one entry per group - a heading may hold several groups, each under its
own caption and closed by its own Total (Borrower I's Schedule H is Direct
then Indirect). A group is trusted only when its lines sum to that Total.
`expand_with_schedules` then replaces a P&L GROUP line (direct / indirect /
operating / administrative / establishment ... expenses - never "Other
expenses") with its schedule's lines, only when the schedule's total equals
the line's amount and a name word matches. A schedule line the face also
prints on its own (Borrower I's Depreciation, inside Indirect expenses AND on
the P&L) is dropped only when such lines account for the whole difference.
The block's totals are unchanged by construction.

Also: ESIC / PF contributions map to Employee Costs (not a "PF Payable"
liability); "Repairs and Maintenance" and the misspelt "Disel" map to
Transport, like their singular / correct forms already did.

**Result:** Borrower E 11 -> 7 rows, Borrower I 55 -> 49.

**Still open:**
- Analyst conventions on small lines: Borrower E's analyst puts Telephone,
  Professional Fees, Conveyance and PT into Transport & Admin and
  Refreshment into Employee; the general rules keep them in Other Expenses.
- Balance-sheet schedules are read but not expanded. The two analysts
  disagree: Borrower E's spreads ALL loans (incl. 78 lakh unsecured and a 1 cr
  Cash Credit) as Asset Financed; Borrower I's splits Schedule B into Asset
  Financed / CC-OD / Unsecured (~18 rows). Needs a decision, not a rule.
- Borrower I FY2023: the Balance Sheet (p30) fails its check - a scan issue.

### 28. One unreadable rupee figure failed a whole P&L — FIXED 2026-09-16

**The filing:** Borrower D FY2025 (a 2-page scanned Schedule III set).
Page 1's P&L failed, so EVERY P&L row of that year read "Check ITR".

**Ground truth (page 1):** "(f) Finance costs | 30 | 63 | 1,25,03,233" - a
real Rs 63 finance cost beside its Note No. 30. The four expense lines that
WERE read sum to 12,57,54,699 against a printed 12,57,54,761: short by 62.
(The filing's own total is off by Re 1; that is inside TOLERANCE.)

**Root cause:** `_AMOUNT_RE` takes a bare integer only when it is exactly 3
digits, so that "Note 25" is never harvested as Rs 25. A genuinely small
amount is indistinguishable by digit count, so the row carried no amount at
all, was taken for a heading and dropped - and the section then failed by
exactly that figure.

**Fix:** statement columns are RIGHT-ALIGNED. `_period_right_edge` takes the
median right edge of the figures already readable without ambiguity, and a
bare 1-2 digit cell is admitted only when it ends there (the Note No. column
ends far to the left). Digit count decides nothing; position does.

**Two more bugs the same file exposed, all one wording:** Schedule III spells
trade payables as two line items that OPEN with "total" ("total outstanding
dues of micro ...", "... of creditors other than micro and small
enterprises"). Three separate layers each read that as a total row:
- `_ANCHOR_RE` closed the Current liabilities section on it: the Rs 4.32
  crore payables became a section total with no items, and every line BELOW
  it lost its [CL] tag - so "(c) Current tax liabilities (net)", a balance
  sheet liability, was spread as the P&L's current tax expense and the loss
  was overstated by Rs 66.75 lakh.
- `_SKIP_RE` then discarded the row as structure.
- `taxonomy._IGNORE_RE` set it aside as a restatement, so Rs 432.25 lakh of
  creditors never reached the sheet.
Each now carries the same `(?!\s+outstanding\s+dues)` exception; a real
"Total ..." row is still an anchor, still skipped, still ignored.

**Result:** both statements verify. Every row matches the printed statement -
Revenue 952.95, PBT -304.60, PAT -290.91 (was -357.67), Sundry Creditors
432.25 (was 0.00), and Total of Liabilities = Total of Assets = 1,660.13
(the liabilities side was Rs 499 lakh short before). Borrower E, the one borrower
whose reading is cached, is unchanged at 7 rows.

**Still open on this file:** the EPS line "(1) Basic (2,909.11)" is harvested
and then reported as unclassifiable (-2,909). It is excluded from every
figure, so nothing is wrong in the sheet - but the warning is noise, and
`_IGNORE_RE`'s "basic (" pattern does not match this "(1) Basic" spelling.

### 29. A provisional set: two files, one year, and a two-digit year — FIXED 2026-09-16

**The filing:** Borrower D' provisional set - one file holding the
Provisional Balance Sheet "as at 31st March, 2026", another holding the
"PROVISIONAL PROFIT AND LOSS ACCOUNT FOR THE YEAR ENDED 31.03.26". Both
digital, both one page.

**What happened:** they came out as TWO columns - an FY2026 balance sheet
with no P&L, and an undated P&L with no balance sheet, side by side.

**Root cause:** `_PERIOD_RE` requires a FOUR-digit year. "31.03.26" matched
nothing, so the P&L had no period at all and could not be pooled with the
balance sheet for the same year. `_find_period` now widens a two-digit year
inside a complete numeric date (`_widen_short_year`); a bare two-digit
number is still never a year.

**Result:** one FY2026 column, both statements verified, every figure equal
to the printed statement (Revenue 902.89, PBT 15.82, PAT 6.61, Creditors
132.25, Total 1,392.84 on both sides).

### 30. The comparative column is now a year of its own — NEW CAPABILITY 2026-09-16

Case 14 harvested the comparative column's GRAND TOTALS and used them only
to rescue a year whose own statement failed. But a Schedule III statement
prints last year beside this year LINE FOR LINE, and a case often arrives
with no file for that year at all - sample_d's provisional set is FY2026 only,
yet carries the whole of FY2025.

`_harvest_vertical` now reads the prior column line by line (including rows
that are nil this year and real last year - "(b) Deferred tax | - |
(13,69,133)"), and `find_blocks` emits it as a statement of the PRIOR year,
marked `from_comparative`. It is checked against its own printed totals like
any other statement, and `spread_many` keeps it only where that year has no
statement of its own OF THAT KIND - the borrower's own filing always wins,
and the two are never pooled together (which would add them). Every such
column carries a warning and an Audit Trail note.

**Validated against the audited filing:** FY2025 read from the provisional
set's comparative column matches sample_d's own audited FY2025 statements to
the rupee on every row - Revenue 952.95, Purchases 884.10, Employee 44.36,
Depreciation 119.63, Other 209.45, PBT -304.60, PAT -290.91, Creditors
432.25, Total Assets 1,660.13, Net Worth 33.72.

**Caveat kept from Case 14:** a later auditor can reclassify figures between
buckets, so a comparative-derived column is the weakest reading of a year -
hence the warning, and hence a real filing always displacing it.

**Not yet measured:** the golden suite has NOT been re-run for the other 14
borrowers (2 GB free RAM; the runs keep being killed). This adds prior-year
columns only where a year has no statement of its own - Borrower G FY2024 and
Borrower I FY2023 are the likely movers. Borrower E, whose reading is cached, is
unchanged at 7 rows.

### 31. Electricity row removed; current liabilities confirmed — DECISION 2026-09-16

By the user's decision:
- The **Electricity** row is gone from the sheet. "Electricity", "Light
  Bill", "Power & Fuel", "MSEB"/"MSEDCL" now map to Transport operation and
  admin charges, so the money stays in Gross Expenses and profit is
  unchanged. The analysts' reference sheets still carry the row, so the
  golden suite folds THEIRS into transport the same way - otherwise the same
  rupees would count twice as a divergence.
- **Current Liabilities and Provision** = current tax liabilities + other
  current liabilities (+ any other current-liability line). Trade payables
  keep their own **Sundry Creditors** row, so Creditor Days still works.
  Nothing current is placed in **Secured loan maturity within one year**:
  only the wording "current maturities ..." reaches that row.

Verified on sample_d FY2026: Sundry Creditors 132.25 (trade payables),
Current Liabilities and Provision 243.21 (other current 171.95 + current tax
71.26), Secured loan maturity within one year 0.00, Total of Liabilities =
Total of Assets = 1,392.84. Borrower E is unchanged at 7 rows.

### 32. The workbook titled itself after the borrower's address — FIXED 2026-09-16

**Ground truth:** Borrower D' statements head with the company name,
its CIN, then the address, then the heading:

    Borrower D
    (CIN: U35120MH2008PTC182130 )
    6-A, H & G House, Sector 11, C B D Belapur, Navi Mumbai, ... 400614
    Provisional Balance Sheet as at 31st March, 2026

**What happened:** the entity read as the ADDRESS line, so every column, the
workbook title and the download filename carried it instead of the company.

**Root cause, in two layers:**
- `_ADDRESS_RE` is a keyword list (road, nagar, marg, building ...) and this
  address uses none of them - it has "House", "Sector" and place names. So
  `_find_entity` never skipped it. An address also has a SHAPE a business
  name does not: a six-digit PIN code, or several comma-separated locality
  parts. Either now identifies one on its own.
- `_main_entity` had already REJECTED the line (`_belongs_to` caps a name at
  70 characters), but `_assemble`'s fallback took the first block's entity
  whatever it was, so the rejected line still titled the column. The fallback
  now applies the same screen.

**Result:** both sample_d sets title as Borrower D, and
the filename follows. Borrower E is unchanged at 7 rows; the borrower names in
the golden set (including "Borrower A", which carries an address
WORD) are unaffected.

### 33. A statement printed across two pages — FIXED 2026-09-16

**The filing:** A CUSTOMER FY2026, a Tally set. Both statements run
onto a second page, bridged by Tally's own devices:

    P&L   page 1 ... Nett Profit 1,76,75,079.92 / "continued ..."
          page 2     Total 3,01,32,082.59 | Total 3,01,32,082.59
    BS    page 3 ... Carried Over 14,98,54,659.78 | Carried Over 6,06,57,088.80
          page 4     Brought Forward (the same two figures), Current Assets ...,
                     Total 14,98,54,659.78 | Total 14,98,54,659.78

**Root cause:** `find_blocks` assumed a statement is printed whole on one
page ("a page boundary also ends a block"). So the first half of the Balance
Sheet could never balance - its assets continue overleaf - and the second
half read as a separate statement whose two "Brought Forward" lines (Rs 21.05
crore) fitted no row. On the P&L the break cost the entire Indirect Expenses
block (Rs 1.24 crore, including Rs 86.98 lakh of employee cost): its only
real Total sits overleaf, so `_harvest_segmented` had nothing to check the
second half against and the harvest stopped at the trading total. Profit read
Rs 292.50 lakh against a printed Rs 176.75 lakh. NOT an OCR fault - Vision
re-reads the same page and cannot see the other one either.

**Fix:** `_join_continuations` joins a statement across either bridge -
Carried Over / Brought Forward, or "continued ..." with the heading
reprinted overleaf - dropping the bridge rows (they restate what is already
above) and emptying the continuation page so it keeps its number. Two
statements merely printed back to back have neither bridge and stay separate.

**And two mapping bugs it exposed - one principle:** a line's SECTION TAG
now outranks its wording (`taxonomy._blocked_targets`).
- "[EQ] Interest Paid On Housing Loan" is a movement inside the capital
  account, not the P&L's interest: equity was short Rs 8.82 lakh.
- "[EXP] Sales & Commission" is an expense, not revenue: income overstated
  Rs 1.45 lakh and the cost lost - Rs 2.90 lakh of phantom profit.
A balance-sheet line can no longer map to any P&L bucket, a debit line can
never be income, and a credit line can never be a cost.

**Result:** every figure matches the printed statements - Sales 1,850.17,
Employee Costs 86.98, PAT 176.75, Equity 490.42, Creditors 659.33, Debtors
835.18, and Total Liabilities = Total Assets = 1,498.55. Both statements
verify with no warnings. Borrower E unchanged at 7 rows; both sample_d sets
unchanged and still balancing.

### 34. Retained profit filed as a current liability — FIXED 2026-09-16

Found while checking case 33's Balance Sheet row by row. Tally lists its
primary groups down the liabilities side:

    Capital Account        4,90,42,016.76
    Loans (Liability)      1,58,46,361.47
    Current Liabilities    6,72,91,201.63
      Provisions             13,58,431.00
      Sundry Creditors     6,59,32,770.63
    Profit & Loss A/c      1,76,75,079.92      <- a group of its own
      Current Period       1,76,75,079.92

"Profit & Loss A/c" was not recognised as a heading, so the [CL] context from
Current Liabilities ran on: the year's retained profit was tagged
"[CL] Current Period" and spread as a CURRENT LIABILITY. Current Liabilities
read Rs 190.34 lakh against a printed Rs 13.58, Reserves read 0, and Net
Worth was Rs 176.75 lakh short.

**The Balance Sheet still balanced** - Total Liabilities and Total Assets
both 1,498.55 - which is exactly why the arithmetic proof cannot catch this
class of error, and the section tag must. Added to `_SECTIONS` as [EQ].

**Result:** Equity 667.17 (capital 490.42 + retained profit 176.75), Current
Liabilities 13.58, Net Worth 667.17, both totals still 1,498.55 - every row
of both statements now equal to the printed figures. Borrower E unchanged at 7
rows; both sample_d sets unchanged (their equity/reserves split is untouched,
Schedule III naming its own sections).

### 35. Sheet 1 matched to the analyst's quick-analysis layout — DECISION 2026-09-16

Audited the analyst's filled workbook ("ITR - Borrower D.xlsx") row by row - name, values, formulas. It IS our own output
(the `=274.62735+57.97803+492.79707` cells are our source-breakdown), with
two edits, both now adopted:

- the **DSCR Calculation** block removed: its Existing/Proposed EMI rows are
  keyed by hand from a loan schedule, not read from any ITR. The DSCR ratio
  stays in the Ratios block;
- a closing **Year-on-year growth** block added, as live formulas
  `=(C6-B6)/ABS(B6)` in the analysts' own percent format.

Sheet 1 now reproduces that workbook exactly: 92 rows, identical labels,
zero value differences across all three years (FY2024, FY2025 audited,
FY2026 provisional). Analysis and Audit Trail remain as sheets 2 and 3 -
nothing is lost.

**Kept deliberately:** Interest Coverage still prints its real value even
when it is absurd - FY2025 reads -293,599.76 because that year's interest is
Rs 63. By decision: show everything, hide nothing.

**Also checked in that audit:** every formula (ROCE, TOL/TNW with unsecured
loans as quasi-equity, Current Ratio, Cash Profit, Debtor/Creditor Days on
turnover) matches the analysts' definitions, and both totals balance each
year.

### 36. Two regressions the audit caught — FIXED 2026-09-17

A deep audit before committing re-ran the golden borrowers. Two had got
WORSE since the last full run, both from changes made the day before.

**(a) Borrower A: a full match -> 28 rows wrong.** Case 34 taught
the harvester that Tally's "Profit & Loss A/c" group is equity. True on the
LIABILITIES side (A CUSTOMER' retained profit) - but Borrower A prints
"PROFIT & LOSS" 310.29 lakh among its ASSETS, an accumulated loss carried as
an asset, which its analyst keeps in current assets. Read as equity it moved
Rs 310.29 lakh across the sheet: equity 273.70 against a printed -36.59, both
totals out by the same. Fixed by making it side-aware (`_PL_GROUP_RE`): the
group is read as equity only on a side that has already shown equity or
liability groups, never on the assets side.

**(b) Borrower B: 11 -> 45 rows wrong.** A petrol pump prints its Trading
("PUMP ACCOUNT") and its Profit & Loss on ONE page. Both are profit_loss,
neither names an entity, so both share `_dedupe`'s (entity, kind, year) key -
and only the "best" was kept. FY2025 lost its whole trading account: income
read Rs 21.71 lakh against Rs 1,659.38 printed.

It had worked before only by accident: the P&L block's "entity" used to be a
misread DATA row ("Gross prfit trf to P&I 41,86,923.00 ..."), which differed
from the other block's, so both survived. Case 32's address test correctly
rejects that junk - and exposed the real defect underneath. `_dedupe` now
treats same-key blocks as rivals only when their FIGURES overlap; two
complementary statements both survive, while a reprint is still dropped.

**After both fixes:** J K 11 -> 8, Borrower A back to a full match, Borrower G
52 -> 22 (its FY2024 now read from the following year's comparative column),
Borrower E 11 -> 7. Borrower K 1, Borrower L full match, Borrower J 14, Borrower Q 31, Borrower F 27 -
all unchanged.

**Lesson:** an accidental pass is worth as little as an accidental failure.
Both bugs here were structural (same key, wrong side), and both left the
statement balancing - only the golden comparison caught them.

### 37. A year lost to punctuation took its whole comparative year with it — FIXED 2026-09-17

**The filing:** A CUSTOMER AY2025-26, a digital Schedule III set. The
P&L (page 1) and Balance Sheet (page 2) both print FY2025 beside FY2024, line
for line. The sheet got FY2024's Balance Sheet but no FY2024 P&L.

**Root cause:** the P&L heading reads "Profit & Loss A/c as on 31st, March ,
2025". `_PERIOD_RE` allowed one separator between the day and the month and
refused the comma, so the P&L had NO year. A comparative column is emitted as
the prior year's statement only when the statement's own year is known - so
the entire FY2024 P&L (revenue 1,147.08 lakh, PAT 36.97) was never produced.
The Balance Sheet, dated cleanly on the next page, did produce its FY2024
column, which is why the year was half there.

**Fixed in layers, not just the comma:**
1. Heading dates tolerate any run of spaces and commas around the separator.
2. **A statement's own column headers are a second source of its years**
   (`_header_years`) - "2025 | 2024", "As at 31st March, 2025", read above the
   first figure only. A heading with no date at all, or one the pattern
   cannot parse, still gets its year, and the comparative takes the header's
   second year when one is printed.
3. Two-digit years are widened after a month NAME too ("1-Apr-25").

**The first version of (2) regressed a borrower, caught by the golden set:**
Tally heads statements with the period as a RANGE, "1-Apr-2023 to
31-Mar-2024". Read year by year, the range's START was taken and Borrower L's FY2024 statements were dated 2023 - a full match turned into 69 rows
off. Within a cell stating a range, only the END year counts; separate cells
remain separate columns.

**Result:** YES AY2025-26 yields both years, every row equal to the printed
statement - FY2024 revenue 1,147.08, PBT 39.45, tax 2.48, PAT 36.97, assets
518.35; FY2025 revenue 1,426.48, PAT 33.66, assets 1,063.02. Borrower L back to a
full match; Borrower E 7, Borrower A full match, J K 8, Borrower K 1, Borrower J 14, Borrower G
22 - all unchanged. **Also corrected:** YTM (case 33) is now dated FY2026, the
year its "1-Apr-25 to 31-Mar-26" statements actually end in - it had been
shown as FY2025.


### 38. Consistency checks, stage 1 — ADDED 2026-09-17

Every P&L and Balance Sheet row now carries a trust level - proven,
consistent, doubtful or unread (`engine/checks.py`). A statement that misses
its own section total by at most 0.5% is KEPT, only that section's lines
doubtful, instead of being dropped whole (`financials.salvageable`): one
unreadable Rs 63 line used to cost sample_d FY2025 every P&L row.

Six independent checks run after every year is assembled - section totals;
the sheet balancing AFTER mapping; profit against the statement's own profit
line; profit against the ITR computation; the profit a capital account
credits (an Income & Expenditure account calls it a surplus); and a year
printed twice. A check lacking its inputs SKIPS, and a failing check disputes
only rows that were not already proven.

**A year printed twice** ("ITR 2025-26" prints FY2025 beside FY2026; "ITR
2024-25" prints FY2025 itself) is kept as that year's `alternate`. The two
printings are compared, and a field one of them lost is taken from the other -
never over a proven reading, only from a proven row, only when both agree on
their grand totals, and only when the fill CLOSES the gap on the total that
row feeds. That last rule came from the golden gate: Borrower E FY2024's own
balance sheet is a summary whose "Current Assets" already holds the debtors,
while the comparative column breaks them out, and filling Debtors counted Rs
1.89 crore twice (7 -> 18 rows). A statement too broken to salvage now takes
the other printing whole, instead of reading "Check ITR".

Doubtful INPUT rows are amber in the workbook with a note naming the check and
the gap; a filled row carries its evidence (file and page). Totals, the
Financial Snap, the ratios and the growth block are formulas over those rows
and are never marked or counted twice. The Audit Trail lists every check; the
app lists the failing ones per year.

**Golden gate:** Borrower F 27, Borrower E 7, Borrower K 1, Borrower L pass, Borrower G 22, J K 8,
Borrower A pass, Borrower M 25, Borrower J 14 - all unchanged; **Borrower H 28 -> 11**.
sample_d (audited + provisional), YTM and YES AY2025-26 read exactly as before.

### 39. A damaged heading dated a statement a year early — FIXED 2026-09-17

Borrower F FY2025: the scan damaged the heading's year ("Balance Sheet as
at 31st March,?025"), so the column headers decided the year - and OCR had
fused both into ONE cell, "31st March 2025 31st March 2024". The fallback
sorted those by value and took 2024: the FY2025 balance sheet was dated
FY2024, added to FY2024's own (13.43 + 21.84 crore, both years wrong), and
FY2025 had no balance sheet at all. Golden 27 -> 58 rows.

Three guards, all general:
- a statement's own year is the LATEST year its columns name (a current
  period is never older than its comparative), whatever the print order or
  fusing - this also covers previous-year-first layouts (Borrower E, Borrower K);
- years within one cell keep their printed order;
- only plausible statement years (20xx) from the rows directly under the
  heading. An audit-report sentence mistaken for a heading ("... as at the
  balance sheet date.") had picked "1948" out of its prose and grown an
  FY1948 column in the workbook.

Borrower F is back to 27 rows and reads 2,184.30 lakh for FY2025 and 1,342.86 for
FY2024, both equal to the printed totals.
