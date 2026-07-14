# Visit Boarding Contract

This contract describes the visit-origin boarding flow added for Visit V11. It is a clinical handoff from an active visit into the boarding desk queue.

## Lifecycle

`Pet Boarding.record_status` is the boarding lifecycle field. The generic `Pet Boarding.status` remains `Open` / `Closed` / `Cancelled`.

Record status values:

```text
Pending Room
Reserved
Checked In
Checked Out
Cancelled
```

`Pending Room` means the visit doctor requested boarding but the boarding desk has not assigned a room yet. It does not occupy a room.

Room occupancy remains derived only from room-assigned active statuses:

```text
Reserved
Checked In
```

Visit-level active boarding means:

```text
Pending Room
Reserved
Checked In
```

## Pet Boarding Fields

New/changed fields:

| Field | Type | Contract |
| --- | --- | --- |
| `record_status` | Select | Adds `Pending Room`. This is the lifecycle source of truth. |
| `service_room` | Link -> Service Room | Conditionally mandatory when `record_status` is not `Pending Room` or `Cancelled`. The server also enforces this. |
| `visit` | Link -> Vet Visit, read-only | Set by `start_visit_boarding`. |
| `boarding_note` | Text | Required visit-origin boarding reason. |
| `boarded_by` | Link -> User, read-only | Session user who started boarding from the visit. |
| `cancelled_by` | Link -> User, read-only | Session user who cancelled the boarding request/stay before check-in. |
| `cancellation_note` | Small Text, read-only | Required cancellation reason captured by `cancel_boarding`. |
| `expected_check_out` | Date | Optional expected checkout date from the visit-origin request. |

`boarding_type` is forced to `Treatment` for visit-origin boarding. The existing room-centric `reserve_room` path still creates normal room reservations and keeps its `Travel`/settings-default behavior. The backend default is `Pet Boarding Settings.default_boarding_type = Travel`; clients must read the setting and must not invent `Treatment` when the value is absent.

## Endpoint: start_visit_boarding

Method:

```text
pet_app.api.healthcare.boarding.start_visit_boarding
```

Whitelisted POST payload:

```json
{
  "visit": "VVT-...",
  "note": "Board overnight for monitoring.",
  "expected_check_out": "2026-07-14"
}
```

`expected_check_out` is optional. `note` is required and whitespace-only notes are rejected.

Creates one `Pet Boarding` row atomically:

```json
{
  "visit": "VVT-...",
  "record_status": "Pending Room",
  "status": "Open",
  "boarding_type": "Treatment",
  "service_room": null,
  "boarding_note": "...",
  "boarded_by": "session user"
}
```

The endpoint uses a savepoint. If validation, insert, or save fails, no partial boarding request remains.

### Who May Start Boarding

Allowed for this visit:

- The visit's current practitioner.
- Coordinator/Admin roles using the same direct-assignment role set as legacy `assign_visit_doctor`: `Coordinator`, `Visit Admin`, `Healthcare Administrator`, `Pet App Admin`, `System Manager`.
- Full-access users / Administrator.

Rejected:

- Users who are neither the visit doctor nor Coordinator/Admin.
- Completed or Cancelled visits.
- A visit that already has active boarding in `Pending Room`, `Reserved`, or `Checked In`.
- Missing visit.
- Empty/whitespace-only note.
- Visit without pet or guardian.

## Endpoint: cancel_boarding

Method:

```text
pet_app.api.healthcare.boarding.cancel_boarding
```

Whitelisted POST payload:

```json
{
  "boarding": "BRD-...",
  "note": "Boarding request was started by mistake."
}
```

Aliases accepted for the boarding id: `boarding`, `boarding_id`, `name`. `note` is required for every caller; whitespace-only notes are rejected.

Cancellable states:

- `Pending Room`: cancellable. No room has been assigned and no custody event happened.
- `Reserved`: cancellable. A room is held, but the pet has not been checked into the cage.

Not cancellable:

- `Checked In`: not cancellable. The pet is physically in custody, so the desk must use checkout. A genuinely free stay checks out cleanly with no invoice.
- `Checked Out` and `Cancelled`: already terminal.

On success the endpoint atomically:

- Sets `record_status = "Cancelled"` and `status = "Cancelled"`.
- Writes `cancelled_by = session.user` and `cancellation_note = note`.
- Clears `Pet Boarding.billable_items`, so no room-stay row or other billable row survives to be invoiced later.
- Does not create a Sales Invoice.
- Uses a savepoint; if validation or save fails, no partial cancellation remains.

Who may cancel:

- The linked visit's current practitioner.
- Coordinator/Admin roles using the same direct-assignment role set as legacy `assign_visit_doctor`: `Coordinator`, `Visit Admin`, `Healthcare Administrator`, `Pet App Admin`, `System Manager`.
- Full-access users / Administrator.

For a visit-origin boarding, cancellation removes it from the visit's active boarding state. The next `get_visit_workbench` response returns `boarding: null`, and `complete_case` is no longer blocked by that cancelled boarding.

## Endpoint: reserve_room

Existing method remains:

```text
pet_app.api.healthcare.boarding.reserve_room
```

Existing room-centric create payload is unchanged:

```json
{
  "roomId": "ROOM-001",
  "petId": "PET-001",
  "guardianId": "GUARD-001",
  "boardingType": "Travel"
}
```

New assign-room payload for a visit-origin request:

```json
{
  "roomId": "ROOM-001",
  "boarding_id": "PB-..."
}
```

Aliases accepted for the boarding id: `boarding_id`, `boardingId`, `name`.

