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
- Room occupancy is derived from room-assigned active `Pet Boarding` records only.
- Visit-active boarding states are `Pending Room`, `Reserved`, and `Checked In`.
- Room-assigned active boarding states are `Reserved` and `Checked In`.
- Closed/history states are `Checked Out` and `Cancelled`.
- `Pending Room` has no assigned room and does not affect room occupancy.
- `Reserved` maps to room occupancy `Reserved`.
- `Checked In` maps to room occupancy `Occupied`.
- No active boarding maps to room occupancy `Available`.
- Prevent more than one room-assigned active boarding per room.
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

- Whitelisted API methods should return the unified envelope `{"ok": true, "data": {...}, "meta": {...}, "errors": []}` on success.
- Error responses should use `{"ok": false, "data": {}, "meta": {"code": "..."}, "errors": [...]}`.
- Legacy top-level keys are mirrored only where they do not conflict with the envelope keys.
- Boarding list/detail/action payloads now live under the envelope `data` object.

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

## Permissions Refactor

Permissions are now fully Frappe-native. Backend security must use `Role`, `Role Profile`, Role Permission Manager / `DocPerm`, `Custom DocPerm`, `User Permission`, and explicit `frappe.has_permission(...)` checks.

Do not add a second permission platform. Do not revive action registries, capability graphs, workflow authorization rules, or backend page-to-DocType security maps. The only backend page registry is `Pet App Access Settings`, and it is for Vue sidebar/route visibility only.

`pet_app.api.permissions.get_current_access` remains as a Vue compatibility snapshot. Its `doctypes{}` value is built dynamically from Frappe `DocPerm` / `Custom DocPerm` for the current user's roles, including roles composed by `Role Profile`.

Compatibility arrays are visibility-only:

- `modules`: derived from allowed enabled page rows in `Pet App Access Settings`
- `pages`: derived from enabled `Pet App Page Access` rows assigned to the user's roles
- `actions: []`

Frontend should use backend-returned `pages`/`modules` for sidebar and direct-route visibility. Frontend should use `canDoctype(doctype, permission)` for buttons and record actions. Backend APIs remain the authority and should return normal Frappe permission errors for unauthorized operations.

Legacy custom permission DocTypes were removed and must not be reintroduced:

- `Pet App Permission Rule`
- `Pet App Workflow Rule`
- `Pet App Workflow Rule Resource`
- `Pet App Workflow Rule DocType`
- `Pet App Role Profile Workflow Rule`

Role Profiles are normal support-managed composition objects. Do not add logic that auto-reassigns profiles, force-recreates mappings, hard-syncs profiles after save/login, or treats Role Profiles as immutable/system-owned.

Detailed handoff: `docs/agent-handoff-frappe-native-permissions.md`.

## Medical Profile, Episodes, And Visit Case Choice

The clinical case source of truth is backend-owned.

Important model rules:

- `Pet Medical Profile` is one permanent dashboard record per pet.
- `Pet Care Episode` is the open/closed case or treatment course.
- `Vet Visit` is the doctor's working record.
- Do not auto-create a `Pet Care Episode` just because a `Vet Visit` exists.
- A visit links to an episode only after the backend receives an explicit case choice.

Visit case choices:

- `wellness`: routine/checkup visit. Keep `Vet Visit.care_episode` empty and do not create a case.
- `continue_case`: link the visit to the current/provided active `Pet Care Episode`.
- `new_case`: create a new `Pet Care Episode` only when the pet has no other active episode.

Backend contract:

- Use workspace action `set_case_choice` for visit case decisions.
- `convert_to_visit` may accept `doctor_case_choice`; otherwise the frontend should call `set_case_choice` after opening the visit.
- Every visit aggregate/workbench response should include `case_context`.
- Frontend must trust `case_context` for `visit_care_episode`, `profile_active_episode`, active episode status, and whether a new case can be opened.
- `create_orders` stays visit-centered and must return order rows with final `linked_doctype` and `linked_name`.
- Wellness visits can still save notes, vitals, routine orders, billables, vaccination/deworming, and invoices.
- `Pet Care Plan Item` requires a visit linked to an episode through `new_case` or `continue_case`.

