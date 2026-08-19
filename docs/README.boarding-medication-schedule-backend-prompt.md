# Boarding Medication Schedule Backend Prompt

Add `dosage`, `frequency` and `duration_days` to `pet_app.api.healthcare.boarding.create_order`
for `kind=medication`, and give them somewhere to live.

**The gap is clinical, not cosmetic.** A boarding medication today records *what* and *how much per
dose* — the medication, the dose option, and the stock it draws. It records nothing about *how
often* or *for how long*. A five-day twice-daily course and a single dose are stored identically
and are indistinguishable after the fact: same row, same `qty`, same everything. The boarding
record cannot answer "what was this animal actually given during its stay", which is the one
question a boarding medication record exists to answer.

This is a request to extend the backend. **It is not a bug report against the client** — the client
cannot fix it, for the reason in §1.

---

## 1. There is nowhere to put them today — verified, not assumed

We checked before writing this, so the work does not start from a wrong assumption that some column
already exists.

**`create_order` declares nine parameters and takes no `**kwargs`:** `boarding_id`, `kind`, `pet`,
`template_id`, `care_service_id`, `item_code`, `priority`, `note`, `dose_option`, `warehouse`. Per
[`BOARDING_CREATE_ORDER_API.md`](BOARDING_CREATE_ORDER_API.md) §2, anything outside that list is
filtered out of `form_dict` by Frappe before the call — silently, with no error and no effect.

**The billable row has no field for them either.** The `billable_items[]` shape (§3.3 of the same
doc) carries `qty`, `rate`, `amount`, `note`, `dose_option`, `warehouse`, `dispense_status`,
`stock_issued_qty` and friends. There is no `dosage`, no `frequency`, no `duration_days`.

So the client currently sends nothing, deliberately. Adding the keys client-side without a backend
change would be **worse than the gap**: they would be dropped in transit, no error would surface,
and the UI would imply a five-day BID course had been recorded when nothing had. We would rather
ship the honest omission than a silent lie, which is why the boarding dialog has no frequency or
duration field.

**Please do not work around this by packing the schedule into `note`.** `note` reaches the invoice
as the line description (§3.3), so structured data there corrupts billing output, and it is
unqueryable besides.

---

## 2. Where should they live? — the real design question

We can see three options and do not have the standing to pick. The trade-off is ours to describe,
yours to decide.

### Option A — add the fields to the billable row

Simplest, and matches how `dose_option` and `warehouse` already work: stamped at order time, echoed
back on the row. Fits the existing `create_order` → `billable_items[]` flow with no new document.

The objection: the billable row is a **billing** artifact. Frequency and duration are clinical facts
that do not affect the charge, and a boarding stay's medical rate may absorb the row entirely
(`status: "Included"`). Putting clinical history on a row that billing owns means the clinical
record inherits billing's lifecycle — including cancellation.

### Option B — boarding creates a prescription row, like a visit does

The visit path already models exactly this. `Vet Visit Medication Item` (child of
`Vet Visit.prescribed_medications`) carries `dosage`, `frequency`, `duration_days`, `dose_option`,
`warehouse` and the whole dispense lifecycle — see
[`MEDICATION_BILLING_CONTRACT.md`](MEDICATION_BILLING_CONTRACT.md) §"Fields on `Vet Visit Medication
Item`". Boarding would gain the same clinical grain the visit has, and the two paths would stop
diverging.

The objection: a boarding medication has **no order document at all** today — `order_id` is opaque
and only `item_id`, the billable row, is real (§3.2). Option B introduces a document where there
isn't one, which is the largest change of the three.

### Option C — a `Pet Care Plan Item`

`Pet Care Plan Item` already holds `medication`, `dose`, `frequency` and `duration` — see
[`README.boarding-medication-give-backend-prompt.md`](README.boarding-medication-give-backend-prompt.md)
§1. Boarding already reads care-plan items for its medication schedule, and
`record_medication_given` already advances them. A course with a frequency and a duration *is* a
schedule, and this is the doctype that already models schedules.

The objection: care-plan items hang off an open case/episode. A boarding medication requested
without a visit may have no case to attach to, and inventing one to hold a single dose is heavier
than the problem.

### What we would suggest

**Option C if a case reliably exists for a boarded pet, otherwise Option A.** C puts a schedule in
the doctype already built for schedules and needs no new fields — only a write path. A is the
cheapest correct thing if it does not. B is the most faithful to the visit but the most work, and
it is only worth it if boarding is expected to grow the full prescribe/dispense lifecycle.

**Please tell us which you chose before we build the UI**, since the three differ in what the
client reads back and where it reads it from.

---

## 3. Field specification

Whichever option is chosen, these are the three parameters `create_order` should accept for
`kind=medication`. Names match the visit's existing fields deliberately — the client already has
these exact shapes from `SaveVisitPrescribedMedicationPayload` and would reuse them.

| Name            | Type   | Required | Notes                                                                                                                                                                 |
| --------------- | ------ | :------: | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dosage`        | string |    no    | Free text, the clinical dose as written: `"0.5 cc"`, `"1 tablet"`, `"5 mg/kg"`. **Not** a number and not parsed. Distinct from `dose_option`, which is the stock draw. |
| `frequency`     | string |    no    | Free text: `"BID"`, `"every 8h"`, `"once daily"`. Matches `Vet Visit Medication Item.frequency`.                                                                      |
| `duration_days` | int    |    no    | Whole days. Matches `Vet Visit Medication Item.duration_days`.                                                                                                        |

