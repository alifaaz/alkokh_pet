# Boarding API contract — `create_order` and `reserve_room`

**Status** current as of 2026-08-16
**Breaking:** `create_order` now requires `pet`. **Additive:** `reserve_room` gains `petIds`.

Everything here is read from the implementation and from real responses captured against
a live site. Where a shape is surprising it is called out rather than smoothed over.

| Endpoint | Change | Breaking? |
|---|---|---|
| [`create_order`](#part-a--create_order) | `pet` required | **yes** — ship with the client |
| [`reserve_room`](#part-b--reserve_room) | `petIds` list added | no — `petId` still works |

Both land in the same release.

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
| `status` | `Draft` · `Billable` · `Billed` · `Cancelled` |
| `note` | reaches the invoice as the line description |
| `linked_service_id`, `linked_doctype`, `linked_name`, `order_id` | |
| `care_episode` | |
| `dispense_status`, `dispensed_qty`, `dispensed_by`, `dispensed_at` | medication only |

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
| pet not the guardian's | `Pet <b>X</b> is not linked to Guardian <b>Y</b>.` |
| **pet already boarding** | `Pet <b>X</b> is already on boarding <b>BRD-…</b> (<b>Checked In</b>).` |
| room inactive / occupied | `Service Room <b>X</b> is inactive.` · `… already has active boarding …` |

Every per-pet failure **names the pet that failed**, so a list that fails on its fourth entry
says so.

Error envelope and the HTML-in-messages and `meta.code` caveats are as in Part A §4.
