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

## ERPNext Trap: Template Attributes Cannot Be Added After The First Variant

**Do not build "add an attribute to an existing template". It is unsafe and ERPNext
will not stop you.** This was investigated in full on 2026-08-10; the conclusion is
that attributes are effectively fixed once a template has variants.

### Why it looks safe and isn't

`Item.validate_attributes_in_variants`
(`apps/erpnext/erpnext/stock/doctype/item/item.py:822-834`) begins with:

```python
if old_doc_attributes.issubset(set(own_attributes)):
    return
```

Adding an attribute is *always* a superset, so the guard returns before checking
anything. The save succeeds silently and every existing variant is left with no row
for the new attribute. Nothing throws afterwards either -
`Item.validate_variant_attributes` (`item.py:964`) only runs `if self.is_new()`, and
`validate_item_variant_attributes` skips empty values - so the inconsistency has no
failure mode that surfaces it.

The damage appears at generate time, in `find_variant`
(`apps/erpnext/erpnext/controllers/item_variant.py:184`):

```python
if len(args.keys()) == len(variant.get("attributes")):
```

Pre-existing variants carry fewer attribute rows than the new request has keys, so
they can never match and the duplicate check goes blind. Generating `Weight x Flavour`
on a template that previously declared only `Weight` mints a fresh `...-2KG-CHK` while
the old `...-2KG` survives as an orphan - still priced, still publishable, still
sellable, competing with its own replacement inside one product.

### Removal is guarded; addition is not

The non-subset branch of the same function collects every variant carrying a removed
attribute and throws an HTML table naming them. So removal fails loudly, addition
succeeds quietly. Do not reason from "ERPNext validates this" - it validates one
direction only.

### `validate_stock_exists_for_template_item` does NOT protect this path

`item.py:891-910` does cover attribute changes, but the whole block is gated on
`self.stock_ledger_created()` - **the template's own** Stock Ledger Entries. A template
is never stocked (stock lives on the variants), so the guard is inert here. Verified on
this site: the only template had 0 SLEs and so did all of its variants. That guard is
what blocks converting a stock-bearing simple Item into a template; it is not a
template-attribute guard.

### What we built instead

`pet_app.api.variants.set_template_attributes` makes the safe window explicit: it
rewrites a template's attributes **only while the variant count is 0**, and refuses
otherwise with a message that says attributes are fixed and points at creating a new
template. `create_variant_template` states the same rule in its docstring and returns
`attributes_editable` + `attributes_notice`, so a client can surface it at creation -
the one moment the fact is actionable. Both responses share
`ATTRIBUTES_LOCKED_NOTICE` so the wording cannot drift.

### Related: saving a template cascades to its variants

`Item.on_update` calls `update_variants()`, and `Item Variant Settings.do_not_update_variants`
is `0` on this site, so **any** template save re-saves every variant (inline up to 30,
queued beyond) and runs `copy_attributes_to_variant`. Checked and safe: `attributes` is
`reqd = 0` and absent from the `Variant Field` list, so the template's blank attribute
rows are not pushed down, and `custom_store_published` is likewise absent so per-variant
publishing survives. But `disabled`, `stock_uom`, `item_group`, `brand` and
`is_stock_item` **are** in that list and will overwrite the variants on every template
save. Any future template-editing endpoint inherits this whether it intends to or not.

## Clinic Branch Separation

Multiple clinics share one site. Only **worklists** are separated. Implemented 2026-08-11.

Contract: `docs/CLINIC_BRANCH_SEPARATION.md`. Frontend consumer: `docs/frontend-branch-separation.md`.

Branch is ERPNext `Branch`, assigned via standard `User Permission` rows. There is no
custom branch DocType — an earlier `clinic branch` stub was removed by
`pet_app/patches/clinic_branch_foundation.py`.

Scoped, and nothing else (`SCOPED_DOCTYPES` in `pet_app/utils/branch.py`):