Main files:

- `pet_app/utils/medical_profile.py`: case choice helper, profile sync, and `case_context`.
- `pet_app/api/workspace.py`: `set_case_choice`, conversion flow, visit aggregate.
- `pet_app/api/visit_workbench.py`: raw visit workbench response.
- `pet_app/api/care_plan.py`: episode-required course plan items.
- `pet_app/patches/visit_case_choice_schema.py`: custom fields for visit case choice audit.
- `docs/frontend-medical-profile-episode-visit-flow.md`: Vue contract and flow guide.

## Backend Issues Resolution Log

This section records the backend issue work completed across the recent pet_app stabilization sessions. Keep it in sync when follow-up migrations or frontend contracts change.

### #9 Complete Case Idempotency

- `pet_app/api/workspace.py` now treats the `complete_case` workspace action as idempotent.
- If the target `Vet Visit` is already `Completed`, the action returns the current visit aggregate without re-running completion side effects.
- This prevents duplicate workflow/profile/follow-up/billing side effects from repeated frontend submissions.

### #5 Guardian Field Typo Rename

- Renamed `gurdian_name` to `guardian_name` across the codebase.
- Scope covered 45 occurrences across 15 files, including DocType JSON, Python code, tests, fixtures, custom fields, and property setters.
- Patch: `pet_app/patches/rename_guardian_typo_fields.py`.
- Database columns were renamed by the patch; old data was test data, so no preservation/backfill path was required.

### #12 Boarding Deposit Payment Entry

- `Pet Boarding` now has `deposit_payment_entry` to store the created `Payment Entry` reference.
- `pet_app/api/healthcare/boarding.py` auto-creates and submits a receive `Payment Entry` during `check_in_boarding` when `deposit > 0`.
- Deposit payment creation is idempotent for an existing non-cancelled `deposit_payment_entry`.

### #10 Response Shape Standardization

- Added `pet_app/api/link_aliases.py` with `enrich_link_aliases` and `with_link_aliases`.
- API rows/payloads exposing link IDs now include flat display aliases where available:
  - `pet_id`, `pet_name`
  - `guardian_id`, `guardian_name`
  - `doctor`, `doctor_name`
  - `provider`, `provider_name`
- The sweep covered all whitelisted pet_app API endpoints that returned Pet, Guardian, Healthcare Practitioner, or provider IDs without display names.
- `docs/RESPONSE_SHAPE_CHANGES.md` documents old shape to new shape changes for frontend coordination.

### #16 Response Envelope Standardization

- Added `pet_app/api/response.py` helpers, including `standardize_response`.
- Added `ok_response` / `error_response` aliases in `pet_app/utils/api_response.py`.
- 245 whitelisted endpoints now return the unified envelope shape.
- Standard success shape: `{"ok": true, "data": {...}, "meta": {...}, "errors": []}`.
- Standard error shape: `{"ok": false, "data": {}, "meta": {"code": "..."}, "errors": [...]}`.
- Protocol exceptions remain raw by design:
  - `pet_app.api.auth_api.login_and_get_oauth_token` for OAuth token compatibility.
  - `pet_app.api.whatsapp.webhook` GET verification challenge for Meta webhook compatibility.

### Previous Safe/Caution Fixes

- Added `permission_query_conditions` coverage for `PetCareService`.
- Extended `Healthcare Practitioner.practitioner_type` Select options to include Service Provider and Coordinator.
- Added the `service_categories` child table on Healthcare Practitioner.
- Added `get_providers_for_category` and `bulk_create_pet_care_services` APIs.
- Aligned `bulk_create_pet_care_services` return shape to `created` / `failed`.
- Added `title_field` metadata for Pet, Guardian, and PetCareService.
- `care_plan.list_due_plan_items` returns `pet_name`, `doctor_name`, and `guardian_name` via `enrich_link_aliases`.
