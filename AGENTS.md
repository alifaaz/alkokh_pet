# Agent Notes

This app is a Frappe/ERPNext custom app. Keep changes aligned with Frappe DocType metadata, controller hooks, and whitelisted method contracts already used in `pet_app`.

## Contract Ownership

A backend contract lives in the backend repo, with the code it describes. Exactly one home. Frontend and SDK docs are CONSUMERS -- they link or summarize, they never hold authority. A contract outside version control is not a contract, it is a note on someone's laptop.

Canonical backend contracts live in this app's `docs/` directory:

- `docs/VISIT_BOARDING_CONTRACT.md`
- `docs/VISIT_WORKBENCH_RESULT_FILES_CONTRACT.md`
- `docs/MEDICATION_BILLING_CONTRACT.md`
- `docs/MOBILE_API_README.md`
- `docs/VISIT_REFERRAL_CONTRACT.md`

## Backend Guardrails

- Backend APIs remain the authority even when the frontend hides UI. Enforce access through Frappe roles, DocPerm, User Permission, and explicit backend checks.
- Use exact Frappe DocType and field names. Do not invent aliases in contracts or API docs.
- `Guardian` links to ERPNext `Customer` through `Guardian.customer_id`; pets link to guardians through `PetGuardian`.
- Clinical workflows are Pet-native. Do not reintroduce a legacy Patient bridge.
- Billing must flow through backend-owned entry points. Billed clinical or boarding source rows should be treated as locked after invoice creation.
- Whitelisted API methods should use the standard response envelope unless a protocol integration requires a raw response.

## Healthcare Boarding Flow

The boarding backend lives in:

- `pet_app/api/healthcare/boarding.py`
- `pet_app/pet_app/doctype/pet_boarding/`
- `pet_app/pet_app/doctype/service_room/`
- `pet_app/pet_app/doctype/pet_billable_item/`
- `pet_app/pet_app/doctype/pet_boarding_settings/`
- `docs/VISIT_BOARDING_CONTRACT.md`
- `docs/boarding-frontend.md` (frontend consumer/handoff)

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
- `Pet Boarding Settings.default_boarding_type` defaults to `Travel`; clients must read it and must not invent `Treatment` client-side.
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

## Disease Naming: Name vs Primary Key

The `Disease` doctype is named `field:disease_name`, so a record's primary key
*is* its human-readable name. The application came to depend on that coincidence
in both directions: it looked records up **by primary key using a human-typed
name**, and it returned **names as link values**. Both are correct only while
`autoname` remains `field:disease_name`.

This matters because Disease is slated to move to an autoname series
(`DISEASE-#####`) so the same disease name can exist for more than one species.
The moment it does, every PK-by-name lookup misses.

The worst case was `_ensure_disease` in `pet_app/api/workspace.py`. It did
`frappe.db.exists("Disease", disease_name)` and, on a miss, inserted a new
Disease. After the rename that lookup would miss *every time*, so **every
diagnosis save would silently create a duplicate Disease** - unbounded, with no
error, and with the `unique` index that would have caught it removed by the same
migration.

### `_resolve_disease_key`

Added in `pet_app/api/workspace.py`. Resolves a human-readable disease name to an
actual primary key:

- Looks up **by field** (`{"disease_name": ...}`), never by primary key.
- Returns the resolved `name`, or `doc.name` on the create path - **never the
  input string**.
- Fallback order, most specific first:
  1. `(disease_name, species)` when a species is supplied
  2. `(disease_name, species="")` - the generic, species-less catalogue entry, in
     preference to an arbitrary other-species record
  3. `disease_name` alone
- Uses `get_all(..., order_by="creation asc", limit=1)` rather than `get_value`,
  which gives no ordering guarantee. Once duplicate `disease_name` values become
  possible, the **oldest** (most established) catalogue entry wins deterministically.

Behaviour-preserving today: `name == disease_name` for all records and
`disease_name` still carries a UNIQUE index, so all three filter sets collapse to
the same row. That is what makes the fix deployable *in advance* of the rename
and independently verifiable now.

