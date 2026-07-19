# Backend Issues — alkhokh pet store

Standalone reference for the **backend team**. Compiled from frontend engineering notes in `docs/AGENT.md` across multiple work sessions.

Each item is a place where the **backend** must change (or where the frontend is carrying a workaround that should be removed once the backend is fixed). Frontend-only bugs that turned out *not* to be backend problems are listed at the end so they aren't mistakenly chased.

**Severity key:** 🔴 Critical (broken functionality or data-confidentiality gap) · 🟡 Medium (forces a frontend workaround / fragile) · 🟢 Low (cosmetic or fully absorbed by frontend).

---

## Suggested priority order
1. **#1** PetCareService `permission_query_conditions` (security / confidentiality)
2. **#2** practitioner_type Select + **#3** service_categories child table + **#4** wizard methods (features broken today)
3. **#9** `complete_case` idempotency (fragile visit-completion flow)
4. **#6 / #7 / #8 / #10 / #11** name-vs-ID standardization (removes a whole class of frontend enrichment workarounds)
5. Everything else as cleanup.

---

## 🔴 CRITICAL

### 1. `PetCareService` has no row-level permission scoping
Service providers can query everyone's services, not just their own.

- **Problem:** `GetHealthcareServices` hits `GET /api/resource/PetCareService` (generic resource list). The "providers only see their own services" rule depends entirely on Frappe doctype permissions; the frontend does not (and cannot securely) inject a `provider = me` filter.
- **Frontend file(s):** `src/api/healthcareOperationalRecordsApi.ts` (`serviceRecordService`), `src/components/healthcare/ServiceProviderBillingWorkspace.vue`.
- **Workaround applied:** None shipped. A non-secure, DevTools-bypassable UX-only filter (`GetDoctorByLinkedUser` → inject `[SERVICE_DOCTYPE,"provider","=",id]`) was documented but intentionally **not** applied.
- **Backend must:** add a `permission_query_conditions` hook on `PetCareService` filtering by `provider = <Healthcare Practitioner linked to frappe.session.user>` for non-admin roles. Frontend can then keep using `/api/resource/PetCareService` unchanged.

### 2. `Healthcare Practitioner.practitioner_type` Select rejects valid roles
- **Problem:** Saving a practitioner with `Service Provider` or `Coordinator` returns `"Practitioner Type must be Doctor, Nurse, or Other"`. The doctype Select only allows `Doctor / Nurse / Other`.
- **Frontend file(s):** `src/components/healthcare/settings/HealthcareDoctorSettings.vue` (`practitionerTypeOptions`).
- **Workaround applied:** Frontend ships the extended option list in anticipation; saves of the two new values currently **fail**.
- **Backend must:** extend the `practitioner_type` Select options to include `Service Provider` and `Coordinator`. Once deployed the new values save with no further frontend work.

### 3. `service_categories` child table missing on `Healthcare Practitioner`
- **Problem:** Frontend sends a `service_categories` child array (Link → `CategoryCareServices`) on practitioner save. If the child table doesn't exist, the field is **silently dropped** — provider↔category assignment never persists, which breaks the Care Service wizard's provider matching.
- **Frontend file(s):** `src/api/doctorApi.ts`, `src/components/healthcare/settings/HealthcareDoctorSettings.vue`.
- **Workaround applied:** Mapper tolerates empty/missing field without erroring, but the data simply isn't saved.
- **Backend must:** add the `service_categories` child table (single Link column `category` → `CategoryCareServices`) to the Healthcare Practitioner doctype.

### 4. Care Service wizard depends on two whitelisted methods that may not exist
- **Problem:** The multi-pet wizard calls `pet_app.api.care_service.get_providers_for_category` and `pet_app.api.care_service.bulk_create_pet_care_services`. These are assumed contracts.
- **Frontend file(s):** `src/api/careServiceWizardApi.ts`.
- **Workaround applied:** `get_providers_for_category` swallows errors → empty state; `bulk_create_pet_care_services` re-throws → toast. The wizard degrades gracefully but **cannot create services** if the endpoint is absent.
- **Backend must:** implement both methods.
  - `get_providers_for_category?category=<id>` → enabled practitioners whose `service_categories` contains the category (empty list is fine).
  - `bulk_create_pet_care_services` with `{ entries }` → one `PetCareService` per entry, reusing single-create's `service_option → item_code/price` resolution. Must return `{ created: [<docnames>], failed: [{ pet_id, reason }] }`.

---

## 🟡 MEDIUM

