# Agent Notes

This app is a Frappe/ERPNext custom app. Keep changes aligned with Frappe DocType metadata, controller hooks, and whitelisted method contracts already used in `pet_app`.

## Healthcare Boarding Flow

The boarding backend lives in:

- `pet_app/api/healthcare/boarding.py`
- `pet_app/pet_app/doctype/pet_boarding/`
- `pet_app/pet_app/doctype/service_room/`
- `pet_app/pet_app/doctype/pet_billable_item/`
- `pet_app/pet_app/doctype/pet_boarding_settings/`
- `docs/boarding-frontend.md`

Important rules:

- `Service Room` is a normal CRUD DocType.
- Do not add occupancy, availability toggles, or pricing fields to `Service Room`.
- `Service Room` must not have `daily_rate`.
- Room occupancy is derived from active `Pet Boarding` records only.
- Active boarding states are `Reserved` and `Checked In`.
- Closed/history states are `Checked Out` and `Cancelled`.
- `Reserved` maps to room occupancy `Reserved`.
- `Checked In` maps to room occupancy `Occupied`.
- No active boarding maps to room occupancy `Available`.
- Prevent more than one active boarding per room.
- Checkout must close the boarding and release the room by moving the boarding out of active states.

Guardian/customer relationships:

- `Guardian` links to ERPNext `Customer` through `Guardian.customer_id`.
- Pets link to guardians through `PetGuardian`.
- Boarding must validate that the selected guardian is linked to the selected pet.
- Invoices must use the customer resolved from the guardian, not a manually supplied unrelated customer.

Billing:

- Boarding pricing comes from `Pet Boarding.billable_items`.
- `Pet Billable Item.amount` is always `qty * rate`.
- `Pet Boarding.total_cost` and `balance` are computed from billable items and `deposit`.
- Room-stay item mapping comes from `Pet Boarding Settings`:
  - `Travel` uses `travel_boarding_item`.
  - `Treatment` uses `treatment_boarding_item`.
- Checkout creates `Sales Invoice` lines from final `billable_items`.

API contracts:

- `list_boarding_units(search=None, occupancy=None, date=None)`
- `get_boarding_detail(boarding_id=None, room_id=None, name=None)`
- `list_boarding_records(search=None, status=None, pet_id=None, guardian_id=None, date_from=None, date_to=None, limit_start=0, limit_page_length=10, order_by='modified desc')`
- `reserve_room(roomId, petId, guardianId, checkIn=None, checkOut=None, note=None, boardingType=None)`
- `check_in_boarding(boarding_id)`
- `check_out_boarding(boarding_id)`

Response style:

- List methods return `{"data": [...], "total": number}`. Frappe wraps this under HTTP `message`.
- Detail methods return one object.
- Action methods return `{"success": true, ...}`.

Migration:

- Boarding schema migration is in `pet_app/patches/boarding_flow_setup.py`.
- Run `bench --site <site> migrate` after DocType metadata changes.
- `pet_app/patches/sync_alkohk_workspace.py` skips workspace links to missing doctypes so migrate can succeed on sites without optional Healthcare doctypes.

Smoke tests:

```bash
bench --site mo.com migrate
bench --site mo.com execute pet_app.api.healthcare.boarding.list_boarding_units
bench --site mo.com execute pet_app.api.healthcare.boarding.list_boarding_records --kwargs "{'limit_page_length': 5}"
```