### Why reuse beats create when species is unknown

`Visit Diagnosis` has **no species column** (`disease`, `diagnosis_text`,
`is_primary`, `severity`, `note`). `_ensure_disease` is called only from
`_save_diagnoses`, which passes a diagnosis row - so in practice **no species is
supplied and the name-only path is the operative one**.

The two failure modes are not symmetric:

- Binding a Dog diagnosis to a Cat record is a wrong link: **visible, bounded to
  one row, and repointable** (exactly the operation performed in the Aug 2026
  catalogue cleanup).
- Creating a duplicate Disease on every save is **silent, unbounded and
  compounding**.

When there is nothing to discriminate on, reuse is the recoverable error. Hence
the name-only fallback resolves rather than creates.

**Proper long-term fix, not yet done:** pass the patient's species down from the
visit (`Vet Visit` -> `animal_patient` -> species) into `_ensure_disease` so the
lookup can actually discriminate. This is needed **before** species-specific
duplicate records start being created - that is the moment the name-only fallback
stops being harmless.

### The `Unspecified` near-miss

The Aug 2026 cleanup folded 26 junk/leaked-category records into a single
replacement. It was nearly named `Unspecified`.

`clinical_reports.top_diagnoses` already emits the **literal string**
`'Unspecified'` via `coalesce(..., 'Unspecified')` for visits with no diagnosis at
all. Naming the record `Unspecified` would have made `GROUP BY` merge two
unrelated populations - real reassigned diagnoses and never-diagnosed visits -
into one indistinguishable row.

Note the direction of the hazard: **the `tabDisease` JOIN added to that report
would have *created* the collision, not avoided it.** Before the JOIN the report
grouped on `d.disease` (the key); after it, `dis.disease_name` resolves to the
record's name, which would have been the literal string `'Unspecified'`. The
record was named **`Unspecified Diagnosis`** instead, so the two stay distinct.

Do not name any Disease record exactly `Unspecified`.

### Restarting after editing these files

`kill -HUP` does **not** reload code here - gunicorn runs with `--preload`, so HUP
recycles workers from an already-imported `sys.modules` and verifies nothing. Use:

```
sudo supervisorctl restart frappe-bench-web:frappe-bench-frappe-web
```

The short name fails; the process lives in a supervisor **group**.

Also note: `bench console` starts a *new* process that imports the current source
from disk, so `hasattr(module, "new_symbol")` is **True there regardless of
whether the running workers reloaded**. It is not a valid proof of reload - only
an HTTP request through the actual workers is.

### Still outstanding, deliberately not done

- **`category_a` options string** (`disease.json`): unlike `species`, it has no
  leading blank line, so an unset value silently becomes the first option -
  every auto-created Disease is born filed as `Cardiovascular System`.
  `_ensure_disease` passes `None` for category in the normal case, so this fires
  routinely. One-character fix: prepend `\n` to the options string.
- **`workspace.py` `_visit_diagnoses`**: `"disease_name": _disease_label(row.disease)
  or row.disease` leaks the raw primary key when the label lookup fails. Should
  return `None` (the response already carries `disease` and `diagnosis_text`
  separately). This is an API contract change and needs frontend coordination.
- **`top_diagnoses` GROUP BY**: `tabVet Visit` has a real column named
  `diagnosis`, so `group by diagnosis` binds to **that column, not the SELECT
  alias**. The report therefore emits one row per distinct free-text
  `v.diagnosis` value - the same label repeats (59 rows where 44 are correct).
  Pre-existing and unrelated to the naming work; fix by grouping on a
  non-colliding alias.
- **Composite unique index** on `(disease_name, species)`, replacing the
  single-column `unique` on `disease_name`. The application-level check already
  exists in `disease.py::_validate_duplicate` but is masked by the DB index.
- **The rename itself**: `autoname` -> `DISEASE-.#####`, plus
  `show_title_field_in_link: 1` so link fields keep displaying the name.