- `Vet Visit`, `Vet Case Sheet`, `Appointment`, `Pet Queue Ticket`, `PetCareService`,
  `Pet Procedure`, `Sales Invoice`

Deliberately **not** scoped:

- **Patient record** — Pet, Guardian, Pet Medical Profile, Pet Care Episode, vaccination
  and deworming history. Pets and guardians are shared; a vet must see prior treatment
  from any clinic. Hiding it is a clinical safety problem.
- **Boarding** — `Pet Boarding`, `Service Room`. One facility serves every clinic; the
  Service Room pool is shared and has no branch, so scoping the booking would let one
  clinic reserve a room another clinic could neither see nor check out. Accountability
  lives on `Pet Boarding.practitioner`, an **optional, frontend-owned** field the backend
  never infers.
- **Diagnostics** — `Lab`, `Imaging`. One lab/radiology operation serves all clinics.
  `_diagnostic_items` in `pet_app/api/workspace.py` is unscoped **by decision**; do not
  "fix" it.
- **The money** — Customer, Payment Entry, GL Entry, POS Profile. One Company, one
  receivable ledger, so a customer's balance is company-wide and a debt raised at one
  clinic is collectable at another. `Sales Invoice` **is** scoped (the document belongs
  to the clinic that raised it, inherited from the linked visit) but the balance is not.
  Scoping a balance would make a customer look settled at one clinic while owing at
  another.

`SCOPED_DOCTYPES` is an allow-list, not a deny-list. ERPNext already puts an unrelated
`branch` custom field on Sales Invoice, Sales Order, Payment Entry, Stock Entry and POS
Profile for reporting; a deny-list would silently scope billing.

Rules:

- A **NULL branch is visible to every clinic**, not none — Frappe emits
  `ifnull(branch,'')='' or branch in (...)`. Backfill is a correctness requirement, and
  no write path may leave the field empty. `apply_strict_user_permissions` is **not**
  enabled; it is global and would change existing warehouse/POS/practitioner restrictions.
- A **user** with no Branch User Permission is likewise unrestricted and sees everything.
  Administrator is intentionally left unassigned so they see every clinic.
- Stamp through the `before_insert` hook (`pet_app.utils.branch.stamp_branch_on_insert`),
  never per call site — `Vet Visit` alone has five non-test insert paths.
- `Healthcare Practitioner.clinic_branch` is a read-only mirror of the User Permission.
  The sync hook runs on `after_delete`, not `on_trash`, because `on_trash` fires before
  the row leaves the table.
- Filtering is applied per endpoint in the API layer, because `frappe.get_all` does not
  check permissions. This is **worklist separation, not tenant isolation** — say so
  plainly rather than implying the site is multi-tenant.

Do not build a UI that configures which doctypes are scoped. Branches and assignments are
data; the rules are code. See the "Permissions Refactor" section above — this app already
deleted five permission DocTypes built that way.

Tests: `pet_app/tests/test_branch_scope.py`. Several assert that history, diagnostics and
billing stay **global**, so a future change that scopes them fails with a message naming
the decision.

## Meta Template Mirror (2026-08-23)

Meta's message templates are mirrored into their own doctype rather than bolted onto the
local one. Built in four phases: schema, Graph client and six endpoints, webhook route,
docs. `docs/whatsapp-frontend.md` holds the frontend contract.

Two doctypes, both created by `pet_app/patches/p1_12_meta_template_mirror.py`:

- `Pet App WhatsApp Meta Template` — autonamed `field:meta_template_id`, so Meta's own ID is
  the docname and a re-sync is a stable upsert.
- `Pet App WhatsApp Meta Category Map` — local category to Meta category, as data.

### Why a mirror and not `meta_*` fields on `Pet App WhatsApp Template`

The two objects have different owners and different lifecycles. Meta owns approval state,
rejection reason and component structure; this app owns the event binding, the delivery mode
and the body preview. Merged, a sync job writes into rows a human is editing, and a template
Meta has never seen is indistinguishable from an approved one.

