# Preventive Care Record — frontend contract

**Status: backend complete, Stages 1–9.** One destructive patch (`preventive_care_purge`) is
pending the owner running it; it removes superseded data and changes nothing in this contract.

`Preventive Care Record` replaces `Pet Vaccination Record`, `Pet Deworming Record`, and
`PetCareService` as the home for vaccination and deworming. **The first two doctypes no longer
exist.** Anything still reading them must move here.

The catalogue is unchanged: `CareService template`, `Care Service Billing Option` and
`CategoryCareServices` are exactly as they were, and this record reads them.

---

## 1. The doctype

| | |
|---|---|
| Doctype | `Preventive Care Record` |
| Name series | `PCR-.#####` (`PCR-00001`…) |
| Submittable | **No.** `status` is the lifecycle, as for `Lab` and `Imaging` |
| Module | Pet App |
| Fields | 44 (excluding layout breaks) |

### 1.1 Client-settable vs derived — read this first

Three categories. Sending a derived or server-owned field is **ignored**, except `kind`, which
is refused if it contradicts the catalogue.

| Category | Fields |
|---|---|
| **Client may set** | `pet`, `visit`, `guardian`, `doctor`, `provider`, `care_service`, `service_option`, `kind`¹, `medication`, `branch`, `due_date`, `scheduled_datetime`, `priority`, `weight`, `qty`, `dose`, `vaccine_type`, `batch_no`, `next_due_date`, `reminder_enabled`, `notes`, `source_doctype`+`source_name` |
| **Derived — never send** | `category`, `item_code`, `rate`, `medication_name`, `performing_branch` |
| **Server-owned — never send** | `status`, `billed`, `sales_invoice`, `administered_on`², `administered_by`, `administered_at`, `cancelled_by`, `cancelled_at`, `stock_issued_qty`, `stock_entry`, `stock_warehouse`, `order_id`, `reminder_status` |

¹ `kind` is normally derived. Send it **only** for a history entry with no catalogue row (§1.4).
² `administered_on` *is* settable on the `administer_preventive` action — see §4.2.

### 1.2 Fields, with types

**Order & provenance**

| Field | Type | |
|---|---|---|
| `visit` | Link → Vet Visit | **Optional.** Unlike `Lab`, there is no `mandatory_depends_on` |
| `source_doctype` | Link → DocType | For records raised from something other than a visit |
| `source_name` | Dynamic Link → `source_doctype` | Required *together with* `source_doctype` if either is sent |
| `order_id` | Data, read-only | Matches `Visit Order.order_id` |
| `pet` | Link → Pet | **Required — the only hard requirement** |
| `guardian` | Link → Guardian | Auto-filled from the visit, else the pet's primary owner |
| `doctor` | Link → Healthcare Practitioner | Label **"Ordering Practitioner"** — who ordered it |
| `provider` | Link → Healthcare Practitioner | Label **"Administering Practitioner"** — who gave it |
| `branch` | Link → Branch | **See §1.3 — sometimes required from the client** |

> `doctor` and `provider` are two different people and both are kept. Do not collapse them:
> the ordering vet and the administering nurse are usually not the same person. `provider` is
> deliberately empty on a visit-ordered record until the dose is started.

> **A record with neither `visit` nor `source_name` is legal and common** — a dose given
> straight on a pet's record. 28 of the 29 pre-existing preventive rows had no visit. This is
> a first-class path, not an exception.

**Catalogue** (read from the master rows; all read-only except the two links)

| Field | Type | |
|---|---|---|
| `care_service` | Link → CareService template | Not required |
| `service_option` | Link → Care Service Billing Option | Not required |
| `kind` | Select | `"Vaccination"` \| `"Deworming"`. Derived, read-only, frozen at insert |
| `category` | Link → CategoryCareServices | Derived, read-only, frozen at insert |

**What was given**

