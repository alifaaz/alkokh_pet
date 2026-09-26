# Visit Boarding Contract

This contract describes the visit-origin boarding flow added for Visit V11. It is a clinical handoff from an active visit into the boarding desk queue.

## Invoice stock policy (2026-09-09)

Regular stock and service charges reuse the same customer/company/branch draft. Stock moves only when ERPNext submits that invoice. Checkout and death settlement use the shared invoice helper. See [Invoice reuse and stock policy](INVOICE_REUSE_CONTRACT.md).

`dispense_medication` now records clinical state without a Material Issue. Existing historical issue fields and their return behavior are preserved. Billable medication carries its explicit dose conversion onto the invoice; quantities and charges are not increased.

Included medication remains excluded from invoices. Its dispensing response includes `data.stock_notice`, and an orange message explains that no invoice stock line exists. It therefore has **no automatic stock deduction** under the invoice-only policy. No extra charge or inferred stock line is added. Visit-prescribed medication absorbed into a Treatment stay has the same unresolved conflict.

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

Visit-level active boarding means the visit has a current boarding row to display and manage:

```text
Pending Room
Reserved
Checked In
```

Only `Checked In` means physical custody. It is the only boarding state that makes the linked visit read-only or blocks `complete_case`. `Pending Room` and `Reserved` are reservations/work queue states; clinical work continues normally. The lock is temporary and lifts when the boarding reaches `Checked Out`.

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

## Visit Read-Only / Completion Guard

Boarding only gates the visit while the pet is physically in custody:

- `Pending Room`: visit remains editable and `complete_case` is allowed.
- `Reserved`: visit remains editable and `complete_case` is allowed.
- `Checked In`: visit is read-only and `complete_case` rejects. The error names the blocking `Pet Boarding` record.
- `Checked Out`: the temporary lock is lifted; the visit is editable/completable again if its own visit status allows it.
- `Cancelled`: history; does not block visit edits or completion.

The care episode / medical-file path is not gated by boarding. Adding care-plan/episode items continues to work regardless of boarding state, including while the boarding is `Checked In` and after the visit is completed.

## Billing Contract

Boarding billing remains separate from visit billing.

Boarding charges live on the boarding record (`Pet Boarding.billable_items`) and create a separate Sales Invoice during `check_out_boarding`. Visit completion creates the visit Sales Invoice from `Vet Visit.billable_items`.

This feature does not merge boarding charges into the visit invoice. Completing a visit while boarding is `Pending Room` or `Reserved` does not double-charge or lose boarding charges: visit completion creates only the visit invoice, and boarding checkout later creates the separate boarding invoice from `Pet Boarding.billable_items`.

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
| `active_boarding_for_visit` | Returns `Pending Room`/`Reserved`/`Checked In` for visit payload, duplicate-start prevention, and cancellability checks. It is not the visit read-only guard. |
| `checked_in_boarding_for_visit` | Returns only `Checked In`; this is the visit read-only and `complete_case` guard. |
| `complete_case` | Guard blocks only `Checked In`. `Pending Room`, `Reserved`, `Checked Out`, and `Cancelled` do not block completion. |
| `workspace._assert_record_access` | Visit write actions are read-only only when `checked_in_boarding_for_visit` returns a row. Care-plan/episode actions are intentionally not routed through this lock. |
| `visit_workbench._workbench_permissions` | Visit edit/complete permissions go false only for `Checked In`; `can_add_plan_item` remains independent of the boarding custody lock. |

## Non-Goals

This feature does not create a new `HealthcareStatus`.

This feature does not alter visit status when boarding starts.

This feature does not delete or replace the existing room-centric boarding flow.

## Accommodation discount at checkout (v1)

`check_out_boarding(boarding_id, check_out_note=None, checkout_note=None,
checkout_notes=None, discount=None)` accepts an optional object (or its JSON encoding):

```json
{"discount": {"type": "percentage", "value": 10}}
```

Types are exactly `amount` and `percentage`. Values must be finite, nonnegative JSON
numbers (not strings or booleans). Percentage is at most 100; an amount exceeding the
eligible accommodation subtotal is rejected. Omission/null means no request; an explicit
zero request is valid and echoed. Existing note aliases and checkout permissions apply.

Both checkout and detail return these keys at the envelope's `data` root (also within
`boarding` where that nested object exists):

```json
{
  "discount": {"type": "amount", "value": 10000},
  "accommodation_subtotal": 100000,
  "accommodation_discount_amount": 10000,
  "accommodation_total": 90000
}
```

These are booking-scoped accommodation amounts in invoice currency (IQD on this site),
using the tax-exclusive accommodation basis before discount and tax recalculation. They are not whole-invoice totals.
`discount` is null when no discount was requested. Final figures are null before checkout
and for historical records without a saved calculation; known zero amounts remain zero.
No historical invoice or historical discount is reconstructed or changed.

The booking stores `discount_request` (JSON text), the three accommodation amounts,
`discount_recorded_by` (acting User), and `discount_recorded_at` (checkout timestamp).
Legacy `total_cost`, `balance`, and original billable rates keep their gross semantics;
use `accommodation_total` for discounted accommodation and `invoice_outstanding` for
actual debt. Existing invoice, payment, warning, and allocation keys remain available.