Only `Pending Room` boarding records can be assigned a room through this path. On success it sets:

```json
{
  "record_status": "Reserved",
  "status": "Open",
  "service_room": "ROOM-001"
}
```

It also appends the room-stay charge to `Pet Boarding.billable_items` using the same room charge helper as the room-centric reservation path. The row is keyed by `linked_service_id = "boarding_room_stay:<boarding_type>"`, with a fallback match on `item_type = "Room Stay"` and `item_code`, so assigning again or re-saving the room charge does not stack duplicate room rows.

A room lock and the existing active-room check are used, so a room with an active `Reserved` or `Checked In` boarding cannot be double-booked.

## Workbench Shape

`get_visit_workbench` always emits the top-level `boarding` key. It is either an object for the active visit boarding or `null`.

```json
{
  "boarding": {
    "name": "PB-...",
    "status": "Pending Room",
    "boarding_type": "Treatment",
    "service_room": null,
    "service_room_name": null,
    "expected_check_out": "2026-07-14",
    "boarding_note": "Board overnight for monitoring.",
    "boarded_by": "doctor@example.com",
    "boarded_by_name": "Dr. Example",
    "created_at": "2026-07-13 10:15:00"
  }
}
```

The workbench permissions object also always includes:

```json
{
  "can_start_boarding": true,
  "can_cancel_boarding": false
}
```

`can_start_boarding` is true only when the current session user may call `start_visit_boarding` for this visit and no active boarding exists.

`can_cancel_boarding` is true only when this visit has cancellable active boarding (`Pending Room` or `Reserved`) and the current session user may call `cancel_boarding` for it. The key is always emitted; its absence means the backend contract has not shipped.

## Visit Completion Guard

`complete_case` rejects while the visit has active boarding in `Pending Room`, `Reserved`, or `Checked In`. The error names the blocking `Pet Boarding` record and its status.

`Checked Out` and `Cancelled` boarding rows are history and do not block visit completion.

## Billing Contract

Boarding billing remains separate from visit billing.

Boarding charges live on the boarding record (`Pet Boarding.billable_items`) and create a separate Sales Invoice during `check_out_boarding`. Visit completion creates the visit Sales Invoice from `Vet Visit.billable_items`.

This feature does not merge boarding charges into the visit invoice. The completion guard prevents closing the visit while a stay is still active, but it does not change the separate boarding invoice behavior.

Room-stay pricing is added when a room is reserved or assigned, not by pushing rows onto the visit. The configured room item comes from `Pet Boarding Settings.travel_boarding_item` or `Pet Boarding Settings.treatment_boarding_item`. Its rate is resolved from the configured veterinary selling price list via `pet_app.utils.price_list.get_veterinary_selling_price_list()` and falls back to `Item.standard_rate` if no `Item Price` exists. The default configured price list is `Standard Selling`.

Room stay is billed per elapsed 24-hour day: `qty = stay_days`, where `stay_hours` remains the underlying duration measurement and `stay_days = ceil(stay_hours / 24)`, minimum 1. The auto row uses `linked_service_id = "boarding_room_stay:<boarding_type>"` to remain idempotent and update in place. The provisional row created at room assignment uses the current duration and check-out recomputes the final elapsed duration from full check-in/check-out datetimes.

A zero-cost boarding is valid. If checkout has no non-cancelled invoice items, or the computed invoice total is zero, `check_out_boarding` closes the boarding without creating a Sales Invoice. The response returns `sales_invoice: null`.

## Status Query Audit

Code paths checked during implementation:

| Call site | Status behavior after this change |
| --- | --- |
| `PetBoarding._validate_status_consistency` | Treats `Pending Room`, `Reserved`, and `Checked In` as open. |
| `PetBoarding._validate_room` | Allows no room for `Pending Room` and `Cancelled`; room-assigned active states still require `service_room`. |
| `PetBoarding._validate_single_active_room_boarding` | Checks only `Reserved`/`Checked In` against room occupancy. |
| `get_active_boarding_for_room` | Returns only room-assigned active stays: `Reserved`/`Checked In`. |
| `list_boarding_units` / `_get_active_boardings_by_room` | Room board remains based on `Reserved`/`Checked In`; Pending Room is not shown as occupying a room. |
| `get_boarding_detail` | Can load a Pending Room record by `boarding_id`; room detail by `room_id` still ignores Pending Room. |
| `list_boarding_records` | Lists Pending Room rows and can filter by `status="Pending Room"`; LEFT JOIN handles no room. |
| `reserve_room` | Existing no-boarding-id path still creates `Reserved`; new boarding-id path moves `Pending Room` to `Reserved`. |
| `check_in_boarding` | Still accepts only `Reserved`. Pending Room must be assigned first. |
| `check_out_boarding` | Still accepts only `Checked In`. |
| `create_order` | Still accepts only `Checked In`. |
| `guardian_portal._active_boarding` | Still shows `Reserved`/`Checked In` only; Pending Room is desk inbox state, not room occupancy. |
| `analytics.boarding_occupancy` | Still counts `Reserved`/`Checked In` only. |
| `medical_file._boarding_events` | History listing continues to include boarding rows by pet. |
| `boarding_death_cascade` | Terminal handling unchanged; Pending Room is non-terminal but carries no room-day billing. |
| `complete_case` | Guard blocks `Pending Room`/`Reserved`/`Checked In` for the linked visit. `Cancelled` does not block. |

## Non-Goals

This feature does not create a new `HealthcareStatus`.

This feature does not alter visit status when boarding starts.

This feature does not delete or replace the existing room-centric boarding flow.