| Field | Type | |
|---|---|---|
| `medication` | Link → Medication | Defaulted band-first then template, **only when empty** — an operator's substitution is never overwritten |
| `medication_name` | Data, read-only | **Replaces both `vaccine_name` and `medication_name`** from the old doctypes |
| `vaccine_type` | Data | Show only when `kind == "Vaccination"` |
| `dose` | Data | Free text. Show only when `kind == "Deworming"` |
| `batch_no` | Data | Hand entry only; never auto-filled |
| `weight` | Float | The pet's weight — this is what selects the deworming band |
| `qty` | Float, precision 4 | How much of the consumable this dose claims. Defaulted **only when empty**, then left alone — editable for a partial dose. Where the default comes from depends on the kind; see below |

> **`qty` defaults by kind** (corrected 2026-09-13). There are three answers because three
> different things are authoritative about the quantity:
>
> | Dose | Default | Why |
> |---|---|---|
> | **Deworming** (has a band) | the band's `stock_deduction_qty`, **including `0`** | The band is where the number is configured, and `0` there means nobody configured it |
> | **Vaccination** (template, no band) | **`1`** — one whole vial | `CareService template` has no quantity field at all, so there is nothing to read. The invoice bills one unit for it either way |
> | **History** (`kind` alone) | `0` | Never billed, so there is no invoice line for a quantity to agree with |
>
> Until this date a vaccination defaulted to `0` and was administered anyway, producing a
> record that claimed it consumed nothing while its invoice relieved a vial. The whole-vial
> default was documented in the backend but had been lost in a rewrite, and `0` reads as an
> empty field rather than as a missing rule, so nothing on screen showed it was absent.
>
> **`0` on a dose that consumes a named drug is now refused at administration** — never at
> creation, because an `Ordered` dose may legitimately precede its band being configured. The
> refusal names the band: *"Service Option CSBO-00051 consumes X but has no Stock Deduction
> Qty, so PCR-00009 cannot record what it used. Set Stock Deduction Qty on that Service
> Option, then administer this dose."* A dose that consumes **nothing** is unaffected — `0` is
> still a silent, legitimate answer there.

**Schedule & lifecycle**

| Field | Type | |
|---|---|---|
| `status` | Select | `Ordered` \| `In Progress` \| `Administered` \| `Cancelled`. Default `Ordered` |
| `due_date` | Date | The day the dose is owed |
| `scheduled_datetime` | Datetime | The appointment. **Never defaulted** — absent means "do it now" |
| `priority` | Select | `Routine` \| `Normal` \| `High` \| `Urgent` |
| `start_at`, `end_at` | Datetime | Stamped by the actions |
| `administered_on` | Date | Stamped at administration; defaults to today if not supplied |

**Recurrence** — no counterpart on Lab or Imaging

| Field | Type | |
|---|---|---|
| `next_due_date` | Date | **Operator-chosen, never computed.** Empty is a valid answer meaning no recurrence — no reminder is raised. Must be **strictly after** the day the dose is given. Setting it now also **raises the next dose** — see §4.4 |
| `reminder_enabled` | Check, default `1` | |
| `reminder_status` | Select | `Pending` \| `Sent` \| `Acknowledged` \| `Cancelled`. Written by the reminder sweep |

**Billing** (read-only): `item_code` Link → Item · `rate` Currency · `performing_branch` Link → Branch · `billed` Check · `sales_invoice` Link → Sales Invoice

**Stock** (read-only): `stock_warehouse` Link → Warehouse · `stock_issued_qty` Float · `stock_entry` Link → Stock Entry

> **Do not label these as "issued".** Administration posts **no** Material Issue — stock moves
> when the Sales Invoice is submitted. `stock_issued_qty` and `stock_entry` are **always empty**
> on this path. `stock_warehouse` is set only when the billed item is a stock item, and records
> *where the goods will leave from*.

**Audit** (read-only): `administered_by` Link → User · `administered_at` Datetime ·
`cancelled_by` Link → User · `cancelled_at` Datetime · `cancellation_reason` Small Text.
Plus `notes` Small Text (writable).

### 1.3 `branch` — when the client must send it

| Acting user | Behaviour |
|---|---|
| Restricted to one clinic (has a Branch User Permission) | Stamped automatically. Don't send it |
| **Unrestricted** (no Branch User Permission — back-office, admin) | **Must send `branch`.** Otherwise `MandatoryError: "Select a branch for this Preventive Care Record. Your user is not restricted to one clinic, so the branch cannot be inferred."` |
| Ordered from a visit | Inherited from the visit. Don't send it |