### 5. Guardian field spelling normalized on Vet Visit / appointments
- **Status:** Fixed in backend. Vet Visit, Pet Boarding, and PetGuardian now use `guardian_name`; Appointment now uses `custom_guardian`.
- **Frontend file(s):** `src/pages/healthcare/visits/index.vue` (sort map `guardian → guardian_name`), `src/api/healthcareVisitApi.ts`, `src/api/healthcareAppointmentApi.ts` (`custom_guardian`).
- **Backend change:** DocType JSON, customizations, fixtures, Python references, and tests use the corrected names.
- **Migration:** pre-model-sync patch renames existing DB columns before DocType/customization sync.
- **⚠️ Caveat:** the `Guardian` doctype itself correctly uses `full_name` — do **not** "fix" this by spreading the typo to `Guardian` (see #14 in the "not backend issues" list).

### 6. DocTypes don't reliably set `title_field` → Link filters/pickers show raw IDs
- **Problem:** Link-field value dropdowns and pickers show document IDs (`PET-00241`, `GUARDIAN-00115`, `HCP-00002`, `CCS-002`) because the linked DocTypes don't expose a usable `title_field` via meta.
- **Frontend file(s):** `src/components/shared/FrappeConditionBuilder.vue` (`effectiveLinkConfig` / `linkedDoctypeConfig`), `src/pages/permission.vue` (`ALLOWED_VALUE_LABEL_FIELDS`), `src/services/frappe/meta.ts`.
- **Workaround applied:** hardcoded per-doctype display-field overrides (`Pet→pet_name`, `Guardian→full_name`, `Healthcare Practitioner→practitioner_name`, `CategoryCareServices→category_name`, `Customer→customer_name`, `Item→item_name`, …) plus `title_field` auto-resolution from meta.
- **Backend should:** set `title_field` on these DocTypes so the frontend overrides become unnecessary.

### 7. Care-plan / follow-up queue returns Link IDs without display names
- **Problem:** `pet_app.api.care_plan.list_due_plan_items` returns `pet` and `doctor` as raw IDs (`PET-00010`, `HCP-00002`) with no `pet_name` / `doctor_name`.
- **Frontend file(s):** `src/api/visitWorkbenchApi.ts` (`normalizePlanItem`, `enrichDuePlanItemNames`), `src/components/visit-workbench/FollowUpSchedulingQueue.vue`.
- **Workaround applied:** frontend enrichment pass batch-fetches `Pet.pet_name` + `Healthcare Practitioner.practitioner_name` by ID and patches rows (one extra GET per refresh).
- **Backend should:** include `pet_name` and `doctor_name` / `practitioner_name` in the `list_due_plan_items` response to drop the extra round-trips.

### 8. `PetCareService` list doesn't return joined practitioner names
- **Problem:** `doctor.practitioner_name` / `provider.practitioner_name` don't reliably come back from the resource list, so doctor/provider render as `null` or IDs.
- **Frontend file(s):** `src/api/healthcareOperationalRecordsApi.ts` (`enrichServicePractitionerNames`).
- **Workaround applied:** enrichment pass batch-fetches Healthcare Practitioner names by ID and patches rows (wired into `GetHealthcareServices`, `GetHealthcareServiceById`, `UpdateHealthcareService`).
- **Backend should:** return the joined `practitioner_name` for `doctor` / `provider` (or expose a whitelisted list method that does), removing the need for enrichment.

### 9. Vet Visit completion: `complete_case` vs `docstatus` desync
- **Problem:** Closing a visit can leave `Vet Visit.status = Completed` while `docstatus` is still `0` (draft). Calling workspace `complete_case` again errors: `"complete_case is not allowed ... while status is Completed"`. Submitted docs are immutable.
- **Frontend file(s):** visit workbench flow / `src/api/visitWorkbenchApi.ts`.
- **Workaround applied:** elaborate frontend dance — after `complete_case`, fetch raw visit; if `docstatus === 0`, submit via `frappe.client.submit`; if status already `Completed` but draft, skip `complete_case`, save notes directly, then submit; verify after submit.
- **Backend should:** make `complete_case` idempotent and own the status↔docstatus transition atomically so the frontend doesn't have to orchestrate submit + re-verify.

### 10. Inconsistent Link display: names vs IDs returned unpredictably
- **Problem:** Backend sometimes returns only IDs, sometimes flat names, sometimes nested objects — for pet/guardian across boarding, services, and visits. Forces multi-key fallback mappers everywhere.
- **Frontend file(s):** `src/api/healthcareApi.utils.ts`, boarding mappers, `src/api/healthcareOperationalRecordsApi.ts`.
- **Workaround applied:** mappers accept nested objects and flat keys (`pet_name`, `guardian_name`, `active_boarding.pet_name`, dotted `pet_id.pet_name`).
- **Backend should:** standardize list/detail payloads to return **both** the link ID and its display name under consistent keys.

### 11. `guardian_id.full_name` flattened to top-level `full_name` (fragile mapper)
- **Problem:** Backend flattens the linked field to a bare top-level `"full_name"` key. The mapper must check `"full_name"` **first**, which is fragile — any future field-list entry bringing a different `*.full_name` (e.g. `customer.full_name`) would be mis-picked.
- **Frontend file(s):** `src/api/healthcareOperationalRecordsApi.ts` (`mapHealthcareServiceRecord`).
- **Workaround applied:** `toNullableString(raw, ["full_name", "guardian_id.full_name", "guardian_name"])` with the bare key first.
- **Backend should:** return the namespaced `guardian_id.full_name` (not a flattened `full_name`) so the namespaced key can win unambiguously.

### 12. Boarding deposit → Payment Entry not created/linked
- **Problem:** When a deposit is captured at `check_in_boarding`, there's no guaranteed Payment Entry creation or reference stored on the boarding, so deposit collection isn't traceable.
- **Frontend file(s):** boarding check-in / detail mappers.
- **Workaround applied:** frontend sends deposit as both `deposit` and `advance_amount`; reads many fallback aliases (`deposit_payment_entry`, nested `active_boarding.*` / `boarding.*`) to render whatever the backend returns.
- **Backend should:** on deposit capture, create + submit a `Payment Entry` for the linked customer and store its reference on the boarding (e.g. `deposit_payment_entry`).

### 13. Cashier cash-account scoping not enforced server-side
- **Problem:** Cashier-profile access is filtered only on the frontend; account linking on approval is assumed.
- **Frontend file(s):** cashier pages / `src/helper/permissionHelper.ts`.
- **Workaround applied:** frontend filtering + reads `custom_cash_account` / `cash_account` as source of truth.
- **Backend should:** enforce user access to cashier profiles server-side and create/link the cash account when admin/accountant approves the setup action.

---

## 🟢 LOW

### 14. `Field not permitted in query` on various DocTypes
- **Problem:** Frappe rejects querying certain fields: `pet_id`, `animal_patient` (on `Pet Medical Profile`), `category_type` (on `CategoryCareServices`), `payment_status`, `intake_summary`, and double-link traversal (`pet_id.food_type.type_name`).
- **Frontend file(s):** `src/api/petMedicalProfileApi.ts`, `src/api/categoryCareServicesApi.ts` (`CATEGORY_CARE_FIELDS`), generic master-data list flow.
- **Workaround applied:** offending fields pruned from field lists; keyword matching derives icons/colors without `category_type`; queries limited to one link hop.
- **Backend should:** if these fields are meant to be queryable, grant read permission / fix field-level perms — notably `category_type` on `CategoryCareServices` (the preferred stable field for Service/Lab/Radiology classification), currently unusable and forcing fragile category-name string matching.

### 15. `frappe.client.get_list` SQL bug on User-linked dotted fields
- **Problem:** Requesting joined User fields (`rated_by.first_name` / `last_name` / `full_name`) triggers `(1054, "Unknown column 'tabUser.name' in 'WHERE'")` — Frappe injects a `tabUser.name` permission clause without the join being set up.
- **Frontend file(s):** `src/api/ratingApi.ts`.
- **Workaround applied:** reverted joined fields; `lookupUserDisplayNames` batches a separate `User` `name IN [...]` query and patches `ratedByName`.
- **Backend should:** expose rater display name via a whitelisted method or proper join, so a second query isn't needed. (Cosmetic — names already resolve via workaround.)

### 16. Inconsistent response envelopes
- **Problem:** Some endpoints return `data`, some `message.data`, some include `total`, some require a separate count fetch.
- **Frontend file(s):** `src/services/frappe/*`, `unwrapMessage` helper.
- **Workaround applied:** helpers handle all shapes defensively.
- **Backend should:** standardize the response envelope across whitelisted methods.

### 17. `update_stock` omitted on purchase-invoice reads
- **Problem:** Backend sometimes omits `update_stock`; treating absence as `false` would be wrong.
- **Frontend file(s):** warehouse / purchase-invoice mapping.
- **Workaround applied:** frontend defaults missing `update_stock` to checked/true.
- **Backend should:** always return `update_stock` explicitly.

### 18. `Boarding Type` handling
- **Problem:** Field optional/absent; frontend wants to display it on the room card and stay summary.
- **Frontend file(s):** boarding mappers / room board.
- **Workaround applied:** treated as optional; rendered when present.
- **Backend should:** finalize and consistently return `Boarding Type` when set.

### 19. Legacy visit-level follow-up API is unused by the healthcare Follow-ups page
- **Problem:** The active healthcare Follow-ups page is backed by `Pet Care Plan Item` rows and their linked `Appointment` records through `pet_app.api.care_plan.list_due_plan_items` / `schedule_plan_item_appointment`. The older `pet_app.api.follow_up` module works from `Vet Visit.follow_up_*` fields and is now a parallel legacy surface.
- **Current state:** No `FollowUp` DocType exists on the site; the only matching DocType is Frappe's unrelated `Document Follow`.
- **Backend should:** leave the legacy API/data alone for now, but plan a later removal or migration so follow-up concepts live in one workflow.

---

## ⚪ NOT backend issues (verified frontend-only — do not chase)

- **`guardian_name` error on the follow-ups Guardian filter** — was a frontend bug: it requested a non-existent column on the `Guardian` doctype (whose display field is correctly `full_name`). Fixed in `src/components/visit-workbench/FollowUpSchedulingQueue.vue`. The `Guardian` doctype is fine. (Distinct from #5, which was the legacy Guardian display-field typo on **Vet Visit**.)
- **"Pet weight always shows 0" in Service Provider workspace** — frontend mapper bug: `"pet_id.weight"` was missing from `mapPetSummary`'s pick list (it was grabbing the PetCareService's own null billing weight). Fixed in `src/api/healthcareApi.utils.ts`. Backend data was correct.
