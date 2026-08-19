# Legacy sheet findings

Extracted 2026-08-11 from the three authoritative sample files listed in CLAUDE.md §15.
These are observations about the **legacy manual process**, recorded so the engine can
reproduce what is correct and deliberately not reproduce what is broken.

Sources:
- `sample_renumeration_sheet.xlsx` — 21 columns, 1 data row (VEMA PRUDHVI SAI)
- `sample_invoice_generation_sheet.xlsx` — sheet "March- Malineni", 34 populated columns
- `MALINENI Trainer Invoice Details- Month of JULY (Responses).xlsx` — 17 form responses

---

## 1. Column orders — these are output contracts (§11)

**Remuneration sheet, 21 columns in order:**

```
Sl. No. · PAN · College Tags · Name · Commercials · Unit · No.of days per month ·
No. of days to be paid in <MON> · Commercials per day · Earned · TA & DA ·
Accomodation · Travel reimb · Gross · Deductions · TDS · Net Pay ·
Bank AC no. · IFSC · Name of the Bank · Invoice Number
```

Note `Accomodation` is misspelled in the legacy header. **Preserve the misspelling** —
§11 says "people trust the format they read". Column 8 is month-parameterised.

**Invoice sheet, 34 populated columns in order:**

```
date · Name · PAN No. · Expense for the month · account_name · College name ·
Commercials(per month- Actual) · Unit · Attendance ( no.of worked days) ·
Commercials per day · Earned per month · TA & DA · Accomodation Allowance ·
Travel reimb · Gross · Deductions · TDS · Net Pay · Total Pay · Bank AC no. ·
IFSC · Name of Bank · Branch · Invoice Number · Address · Contact Number ·
Program Name · From · To · Amount in Words · Trainer Mail ID · AM Mail ID ·
HR Mail ID · SMO Mail ID
```

Columns 35–56 exist but are entirely empty. `AM Mail ID` is a recipient CLAUDE.md §6
does not mention — confirm who "AM" is before wiring the comms templates.

---

## 2. Float artifacts, observed in production

The invoice sheet stores:

| Field | Stored value | Correct value |
|---|---|---|
| Earned per month | `65000.00000000001` | `65000` |
| Gross | `65000.00000000001` | `65000` |
| TDS | `6500.000000000001` | `6500` |
| Net Pay | `58500.00000000001` | `58500` |

This is the artifact CLAUDE.md §6 warns about, live in the current process. It is why
R7 (`Decimal` only, no exceptions) exists. Our output must be exactly `65000`.

`Amount in Words` renders `#NAME?` — the missing Excel macro noted in §6. Not reproduced.

---

## 3. The rounded per-day rate is a real underpayment source

`Commercials per day` is a **display column**. The legacy process rounds it, then
multiplies by it. Observed:

| Trainer | Rate | Days | Legacy arithmetic | Correct (`rate × days ÷ dim`) | Error |
|---|---|---|---|---|---|
| VEMA PRUDHVI SAI | 80,000/mo | 6/31 | per-day shown `2581`; `2581 × 6 = 15,486` | **15,483.87** | +₹2.13 |
| Vijay Chalagala | 70,000/mo | 26/31 | `2258 × 26 = 58,708` (written in the cell) | **58,709.68** | −₹1.68 |
| B Tharakeswar rao | 68,000/mo | 31/31 | `2193 × 31 = 68,000` (written in the cell) | **68,000** | arithmetic is wrong; `2193 × 31 = 67,983`. The author wrote the intended answer, not the computed one. |

The sheet's own `Earned` column for VEMA is `15484`, i.e. the *correct* value — so the
per-day column and the earned column disagree with each other in the same row. This is
direct empirical support for §6's "multiply before dividing" rule and for R6's
"never feed a rounded value back into a calculation".

---

## 4. CRT evidence — partially answers §14 Q1

CLAUDE.md §14 Q1 records that all validated fixtures are bCAP and a real per-day sheet
is needed. The form responses contain **three CRT rows**:

| Trainer | WO rate field | Attendance | Period | Earned | TA&DA |
|---|---|---|---|---|---|
| Vijaya Raghava Krishna Kumar Yellepeddi | `3500/day` | 6 | 13–18 Jul 2026 (6 cal. days) | 21,000 | "Na" |
| Kota Lakshmi Manikanta | `2700` | 7 | 11–18 Jul 2026 (8 cal. days) | 18,900 | — |

What this establishes:

- **The day rate lives in the work order.** The form field is literally labelled
  "Commercials(per month- As per Work Order)" and holds `3500/day`. Answers half of Q1.
- **CRT counts payable days UP, not down.** Kota's period spans 8 calendar days but
  attendance is 7 and earned is `2700 × 7 = 18,900`. A count-down model would have paid 8.
  This confirms the §5 table.
- **`earned = rate × payable_days`** with no `days_in_month` divisor on the CRT path.

What it does **not** establish, so Q1 stays open:

- Whether TA&DA is per travel day. Both CRT rows record "Na"/blank, so there is no
  evidence either way. **Do not guess.**
- Whether the day rate can also be set per program rather than per work order.

**Data-quality warning for the Intake agent:** Kota's program is written as
`"CRT training"`, not `CRT`, and the per-day rate `2700` sits in a field labelled
"per month". Program type and rate basis cannot be parsed from these fields naively.

---

## 5. bCAP count-down confirmed

Vijay Chalagala: 70,000/mo, period 01–26 Jul, leave dates 27–31 Jul, `No.of LOPs = 5`,
attendance `26`. That is `31 − 5 = 26` — counted **down** from period length, per §5.

---

## 6. Invoice numbers in the legacy data are inconsistent

§6 specifies `{PAN[0:4]}/{FY}/{MON}{seq}` with FY derived from the payout month
(April–March). For July 2026 that is `26-27`. Observed:

| Value | PAN prefix | FY | Seq | Correct? |
|---|---|---|---|---|
| `BCDP/26-27/JUL1` | ✓ | ✓ | ✓ | yes |
| `AFBP/26-27/JUL1` | ✓ | ✓ | ✓ | yes |
| `CHQP/26-27/JUL1` | ✓ | ✓ | ✓ | yes |
| `NPWP/26-27/JUL1` | ✓ | ✓ | ✓ | yes |
| `ANSP/25-26/JULY` | ✓ | **wrong FY** | **`JULY`, not `JUL1`** | no |
| `BRMP/25-26/JUL1` (remuneration sheet, VEMA) | ✓ | **`25-26` for a Jul-2026 payout** | ✓ | no — unless that row is July 2025 |

**Root cause:** the Google Form's own field label is
`Invoice Number  (XXXX/25-26/SEP1)`, so trainers copy `25-26` verbatim. The placeholder
teaches the wrong fiscal year.

**Consequence for us:** invoice numbers are trainer-supplied today and roughly 1-in-5 are
malformed. The system must **generate** them, not accept them, and the
`(pan, fiscal_year, month, seq)` unique constraint will reject collisions that the manual
process silently tolerated. Expect the Phase 2 parallel run to surface mismatches against
historical numbers — that is the system being right, not wrong.

---

## 7. Still unknown

- **§14 Q2 — `Expense for the month`.** Confirmed present: `2026-07-26` against a
  01–31 July period, for a trainer whose period was the full month. Semantics remain
  unknown. Do not invent a meaning.
- Whether TA&DA on CRT is per travel day (§14 Q1, above).
- Who "AM" is in `AM Mail ID`.