Eligibility comes from this booking's non-Cancelled/non-Included `Room Stay` billable
rows, including departed pets and previous admissions. Server-owned occupant dates
reconcile nights; the existing catalogue resolver fills missing rates, preserving saved
rates and authorized billable edits. Checkout never creates a deliberately removed charge.
Each invoice description carries both the parent booking provenance marker and an exact
`Pet Billable Item` source marker. Selection requires both markers plus the source row's
category; labels, item codes, and active-pet lists do not establish eligibility.
Medication, service, product, and other-booking rows are excluded even with the same Item.
New checkout invoices remain separate (`force_new=True`); the discount helper also protects
unrelated rows when presented with a mixed draft. Submitted historical invoices are outside
this feature.

The Sales Invoice keeps each original accommodation row's quantity, nightly rate, and
amount intact. The discount is recorded in native `Sales Invoice.discount_amount` with
`apply_discount_on = "Net Total"`. For example: three nights at 15,000 IQD remain one
45,000 IQD row, with an invoice discount of 5,000 IQD and a grand total of 40,000 IQD
before any applicable taxes. The invoice screen's pre-discount subtotal is `total`;
`net_total` is the discounted tax-exclusive amount.

`custom_boarding_discount_booking` records the booking whose accommodation may receive
the invoice discount. The Sales Invoice controller extends ERPNext's discount allocation
only: eligible rows receive native `distributed_discount_amount`, `net_amount`, and
`net_rate`; original `qty`, `rate`, and `amount` never change. Fixed discounts are
proportional to eligible tax-exclusive row amounts, with currency rounding and a bounded
final adjustment. Percentages are converted to a fixed amount using the same basis.
Medications, services and other bookings retain both their gross and net row amounts.
ERPNext continues to calculate all taxes, rounding, ledger entries and payment allocations.
Regular invoices without this booking scope retain standard ERPNext behavior. Returns
of scoped invoices derive discount amounts from the returned source rows; a service-only
return receives no accommodation discount.

This supersedes the initial implementation that reduced unit rates and split nights to
represent rounding. Submitted invoices from that implementation are not rewritten.

Checkout uses a savepoint and booking/room locks, including a database booking row lock
held until transaction completion. It reconciles charges, calculates the discount and
existing taxes, attaches advances to the draft, submits as non-POS (which reconciles those
advances into Payment Entry References), then closes/submits the booking. Any failure rolls
back the checkout, including occupant/stint departures, invoice/ledger writes and payment
allocations. An unpaid balance is a warning, not a validation error. Excess payments remain
customer credit; the reported remainder includes all payments, even after the invoice's
allocation ceiling is reached. A wholly free stay closes without an invoice; remaining
non-accommodation charges are invoiced normally.

Repeated checkout returns the saved discount/figures and existing invoice with current
payment/outstanding information. Omission or the identical discount succeeds; an explicitly
different discount is rejected. Replays do not alter notes, timestamps, prices, or deposits.
An open booking already referenced by an invoice is refused for review. MariaDB deadlock
or snapshot conflicts return the standard `QueryDeadlockError` envelope: retry the entire
request/transaction, never only the last SQL statement. Individual pet departure does not
apply or record a discount.

### Deployment and verification

Migrate the site's `Pet Boarding` metadata, run
`pet_app.patches.boarding_invoice_discount` to install the Sales Invoice scope field,
and restart the web workers before frontend use. The patch is also registered for fresh
installs. Existing checkout and payment APIs do not change.
`get_boarding_detail.data.capabilities.boarding_discount_v1` is a literal boolean and is
true only with the supporting code, all required booking fields, and the invoice booking-scope field. This describes server
support, not the user's permission to check out. Production migration and worker reload
must be verified separately from tests on the isolated site.

Tests: `pet_app.tests.test_boarding_discount` (rollback per test) and
`pet_app.tests.boarding_discount_concurrency.run` (committed fixtures on the isolated test
database only). They cover contract persistence/replay, validation, excluded charges,
departed admissions, rounding, taxes, full/zero discounts, actual Payment Entry allocation,
post-submission rollback and concurrent requests. Do not run the concurrency harness on
production.

### Production verification — 2026-09-13

Deployed on `frappe.localhost` after database backup
`20260913_233538-frappe_localhost-database.sql.gz`. The focused command
`bench --site frappe.localhost reload-doc pet_app doctype pet_boarding` installed exactly
the six new fields; existing fields and permissions matched the live metadata. Unrelated
pending patches were not run. Site caches were cleared.

The supervised web master was gracefully terminated by its owning OS account and restarted
by supervisor (`autorestart=true`), producing a fresh master and workers. Authenticated HTTP
through nginx verified `boarding_discount_v1: true`, all four canonical response fields,
null final figures for an open booking, and the new invalid-discount validation envelope.
The validation probe used a nonexistent booking ID; no production checkout was performed.
The implementation had passed 18 isolated contract tests and three concurrent-request rounds
before deployment.

### Invoice discount correction deployed — 2026-09-13

Following review of the initial rate-splitting presentation, checkout was changed to the
native invoice-discount representation described above. Production backup:
`20260913_234738-frappe_localhost-database.sql.gz`. The focused
`pet_app.patches.boarding_invoice_discount` patch was applied and recorded in Patch Log;
web caches were cleared and the supervised gunicorn master/workers were restarted.

Validation: 22 boarding contract tests, four regular/driver invoice regression tests, and
three concurrent-request rounds passed. An HTTP checkout on the isolated test site through
the restarted web server produced one row (3 nights × 15,000 IQD), subtotal 45,000,
invoice discount 5,000, grand total 40,000, advances 15,000, and outstanding 25,000.
Production HTTP reads verified the capability and new schema. Existing submitted invoices
were not amended; correction of those documents requires separate authorization.