This site has two branches (`main`, `hotel`), so the unrestricted case is live. A branch picker
is needed for unrestricted users on the create form.

> `branch` (which clinic owns the record) and `performing_branch` (where the catalogue says the
> service is physically performed) are different fields. `performing_branch` is blank on every
> catalogue row on this site.

### 1.4 The selection rule — the one thing the picker must get right

**At least one of three things is required**, and the server refuses a record with none,
naming all three:

| Send | For |
|---|---|
| `care_service` | A vaccination — the catalogue template |
| `service_option` | A deworming — the weight band |
| `kind` alone | **History**: a dose given elsewhere, with no catalogue row |

Why it isn't simply "a template": the live catalogue has **1 vaccination template with no
bands**, and **2 deworming bands with no template**. So a vaccination picker offers templates;
a deworming picker offers weight bands.

- If **both** `care_service` and `service_option` are sent they must resolve to the **same
  category**, or the server refuses.
- If `kind` is sent **alongside** a catalogue row it must **agree** with what that row resolves
  to, or the server refuses. It is not silently corrected.
- A history record (`kind` alone) gets **no** `item_code`, **no** `rate` and **no** `category`.
  It can never be billed, and the mobile path creates it already `Administered`.

### 1.5 Resolved labels

Returned alongside their links so a list does not need one lookup per row.

| Label | From |
|---|---|
| `doctor_label` | `Healthcare Practitioner.practitioner_name` |
| `provider_label` | `Healthcare Practitioner.practitioner_name` |
| `care_service_label` | `CareService template.service_name` |
| `service_option_label` | `Care Service Billing Option.service_title` |

The **list endpoint only** adds two more: `pet_name` (`Pet.pet_name`) and `guardian_name`
(`Guardian.full_name`).

### 1.6 Not present, deliberately

No `result`, `report`, `specimen`, `sample_collected_*`, `result_entered_*`, `released_*`,
`doctor_reviewed*`, `result_visibility` or `attachment_required`. A vaccination has no specimen
and no release step — **do not build result-entry or release UI for this doctype.**

---

## 2. Lifecycle

```
Ordered ──→ In Progress ──→ Administered   (terminal)
   └──────────┴───────────→ Cancelled      (terminal)
```

Legal transitions, exhaustively — anything else is refused with
`Invalid Preventive Care Record status transition from X to Y`:

| From | To |
|---|---|
| `Ordered` | `In Progress`, `Administered`, `Cancelled` |
| `In Progress` | `Administered`, `Cancelled` |
| `Administered` | *(nothing — terminal)* |
| `Cancelled` | *(nothing — terminal)* |

There is no `Pending` state. `Ordered → Administered` directly is legal (skipping `In Progress`).

**Action availability.** All three actions are allowed only from `Ordered` or `In Progress`.
Once `Administered` or `Cancelled`, hide every action — a second `administer_preventive` is
refused precisely because it would bill and dispense the same dose twice.

---

## 3. Endpoints

All return the standard envelope:

```json
{ "ok": true,  "data": { … }, "meta": { … }, "errors": null }
{ "ok": false, "data": null,  "meta": { … }, "errors": [ { "message": "…", "code": "…" } ] }
```

A refused call changed **nothing** — every write path is wrapped in a savepoint.

### 3.1 Create a dose for a pet

```
POST  pet_app.api.preventive_care.create_preventive_care_record
```
```json
{ "payload": {
    "pet": "PET-00128",                 // required
    "care_service": "CareService-00022", // or "service_option", or "kind"
    "branch": "main",                    // required for unrestricted users
    "provider": "HCP-00007",
    "next_due_date": "2027-09-11",
    "weight": 4.2
} }
```
```json
{ "ok": true, "data": { "record": { /* all fields + the 4 labels */ } } }
```

Created at `Ordered`. **Nothing is billed.** Creating is not giving — the charge, the stock
check and the clinical stamps all belong to `administer_preventive`, so a dose created and then
cancelled never raised a charge.

### 3.2 Drive a dose — the three actions

```
POST  pet_app.api.workspace.perform_action
```
```json
{ "source_type": "preventive", "name": "PCR-00001",
  "action": "administer_preventive", "payload": { … } }
```