Notes on semantics:

- **All three optional, and absence must stay valid.** A single one-off dose during a stay is a real
  case and must not be forced to invent a frequency. Today's behaviour is the no-schedule case and
  must keep working unchanged.
- **`dosage` is not `dose_option`.** `dose_option` names a `Medication Dose Option` and drives the
  stock deduction; `dosage` is the human-readable clinical dose. A medication can have one, both, or
  neither. Please do not derive one from the other, or reject a row that has only one.
- **`duration_days` should not alter `qty`, `rate` or `amount`.** The client sends the dose count in
  `qty` as it does now. If the backend wants to derive a total from frequency × duration, that is a
  separate decision we would want to discuss first — silently multiplying the charge would be a
  billing regression.
- **Echo all three back** on whatever row carries them, exactly as sent, the way `dose_option` and
  `warehouse` are echoed (§3.3). The client verifies the echo to confirm storage rather than
  assuming it, and cannot do that for a write-only field.

---

## 4. Validation

Keep it minimal — these are clinical free text, and over-validating them will reject legitimate
entries.

| Case                                         | Expected                                                                   |
| -------------------------------------------- | -------------------------------------------------------------------------- |
| all three omitted                            | accepted; behaves exactly as today                                         |
| `dosage` / `frequency` in any free-text form | accepted; no vocabulary enforced, Arabic and English both                  |
| `duration_days` negative or zero             | rejected with a clear message                                              |
| `duration_days` non-integer (`"5.5"`)        | your call — reject, or floor it, but please document which                 |
| sent with `kind != medication`               | ignored, matching how `dose_option` and `warehouse` behave for other kinds |

Please do **not** enforce a controlled frequency vocabulary. The visit path accepts free text, the
two would diverge, and clinics write frequencies in both languages.

---

## 5. What the client will do once this lands

The boarding dialog
([`BoardingMedicationRequestDialog.vue`](../src/components/healthcare/boarding/BoardingMedicationRequestDialog.vue))
already implements the dose-option half: `GetMedicationDoseOptions`, the chips, pre-select on a
single option, a required choice when there are several, and a line showing the stock draw
("Deducts 0.033 Vial"). It sends `dose_option` and `warehouse` today.

On confirmation of the chosen option we will add a free-text dose field, a frequency field and a
duration field, reusing the visit's controls rather than building a second set, and send all three
through `CreateBoardingOrder` in [`boardingApi.ts`](../src/api/boardingApi.ts). Both `en` and `ar`
strings ship with it.

Until then the client sends nothing for these and the dialog shows no fields for them — the gap is
visible as an absence rather than papered over.

---

## 6. Please confirm back

1. Which of A / B / C, so the client knows where to read the values from.
2. The exact accepted parameter names, if they differ from §3.
3. Whether the three are echoed on the row, and under which keys.
4. Whether `duration_days` affects `qty` / `amount` in any way (we are assuming **no**).
