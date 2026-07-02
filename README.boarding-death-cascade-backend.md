# Boarding Death-Cascade — Backend

When a pet dies while boarded, the front desk reports the death from the boarding
screen via `pet_app.api.mortality.report_pet_death` with
`source_doctype = "Pet Boarding"` and `source_name = <boarding id>`.

The death record is created exactly as for any other source (all payload fields
are accepted and persisted regardless of `source_doctype`). When the source is a
boarding, `report_pet_death` additionally runs the **boarding death cascade**
implemented in [`pet_app/utils/boarding_death_cascade.py`](pet_app/utils/boarding_death_cascade.py).

## Cascade steps

`run_boarding_death_cascade(death_doc)` performs, in order:

1. **Pet → deceased.** Sets `is_deceased = 1`, `status = "Deceased"`,
   `pet_status = "Deceased"`, `death_date`, and `death_record` on the `Pet`.
   After this the existing `validate_document_not_deceased` `before_insert` hook
   blocks any new bookable/boardable docs (Appointment, Vet Visit, Vet Case
   Sheet, Pet Boarding, PetCareService, Pet Procedure) for the pet.

2. **Boarding → closed.** Transitions the `Pet Boarding` to the terminal
   checked-out state: `record_status = "Checked Out"`, `status = "Closed"`,
   `workflow_state = "Closed"`, `boarding_outcome = "Death"`,
   `death_during_boarding = 1`, `death_record` linked, and a death note appended
   to `checkout_notes`. Room-day accrual is stopped by capping `check_out` at the
   death datetime before totals are recomputed. Submittable boardings are
   submitted once an invoice exists.

3. **Linked open clinical docs → closed.** For the pet:
   - `Lab` orders in `Ordered / Sample Collected / In Progress / Result Entered` → `Cancelled`
   - `Imaging` orders in `Ordered / Scheduled / In Progress / Reported` → `Cancelled`
   - `PetCareService` in `Pending / Scheduled / In Progress / Ordered / Active` → `Cancelled`
   - open `Vet Visit` (`Draft / In Progress / Follow-up Needed`) → `Cancelled`
   - open `Vet Case Sheet` (`Draft / Waiting Practitioner / In Consultation`) → `Closed`

   Each is status-guarded (already-closed docs are skipped), the `death_record`
   link is stamped where the field exists, and **nothing is deleted** — history
   is preserved. Lab/Imaging are matched by `pet` because the data model has no
   direct boarding→order link yet (orders relate via `visit`/`care_service`); once
   `create_order` lands a boarding anchor, the matcher can be narrowed.

4. **Death record stays linked** to the boarding via `source_doctype` /
   `source_name` (set on insert) plus the boarding's `death_record` /
   `death_during_boarding` / `boarding_outcome` fields, so both the boarding hub
   and the pet profile can show "deceased during boarding" with a link.

## Billing rule on death (decided)

**Invoice up to the death datetime.** `check_out` is set to the death datetime,
the stay is recomputed (no room-days past the death), and a Sales Invoice is
generated immediately — the same machinery as a normal checkout
(`_ensure_room_stay_billable_item` + `_build_sales_invoice_items`). The boarding
becomes `billing_status = "Invoiced"` with `sales_invoice` set.

Edge case: a boarding that died while only **Reserved** (never checked in) has no
room-days and no billable items, so it is closed with **no invoice**
(`billing_status` stays `Unbilled`) rather than erroring; staff can settle/waive
manually. Likewise, if no customer can be resolved, the boarding closes unbilled.

## Idempotency & guards

- Once the pet is already deceased, a repeated `report_pet_death` is a no-op: it
  returns the existing (non-cancelled) death record and re-runs the cascade
  (which is itself idempotent), so retries converge without duplicate death
  records or double-invoicing.
- An already-closed/submitted boarding is tolerated: the pet status and death
  links are still stamped, but the boarding is not re-closed or re-invoiced.
- Permissions are unchanged: the caller needs the existing mortality write/create
  access (`_assert_pet_access(write=True)`); the cascade runs with
  `ignore_permissions` for the linked-doc writes, consistent with the rest of the
  mortality module.

## Response

Unchanged — `report_pet_death` returns `{ "death_record": PetDeathRecord }` in the
standard envelope. The boarding hub refetches after the dialog emits `saved`.
A `pet_death.boarding_cascade` audit event records what the cascade changed.

## Tests

See `pet_app/tests/test_p2_growth_mortality.py`:
`test_boarding_death_cascade_marks_pet_deceased_and_closes_boarding`,
`test_boarding_death_settles_billing_up_to_death`,
`test_boarding_death_closes_open_clinical_docs`,
`test_boarding_death_is_idempotent`,
`test_boarding_death_tolerates_already_closed_boarding`.