`source_type` accepts any of: `preventive`, `preventive care`, `preventive_care`,
`preventive care record`, `vaccination`, `deworming`.

Or pass the **visit** as the source and name the dose in the payload — the visit screen does
not need to switch `source_type`:

```json
{ "source_type": "visit", "name": "VVT-2026-01982",
  "action": "administer_preventive",
  "payload": { "preventive_care_record": "PCR-00001" } }
```

Returns the same shape as `get_record` (§3.3).

| Action | → status | Payload fields accepted (**anything else is ignored**) |
|---|---|---|
| `start_preventive` | `In Progress` | `provider`, `weight` |
| `administer_preventive` | `Administered` | `provider`, `administered_on`, `next_due_date`, `reminder_enabled`, `batch_no`, `dose`, `vaccine_type`, `qty`, `medication`, `weight`, `notes` |
| `cancel_preventive` | `Cancelled` | `cancellation_reason` |

Omitted **and** blank both mean "leave unchanged" — a status action is not an editing endpoint,
so a partial body cannot wipe a `next_due_date`.

Using a foreign action is refused by name: `administer_preventive` on a `PetCareService`, or
`finish_service` / `release` on a Preventive Care Record.

### 3.3 Read one dose

```
GET/POST  pet_app.api.workspace.get_record
{ "source_type": "preventive", "name": "PCR-00001" }
```

**The dose is always at `data.focus_source.detail`** — all 49 fields, the 4 labels resolved,
whether or not it has a visit. One expression, no branch.

```js
const dose = response.data.focus_source.detail;   // 49 fields, both cases
```

What differs between the two cases is only what comes *around* the dose:

| | No visit (28 of 29 doses) | Visit-linked |
|---|---|---|
| `data.focus_source.detail` | the dose, 49 fields | the dose, 49 fields |
| `data.summary` | the dose's own brief — `source_type: "Preventive Care"`, `title: medication_name` | the **visit's** brief |
| `data.pet`, `data.guardian` | `{}` — fetch separately, or carry from the worklist row | populated from the visit |
| `data.billing` | `{}` — use `detail.sales_invoice` | populated, 12 keys |
| `data.clinical`, `data.orders`, `data.diagnoses` | empty | the visit's |
| `data.notes`, `data.attachments`, `data.timeline` | present | present |