`Pet App WhatsApp Template` was not modified. The mirror points at it through
`local_template`; it does not point back.

### `status`, `category` and `language` are `Data`. Do not make them `Select`.

Meta extends those vocabularies on its own schedule. A `Select` is a hardcoded list living in
a patch: a value outside it throws on save or is silently dropped, and the mirror stops
reflecting reality until somebody ships a patch. Nothing validates, maps, or re-cases these
values — whatever Meta sent is stored. A webhook carrying `SOME_FUTURE_STATE_2031` lands
unchanged; that is a tested property, not an aspiration.

`raw_json` carries the same guarantee at the object level. The live WABA already returns
three keys with no column — `disable_ios_autofill`, `is_primary_device_delivery_only`,
`parameter_format` — and all three survive every sync. Adopting one later is a read, not a
migration plus a backfill.

Category translation is a lookup, never an `if` chain and never a dict literal. `Service`
seeds blank because Meta has no Service category, and submitting in it throws naming the row
to fill. Resolution accepts either side of the mapping, but both answers come out of the
table.

### Configuration comes from the account row

WABA ID from `whatsapp_business_account_id`, API version from `graph_api_version` — the site
runs `v25.0`, not the `v20.0` default, which is exactly why no version literal may appear in
code. Template management additionally needs the `whatsapp_business_management` token
permission; `whatsapp_business_messaging` alone reaches the messaging edge but not this one,
so a token can work for sending and still fail here. `MetaTemplatePermissionError` detects
that case and names the fix.

### Binding is exact, and the collation fights you

Mirror rows bind to local templates by exact `(template_name, language)`. One match binds,
none is `unbound`, several is `ambiguous` and binds neither. No fuzzy matching, no
slugifying, no normalisation.

The database collation is case-insensitive, so a plain `frappe.get_all` filter *is* a fuzzy
match — it will bind `Feedback` to `feedback`. Candidates are therefore re-checked
byte-for-byte in Python before one is accepted. `resolve_binding` and `apply_status_update`
both do this; keep it if you touch either.

A high `unbound` count is normal. A local row with `language = "Arabic"` never binds, because
Meta's locale code is `ar`.

### Webhook

`_extract_events` now reads `change.get("field")`. `message_template_status_update` becomes a
`template_status` event instead of falling into the `raw` catch-all, where it was stored and
dropped. It writes `status`, `rejected_reason` and `last_synced_at` on the matched row.

No matching row is not an error: nothing is created and nothing throws, because a template
can be approved before this site has synced it, and a row invented from a status payload
would have no components and no `raw_json`. The reason goes to `processing_error` on the
stored `Pet App WhatsApp Webhook Event` row — a field that existed since `p1_5` and was
written by nothing until now. Unhandled webhook fields leave a note there too.

`reason: "NONE"` is stored verbatim, so an approved row reads `NONE` after a webhook and
blank after a sync. That is deliberate: normalising it is the value-list logic this design
avoids. It is solved on the frontend.

**The WABA must be subscribed to `message_template_status_update` in the Meta App dashboard
under WhatsApp → Configuration → Webhook fields.** Nothing in this repo can declare, verify
or repair that subscription, and if it is off, approvals arrive nowhere with no error
anywhere. `sync_meta_templates` is the fallback path.

### Deliberately not done

- Nothing validates that a template is `APPROVED` before sending. `engine.py` was not
  touched; a local template whose name Meta has never seen still fails at send time.
- `_response_json` in `notifications/meta_templates.py` is a deliberate copy of the one in
  `channels/whatsapp_meta.py`, not a shared helper. The send path and the template path stay
  uncoupled; do not collapse them.
- Template-status webhook events are not deduplicated — they carry no message ID, and
  `provider_message_id` was not overloaded with a template ID. Re-delivery rewrites the same
  values and adds a log row.

Tests: none added. Verification was done against the live site by running each path inside a
transaction and rolling back — including a 102-payload replay of stored webhook events
through the new `_extract_events`, which produced zero classification changes.
