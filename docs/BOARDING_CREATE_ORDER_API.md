# Boarding API contract — `create_order` and `reserve_room`

**Status** current as of 2026-08-17
**Breaking:** `create_order` now requires `pet`. **Additive:** `reserve_room` gains `petIds`;
`create_order` gains `dose_option` and `warehouse` for `kind=medication`, and **`provider`**
for every kind.

Everything here is read from the implementation and from real responses captured against
a live site. Where a shape is surprising it is called out rather than smoothed over.

| Endpoint | Change | Breaking? |
|---|---|---|
| [`create_order`](#part-a--create_order) | `pet` required | **yes** — ship with the client |
| [`create_order`](#2-request) | `dose_option`, `warehouse` added | no — both optional |
| [`create_order`](#22-provider--who-performs-the-work) | `provider` added; echoed on `billable_items[]` with `provider_name` | no — optional, absence unchanged |
| [`reserve_room`](#part-b--reserve_room) | `petIds` list added | no — `petId` still works |
| [`get_boarding_detail`](#1-the-occupant-read-shape) | now returns `occupants[]` | no — additive |
| [`add_occupant`](#4-add_occupant--a-pet-joins-an-existing-booking) | new — a pet joins mid-stay | no — new endpoint |
| [`depart_occupant`](#2-depart_occupant--one-pet-goes-home-the-booking-runs-on) | new — one pet leaves, booking runs on | no — new endpoint |
| [`transfer_occupant_room`](#3-transfer_occupant_room--move-one-pet-to-another-room) | new — move one pet between rooms | no — new endpoint |
| [`list_boarding_units`](#part-d--the-hub-list_boarding_units) | room-grained; **`occupancy` changed meaning** | **watch** — see Part D §4 |
| [occupant rows](#31-the-photo-and-the-animal-type--now-sent) | `animal_type`, `pet_image` added — both grains | no — additive |
| [room cards](#23-capacity--the-denominator-and-what-it-is-not) | `capacity` added — the real denominator for `n / m` | no — additive |

Parts A and B land in the same release. Part C describes the Stage 5 occupant model, which
raised `max_pets_per_booking` from 1 to **7** — so a booking now really does hold several
pets, and the three occupant endpoints are how one joins, leaves or moves. Part D is the
hub, whose grain is the room rather than the booking.

---

# Part A — `create_order`

**Endpoint** `POST /api/method/pet_app.api.healthcare.boarding.create_order`

---

## 1. Breaking change

`pet` is a **new required parameter**. A request without it is refused and creates nothing.

It is required rather than defaulted to the booking's pet on purpose: a default would be
correct today — a booking holds one pet — and would silently file every order against the
wrong animal the day a booking holds seven. There is no transitional period where omitting
it works.

**Backend and client must deploy together.** Until the client sends `pet`, every boarding
order fails.

---

## 2. Request

Parameters are **flat form fields**. This endpoint has **no `data={...}` envelope** and no
`**kwargs` — unlike `start_visit_boarding` and `dispense_medication`, which do. A nested
`data` object is ignored, and you will get *"Pet Boarding is required."*

Because there is no `**kwargs`, **only the names in the table below reach the function**;
Frappe filters everything else out of `form_dict` before the call, silently. So a parameter
that is not listed here does nothing at all — no error, no effect. If you need to send
something new, the signature has to gain it first. Conversely, every name in this table
*is* a real declared parameter, verified against the live signature, not just documented.

| Name | Type | Required | Notes |
|---|---|:--:|---|
| `boarding_id` | string — Pet Boarding name | **yes** | e.g. `BRD-00055` |
| `kind` | enum | **yes** | `lab` · `radiology` · `service` · `medication`. Lower-cased server-side |
| `pet` | string — Pet name | **yes** | e.g. `PET-02075`. Must be a pet on that booking |
| `template_id` | string | **yes** | CareService template name for `lab`/`radiology`/`service`; **Medication name** for `medication` |
| `care_service_id` | string | no | Defaults to `template_id`. Only meaningful for `kind=service` |
| `item_code` | string — Item | no | Billing override. For `medication`, falls back to `Medication.linked_item` |
| `priority` | enum | no | `Routine` · `Normal` · `High` · `Urgent`. Title-cased server-side; default `Routine`. Ignored for `medication` |
| `note` | string | no | Free text |
| `dose_option` | string — Medication Dose Option name | no | **`medication` only**, ignored for other kinds. Which dose this order is for. Needed only when the medication offers more than one enabled dose option — see [2.1](#21-dose_option-and-warehouse-medication-only) |
| `warehouse` | string — Warehouse name | no | **`medication` only**, ignored for other kinds. Where the goods leave from at dispense. Blank falls back to `Medication.default_warehouse`, then Stock Settings' default |
| `dosage` | string | no | **`medication` only.** Free text — `"0.5 cc"`, `"1 tablet"`. Not `dose_option` |
| `frequency` | string | no | **`medication` only.** Free text — `"BID"`, `"every 8h"` |
| `duration_days` | int | no | **`medication` only.** Stored only; does **not** multiply `qty`, `rate` or `amount` |
| `scheduled_datetime` | string — datetime | no | When the order is due. Never defaulted; absent means "do it now". All kinds |
| `provider` | string — **Healthcare Practitioner name** | no | Who performs the work. Never defaulted to the caller. All kinds — see [2.2](#22-provider--who-performs-the-work) |

### 2.1 `dose_option` and `warehouse` (medication only)

Both are optional and both exist for the same thing: medication dispensing now moves real
stock, but **only for a medication the owner has opted in** by giving it a Medication Dose
Option with a Stock Deduction Qty. A medication with no dose option deducts nothing, and
these two parameters change nothing for it.

- **`dose_option`** — send it when the medication has **more than one** enabled dose option.
  With exactly one, the dispense resolves it on its own; with several and none named, the
  **dispense** refuses rather than guessing which dose was given.
- **`warehouse`** — send it when the stay should draw on its own store rather than the
  clinic's. Boarding carries its own branch precisely because the facility is a distinct
  place, and this is the per-row equivalent for stock.

**Both are stamped on the billable row at order time and read at dispense time.** That split
matters, because validation is split with it:

| Sent | Rejected at order time | Rejected at dispense time |
|---|---|---|
| `dose_option` that does not exist | **yes** — `Could not find Row #2: Dose Option: NOPE-123` | — |
| `warehouse` that does not exist | **yes** — `Could not find Row #2: Warehouse: X` | — |
| `dose_option` belonging to a *different* medication | no — accepted | **yes** — `Dose Option <b>X</b> does not belong to Medication <b>Y</b>.` |
| a **group** warehouse (e.g. `All Warehouses - K`) | no — accepted | **yes** — `Warehouse <b>X</b> is a group warehouse and cannot hold stock.` |
| a warehouse the user is restricted from | **yes** — `Not permitted` | — |

So an order can be created successfully and still fail to dispense. A client that treats a
`201` on `create_order` as proof the dose is dispensable will surface the error late, at the
bedside — in front of the guardian, with the animal waiting, and with nobody at the kennel
able to fix a catalogue problem.

#### What to check on the way back

Both values **are echoed back** on `data.billable_item` (and on the matching row in
`data.billable_items[]`). They are currently discarded by the client. Reading them is what
moves the two deferred failures from dispense time to order time:

| Check on `billable_item` | Why | If it fails |
|---|---|---|
| `dose_option` equals what you sent | Confirms it was stored rather than dropped | Treat the order as not dispensable; re-send |
| `dose_option` is one of **this medication's** options | A dose option belonging to another medication is **accepted at order time** and only refused at dispense. The client already holds the medication's option list — it rendered the picker from it — so it can catch this locally | Show `Dose Option X does not belong to Medication Y` at order time and let them re-pick |
| `dose_option` is non-null **when the medication has ≥2 enabled options** | With several options and none named, the dispense refuses rather than guessing the dose | Require the choice in the order form; do not let the order be placed without it |
| `warehouse` equals what you sent | Same storage confirmation | Re-send |
| `warehouse` is **not a group warehouse** | A group warehouse (`Warehouse.is_group = 1`, e.g. `All Warehouses - K`) is accepted at order time and refused at dispense. Filter groups out of the picker; verify on the echo | Show `Warehouse X is a group warehouse and cannot hold stock` and let them pick a leaf |
| `stock_issued_qty` is `0` | A fresh order has never moved stock. Non-zero means this row was already dispensed | Do not offer "dispense" again |

None of these require an extra request — every value needed is already in the
`create_order` response, and the medication's dose options are already in hand.

**A medication with no dose option at all deducts no stock and none of this applies to it.**
That is 260 of 264 medications today, so for most orders these checks are no-ops. They
matter for exactly the medications the owner has deliberately configured — which are also
the ones where a failed dispense is expensive.

### Example

```
POST /api/method/pet_app.api.healthcare.boarding.create_order
Content-Type: application/x-www-form-urlencoded

boarding_id=BRD-00055&kind=lab&pet=PET-02075&template_id=CareService-00007&priority=High&note=Fasting%20sample
```

### Preconditions on the booking

All produce errors: `docstatus` must be `0`, `sales_invoice` must be empty, and
`record_status` must be **`Checked In`** — not `Reserved`, `Checked Out`, `Cancelled`, or
`Pending Room`.

### Permissions

One of `System Manager`, `Healthcare Practitioner`, `Doctor`, `Accounts User`, `Healthcare`,
`Accounting`, **or** write permission on Pet Boarding. Plus `create` on the target doctype
(`Lab` / `Imaging` / `PetCareService`), or `read` on `Medication`.

---

### 2.2 `provider` — who performs the work

**Send the Healthcare Practitioner docname**, e.g. `HCP-00072`. Not a User email.

`provider` is a **Link to `Healthcare Practitioner`** everywhere it exists in this system —
`PetCareService.provider`, `Pet Procedure.provider` and now `Pet Billable Item.provider`.
This is the same identifier the visit path has always wanted; the picker's `option.id` is
already correct and needs no change.

> Earlier revisions of [`frontend-visit-orders-billing-handoff.md`](frontend-visit-orders-billing-handoff.md)
> showed `provider: "provider@example.com"`. That was **wrong documentation, not a wrong
> field** — it has been corrected. Visit-path assignment was never broken by it.

A **User email is accepted as a convenience** and resolved through
`Healthcare Practitioner.user_id`, so a caller holding only a login still gets the
assignment stored. It is resolved, not stored: what comes back is always the docname. If
the email matches two practitioners the call is refused rather than guessing.

**Optional, and absence stays valid.** An unassigned order is the ordinary "whoever is
free" case and works exactly as before. It is **never defaulted to `frappe.session.user`** —
a defaulted assignee is indistinguishable from a real one, and the desk would lose the
ability to see what still needs assigning. Same rule as `scheduled_datetime`.

**An unresolvable provider refuses the call.** `{"ok": false, "meta": {"code":
"ValidationError"}}` with *"Provider `X` is not a Healthcare Practitioner."* Nothing is
created — no order document, no billable row. Resolution happens before the first write, so
a bad value cannot leave a Lab behind and fail on the boarding save.

**Where it is stored, per kind.** It always lands on the billable row. It additionally lands
on the order document wherever that document has somewhere to put it:

| kind | order document | `provider` on the document | on the billable row |
|---|---|:--:|:--:|
| `service` | `PetCareService` | ✅ `PetCareService.provider` | ✅ |
| `lab` | `Lab` | ❌ — `Lab` has `doctor`, not `provider` | ✅ |
| `radiology` | `Imaging` | ❌ — `Imaging` has `doctor`, not `provider` | ✅ |
| `medication` | *(none — the row is the order)* | — | ✅ |

Lab and Imaging carry a `doctor` (who ordered) and no `provider` (who performs). Rather than
add a field to two shared clinical doctypes for boarding's sake, the assignment for those
kinds lives on the billable row alone — which is the row the orders panel reads, so it is
visible either way. If a lab or imaging assignee needs to reach the Lab/Imaging worklists
themselves, say so and it becomes a separate change.

**Medication accepts it too.** The frontend does not offer an assignee on medication and is
not asked to, but a `provider` sent with `kind=medication` is stored rather than dropped —
dropping a value the caller supplied is the defect this parameter exists to close.

**No billing effect.** Who performs the work never touches `qty`, `rate` or `amount`, and
never reaches the invoice. No availability, roster or qualification check is performed: a
wrong assignment is a desk decision, not an API refusal.


## 3. Response

Every response is `{ok, data, meta, errors}`. **Every key inside `data` is also duplicated
at the top level** for legacy callers — it is not a second payload. **Read from `data`.**

### 3.1 Success — `lab` · `radiology` · `service`

```json
{
  "ok": true,
  "data": {
    "success": true,
    "order_id": "IMG-00036",
    "kind": "radiology",
    "pet": "PET-00129",
    "boarding_id": "BRD-00044",
    "linked_doctype": "Imaging",
    "reused": false,
    "total_cost": 80000.0,
    "balance": 80000.0,
    "billable_items": [ /* every row on the booking — see 3.3 */ ]
  },
  "meta": {},
  "errors": []
}
```

### 3.2 Success — `medication` (a different shape)

```json
{
  "ok": true,
  "data": {
    "success": true,
    "order_id": "BRD-00044-medication-56eb9349b7",
    "item_id": "hr53queo6q",
    "kind": "medication",
    "pet": "PET-00129",
    "boarding_id": "BRD-00044",
    "linked_doctype": "Medication",
    "linked_name": "Vitamin K3",
    "reused": false,
    "total_cost": 80000.0,
    "balance": 80000.0,
    "billable_item": { /* the row just added */ },
    "billable_items": [ /* every row on the booking */ ]
  },
  "meta": {}, "errors": []
}
```

> ### ⚠ `order_id` means two different things
>
> For `lab` / `radiology` / `service` it is a **document name** — `Lab`, `Imaging` or
> `PetCareService` — and you can fetch it directly.
>
> For `medication` there is **no order document at all**. `order_id` is an opaque string,
> and the thing you can fetch is **`item_id`**, the billable-row name. `item_id` is also
> what `dispense_medication` expects.
>
> A client that treats `data.order_id` uniformly will break on medication.

### 3.3 `billable_items[]` row shape

| Field | Notes |
|---|---|
| `name` | row id — this is `item_id` for medication |
| `pet` | which animal this charge is for; `null` on rows created before the pet column existed |
| `item_name`, `item_code` | |
| `item_type` | `Room Stay` · `Service` · `Medication` · `Lab` · `Imaging` · `Procedure` · `Product` · `Other` |
| `qty`, `rate`, `amount` | |
| `status` | `Draft` · `Billable` · `Billed` · `Cancelled` · **`Included`** |
| `note` | reaches the invoice as the line description |
| `linked_service_id`, `linked_doctype`, `linked_name`, `order_id` | |
| `care_episode` | |
| `dispense_status`, `dispensed_qty`, `dispensed_by`, `dispensed_at` | medication only |
| `return_qty`, `returned_by`, `returned_at` | medication only — written by `return_boarding_medication` |
| `dose_option`, `warehouse` | medication only — echoed back exactly as sent, or `null` |
| `stock_issued_qty` | medication only — stock units actually issued at dispense, in the **Item's own stock UOM**. `0` means no stock moved, which is the normal case for a medication with no dose option |
| `stock_entry` | medication only — the most recent Stock Entry this row issued or returned; `null` when none |
| `dosage`, `frequency`, `duration_days` | medication only — echoed exactly as sent, or `null` |
| `scheduled_datetime` | when the order is due, or `null`. All kinds |
| `provider` | **Healthcare Practitioner name** performing the work, or `null` when unassigned. All kinds |
| `provider_name` | display name for `provider`, or `null`. Present so the orders panel needs no second lookup |

> **`provider` is read back, never assumed.** The row is the only proof the assignment
> stored. If a client's `provider` failed to store, this reads `null` — the order shows as
> unassigned rather than showing the name still sitting in the form. Render from this field,
> not from what you sent. Same rule as `scheduled_datetime`.

> **`Included` is not `Cancelled`.** A medication on a Treatment boarding is absorbed by the
> medical rate: the row keeps its true `rate`, never reaches an invoice line, and **still
> dispenses and still moves stock**. Do not hide or grey out `Included` rows — they are the
> record of what the inclusive rate covered.

---

## 4. Errors

```json
{
  "ok": false,
  "data": {},
  "meta": {"code": "ValidationError"},
  "errors": [{"message": "Pet is required. Send the pet this order is for.",
              "details": "Traceback (most recent call last): ..."}]
}
```

Read **`errors[0].message`**. Two warnings:

- **Messages can contain HTML.** `frappe.bold` emits `<strong>…</strong>`, e.g.
  *"Pet `<strong>`PET-00001`</strong>` is not on Pet Boarding `<strong>`BRD-00055`</strong>`."*
  Render or strip.
- **`meta.code` is not stable.** Thrown errors give `"ValidationError"`; the medication
  path's own validation returns `"VALIDATION_ERROR"`. **Do not switch on it** — branch on
  `ok === false` and show the message.

| Trigger | Message |
|---|---|
| `pet` omitted or blank | `Pet is required. Send the pet this order is for.` |
| `pet` not on the booking | `Pet <b>X</b> is not on Pet Boarding <b>Y</b>.` |
| `boarding_id` missing | `Pet Boarding is required.` |
| bad `kind` | `Invalid order kind <b>x</b>. Expected one of: lab, radiology, service, medication.` |
| `template_id` missing | `template_id is required.` |
| bad `priority` | `Invalid priority <b>x</b>. Expected one of: Routine, Normal, High, Urgent.` |
| booking not checked in | `Only checked-in Pet Boarding records can have orders.` (plus specific variants for Reserved / Checked Out / Cancelled / already invoiced) |
| no permission | `Not permitted` — `meta.code: "PermissionError"` |
| unknown medication | `Medication <b>X</b> was not found.` — `meta.code: "VALIDATION_ERROR"` |

---

## 5. Deduplication the client must handle

Two calls with the same `(boarding_id, kind, care_service, pet)` within **120 seconds**
return the **first** order with **`reused: true`** and create nothing new.

**That is a success, not an error.** Do not surface it as a failure and do not retry it.

Since the `pet` parameter landed, a different `pet` is no longer deduplicated against — two
animals can receive the same test within the window and each gets its own order.

---

## 6. Known gaps

- **`errors[0].details` returns a full Python traceback**, including server file paths, to
  the client. It predates this contract and is recorded here so it is not lost. Do not
  display it to end users.
- **`order_id` is not unique and never was.** Two orders for the same pet and service
  outside the dedup window share one. Treat the document name (`data.order_id` for
  non-medication kinds, `data.item_id` for medication) as the identity.
- **Room Stay rows have `pet: null`** until per-occupant pricing lands. Charges created
  through this endpoint always carry their pet.


---

# Part B — `reserve_room`

**Endpoint** `POST /api/method/pet_app.api.healthcare.boarding.reserve_room`
**Additive — nothing breaks.** Existing calls sending `petId` keep working unchanged.

## B1. What changed

`petIds` — a **list** of Pet names — is added alongside the existing single `petId`.
Adopt it when ready.

| | |
|---|---|
| `petIds` supplied | it wins |
| `petId` supplied alone | still works, exactly as before |
| **both** supplied and they agree | accepted |
| **both** supplied and they disagree | **refused** — a caller in two minds is told, not guessed at |

`petIds` accepts a real JSON array (`["PET-1","PET-2"]`) or a JSON-encoded string. Order is
preserved: the first entry becomes the booking's nominal pet.

## B2. Capacity — refused, never truncated

`Pet Boarding Settings.max_pets_per_booking` is currently **1**. A request naming two pets
is **refused**:

> This booking would hold 2 pets. The limit is 1. Reserve a second room for the rest.

It is never silently truncated to the first pet. When the setting rises to 7, the identical
request is accepted with no client change — only the number moves.

## B3. Parameters

| Name | Type | Required | Notes |
|---|---|:--:|---|
| `roomId` | string — Service Room | **yes** | must be `Active` and unoccupied |
| `petIds` | list of Pet names | one of | preferred |
| `petId` | string — Pet name | one of | legacy, still supported |
| `guardianId` | string — Guardian | no | derived from the first pet when omitted |
| `checkIn` / `checkOut` | datetime | no | |
| `boardingType` | `Travel` · `Treatment` | no | defaults from settings |
| `note` | string | no | |
| `boarding_id` / `boardingId` / `name` | string | no | assigns a room to an existing **Pending Room** booking; pet parameters are ignored on that path |

## B4. Success

```json
{
  "ok": true,
  "data": {
    "success": true,
    "boarding_id": "BRD-00060",
    "room_id": "A1",
    "record_status": "Reserved",
    "occupancy": "Reserved",
    "customer": "CUST-0001",
    "pets": ["PET-00459"],
    "boarding": { /* full boarding, as elsewhere */ }
  },
  "meta": {}, "errors": []
}
```

`pets` echoes what was reserved, in order. One occupant row is created per pet, **all
sharing the reserved room** — a shared room is the normal case; a pet gets its own room only
through a later per-pet transfer.

## B5. Refusals

| Trigger | Message |
|---|---|
| neither `petId` nor `petIds` | `Pet is required. Send petIds (a list) or petId.` |
| `petIds` empty | `petIds must name at least one pet.` |
| same pet listed twice | `Pet <b>X</b> appears twice in petIds.` |
| `petId` not in `petIds` | `petId <b>X</b> is not in petIds (<b>…</b>). Send one or the other, or the same pets in both.` |
| `petId` plus a longer `petIds` | `petIds holds N pets while petId names only <b>X</b>. Send petIds alone.` |
| over capacity | `This booking would hold N pets. The limit is M. Reserve a second room for the rest.` |
| pet not found | `Pet <b>X</b> was not found.` |
| **pet is deceased** | `Pet <b>nezoko (PET-00191)</b> is deceased (died 2026-07-19, death record PDR-2026-00011) and cannot be boarded.` — checked **before** the guardian and already-boarding rules, because no other answer can clear it |
| pet not the guardian's | `Pet <b>X</b> is not linked to Guardian <b>Y</b>.` |
| **pet already boarding** | `Pet <b>X</b> is already on boarding <b>BRD-…</b> (<b>Checked In</b>).` |
| room inactive / occupied | `Service Room <b>X</b> is inactive.` · `… already has active boarding …` |

Every per-pet failure **names the pet that failed**, so a list that fails on its fourth entry
says so.

Error envelope and the HTML-in-messages and `meta.code` caveats are as in Part A §4.

---

# Part C — occupants: the roster, departure, and transfer

**Endpoints** all under `pet_app.api.healthcare.boarding`.
Stage 5 raised `max_pets_per_booking` from 1 to **7**, so a booking now genuinely holds
several pets and the roster is the thing the UI is built around.

Everything below is read from the implementation and from a live call, not from the design
notes. Where the backend does not yet do what a roster needs, that is stated as a gap
rather than written as if it were shipped.

---

## 1. The occupant read shape

`get_boarding_detail` returns the roster as **`data.occupants`** — an array, one object per
row, in table order — alongside **`data.active_occupant_count`**. It also appears nested at
`data.boarding.occupants`, and on the success payload of every write endpoint that returns
`boarding`, so a client can update in place after a join, departure or transfer.

A booking always has at least one row. An empty array means a pre-Stage-2 record the
backfill has not reached, not a booking with no animals.

### 1.0 `pet` / `pet_id` / `pet_name` are the FIRST occupant, not the booking's pets

The booking object still carries `pet`, `pet_id` and `pet_name`. They name **the first pet
reserved** — the booking's nominal pet, kept because the underlying `Pet Boarding.pet`
column is still mandatory until Stage 6 drops it. On a seven-pet booking they describe one
animal and omit six.

They are **not deprecated in the response and will not be removed yet** — too many readers
depend on them, including list views and reports outside this contract. But:

- **Never render a roster, a headcount, or a per-pet action from them.** Use `occupants[]`.
- **Never infer "this booking has one pet"** from their presence. Use `active_occupant_count`.
- Treat them as a convenience label for the booking, equivalent to `occupants[0]`.

They disappear with the legacy column in Stage 6. A client written against `occupants[]`
today needs no change then; one written against `pet_id` needs rewriting.

### 1.1 `occupancy` is a room state, not the roster — and it changed meaning

`data.occupancy` **is present** and is not a list of animals. It is a one-word room state —
`Available` · `Reserved` · `Occupied` — and a client reaching for a plausible key will find
it and get a string where it expected an array.

**It is now computed from the pets in the room, not from the booking's `record_status`.**
See [Part D §4](#4-occupancy-changed-meaning) for the full rule and what it breaks. In short:
a room is `Available` when no active pet remains in it, which is not the same as "the
booking ended" — and a room whose animals were all transferred elsewhere now reads
`Available` where it used to read `Occupied`.

### 1.1a Two grains in one payload

`get_boarding_detail` returns **both**, and they answer different questions:

| Key | Grain | Answers |
|---|---|---|
| `data.occupants`, `data.active_occupant_count` | **booking** | every pet on this booking, wherever it sleeps |
| `data.room.occupants`, `data.room.active_occupant_count` | **room** | only the pets in this booking's room |
| `data.room.capacity` | **room** | how many animals that room physically holds — the denominator for the roster header. A property of the room, so it is present whether or not anyone is in it. Full detail in [Part D §2.3](#23-capacity--the-denominator-and-what-it-is-not) |

There is deliberately **no booking-grained capacity**. A booking has no size of its own; what
limits it is `max_pets_per_booking`, and that is a setting rather than a field on the payload.

On a booking split across two rooms the two lists differ, and neither is wrong. Use the
booking grain for the stay's roster; use the room grain for anything about a kennel.

### 1.2 The row shape

The source of truth is the `Pet Boarding Occupant` child table on `Pet Boarding`. Most field
names below are the actual column names, passed through verbatim as `billable_items` are —
what the table says is what the response says.

**Three are resolved from the Pet rather than read off the row**, and are marked *(from Pet)*
below: `pet_name`, `animal_type` and `pet_image`. `pet_name` has a column that almost nothing
writes; the other two have no column on the occupant table at all. All three are batched into
the one query that already served `pet_name`, so this costs no extra round trip.

| Field | Type | Nullable | What a null means |
|---|---|:--:|---|
| `pet` | string — Pet name | no | — |
| `pet_name` | string *(from Pet)* | yes | Display name, read-only. Null when the Pet has no `pet_name`; fall back to `pet` |
| `animal_type` | string *(from Pet)* | yes in shape, never in practice | `Cat` · `Dog` · `Bird` · `Rabbit` · `Horse`. **100% populated** — 0 blank across 2,230 pets — so a null means the Pet was deleted underneath the row, not that the type is unknown. This is the axis to key an icon or a label on |
| `pet_image` | string — file URL *(from Pet)* | **yes, often** | No photo on file. 23% of all pets have one, but **19 of the 24 currently boarded do (79%)**. A null is the cue to draw your fallback — it is expected, not a gap |
| `status` | enum | no | `Active` · `Departed` · `Deceased` · `Cancelled` — see 1.3 |
| `boarding_type` | enum | no | `Travel` · `Treatment`. **Per occupant, not per booking** — two pets on one booking may board on different terms, and it is this field, not the booking's, that decides both the rate and whether a medication is `Included` |
| `service_room` | string — Service Room | yes | The room this pet is in *now*. Null on a pre-Stage-2 row; fall back to the booking's `service_room`. May differ from the booking's after a transfer — see §3 |
| `joined_at` | datetime | **yes** | **Null means the animal has not arrived yet** — the room is reserved and nothing is accruing. It is not "zero nights": billing treats it as a one-night placeholder until check-in replaces it |
| `departed_at` | datetime | **yes** | **Null means still here.** Non-null means this pet stopped accruing at that moment, whether it went home, died, or the booking checked out |
| `death_record` | string — Pet Death Record | yes | Read-only. Non-null only alongside `status: "Deceased"` |
| `departure_note` | string | yes | Free text, appended to across events — may hold several notes separated by newlines |

### 1.3 What each `status` means to the UI

| `status` | Physically present? | Owes nights? | Shown in the roster? |
|---|:--:|:--:|---|
| `Active` | yes | yes, still accruing | yes — this is the room's population |
| `Departed` | no | yes, up to `departed_at` | yes, greyed — it stayed and it is billed |
| `Deceased` | no | yes, up to `departed_at` | yes — its charges stand and bill normally |
| `Cancelled` | no | **no** | it never arrived; safe to hide |

Only `Active` occupants hold the room. `room_occupancy` and every availability check read
this field and not the booking's `record_status`, so a roster filtered on `Active` is the
same set the backend uses to decide whether the room is free.

---

## 2. `depart_occupant` — one pet goes home, the booking runs on

**Endpoint** `POST /api/method/pet_app.api.healthcare.boarding.depart_occupant`

**This is not check-out and must not be presented as one.** It closes one occupant and
touches nothing on the booking: no `check_out`, no `record_status`, and **no invoice**.
The animals still in the room keep accruing.

### 2.1 Request

Accepts flat form fields **or** a `data={...}` envelope (unlike `create_order`), and
tolerates `**kwargs`.

| Name | Aliases | Type | Required | Notes |
|---|---|---|:--:|---|
| `boarding` | `boarding_id` | string — Pet Boarding | **yes** | |
| `pet` | `pet_id` | string — Pet | **yes** | Must be an **Active** occupant |
| `departed_at` | — | datetime | no | Defaults to now. Send it when recording a departure after the fact — it is what the pet's nights are billed to |
| `note` | — | string | no | Appended to the occupant's `departure_note` |

### 2.2 Refusals, verbatim

| Trigger | Message |
|---|---|
| `boarding` missing | `Pet Boarding is required.` |
| `pet` missing | `Pet is required. Send the pet that is leaving.` |
| booking not found | `Pet Boarding <b>X</b> was not found.` |
| submitted booking | `Only open Pet Boarding records can have a pet depart.` |
| already invoiced | `Pet Boarding <b>X</b> is already invoiced.` |
| not checked in | `Only Checked In boarding records can have a pet depart.` |
| pet not on the booking, or already closed | `Pet <b>X</b> is not an active occupant of Pet Boarding <b>Y</b>.` |
| **last occupant** | `Pet <b>X</b> is the last pet on Pet Boarding <b>Y</b>. Check the booking out instead - departing the last occupant individually would leave the booking open with an empty room.` |

> ### The last-occupant refusal is a routing instruction, not an error
>
> It fires whenever the pet being departed is the only remaining `Active` occupant. The UI
> must surface it as **"check the booking out instead"** and offer `check_out_boarding` —
> not as a red failure toast. The rule exists because departing the last occupant would
> leave the booking Checked In with an empty room, its stay length accruing against `now`
> and the room unreservable.
>
> **The client can predict it:** the button should read "Check out" rather than "Depart"
> when `occupants.filter(o => o.status === 'Active').length === 1`.

### 2.3 Success

```json
{
  "success": true,
  "boarding_id": "BRD-00100",
  "pet": "PET-00058",
  "departed_at": "2026-08-13 09:14:02",
  "remaining_active_occupants": 2,
  "room_id": "A19",
  "room_released": false,
  "boarding": { /* the full booking, as Part A §3 */ }
}
```

**`room_released` is always `false` here, and it is there to be read.** The booking-level
check-out response reports `"occupancy": "Available"` in its place; a client that reuses
that mapping will show a kennel as empty with animals still in it.

---

## 3. `transfer_occupant_room` — move one pet to another room

**Endpoint** `POST /api/method/pet_app.api.healthcare.boarding.transfer_occupant_room`

**No financial effect, by design.** Rates come from the pet's `animal_type` and its
boarding type and from nothing else, so every room costs the same and a transfer moves no
figure. Room-stint history is never priced from.

### 3.1 Request

| Name | Aliases | Type | Required | Notes |
|---|---|---|:--:|---|
| `boarding` | `boarding_id` | string — Pet Boarding | **yes** | |
| `pet` | `pet_id` | string — Pet | **yes** | Must be an **Active** occupant |
| `service_room` | `room_id` | string — Service Room | **yes** | Destination. Must be `Active` and not another guardian's |
| `note` | — | string | no | **The reason.** Stored on the new `Pet Boarding Room Stint` as `reason`; when omitted it defaults to `Transferred from <old room>.` Worth prompting for — the stint row is the only record of why an animal moved |

### 3.2 Refusals, verbatim

| Trigger | Message |
|---|---|
| `boarding` missing | `Pet Boarding is required.` |
| `pet` missing | `Pet is required. Send the pet being moved.` |
| `service_room` missing | `Service Room is required.` |
| booking not found | `Pet Boarding <b>X</b> was not found.` |
| submitted booking | `Only open Pet Boarding records can be transferred.` |
| already invoiced | `Pet Boarding <b>X</b> is already invoiced.` |
| booking holds no room | `Only a boarding that currently holds a room can be transferred.` |
| pet not an active occupant | `Pet <b>X</b> is not an active occupant of Pet Boarding <b>Y</b>.` |
| lost the race to another writer | `Pet <b>X</b> is no longer an active occupant of Pet Boarding <b>Y</b>.` |
| same room | `Pet <b>X</b> is already in Service Room <b>Y</b>.` |
| destination inactive | `Service Room <b>X</b> is inactive.` |
| destination is another guardian's | `Service Room <b>X</b> is occupied by another guardian (<b>GUARDIAN-…</b>).` |
| destination at capacity | `Service Room <b>X</b> would hold N pets. The limit is M.` |
| **cross-branch** | `Service Room <b>X</b> belongs to branch <b>hotel</b>, but Pet Boarding <b>BRD-…</b> is attributed to branch <b>main</b>. A stay cannot move between branches: check this booking out and create a new one at <b>hotel</b>, so each branch invoices the nights it actually provided.` |

> ### Show the cross-branch message, do not genericise it
>
> Branch is which set of books the stay's money lands in, frozen when the booking is
> created. A stay cannot move between them: keeping the original branch would bill a stay
> to a site the animal has left, and re-stamping would retroactively move revenue between
> branches. The message names the only supported path — check out here, book again there —
> and a generic "transfer failed" strands the operator with no way forward.
>
> **Currently dormant:** every live Service Room has a null `branch`, so the check returns
> no opinion and never fires. It becomes reachable the moment rooms are branched, so
> handle it now rather than discovering it on the day.

### 3.3 Success

```json
{
  "success": true,
  "boarding_id": "BRD-00100",
  "pet": "PET-00058",
  "from_room": "A19",
  "to_room": "A2",
  "booking_room": "A19",
  "total_cost": 505000.0,
  "boarding": { /* the full booking */ }
}
```

**`booking_room` may differ from `to_room`, and that is correct.** The booking's own
`service_room` names one room and can only move when *every* remaining active occupant is
in the same place. With a booking's pets split across two rooms it stays on the room the
rest are in. **Render the roster from each occupant's `service_room`, never from the
booking's** — the occupant rows are the truth, and the booking-level field is a summary
that is wrong by construction during a split.

**`total_cost` is returned so the client can assert it did not change.** It should be
byte-identical to the value before the call.

---

## 4. `add_occupant` — a pet joins an existing booking

**Endpoint** `POST /api/method/pet_app.api.healthcare.boarding.add_occupant`

**Resolved.** An earlier draft of this contract recorded that no endpoint could add a pet to
an existing booking and told clients not to build the affordance. That gap is closed — build
it.

A guardian bringing a second animal on day three no longer needs a second booking, a second
room and a second invoice. **The joining pet pays from the day it arrives**, not from the
day the booking opened: `joined_at` is stamped server-side and each occupant's room-stay row
is priced from its own arrival.

### 4.1 Request

Accepts flat form fields **or** a `data={...}` envelope, and tolerates `**kwargs`.

| Name | Aliases | Type | Required | Notes |
|---|---|---|:--:|---|
| `boarding` | `boarding_id` | string — Pet Boarding | **yes** | Must be `Reserved` or `Checked In` |
| `pet` | `pet_id` | string — Pet | **yes** | Must belong to the booking's guardian and be free |
| `boarding_type` | — | enum | no | `Travel` · `Treatment`. Defaults to the booking's. **Per occupant** — a pet may join a Travel booking on Treatment terms, and its rate and medication-inclusion follow its own value |
| `service_room` | `room_id` | string — Service Room | no | Defaults to the booking's room — see 4.2 |
| `note` | — | string | no | Stored as the opening stint's `reason` |

### 4.2 Which room the joining pet goes to

**It joins the booking's room by default.** That is the ordinary case and needs no parameter.

**A different room is allowed** when `service_room` is sent, validated exactly as a transfer
would be: the room must be `Active`, must not hold another guardian's animals, must have
capacity, and must not belong to another branch.

It is permitted rather than refused because `transfer_occupant_room` already makes
split-room bookings reachable — refusing here would only mean join-then-transfer, which
reaches the same state while leaving a stint recording a room the animal was never in.
Validating once at the door is more honest than a rule that is one extra call to get around.

When the pets end up split, **the booking's own `service_room` does not move** — it names
the room the rest are in. Render from each occupant's `service_room`.

### 4.3 Capacity

Counted against **active** occupants, never rows. A booking that held seven and saw one go
home has room for another; a departure does not permanently consume a slot.

| Trigger | Message |
|---|---|
| over capacity | `This booking would hold N pets. The limit is M.` |
| destination room over capacity | `Service Room <b>X</b> would hold N pets. The limit is M.` |

`M` is `Pet Boarding Settings → max_pets_per_booking`, **currently 7**. Read it; do not
hardcode it.

### 4.4 Refusals, verbatim

Checked in this order — **death first**, then the rest; the first failure wins, so a pet that
is both deceased *and* another guardian's reports the death, and one that is both another guardian's
*and* already boarding reports the guardian problem.

| Trigger | Message |
|---|---|
| `boarding` missing | `Pet Boarding is required.` |
| `pet` missing | `Pet is required. Send the pet that is joining.` |
| booking not found | `Pet Boarding <b>X</b> was not found.` |
| submitted booking | `Only open Pet Boarding records can take another pet.` |
| already invoiced | `Pet Boarding <b>X</b> is already invoiced.` |
| booking has no room yet | `Pet Boarding <b>X</b> has no room yet. Assign a room before adding another pet.` |
| checked out / cancelled | `Only Reserved or Checked In boarding records can take another pet.` |
| bad `boarding_type` | `Invalid boarding type <b>X</b>. Expected one of: Travel, Treatment.` |
| destination room inactive | `Service Room <b>X</b> is inactive.` |
| **already on this booking** | `Pet <b>X</b> is already an active occupant of Pet Boarding <b>Y</b>.` |
| pet not found | `Pet <b>X</b> was not found.` |
| **pet is deceased** | `Pet <b>nezoko (PET-00191)</b> is deceased (died 2026-07-19, death record PDR-2026-00011) and cannot be boarded.` |
| **not the booking's guardian** | `Pet <b>X</b> is not linked to Guardian <b>Y</b>.` |
| **already boarding elsewhere** | `Pet <b>X</b> is already on boarding <b>BRD-…</b> (<b>Reserved</b>).` |
| over capacity | see 4.3 |
| room is another guardian's | `Service Room <b>X</b> is occupied by another guardian (<b>GUARDIAN-…</b>).` |
| cross-branch room | as §3.2 |
| pre-migrate site | `Run migrations before adding a pet to a booking.` |

### 4.5 Success

```json
{
  "success": true,
  "boarding_id": "BRD-00101",
  "pet": "PET-00126",
  "boarding_type": "Travel",
  "service_room": "A10",
  "joined_at": "2026-08-17 18:22:41",
  "active_occupant_count": 7,
  "total_cost": 1215000.0,
  "boarding": { /* the full booking, including the updated occupants[] */ }
}
```

> ### `joined_at` is null when the booking is only Reserved
>
> Joining a **Checked In** booking stamps `joined_at` now — the animal is arriving.
> Joining a **Reserved** booking leaves it null, exactly as reserving does: the room is
> held, the animal has not come. Check-in then fills it for every occupant at once.
>
> So a null `joined_at` in the response is not a failure. Read the booking's
> `record_status` to know which case you are in.

---

## 5. Updating in place instead of refetching

Both occupant endpoints return the **full booking** under `boarding`, in the same shape as
Part A §3, including a complete `billable_items[]`. That is enough to update a cached
booking without a second round trip:

| After | Apply |
|---|---|
| `add_occupant` | Replace `occupants` and `billable_items` from `boarding`. The joiner's Room Stay row is new; the other pets' rows are refreshed to current accrual, which is not a repricing — their rates are untouched. Use `active_occupant_count` for the headcount |
| `depart_occupant` | Set that occupant's `status: "Departed"` and `departed_at` from the response. Replace `billable_items` wholesale — the departing pet's Room Stay `qty` has been repriced to its own nights, and the others' have been refreshed to now. Keep the room shown as occupied (`room_released: false`). Use `remaining_active_occupants` to decide whether the next departure becomes a check-out |
| `transfer_occupant_room` | Set that occupant's `service_room` to `to_room`. Set the booking's room from `booking_room`, **not** from `to_room`. Assert `total_cost` is unchanged |

**All three return the full booking under `boarding`, including `occupants[]`**, so the
roster can be replaced wholesale after any of them — no refetch, and no client-side roster
to keep in sync by hand.

Error envelope, the HTML-in-messages warning, and the `meta.code` caveat are as in
Part A §4. Note that both endpoints wrap failures in `ok: false` **without raising**, so a
client must branch on `ok`, not on the HTTP status.

---

# Part D — the hub: `list_boarding_units`

**Endpoint** `GET /api/method/pet_app.api.healthcare.boarding.list_boarding_units`

The hub's grain is the **room**, not the booking. A card shows the pets in that room, a
per-room count, and names the booking as *context* rather than as the card's subject. A
booking spread across two rooms appears on both cards, each listing only its own animals.

> ### ⚠ The array is at `data.data`
>
> This endpoint returns `{ok, data: {data: [...], total: N}, meta, errors}` — double-nested,
> unlike `get_boarding_detail`, whose payload sits directly on `data`. Read `data.data`.

---

## 1. Parameters

| Name | Type | Notes |
|---|---|---|
| `search` | string | Matches room `name`, `room_code`, `room_name`, `room_type` |
| `occupancy` | enum | `Available` · `Reserved` · `Occupied`. Case-insensitive exact match — **see §4, the values now mean something different** |
| `date` | — | Accepted and **ignored**. Reserved for a future availability-on-a-date view; sending it does nothing today |

Only `Active` Service Rooms are ever returned.

---

## 2. The room card

| Field | Notes |
|---|---|
| `name`, `room_id` | the Service Room |
| `room_code`, `room_name`, `room_type`, `image`, `notes` | room master data |
| `status`, `room_status` | the Service Room's own status, always `Active` here |
| `occupancy` | `Available` · `Reserved` · `Occupied` — **§4** |
| **`occupants`** | **the pets in THIS room** — array, see §3 |
| **`active_occupant_count`** | **this room's** headcount. Not the booking's |
| **`capacity`** | **how many animals this room physically holds.** The denominator — render `active_occupant_count / capacity`. Per room and editable per room, from `Service Room.capacity`. See §2.3 |
| **`bookings`** | every booking with a pet in this room. Usually one; see §5 |
| **`booking_count`** | length of `bookings` |
| `active_boarding` | the **primary** booking record, for context — §5 |
| `boarding_id`, `record_status`, `boarding_type`, `reserved_at`, `check_in`, `check_out`, `billing_status`, `sales_invoice` | from the primary booking |
| `guardian`, `guardian_id`, `guardian_name`, `customer` | from the primary booking |
| `pet`, `pet_id`, `pet_name` | **first occupant OF THIS ROOM** — §6 |

### 2.1 Exactly which keys are present

An **occupied** card carries these 31 keys, verified against a live response:

```
active_boarding · active_occupant_count · billing_status · boarding_id · boarding_type
booking_count · bookings · capacity · check_in · check_out · customer · guardian
guardian_id · guardian_name · image · name · notes · occupancy · occupants · pet · pet_id
pet_name · record_status · reserved_at · room_code · room_id · room_name · room_status
room_type · sales_invoice · status
```

An **empty** card carries only 17 — and the difference is not "the booking keys are null",
it is that **they are absent**:

```
active_boarding · active_occupant_count · boarding_id · booking_count · bookings
capacity · image · name · notes · occupancy · occupants · room_code · room_id · room_name
room_status · room_type · status
```

`capacity` is in **both** lists: it is a property of the room, so it is present on a free
card as well as an occupied one, and an empty room reads `0 / 5`.

`pet`, `pet_id`, `pet_name`, `guardian*`, `check_in`, `record_status` and the rest of the
booking block **do not exist as keys** on a free room. A client that reads
`card.pet_name ?? "—"` is fine; one that destructures or type-asserts them as always-present
is not. `active_boarding` and `boarding_id` *are* always present, as `null`.

A room with nobody in it carries `occupants: []`, `active_occupant_count: 0`,
`bookings: []`, `booking_count: 0`, and `active_boarding: null`.

### 2.2 The envelope, exactly

```json
{ "ok": true,
  "data": { "data": [ /* cards */ ], "total": 35 },
  "total": 35,
  "meta": {}, "errors": [] }
```

`total` appears **twice** — inside `data` and duplicated at the top level by the response
wrapper. Both are the count of cards *after* the `occupancy` filter, not the number of rooms
on the site. Read the array from **`data.data`**.

### 2.3 `capacity` — the denominator, and what it is not

`capacity` restores the ratio the hub's OCCUPANCY column used to show. The old `/1` was
hardcoded and became wrong the moment a booking could hold more than one animal — a room
with four pets rendered `4/1`. This is the real number.

**Render `active_occupant_count / capacity`.** Both are **this room's**, scoped identically,
so a booking split across two rooms fills each card against its own room rather than showing
the booking's total twice.

| | |
|---|---|
| Source | `Service Room.capacity`, an Int, **editable per room** |
| Where it is returned | **both endpoints** — `list_boarding_units` on every card, and `get_boarding_detail` on `data.room` (and on `data` itself, where the room card is spread in). Same value, same field, one serialiser |
| Why per room | a Kennel and a Suite are different rooms. That is the entire point of the field, and it is why this is not a setting |
| Live values | **every room is 7 today** — all 35 active and both inactive. That is a starting point the owner edits down per room, not a policy, and not a default: the number is data on the row |
| DocType default | **1**, deliberately not 7. It is the floor for a room created later by someone who has not thought about capacity |
| Floor | never `0` and never negative. An unset column, a room predating the field, and a hand-typed `0` all publish `1`, so a card can never read `3 / 0` |

> ### ⚠ `capacity` is NOT `max_pets_per_booking`, and the two can disagree
>
> `max_pets_per_booking` (currently **7**) is a policy about **one booking**: how many pets
> one guardian may put on one reservation, one value for the whole facility. `capacity` is a
> fact about **one room**: how many animals fit in it, one value per room. They are
> different constraints on different things and neither replaces the other.
>
> **They both read 7 right now, and that is a coincidence.** The owner set a booking policy
> of 7; separately, every room was initialised to 7 as a starting point before being edited
> down. Two unrelated decisions landing on the same digit. **Do not collapse them** — the
> first Kennel edited to 2 makes them diverge, which is the whole purpose of the field.
>
> **Nothing enforces `capacity` yet.** The only limit applied on reserve, `add_occupant` and
> `transfer_occupant_room` is still `max_pets_per_booking`. So `active_occupant_count` **can
> exceed** `capacity` — set a Kennel to 2 and the write paths will still admit seven animals
> while the card draws `5 / 2`. A client must render that. Do **not** clamp the ratio or hide
> the overflow — an over-capacity room is precisely what this number exists to make visible,
> and clamping would conceal it.

---

## 3. `occupants[]` on a card

Identical to the Part C §1.2 shape — same eleven fields, same nullability, same meanings,
produced by the same function so the two cannot drift — **plus one**:

| Extra field | Notes |
|---|---|
| `boarding` | which booking this pet belongs to. Needed because a room can hold pets from more than one booking (§5); on a single-booking card it equals `boarding_id` on every row |

So an occupant row carries **exactly twelve keys**, and this is the complete list — verified
against a live response, not the source:

```
animal_type · boarding · boarding_type · death_record · departed_at · departure_note
joined_at · pet · pet_image · pet_name · service_room · status
```

Two notes specific to the room grain:

- **`service_room` is always populated here** and always equals the card's room. On the
  booking grain it can be null for a pre-Stage-2 row; the hub resolves it (falling back to
  the booking's room) before filing the pet under a card, because a null room on a card
  that is definitionally about that room would be nonsense.
- **Only `Active` occupants appear.** Departed, Deceased and Cancelled pets are not in the
  room and are not on the card. For the full history of a stay, read the booking grain via
  `get_boarding_detail`.

### 3.1 The photo and the animal type — now sent

**Shipped.** An earlier draft of this contract recorded that neither a photo nor an animal
type was on the occupant row, so every avatar fell back to a glyph, and told clients not to
wire an alias to the near-misses in the meantime. Both fields are now on the row, in **both
grains**, produced by the same function so the hub and the booking roster cannot disagree.

| Field | Notes |
|---|---|
| `pet_image` | `Pet.pet_image`, a file URL. **Nullable and often null** — 23% of all pets have a photo, but **19 of the 24 currently boarded do (79%)**. Boarded animals are photographed, so the stack mostly shows real faces; a null is the cue to draw your fallback |
| `animal_type` | `Pet.animal_type` — `Cat` · `Dog` · `Bird` · `Rabbit` · `Horse`. **100% populated**: 0 blank across 2,230 pets (Cat 1,794, Dog 424, Bird 8, Rabbit 3, Horse 1). Already load-bearing elsewhere — it is the axis boarding rates resolve on — so it is a maintained field, not a decorative one |

**Client impact is asymmetric.** `pet_image` is already read under its final name, so photos
appear with no client change. `animal_type` needs one alias added on the client side before
it renders.

Cost was as estimated: two more columns on the lookup that already batched `pet_name`, and
two more keys on the payload. **No extra query and no new round trip** — the hub still makes
exactly one `Pet` query for the whole page regardless of how many rooms or animals are on it.

> ### ⚠ `animal_species` is still not sent, and that is deliberate
>
> It is taxonomic **class**, not species: **2,216 of 2,224 pets read `Mammal`**. An icon or a
> label keyed on it would draw one identical glyph for 99.6% of the board — which is the
> failure it was meant to fix, wearing a different name. `animal_type` is the field that
> tells a cat from a dog. Do not re-alias `animal_species` if it reappears from another
> endpoint.

Two near-misses on the **card** remain what they always were, and are still traps:

| Looks usable | What it actually is |
|---|---|
| `image` on the card | the **Service Room's** photo, from `Service Room.image`. Nothing to do with an animal. An alias chain ending in `image` will put a picture of a kennel in an avatar slot. Read `occupants[].pet_image` instead |
| `active_boarding.pet_image` | **present but still always `null` here.** The key exists because the booking serialiser declares it, but the hub's booking query does not select it. It *is* populated by `list_boarding_records`, a different endpoint — so the same key name is real in one place and permanently null in another. The occupant row's `pet_image` is the one to read |

---

## 4. `occupancy` changed meaning

> ### ⚠ This is a visible behaviour change for any client filtering on `occupancy`
>
> Including this endpoint's own `occupancy=` parameter. **A room reading `Occupied` today
> may read `Available` after this release.** That is correct, but it is visible, and a
> saved filter or a cached count will shift.

**Before:** derived from the booking's `record_status` alone — `Checked In` → `Occupied`,
`Reserved` → `Reserved`, anything else → `Available`. A room was "occupied" because some
booking *named* it, whether or not an animal was in it.

**Now:** derived from the pets actually in the room, adopting `room_is_free` — the same
definition every write-side availability check already used:

| State | When |
|---|---|
| `Available` | **no active pet remains in the room** |
| `Reserved` | active pets are assigned to it, none on a Checked In booking — held for animals that have not arrived. **A held room is not free** |
| `Occupied` | at least one active pet is on a Checked In booking |

What actually changes:

- A room whose animals were all **transferred out** now reads `Available`. It used to read
  `Occupied` because the booking still named it.
- A room holding **transferred-in** animals now reads `Occupied`. It used to read
  `Available` — a kennel with an animal asleep in it, advertised as free.
- **The hub and the reservation gate can no longer disagree.** Previously the hub read
  `Pet Boarding.record_status` while `assert_room_available` read occupant status, so staff
  could be shown a free room and then refused when they clicked it. Both now read the same
  set. Verified against every live room: zero disagreements.

---

## 5. More than one booking in a room

A room can legitimately hold pets from two bookings — the one-guardian rule permits a
guardian's two bookings to share, and a transfer can move a pet into a room another booking
holds.

**Every such booking is returned in `bookings[]`.** The previous implementation kept a
`room → booking` map built with `setdefault`, so the second booking was silently discarded
and its animals became invisible on the hub.

`active_boarding` and the flat booking fields name **one** of them, the *primary*, chosen:

1. the booking whose own `service_room` is this room, then
2. the booking with the most pets in this room, then
3. lowest name — so the answer never depends on row order.

**Do not treat the primary as "the booking in this room" when `booking_count > 1`.** Use
`bookings[]`, and attribute each pet with its own `occupants[].boarding`.

---

## 6. `pet` / `pet_id` / `pet_name` — first occupant *of that room*

Kept for compatibility, **but their meaning has moved with the grain.** They are now the
first occupant **of this room**, not the booking's nominal pet.

That was a bug worth fixing on its own: the old fields returned `Pet Boarding.pet`, the
first pet ever reserved on the booking. After a transfer that is frequently an animal that
is no longer in the room being drawn — a card for room A naming a pet that now sleeps in B.

They fall back to the booking's nominal pet only when a card has no occupant rows at all.
As on the booking grain: **never render a roster or a headcount from them.** Use
`occupants[]` and `active_occupant_count`.

---

## 7. Confirming the names the client guessed

Part D reached the repo late and the room card was built from prose, with tolerant alias
arrays standing in for a contract. Each guess, checked against a live response:

| Guessed | Verdict |
|---|---|
| `occupants` | ✅ **correct** — top level of the card |
| `active_boarding.occupants` | ❌ never exists. `active_boarding` is a flat booking record with no child tables |
| `boarding.occupants` | ❌ no `boarding` key on a hub card at all. That key exists on `get_boarding_detail`, where it holds the **booking's** roster — a different grain (Part C §1.1a) |
| `active_occupant_count` | ✅ **correct**, and it is **the room's**, not the booking's. On a booking split across two rooms the two cards read e.g. 5 and 2, never 7 and 7 |
| `pet_image` | ✅ **correct, and now sent** on the occupant row — §3.1. Note the identically-named key on `active_boarding` is still always `null`; read the occupant's |
| `petImage` | ❌ nothing in this API is camelCase. Every key is snake_case |
| `image` | ⚠️ **exists and is the wrong thing** — it is the Service Room's photo. An alias chain that falls through to it renders a kennel where a face belongs. Remove it from the chain |
| `animal_type` | ✅ **correct, and now sent** on the occupant row — §3.1. Needs one alias added client-side before it renders |
| `animalType` | ❌ camelCase again |
| `animal_species` | ❌ returns taxonomic class. 2,216 of 2,224 pets are `Mammal`. Not what an avatar or a label wants, and deliberately still not sent — §3.1 |
| `species` | ❌ no such field on Pet |

**One alias is still actively harmful and should be deleted rather than left as a fallback:**
`image`, which renders the room. `animal_species` is no longer reachable from this endpoint,
but keep it out of the type so it cannot be re-aliased from elsewhere — it renders "Mammal"
for 99.6% of animals. The rest are safely inert — they resolve to nothing.

---

## 8. Known gaps

- **`get_active_boarding_for_room` is still booking-grained** and still returns *a* booking
  via `limit_page_length=1`. It is not used by the hub any more, but it still backs
  `get_boarding_detail(room_id=…)` — so **fetching a room's detail by room id on a
  two-booking room silently shows one of them**, while the hub card shows both. Its other
  four call sites are boolean "is this room taken" guards where returning any one booking is
  adequate, and the real gate (`assert_room_available`, occupant-grained) runs at save
  regardless. Worth aligning the detail-by-room path next; it is not a correctness hole in
  the reservation flow.
- **`date` is accepted and ignored** (§1).
- **Three Checked In occupants carry a null `joined_at`** — pre-existing rows from before
  check-in stamped it. On a card they read as "has not arrived yet" while the animal is in
  the kennel, and they bill a one-night placeholder. They need correcting by hand; the
  check-in fix only prevents new ones.