> **Changed 2026-09-12.** Until this date the no-visit branch returned the aggregate with
> **no dose fields in it at all** — `data.summary` carried a name, a status and a title, and
> nothing else. There was no `focus_source`, so `cancellation_reason`, `next_due_date`,
> `batch_no`, `administered_by` and 45 others were unreachable on the read the contract
> documented, for the majority of doses on the site. The earlier wording ("→ the source
> aggregate") was accurate and still cost a detail screen its data. A client written against
> the old text keeps working: `summary` is unchanged and `focus_source` is additive.

`perform_action` (§3.2) returns this same shape, so the response to `cancel_preventive` now
carries the `cancellation_reason` it just wrote. Re-reading after an action is no longer
necessary.

**Labels resolved for you**, so a detail screen issues no follow-up reads:
`doctor_label`, `provider_label`, `care_service_label`, `service_option_label`.

**Not in `detail`, deliberately:** `pet_name` and `guardian_name` — `detail` gives you
`pet` and `guardian` as ids. Carry the names from the worklist row you opened, or read the
pet separately. `stock_entry` is on the doctype but is never populated on this path and is
not returned.

### 3.4 The worklist — `/healthcare/preventive-care`

```
GET/POST  pet_app.api.preventive_care.list_preventive_care_records
```
```json
{ "payload": { "open_only": 1, "limit": 50, "cursor": 0 } }
```
```json
{ "ok": true,
  "data": { "records": [ { /* 36 fields + 4 labels + pet_name + guardian_name */ } ] },
  "meta": { "count": 50, "has_more": true, "next_cursor": 50,
            "start": 0, "limit": 50, "filters_applied": [ … ] } }
```

**Filters** — all optional, combined with AND:

| Filter | Effect |
|---|---|
| `name` | exact match — **how you fetch one dose from the worklist** |
| `pet`, `kind`, `status`, `guardian`, `visit`, `branch`, `care_service`, `service_option`, `provider`, `doctor` | exact match |
| `open_only: 1` | `status in (Ordered, In Progress)` — the worklist's default question |
| `overdue: 1` | has a `next_due_date`, it is before today, and neither `status` nor `reminder_status` is `Cancelled` |
| `due_before`, `due_after` | on `next_due_date`; rows with no date are **excluded** |
| `administered_from`, `administered_to` | on `administered_on`; both bounds honoured together |

**Paging.** `limit` defaults to 50, clamped to 100. `cursor` (alias `start`) is a row offset.
`meta.has_more` is a fact, not a guess — one extra row is fetched to determine it. Pass
`meta.next_cursor` back as `cursor` for the next page.

**Ordering.** `administered_on desc, creation desc` when an `administered_*` filter is used,
otherwise `creation desc`.

**Scoping.** Results are filtered by the caller's Branch User Permission — one clinic does not
see another's worklist.

**An unrecognised key is refused, not ignored** (changed 2026-09-12). The keys above are the
complete set this endpoint reads; anything else returns `ok: false` with
*"`<key>` is not a filter on this worklist"*. Previously an unknown key was dropped silently,
so `{ "name": "PCR-00005" }` — before `name` was a filter — returned **every** row with
`ok: true`, and a screen that trusted it rendered the wrong dose. A widened filter is a wrong
answer, not a missing one, which is why this refuses where the write paths ignore.

> A dose with **no** `next_due_date` is never overdue. "No next dose" is a real answer, not a
> missing one, and the backend excludes those rows explicitly.

### 3.5 Rate a dose

Ratable since 2026-09-12, through the same generic calls Lab, Imaging and PetCareService use —
there is no preventive-specific rating endpoint.

```json
{ "method": "frappe.client.insert",
  "args": { "doc": {
    "doctype": "Rating",
    "reference_doctype": "Preventive Care Record",
    "reference_name": "PCR-00001",
    "overall_rating": 4,
    "notes": "Gentle and quick." } } }
```

Read them back with `pet_app.api.ratings.get_ratings`:

```json
{ "reference_doctype": "Preventive Care Record", "reference_name": "PCR-00001" }
```
```json
{ "ratings": [ { "overall_rating": 4, "entity_name": "Rabies Vaccination",
                 "performer_name": "dr Admin", "pet_name": "Lemon",
                 "sentiment": "positive", "tags": [] } ],
  "stats": { "total": 1, "average": 4.0, "distribution": { "4": 1, … } } }
```

- `overall_rating` is an **integer 1–5** and is required. Never send a decimal.
- `questionnaire` is optional; none is scoped to this doctype, so omit it — and when it is
  blank, send no `answers` rows.
- The performer is resolved from `provider`, falling back to `doctor`; the rating names the
  dose by `medication_name`.
- One user may rate a given dose once per questionnaire. A duplicate is refused — load the
  existing rating and offer edit via `frappe.client.save`.

### 3.6 Correct a dose that has not happened yet

```
POST/PUT  pet_app.api.preventive_care.update_preventive_care_record
```
```json
{ "name": "PCR-00001",
  "payload": { "batch_no": "B-7741", "qty": 1, "next_due_date": "2027-10-01" } }
```

Returns `{ "record": { /* the same 49 fields §3.3 returns */ } }`, so a screen re-renders from
the response without a second read.

**Allowed only while `Ordered` or `In Progress`.** An `Administered` or `Cancelled` dose is
refused: *"PCR-00001 is Administered and can no longer be edited. Record a correction as a new
dose."* A charge exists, a named person is recorded as having given it on a named day, and the
invoice that relieves the stock has been raised — a correction is a new dose, and the wrong one
stays visible.

**Editable fields — anything else is refused by name, not ignored:**

`provider` · `due_date` · `scheduled_datetime` · `priority` · `weight` · `qty` · `dose` ·
`vaccine_type` · `batch_no` · `medication` · `next_due_date` · `reminder_enabled` · `notes`

Sending `status`, `billed`, `sales_invoice`, `item_code`, `rate`, `pet` or `visit` returns
`ok: false` naming the field. Derived and lifecycle-owned fields are never editable.

> **Blank clears; absent leaves alone.** The opposite of the status actions (§3.2), and
> deliberately so — this is the editing endpoint, so `"next_due_date": ""` is how a client says
> *there is no next dose after all*. A key that is not in the body is untouched.

**The gate is enforced below the endpoint too** (new 2026-09-12). A dose that committed
something — an item code, a charge, a staff administration or cancellation stamp, stock — is
frozen in the controller once it reaches a terminal status, so `frappe.client.save` and
`frappe.client.set_value` are refused as well. Until this date those generic writes could
rewrite an administered dose, including clearing `billed` and `sales_invoice`. A client should
still gate its own controls on `status`, so a closed dose shows no edit affordance at all.

---

## 4. Behaviour the UI must reflect

### 4.1 Billing

| Case | What happens at `administer_preventive` |
|---|---|
| **No visit** | Billed directly to the guardian: appended to the branch's open Draft invoice, or a new one. `billed = 1`, `sales_invoice` set |
| **Visit-linked** | The charge is on `Vet Visit.billable_items` from creation (`item_type = "Preventive"`, qty 1). At administration it is invoiced through the visit's own path. `billed = 1`, `sales_invoice` set |
| **Cancelled** | No charge, ever. The visit's billable row is marked `Cancelled` |
| **History** (`kind` only) | Can never be billed — no `item_code` |

Exactly one biller can ever act on a given dose, so a double charge is impossible. Invoices stay
**Draft** for the cashier to settle; nothing is submitted here.

### 4.2 Two stock messages, and they are not the same

| | |
|---|---|
| **Non-blocking** — arrives on a **successful** response | *"… has a configured consumable (X, quantity N) with no corresponding stock item on its invoice line…"* The dose **was** administered and **was** billed. Surface it as a warning; **do not** show it as a failure |
| **Blocking** — `ValidationError`, nothing written | *"… records quantity N, but its invoice bills 1 stock unit(s) of the same item. Correct the quantity before administering it."* |

### 4.3 Ordering from a visit

Add to `Vet Visit.orders` with `kind: "preventive"` (`vaccination`, `vaccine`, `deworming`,
`deworm` are all accepted and normalise to it), via the existing `create_orders` action:

```json
{ "source_type": "visit", "name": "VVT-…", "action": "create_orders",
  "payload": { "orders": [
      { "kind": "vaccination", "template_id": "CareService-00022" },
      { "kind": "deworming",   "service_option": "CSBO-00051" }
  ] } }
```

Optional per order: `provider`, `due_date`, `weight`, `note`, `priority`, `scheduled_at`.
Neither `template_id` nor `service_option` → refused: *"Order X requires a Care Service or a
Service Option."*

- **Two different bands on one visit produce two distinct records** — they no longer collide.
- Re-posting the same orders creates no duplicates.
- The `Visit Order` row gets `linked_doctype: "Preventive Care Record"` and `linked_name`.
- Row status tracks the record: `Administered` maps to `Completed`.

### 4.4 The next dose is raised automatically

**New 2026-09-13**, reversing an earlier decision. Administering a dose that carries a
`next_due_date` creates the next one as an `Ordered` record due on that date, so the worklist
shows what is owed instead of the date living only on the dose that is finished.

```
parent (Administered, next_due_date 2027-09-13)
   └── child (Ordered, due_date 2027-09-13, source_doctype/source_name → parent)
```

| The child carries | |
|---|---|
| **Copied** | `pet`, `guardian`, `care_service` / `service_option`, `branch`, `reminder_enabled` |
| **Re-derived from the catalogue** | `kind`, `category`, `item_code`, `rate`, `medication`, `medication_name`, `qty` |
| **Empty** | `provider`, `doctor`, `weight`, `visit`, `scheduled_datetime`, `batch_no`, `dose`, `vaccine_type`, `notes`, **`next_due_date`** |

Nothing observed about the parent travels. A substituted vial, a deliberate half dose, last
year's weight and the batch from the fridge all describe the dose that **was** given;
presenting any of them as the plan for a dose a year away would assert something nobody
checked. `priority` is not inherited either — an urgent booster today does not make next
year's booster urgent.

**The child gets no `next_due_date`.** That keeps §1.2's rule intact (the field is
operator-chosen, never computed), keeps the reminder sweep pointed at one record per series,
and means **the chain advances exactly one step per dose actually given** — administering the
child raises a grandchild only if a human sets a new date. There is no runaway series.

**It bills nothing.** Creation raises no charge, no invoice line, no billable row and no stock
movement — `billed = 0`, `sales_invoice` empty, exactly as §4.1 requires. The charge still
lands at administration, on whoever administers the child.

**Duplicates are prevented by two existence checks, not by counting.** A dose that already
raised its next one raises nothing further, and an open dose for the same pet, protocol and
day is never raised twice — so two doses of one protocol administered on the same day share a
single recall. Re-running the transition is safe.

**A dose is never raised** when the parent has no `next_due_date`, when the parent is a
history record (`kind` alone — a child with no Item Code could never be administered), or when
either check above already holds.

If the child cannot be created — a deceased pet, a catalogue row deleted since — the
administration still succeeds and is still billed, and an orange *"Next dose not created"*
message names the date to raise by hand. The treatment is the fact; the recall is the
convenience.

> **A dose that is never given stays `Ordered` indefinitely, and that is the intended cost.**
> It appears in the worklist under `open_only` — which is the recall list — and **nowhere
> else**: it does not match the `overdue` filter (that keys on `next_due_date`, which the
> child has none of), raises no reminder, and does not move the pet's compliance standing or
> the overdue alert, because standing is decided by the most recently *administered* record.
> Close one with `cancel_preventive`; nothing expires it automatically.

---

## 5. The read surface

### 5.1 Route

**`/healthcare/preventive-care`** — one screen for both kinds. The backend emits this route
from the pet dashboard's preventive-lapse insight.

> `/healthcare/vaccinations` and `/healthcare/deworming` are **gone**. They were emitted by the
> backend and never existed in the SPA — both links were dead. Do not build them.

### 5.2 Timeline and portal events

`medical_file.get_pet_medical_timeline` and the guardian portal emit:

```json
{ "type": "vaccination" | "deworming",
  "source_doctype": "Preventive Care Record",
  "name": "PCR-00001", "summary": "<medication_name>",
  "at": "<administered_on>", "next_due_date": "…", "status": "<reminder_status>" }
```

`Cancelled` doses are excluded from **every** read surface — timelines, guardian history,
compliance standing, the overdue alert, the mobile listing, and the reminder sweep.

### 5.3 Dashboard

`clinical.preventive` carries `due_count`, `overdue_count`, `next_due` and per-protocol items,
computed **per kind** — a pet up to date on worming and overdue on rabies is *overdue*, not
half-compliant. The overdue warning key is still `"vaccination"` (unchanged, so no client
switch breaks); its label reads "Preventive care overdue".

### 5.4 Reminders

One sweep over one doctype. `kind` decides the type: `Vaccination Due` / `Deworming Due`.
`Pet Reminder.reference_doctype` is now `"Preventive Care Record"`. A reminder is raised only
when `reminder_enabled = 1`, `next_due_date` falls in the window (30 days ahead to 90 days
back), and `status != Cancelled`. When it sends, the record's `reminder_status` becomes `Sent`.

### 5.5 Mobile — externally unchanged

`pet_app.api.mobile.pets.{list,add,update,delete}_medical_record` keep their shape:
`record_type` is still `"vaccination"` / `"deworming"`, and **`vaccine_name` is still accepted
on the way in and echoed back**, so an app built against the previous shape keeps working.

Underneath: one doctype, `kind` separates the types, and the single label field is
`medication_name`. Three things changed that a client can observe:

1. `id` is now `PCR-…`
2. `source_doctype` is `"Preventive Care Record"`
3. The listing returns **`Administered` rows only** — an ordered dose has not happened yet and
   belongs to the clinic worklist, not the pet's history

A guardian-created record is history: no catalogue row, no price, created `Administered`, and
it can never be billed. **It stays editable** through `update_medical_record` — correcting a
self-reported dose is what that endpoint is for, and the terminal-status freeze in §3.6 does
not apply to it, because it committed nothing.

**A clinic dose is not editable there** (new 2026-09-12). `update_medical_record` and
`delete_medical_record` now refuse any record the clinic committed — an item code, a charge, a
staff stamp, stock — with HTTP 403 and code `pet.medical_record_clinic_owned`:

```json
{ "error": { "code": "pet.medical_record_clinic_owned",
             "message": "This dose was recorded by the clinic and cannot be changed here. Ask the clinic to correct it." } }
```

Previously the check tested only that the record belonged to the pet, so a guardian could edit —
or delete — a dose the clinic gave. The code is distinct from `pet.medical_record_not_found` on
purpose: these rows still appear in the pet's history and should render, with the edit and
delete controls hidden rather than the record.

---

## 6. Refusals the UI should pre-empt

All `ValidationError` with an operator-readable message naming the field and the fix. **Nothing
is written** when one fires.

| Condition | Message shape |
|---|---|
| No `pet` | "Pet is required on a Preventive Care Record." |
| None of `care_service` / `service_option` / `kind` | "…must name a Care Service, a Service Option, or a Kind…" |
| Template and band in different categories | "Care Service X is in category A but Service Option Y is in category B…" |
| `kind` contradicts the catalogue | "This record is marked X, but its catalogue rows resolve to Y." |
| Category is not Vaccination/Deworming | "Category X is not a preventive care category…" |
| Neither catalogue row has an Item Code | "…cannot be priced or billed. Set Item Code on the catalogue row…" |
| `next_due_date` on or before the dose date | "Next Due Date must be after the date this dose is given…" |
| `branch` missing for an unrestricted user | `MandatoryError` — "Select a branch for this Preventive Care Record…" |
| `source_doctype` without `source_name` | "A source record needs both Source Doctype and Source Name." |
| Billing at administration with no price | "…has no price, so it cannot be billed…" — stays `Ordered` |
| Stock quantity disagrees with the invoice | "…records quantity N, but its invoice bills 1 stock unit(s)…" — stays `Ordered` |
| Consumable configured with no quantity | "Service Option X consumes Y but has no Stock Deduction Qty…" — stays `Ordered`; fix the band, not the input |
| Visit already invoiced | the standard billed-visit lock message |
| Action from a terminal status | "administer_preventive is not allowed for Preventive Care Record while status is Administered." |
| Foreign action | "administer_preventive needs a Preventive Care Record…" / "This action requires a service record." |
| Editing a dose that is `Administered` or `Cancelled` | "PCR-00001 is Administered and can no longer be edited. Record a correction as a new dose." — from `update_preventive_care_record`, and from any generic write once the dose committed something |
| Editing a field that is not editable | "billed, sales_invoice cannot be changed on a Preventive Care Record. Editable: …" |
| Guardian editing a clinic dose via mobile | `403` `pet.medical_record_clinic_owned` — "This dose was recorded by the clinic…" |

**There is no "A Vet Visit or a source record is required" refusal.** An earlier draft of this
document listed one, copied from `Lab`. It does not apply: a record with neither is legal.

---

## 7. Permissions

The doctype ships with System Manager only; a patch grants the role set its predecessors held —
the union of `Pet Vaccination Record`, `Pet Deworming Record` and `PetCareService`, strongest
flag wins. **38 roles** get read/write/create as appropriate, including `Doctor`,
`Pet App Admin`, `Coordinator`, `Reception`, `Groomer` and the Visit/Lab admin roles.

`delete` is **not** granted to any of them — only System Manager can delete a dose. A client
should not offer a delete action; use `cancel_preventive`.

---

## 8. What the frontend must stop doing

| Stop | Start |
|---|---|
| Reading `Pet Vaccination Record` / `Pet Deworming Record` | `Preventive Care Record` — the old doctypes are gone |
| Reading `vaccine_name` | `medication_name` (mobile keeps the alias) |
| Linking to `/healthcare/vaccinations`, `/healthcare/deworming` | `/healthcare/preventive-care` |
| `kind: "service"` orders for vaccination | `kind: "preventive"` |
| `start_service` / `finish_service` / `close_service` on a dose | `start_preventive` / `administer_preventive` / `cancel_preventive` |
| Calling `list_vaccination_deworming_services` | Removed — it had no callers. Use §3.4 |
| Treating `stock_issued_qty` as "issued" | Always empty; stock moves on invoice submit |
