# Alkokh Vet System — Frontend AI Onboarding Brief

Generated from the local Frappe app at `apps/pet_app` on 2026-05-13.

This brief is for a Vue.js frontend assistant. It describes the backend domain, Frappe DocTypes, APIs, workflows, permissions, and the current implementation state. Treat exact DocType and method names as case-sensitive.

---

## 1. Project Overview

Alkokh Vet System is a veterinary clinic management system built on Frappe/ERPNext. It manages the daily clinic workflow from guardian and pet registration through appointment intake, consultation, diagnostics, treatment, pharmacy, boarding, billing, marketplace sales, delivery, notifications, and reporting.

Primary users:

- Clinic administrators and managers: manage staff, roles, masters, services, products, rooms, accounting defaults, reports, and operational queues.
- Reception/coordinators: register guardians and pets, create appointments, check in visits, create case sheets, manage queues, and route work.
- Veterinarians / healthcare practitioners: review pet context, start visits, record vitals and notes, diagnose, prescribe, order lab/imaging/procedures/services, request follow-ups, and complete cases.
- Lab/radiology/service operators: perform ordered diagnostics/services and update results/status.
- Cashiers/accounting staff: create invoices, collect payments, settle cash, and manage POS profiles.
- Guardians / pet owners: phone/OTP login, profile completion, pet dashboard, medical timeline/documents, appointments, invoices, and marketplace ordering.
- Drivers: delivery login, order tasks, cash collection/balance.

Core modules:

- Identity and ownership: `Guardian`, `Pet`, `PetGuardian`, `PetAddRequest`, ERPNext `Customer`/`User`.
- Clinical EMR: `Vet Case Sheet`, `Vet Visit`, diagnoses, vitals, medications, addenda, care episodes, care plans.
- Services/diagnostics/procedures: care service catalog, lab, imaging, procedures, consent forms.
- Boarding: rooms, reservations, check-in/out, boarding billables, owner updates.
- Scheduling and queue: appointments, walk-ins, available slots, queue tickets.
- Billing/accounting/cashier: visit/boarding/POS invoices, payment entries, cashier profiles, settlements.
- Marketplace/inventory/delivery: products, product categories, ERPNext item projection, stock, sales orders, coupons, drivers, delivery proof.
- Notifications/reminders: WhatsApp/SMS/Email/In App queue, templates, reminders, webhooks.
- Reports/analytics/audit/import/offline: daily clinical reports, revenue/stock/mortality analytics, audit logs, import jobs, idempotent offline tracking.
- Permissions/admin: Frappe-native roles, role profiles, DocPerms, and User Permission restrictions.

---

## 2. Backend Architecture

### Frappe App Structure

Main app path: `apps/pet_app/pet_app`.

Important folders:

- `pet_app/pet_app/doctype/*`: checked-in DocType JSON/controllers.
- `pet_app/api/*`: whitelisted methods for Vue/mobile/admin.
- `pet_app/api/v1/*`: normalized wrapper APIs for cleaner frontend contracts.
- `pet_app/utils/*`: business helpers for permissions, medical profile sync, billing, mortality, role profiles, guardian/customer sync.
- `pet_app/patches/*`: migration-created DocTypes and custom fields.
- `pet_app/notifications/*`: notification engine, channels, scheduler, webhook handling.
- `pet_app/workflows/clinical_state.py`: status transition and action validation constants.

### Main DocTypes And Key Fields

#### Identity And Ownership

| DocType | Kind | Key fields / notes |
|---|---|---|
| `Guardian` | DocType | `phone`, `full_name`, `email_id`, `address_line1`, `city`, `country`, `guardian_image`, `is_active`, `otp_verified`, `otp_code`, `otp_expires_at`, `wrong_attempts`, `pending_phone_change`, `pending_password_hash`, `user_id`, `customer_id`. Controller validates Guardian-Customer mapping, creates/syncs Customer for admin-created guardians. |
| `Pet` | DocType | `pet_name`, `animal_species`, `animal_type`, `breed`, `birth_date`, `gender`, `weight`, `status`, `pet_status`, `requested_by`, `pet_image`, `food_brand`, `food_type`, notes. Guardian-created pets trigger `PetAddRequest`. |
| `PetGuardian` | DocType | `pet_id`, `guardian_id`, `role` (`primary_owner`, `owner`), display snapshots. This is the ownership/access link. |
| `PetAddRequest` | DocType | `pet_id`, `guardian_id`, `status` (`Pending`, `Approved`, `Rejected`), `approved_by`, `approval_date`, `rejection_reason`. Approval marks pet approved, creates `PetGuardian`, and ensures `Pet Medical Profile`. |
| `Healthcare Practitioner` | DocType | `practitioner_name`, `practitioner_type` (`Doctor`, `Nurse`, `Other`), `phone`, `user_id`, `specialization`, `photo`, `disabled`. This replaces the removed custom `Doctor` DocType; the `Doctor` role still exists. |
| ERPNext `Customer` | Standard | Billing party for `Guardian`. Do not model Customer as the pet owner in the Vue UX. |
| ERPNext `User` | Standard | Login identity. Staff use role profiles; guardians are phone-derived users such as `07...@petapp.local`. |

#### Clinical EMR

| DocType | Kind | Key fields / notes |
|---|---|---|
| `Vet Case Sheet` | DocType | `status`, `priority`, `case_sheet_date`, `appointment`, `guardian`, `customer`, `phone_number`, `animal_patient`, pet snapshots, `chief_complaint`, `complaint_other`, symptom/background/daily-condition fields, `vet_visit`. Validates Guardian-Pet link and conditional fields. |
| `Vet Visit` | DocType | `status`, `priority`, `visit_datetime`, `case_sheet`, `appointment`, `guardian`, `customer`, `animal_patient`, `doctor`, `visit_type`, vitals, `illness`, `diagnosis`, `treatment_plan`, notes, `diagnoses`, `care_services`, `prescribed_medications`, `orders`, `billable_items`, follow-up fields, `sales_invoice`, `billed`, `billing_status`. Identity is locked after creation; billed visits are locked. |
| `Pet Medical Profile` | DocType | Permanent pet medical dashboard: `pet`, `primary_guardian`, `customer`, status fields, `active_care_episode`, `current_visit`, `current_case_sheet`, alerts/summary fields, latest visit/vitals, allergies/chronic/diet/behavior notes. Ignore the legacy `healthcare_patient` bridge in frontend workflows. |
| `Pet Care Episode` | DocType | Active/closed medical case: `pet`, `guardian`, `customer`, `primary_doctor`, `episode_title`, `episode_type`, `episode_status`, `priority`, `severity`, opened/current/last visit links, summary fields, follow-up fields, child tables for problems/medications/monitoring. |
| `Pet Care Plan Item` | DocType | Treatment/follow-up task: `pet`, `care_episode`, `source_visit`, `doctor`, `plan_type`, `title`, `description`, `instructions`, due/start/end fields, `requires_appointment`, `appointment`, `requires_reminder`, `reminder`, `status`, `priority`, completion/conversion fields. |
| `Visit Diagnosis` | Child | `disease`, `diagnosis_text`, `is_primary`, `severity`, `note`. |
| `Disease` | DocType | `disease_name`, `species`, `category`, `active`. |
| `Vet Visit Vital Sign` | Child | `recorded_at`, `recorded_by`, `temperature`, `heart_rate`, `respiratory_rate`, `weight`, body condition, hydration, pain, BP, SpO2, notes. |
| `Vet Visit Medication Item` | Child | `medication_item`, `medication`, `qty`, `rate`, `amount`, dosage/frequency/duration/instructions, warehouse, dispense status, dispensed qty, stock/invoice refs. |
| `Medication` | DocType | `medication_name`, `code`, `linked_item`, `default_warehouse`, `default_price`, dosage defaults, counters. Syncs to ERPNext `Item`/`Item Price`. |
| `Visit Order` | Child | `order_id`, `kind` (`lab`, `radiology`, `service`, `procedure`, `medication`, `other`), `title`, `template_id`, `status`, `priority`, `qty`, `price`, linked record refs. |
| `Visit Consult Request` | Child | `requested_doctor`, `requested_by`, `reason`, `status`, `consult_note`, timestamps. |
| `Vet Visit Addendum` | DocType | `visit`, `addendum_datetime`, author/doctor/guardian/customer/pet, `addendum_type`, `status`, approval fields, `note`, `reason`. |

#### Services, Diagnostics, Procedures

| DocType | Kind | Key fields / notes |
|---|---|---|
| `CategoryCareServices` | DocType | `category_name`, `description`, `active`. |
| `CareService template` | DocType | Service catalog: `animal_species`, `frequency`, `category_id`, `service_name`, `item_code`, `default_price`, `price_list`, diagnostic metadata. This is the current service master. |
| `Care Service Billing Option` | DocType | Pricing alternatives for a parent care service: `parent_care_service`, `care_service`, labels/species/weight range, `item_code`, `default_rate`, `disabled`. |
| `custom services` | Child | Visit service selections: `pet_care_service_id`, `care_service_id`. |
| `PetCareService` | DocType | Scheduled/performed service: `status` (`pending`, `completed`, `overdue`), `due_date`, `pet_service_name`, `pet_id`, `guardian_id`, `care_service_id`, `item_code`, `price`, `doctor`, `visit`, `order_id`, `provider`, `user`, start/end dates. |
| `Lab` | DocType | `visit`, `order_id`, `pet`, `doctor`, `care_service`, `item_code`, `rate`, `status`, collection/result/release fields, `result_visibility`, `attachment_required`, `result`. Syncs billable item to visit. |
| `Imaging` | DocType | Same pattern as Lab, with imaging `report` and `image`. |
| `Procedure Template` | DocType | `procedure_name`, `code`, `species`, `category`, `billing_care_service`, duration, consent/anesthesia flags, `steps`. |
| `Pet Procedure` | DocType | `visit`, `order_id`, `pet`, `guardian`, `doctor`, `provider`, `procedure_template`, `care_service`, `item_code`, `rate`, `status`, timing, consent/anesthesia, checklist, notes/findings/outcome/aftercare. |
| `Procedure Checklist Item` | Child | `step_title`, `required`, `done`, `note`. |
| `Pet Consent Template` / `Pet Consent Form` | DocTypes | Consent text/templates and signed consent records linked to visit/procedure/pet/guardian. |

#### Boarding

| DocType | Kind | Key fields / notes |
|---|---|---|
| `Service Room` | DocType | `room_code`, `room_name`, `room_type`, `status`, `image`, `notes`. |
| `Pet Boarding` | Submittable | `service_room`, `pet`, `guardian`, `customer`, `boarding_type`, `record_status` (`Reserved`, `Checked In`, `Checked Out`, `Cancelled`), `status`, timestamps, stay days, totals/deposit/balance, `billing_status`, `sales_invoice`, `billable_items`, notes. |
| `Pet Boarding Settings` | Single | `travel_boarding_item`, `treatment_boarding_item`, `default_boarding_type`. |
| `Pet Billable Item` | Child | Shared billing row: `item_name`, `item_code`, `item_type`, `qty`, `rate`, `amount`, `status`, linked refs, `order_id`. |
| Patch-created boarding updates | DocTypes | `Pet Boarding Daily Log`, `Pet Boarding Feeding Schedule`, `Pet Boarding Medication Schedule`, `Pet Boarding Incident Report`, `Pet Boarding Media Update`. Used by owner update APIs. |

#### Products, Inventory, Orders, Delivery

| DocType | Kind | Key fields / notes |
|---|---|---|
| `Product` | DocType | `product_name`, `sku`, `barcode`, `description`, `price`, `discounted_price`, `vendor`, `category`, `status`, `image`, `tags`, `item`, `item_group`, `item_price`, variants, `brand`. Projects to ERPNext `Item`/`Item Price`. |
| `Product Category` | NestedSet | `category_name`, `parent_product_category`, `is_group`, `enabled`, `display_order`, `image`, `description`, `item_group`, `lft`, `rgt`. Syncs to ERPNext `Item Group`. |
| `Product Variant` | Child | `options`, `value`, `price`. |
| `FoodBrand`, `FoodType`, `Pet Breed` | Masters | Food and breed metadata. Note: `Pet.food_brand` currently links to ERPNext `Brand`, not `FoodBrand`. |
| ERPNext commerce | Standard/customized | `Item`, `Item Group`, `Item Price`, `Warehouse`, `Bin`, `Stock Entry`, `Sales Order`, `Sales Invoice`, `Payment Entry`, `Driver`, `Coupon Code`, `Pricing Rule`. |
| `Delivery Assignment` | Patch DocType | `driver`, `sales_order`, `sales_invoice`, `guardian`, `customer`, `status`, address/cash/timestamps/failure reason. |
| `Delivery Proof` | Patch DocType | `assignment`, `proof_type`, `file`, `received_by`, `captured_at`, `note`. |
| `Driver Shift`, `Driver Cash Handover` | Patch DocTypes | Driver operational/cash tracking. |

#### Accounting, Admin, Notifications, Growth

| DocType | Kind | Key fields / notes |
|---|---|---|
| `Pet App Accounting Settings` | Single | `treasury_cash_account`, `default_company`, `default_cash_mode_of_payment`. |
| `Pet App Cashier Settlement` | Submittable | cashier/POS/company/account fields, expected/counted/transfer/difference amounts, `payment_entry`, status. |
| `Pet App Workspace View` | DocType | User-saved view: `user`, `view_name`, `active`, `is_default`, filters/columns JSON. |
| `Pet App User Restriction` | Deprecated | Old restriction storage. Do not use for security decisions; use Frappe `User Permission`. |
| Notification engine DocTypes | Patch-created | `Pet App Notification Settings`, `Pet App WhatsApp Account`, `Pet App WhatsApp Template`, `Pet App Notification Rule`, `Pet App Notification Queue`, `Pet App Notification Log`, `Pet App WhatsApp Webhook Event`, `Pet App Communication Consent`, `Pet App Reminder`. |
| Legacy notification DocTypes | Patch-created | `Pet Reminder`, `Pet Notification Template`, `Pet Notification Log`; still supported by compatibility wrappers. |
| Membership/loyalty | Patch-created | `Pet Membership Plan`, `Pet Membership Subscription`, `Pet Loyalty Ledger`, `Pet Loyalty Rule`. |
| Analytics/support | Patch-created | `Pet App Audit Log`, `Pet App Import Job`, `Pet App Import Error`, `Pet App Offline Request`, `Clinical Alert Rule`, `Clinical Alert Log`, `Medication Dose Rule`, `Species Vital Range`, inventory alert/purchase suggestion DocTypes. |
| Mortality | Patch-created | `Pet Death Reason`, `Pet Death Record`; see mortality API section. |

### ERPNext Custom Fields The Frontend Should Know

`Appointment`:

- `custom_appointment_type`: `visit`, `follow_up`, `showering`, `barbering`
- `custom_pet`, `custom_guardian`, `custom_customer`, `custom_doctor`, `custom_room`
- `custom_follow_up_of_visit_id`, `custom_linked_visit_id`, `custom_linked_service_id`
- `custom_converted_target`, `custom_converted_at`
- `custom_duration_minutes`, `custom_care_plan_item`
- offline/death flags: `custom_client_request_id`, `custom_idempotency_key`, `custom_cancelled_due_to_death`, `custom_death_record`

`Sales Order`:

- `custom_order_status`: `Draft`, `Preparing`, `Out for Delivery`, `Returned`, `Cash Collected`, `Completed`, `Cancelled`
- `custom_payment_method`: `Cash on Delivery`, `Qi Card`, `Zain Cash`, `Master Card`
- `custom_payment_status`: `Pending`, `Paid`, `Failed`, `Refunded`
- `custom_driver`, delivery coordinates/fee, `custom_discount_breakdown`

`File`:

- `custom_sha1_hash`, `custom_is_default`

`POS Profile` / `Payment Entry` / invoices:

- branch/accounting dimensions and cashier cash-account fields.

### Hooks And Scheduled Jobs

Active hooks in `hooks.py`:

- `after_install = "pet_app.install.after_install"` runs schema patches for growth, notifications, and medical core.
- `before_tests = "pet_app.tests.bootstrap.before_tests"`.
- `before_request = ["pet_app.api.auth_api.set_cors_for_oauth_token_endpoint"]`.
- `doc_events`:
  - `User`: role profile cleanup/sync via `pet_app.utils.role_profiles`.
  - `Vet Visit`: deceased-pet guard before insert; medication counters on update/trash.
  - `Appointment`, `Vet Case Sheet`, `Pet Boarding`, `PetCareService`, `Pet Procedure`: block creating clinical/boarding records for deceased pets.
  - `Appointment`: identity linking on insert/validate.
  - `Sales Order`: validates and applies order status transitions.
  - `Sales Invoice`: guard before insert; unmark visit billing on cancel.
  - `Product`, `Item`, `Item Price`, `Item Group`: product/item/category projection sync.
  - `Supplier`, `Brand`, `Item Group`: rename/link sync helpers.
  - `Driver`: provision/update linked user/address/account.
  - `Customer`: validate Guardian-Customer identity projection.
  - `Coupon Code`: validate/sync coupon helpers.
- `scheduler_events`:
  - all: enqueue due reminders, retry failed notifications.
  - hourly: repair active product-item projections, enqueue reminders.
  - daily: repair all product projections, send due reminders, create daily reminders, cleanup old WhatsApp webhook events.
- `fixtures`: custom fields, property setters, server script, workflow, email template, print formats, custom roles, and all custom DocPerms.

No active `override_whitelisted_methods` or `auth_hooks` are configured.

Important implementation note: `hooks.py` contains an earlier `doc_events` assignment that is overwritten by the later one. Only the later block is active.

---

## 3. API Endpoints And Frontend Contract

### How To Call Frappe APIs

Whitelisted methods are exposed as:

```text
/api/method/<dotted.python.path>
```

Examples:

```ts
frappe.call({
  method: "pet_app.api.workspace.get_my_workspace",
  args: { mode: "doctor", limit: 50 }
})

frappe.call({
  method: "pet_app.api.workspace.perform_action",
  args: {
    source_type: "Visit",
    name: "VVT-2026-00001",
    action: "save_clinical_note",
    payload: { examination_notes: "...", treatment_plan: "..." }
  }
})
```

Most legacy whitelisted methods accept both GET and POST unless decorated with `methods=["POST"]`. For new Vue work, prefer POST for writes and for complex payloads.

### Authentication

Supported auth modes:

- Session cookie: `pet_app.api.auth_api.login_and_get_session(usr, pwd)` returns `sid`, user, full name. Same-origin Vue can then use normal `frappe.call`. If using raw `fetch`/Axios with cookies for unsafe methods, include Frappe CSRF handling.
- API key token: `Authorization: token <api_key>:<api_secret>`. This is the main mobile/external SPA-friendly auth. `auth_mobile.verify_otp` and `auth_mobile.login` return this token string.
- OAuth bearer: `pet_app.api.auth_api.login_and_get_oauth_token` supports password grant with `client_id`; username may be a verified guardian phone. The hook adds CORS for this endpoint when Frappe OAuth allowed origins are configured.
- Driver token: `pet_app.api.driver.driver_login(phone, password)` returns a driver token and metadata for delivery flows.

Guardian mobile auth flow:

1. `register_guardian(phone, password, full_name)` creates an inactive Guardian and queues/sends OTP.
2. `verify_otp(phone, otp, password)` creates/resolves Customer, creates User, links Guardian, activates the account, and returns API token.
3. `complete_profile(full_name, city, address_line1, email_id?, country?)` syncs Guardian, Customer, Address, and User display name.
4. `login(phone, password)` returns token for verified guardians.
5. `forgot_password` / `reset_password` use OTP.
6. `request_guardian_phone_change_otp` / `change_guardian_phone` safely change phone and Customer mobile.

Phone validation is Iraq-focused: accepted input is `07XXXXXXXXX` or `9647XXXXXXXXX`, normalized to `07...`.

### Response Format

The backend currently has mixed response styles.

Newer APIs use the normalized envelope via `pet_app.api.response.ok/fail`:

```json
{
  "message": {
    "ok": true,
    "data": {},
    "meta": {},
    "errors": []
  }
}
```

Failure envelope:

```json
{
  "message": {
    "ok": false,
    "data": {},
    "meta": { "code": "VALIDATION_ERROR" },
    "errors": [{ "message": "Request failed.", "details": {} }]
  }
}
```

Some legacy APIs return raw dicts under `message`:

```json
{ "message": { "name": "VVT-2026-00001", "doctype": "Vet Visit" } }
```

Some legacy APIs set `frappe.response["data"]` and return no value:

```json
{ "data": [...] }
```

Recommended Vue normalizer:

```ts
function normalizeFrappeResponse(res: any) {
  const msg = res?.message
  if (msg && typeof msg === "object" && "ok" in msg && "data" in msg)
    return msg
  if ("data" in (res || {}))
    return { ok: true, data: res.data, meta: {}, errors: [] }
  return { ok: true, data: msg ?? {}, meta: {}, errors: [] }
}
```

Common error codes from newer APIs:

- `VALIDATION_ERROR`
- `PERMISSION_ERROR`
- `PERMISSION_DENIED`
- `NOT_FOUND`
- `CONFLICT`
- `INVALID_STATE`
- `INVALID_STATUS`
- `CASE_CHOICE_REQUIRED`
- `NOT_INSTALLED`
- Python exception class names for uncaught errors.

Frappe exceptions may also return standard Frappe error fields such as `exc_type`, `exception`, and `_server_messages`.

### Preferred Versioned APIs

`pet_app.api.v1.*` wraps older/raw APIs through `normalize_response`, returning the normalized `ok/data/meta/errors` shape where possible.

| Area | Methods |
|---|---|
| Clinic | `pet_app.api.v1.clinic.get_my_workspace`, `get_record`, `perform_action`, `get_available_slots`, `book_appointment`, `reschedule_appointment`, `cancel_appointment`, `get_doctor_calendar` |
| Guardian | `pet_app.api.v1.guardian.get_my_pets_dashboard`, `get_pet_medical_timeline`, `get_pet_documents`, `get_upcoming_appointments`, `get_guardian_invoices` |
| Marketplace | `pet_app.api.v1.marketplace.get_products`, `get_stock_info`, `list_product_categories`, `place_order` |
| Billing | `pet_app.api.v1.billing.get_guardian_invoices`, `create_sales_invoice_for_guardian`, `create_visit_invoice`, `create_payment_entry_with_cashier_context` |

### Main Frontend Endpoint Catalogue

Use the URL `/api/method/<method>` for every method below.

#### Auth

| Method | HTTP | Auth | Request | Response |
|---|---:|---|---|---|
| `pet_app.api.auth_api.login_and_get_session` | GET/POST | Guest | `{ usr, pwd }` | Raw `message`: `{ sid, user, full_name }` |
| `pet_app.api.auth_api.login_and_get_api_keys` | GET/POST | Guest | `{ usr, pwd }` | Raw `message`: `{ api_key, api_secret, user, full_name }` |
| `pet_app.api.auth_api.login_and_get_oauth_token` | POST | Guest | `{ username/usr, password/pwd, client_id, scope?, grant_type:"password" }` | OAuth token response plus `user`, `full_name` |
| `pet_app.api.auth_mobile.register_guardian` | GET/POST | Guest | `{ phone, password, full_name }` | `{ status, guardian_id, message, otp? }` in debug mode |
| `pet_app.api.auth_mobile.verify_otp` | GET/POST | Guest | `{ phone, otp, password }` | `{ status:"verified", token, guardian_id, customer_id }` |
| `pet_app.api.auth_mobile.login` | GET/POST | Guest | `{ phone, password }` | `{ status:"success", token }` |
| `pet_app.api.auth_mobile.complete_profile` | GET/POST | Token | `{ full_name, city, address_line1, email_id?, country? }` | `{ status, guardian_id, customer_id, primary_address_id }` |
| `pet_app.api.auth_mobile.get_guardian_profile` | GET/POST | Token | `{ guardian_id }` | `data`: `{ guardian, customer, addresses }` |
| `pet_app.api.auth_mobile.forgot_password` / `reset_password` | GET/POST | Guest | `{ phone }`, `{ phone, otp, new_password }` | status/message |
| `pet_app.api.auth_mobile.logout` | GET/POST | Token | `{}` | status/message |

#### Pets, Images, Medical File

| Method | HTTP | Request | Response |
|---|---:|---|---|
| `pet_app.api.pet.list_pet_breeds` | GET/POST | `{ animal_type?, animal_species?, search?, page_size? }` | `data`: breed rows |
| `pet_app.api.pet.list_pets` | GET/POST | `{ page?, page_size?, search? }` | paginated pet list; guardians see linked pets only |
| `pet_app.api.pet.get_pet` | GET/POST | `{ pet_id }` | `data`: pet detail with images |
| `pet_app.api.pet.upload_multiple_files` | GET/POST multipart | files + `doctype`, `docname` | `{ uploaded, skipped, errors }`; max 500 KB/file; SHA1 duplicate detection |
| `pet_app.api.pet.upload_single_file` | GET/POST multipart | file + target fields | replace/upload image; syncs Guardian/Practitioner image to User where linked |
| `pet_app.api.pet.get_pet_images` | GET/POST | `{ doctype, docname }` | `{ fieldname, total, images }` |
| `pet_app.api.pet.set_default_file` | GET/POST | `{ file_id, doctype, docname }` | marks default image and updates first Attach Image field |
| `pet_app.api.pet.delete_multiple_files` | GET/POST | `{ file_names }` | deletes after write permission check |
| `pet_app.api.medical_file.get_pet_medical_summary` | GET/POST | `{ pet/pet_id }` | envelope `{ profile, latest_visit, latest_vitals }` |
| `pet_app.api.medical_file.update_pet_medical_profile` | POST | `{ pet/pet_id, data }` | envelope `{ profile }` |
| `pet_app.api.medical_file.get_pet_medical_timeline` | GET/POST | `{ pet/pet_id, limit? }` | envelope `{ events }` |
| `pet_app.api.medical_profile.get_pet_medical_profile` | GET/POST | `{ pet/pet_id }` | envelope with profile, active episode, active plan, latest visits/vitals |
| `pet_app.api.medical_profile.close_care_episode` | POST | `{ episode, outcome?, closure_reason? }` | envelope episode payload |

#### Scheduling, Queue, Intake

| Method | HTTP | Request | Response |
|---|---:|---|---|
| `pet_app.api.scheduling.get_available_slots` | GET/POST | `{ date?, doctor?/practitioner?, room?, service_type?, duration_minutes? }` | envelope `{ slots }` |
| `pet_app.api.scheduling.book_appointment` | POST | `{ data: { pet, guardian, scheduled_time, doctor?, appointment_type?, ... } }` | envelope `{ appointment }` |
| `pet_app.api.scheduling.reschedule_appointment` | POST | `{ appointment, scheduled_time? / data }` | envelope `{ appointment }` |
| `pet_app.api.scheduling.cancel_appointment` | POST | `{ appointment, reason? }` | envelope `{ appointment }` |
| `pet_app.api.scheduling.get_doctor_calendar` | GET/POST | `{ doctor?/practitioner?, date_from?, date_to? }` | envelope `{ appointments }` |
| `pet_app.api.appointment.check_in_appointment` | POST | `{ appointment_id/name, payload? }` | envelope with queue ticket / linked record info |
| `pet_app.api.appointment.create_walkin_case_sheet` | POST | `{ payload: { pet, guardian, complaint... } }` | envelope `{ case_sheet, queue_ticket }` |
| `pet_app.api.queue.list_queue` | GET/POST | filters by date/status/doctor/branch | envelope `{ items }` |
| `pet_app.api.queue.call_next` | POST | `{ queue_date?, doctor?, practitioner?, room?, branch? }` | envelope queue ticket |
| `pet_app.api.queue.mark_no_show` / `complete_queue_ticket` | POST | `{ ticket/name }` | envelope queue ticket |

#### Healthcare Workspace

| Method | HTTP | Request | Response |
|---|---:|---|---|
| `pet_app.api.workspace.get_my_workspace` | GET/POST | `{ mode?, search?, priority?, status?, limit?, cursor?, date_from?, date_to?, doctor?, guardian?, pet?, species?, visit_type?, source_type?, billing_status?, branch?, room?, assigned_to? }` | Raw `message`: `{ mode, user, metrics, items, next_cursor, cursor, total }` |
| `pet_app.api.workspace.get_record` | GET/POST | `{ source_type, name }` | Raw aggregate detail; linked records usually resolve to parent visit aggregate |
| `pet_app.api.workspace.perform_action` | POST | `{ source_type, name, action, payload? }` | Updated aggregate/detail |
| `pet_app.api.workspace.add_note` | POST | `{ source_type, name, note/content/payload }` | Updated aggregate |
| `pet_app.api.workspace.attach_file` | POST | `{ source_type, name, file_url? or filedata?, file_name?, is_private? }` | Updated aggregate |
| `pet_app.api.visit_workbench.get_visit_workbench` | GET/POST | `{ visit/visit_id/name }` | envelope/raw hybrid: visit, case sheet, pet, guardian, profile, episode, orders, meds, billables, permissions |

Workspace `source_type` aliases:

| UI alias | Backend DocType |
|---|---|
| `Visit`, `visit`, `vet visit` | `Vet Visit` |
| `Case Sheet`, `case_sheet` | `Vet Case Sheet` |
| `Appointment` | `Appointment` |
| `Service`, `pet care service` | `PetCareService` |
| `Procedure`, `pet procedure` | `Pet Procedure` |
| `Lab` | `Lab` |
| `Radiology`, `Imaging` | `Imaging` |
| `Invoice`, `Sales Invoice` | `Sales Invoice` |
| `Payment`, `Payment Entry` | `Payment Entry` |

Workspace actions:

- Clinical: `set_case_choice`, `start_consultation`, `save_clinical_note`, `save_diagnoses`, `create_orders`, `complete_case`, `request_follow_up`, `request_consult`, `complete_consult`
- Conversion: `convert_to_visit`, `convert_to_service`, `convert_follow_up_to_visit`
- Procedure: `start_procedure`, `save_procedure_note`, `complete_procedure`, `close_procedure`, `cancel_procedure`
- Service: `start_service`, `finish_service`, `close_service`
- Diagnostics: `start_test`, `save_result`, `release`
- Coordination/accounting: `assign`, `reassign`, `submit_invoice`, `mark_follow_up`

#### Clinical Supporting APIs

| Method | HTTP | Request | Response |
|---|---:|---|---|
| `pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet.get_pet_context` | GET/POST | `{ pet_name }` | `{ guardian, customer, phone_number, species, breed, sex, weight, age_text }` |
| `pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet.start_visit` | GET/POST | `{ case_sheet_name, doctor_case_choice?, care_episode?, case_choice_note? }` | `{ name, doctype, status?, idempotent? }` |
| `pet_app.pet_app.doctype.vet_visit.vet_visit.get_item_billing_details` | GET/POST | `{ item_code }` | `{ item_code, item_name, uom, rate }` |
| `pet_app.pet_app.doctype.vet_visit.vet_visit.get_care_service_billing_details` | GET/POST | `{ care_service_name }` | `{ care_service, service_name, item_code, rate, price_list }` |
| `pet_app.pet_app.doctype.vet_visit.vet_visit.create_sales_invoice` | GET/POST | `{ visit_name }` | `{ sales_invoice, customer, total_billable_amount, guardian_reference_field }` |
| `pet_app.api.vitals.add_visit_vital` / `update_visit_vital` / `list_visit_vitals` | POST/GET | visit + vital payload | envelope vital(s) |
| `pet_app.api.diagnostics.collect_sample`, `save_lab_result`, `release_lab_result`, `save_imaging_report`, `release_imaging_report` | POST | lab/imaging payloads | envelope diagnostic payload |
| `pet_app.api.case_sheet_templates.*` | GET/POST | template/case sheet response payloads | envelope templates/responses |
| `pet_app.api.care_plan.*` | POST/GET | plan item / visit / pet payloads | envelope plan items, appointment, visit conversion |
| `pet_app.api.pharmacy.*` | POST/GET | visit medication row, qty, warehouse | envelope pending meds / dispensed row |
| `pet_app.api.visit_addendum.*` | POST/GET | addendum payloads | envelope addendum(s) |

#### Boarding

| Method | HTTP | Request | Response |
|---|---:|---|---|
| `pet_app.api.healthcare.boarding.list_boarding_units` | GET/POST | `{ search?, occupancy?, date? }` | `{ data: rooms, total }` |
| `pet_app.api.healthcare.boarding.get_boarding_detail` | GET/POST | `{ boarding_id? / room_id? / name? }` | detail including room and active boarding |
| `pet_app.api.healthcare.boarding.list_boarding_records` | GET/POST | search/status/pet/guardian/date/pagination | `{ data: records, total }` |
| `pet_app.api.healthcare.boarding.reserve_room` | GET/POST | `{ roomId, petId, guardianId, checkIn?, checkOut?, note?, boardingType? }` | boarding detail |
| `pet_app.api.healthcare.boarding.check_in_boarding` | GET/POST | `{ boarding_id }` | boarding detail |
| `pet_app.api.healthcare.boarding.sync_billable_items` | GET/POST | `{ boarding_id/name, billable_items }` | boarding detail with totals |
| `pet_app.api.healthcare.boarding.check_out_boarding` | GET/POST | `{ boarding_id }` | closes/submits boarding and creates Sales Invoice |
| `pet_app.api.boarding_updates.get_owner_boarding_updates` | GET/POST | `{ boarding?, pet? }` | owner-visible boarding updates |
| `pet_app.api.boarding_updates.add_boarding_update` | POST | `{ boarding, update_type?, summary?, file?, data? }` | envelope update |

#### Product, Marketplace, Sales, Delivery

| Method | HTTP | Request | Response |
|---|---:|---|---|
| `pet_app.api.product.publish_product` | GET/POST | Product fields plus optional `product_id`, `qty`; creates/updates Product/Item/Item Price/stock | `data`: product/item/qty/variants/brand |
| `pet_app.api.product.restock_product` | GET/POST | `{ product_id, qty, warehouse?, item_variant? }` | `data`: stock entry, item, warehouse, qty/current qty |
| `pet_app.api.product.get_stock_info` | GET/POST | `{ product_id, warehouse? }` | `data`: stock qty, lifetime restock, variants |
| `pet_app.api.product.get_products` | GET/POST | `{ filters?, fields?, order_by?, limit_start?, limit_page_length?, search_term? }` | `data`: product rows enriched with qty/images/category/brand |
| `pet_app.api.product_category.list_product_categories` | GET/POST | `{ parent?, search?, enabled_only? }` | category tree rows |
| `pet_app.api.product_category.get_product_category` | GET/POST | `{ name }` | category with child/product counts |
| `pet_app.api.product_category.save_product_category` | GET/POST | `{ data or fields }` | saved category and synced Item Group |
| `pet_app.api.product_category.delete_product_category` | GET/POST | `{ name }` | delete result |
| `pet_app.api.order.place_order` | GET/POST | `{ customer?, guardian?, items:[{item_code, qty}], payment_method?, delivery_lat?, delivery_lng?, shipping_address_name?, coupon_code?, shipping_rule? }` | `data`: Sales Order, totals, coupon, items |
| `pet_app.api.sales.create_sales_invoice_for_guardian` | GET/POST | `{ guardian, items, posting_date?, due_date?, is_pos?, pos_profile?, customer? }` | invoice response |
| `pet_app.api.delivery.assign_driver` / `get_driver_tasks` / `mark_delivered` / `mark_failed_delivery` / `upload_delivery_proof` | POST/GET | delivery assignment payloads | envelope assignment/proof/tasks |
| `pet_app.api.driver.create_driver` / `delete_driver` / `driver_login` / `get_driver_balance` | GET/POST | driver payloads | driver metadata, temporary password on create, token on login, balance |

#### Billing, Cashier, Accounting

| Method | HTTP | Request | Response |
|---|---:|---|---|
| `pet_app.api.accounting.settings.get_accounting_settings` | GET/POST | `{}` | settings |
| `pet_app.api.accounting.settings.update_accounting_settings` | GET/POST | `{ data }` | updated settings |
| `pet_app.api.accounting.cashier.list_cashier_profiles_for_user` | GET/POST | `{ user?, company?, search?, include_disabled? }` | profile list |
| `pet_app.api.accounting.cashier.get_cashier_runtime_defaults` | GET/POST | `{ pos_profile?, company? }` | settings + visible profiles + active profile |
| `pet_app.api.accounting.cashier.create_payment_entry_with_cashier_context` | GET/POST | `{ pos_profile?, data, submit? }` | Payment Entry payload |
| `pet_app.api.accounting.cashier.get_cashier_settlement_snapshot` | GET/POST | profile/date filters | cash activity and suggested settlement |
| `pet_app.api.accounting.cashier.settle_cashier_to_treasury` | GET/POST | profile, amount, date, submit | Payment Entry + settlement |
| `pet_app.api.accounting.cashier.list_cashier_settlements` / `get_cashier_settlement_detail` | GET/POST | filters/name | settlement data |

#### Permissions And Users

| Method | HTTP | Request | Response |
|---|---:|---|---|
| `pet_app.api.permissions.get_current_access` | GET/POST | `{}` | `{ roles, roleProfile, roleProfiles, fullAccess, fullAccessRoles, modules, pages, actions, doctypes, restrictions, loadedAt }` |
| `pet_app.api.permissions.get_user_restrictions` | GET/POST | `{ user? }` | restrictions grouped by type |
| `pet_app.api.permissions.update_user_restrictions` | POST | `{ user, restrictions }` | updated restrictions |
| `pet_app.api.permissions.get_access_matrix` | GET/POST | `{}` | deprecated/read-only matrix |
| `pet_app.api.permissions.update_access_matrix`, `register_frontend_resources` | POST | ignored compatibility payloads | deprecated/read-only matrix |
| `pet_app.api.permissions.get_permission_integrity_report` | GET/POST | `{}` | integrity report |
| `pet_app.api.users.get_users_with_role_profile` | GET/POST | pagination/filter args | `data`: users |
| `pet_app.api.users.get_all_role_profiles_with_roles` | GET/POST | `{ limit_page_length? }` | `data`: role profiles and roles |
| `pet_app.api.users.change_user_password` | POST | `{ user, new_password }` | message |

#### Notifications, Mortality, Reports, Utilities

| Area | Main methods |
|---|---|
| Notifications | `pet_app.api.notifications.get_notification_settings`, `update_notification_settings`, `test_whatsapp_account`, `list_templates`, `get_template`, `save_template`, `preview_template`, `queue_notification`, `send_manual_notification`, `retry_notification`, `cancel_notification`, `list_notification_queue`, `get_notification_queue`, `list_notification_logs`, `get_notification_stats`, `create_reminder`, `cancel_reminder`, `list_reminders`, `enqueue_due_reminders`, `send_manual_reminder` |
| WhatsApp webhook | `pet_app.api.whatsapp.webhook` is guest-accessible for provider callbacks. |
| Mortality | `pet_app.api.mortality.list_death_reasons`, `report_pet_death`, `confirm_pet_death`, `finalize_pet_death`, `cancel_pet_death_record`, `get_pet_death_record`, `issue_death_certificate`, `manager_review_pet_death` |
| Guardian portal | `pet_app.api.guardian_portal.get_my_pets_dashboard`, `get_pet_medical_timeline`, `get_pet_documents`, `get_upcoming_appointments`, `get_guardian_invoices` |
| Printing | `pet_app.api.printing.get_visit_summary`, `get_prescription`, `get_lab_result`, `get_imaging_report`, `get_procedure_report` |
| Reports/analytics | `pet_app.api.clinical_reports.*`, `pet_app.api.analytics.*`, `pet_app.api.dashboard.*` |
| Inventory alerts | `pet_app.api.inventory.get_low_stock_alerts`, `get_expiry_alerts`, `get_reorder_suggestions`, `get_medication_usage_forecast` |
| Import/offline/audit | `pet_app.api.import_tools.*`, `pet_app.api.offline.*`, `pet_app.api.audit.list_audit_logs` |
| AI assist/CDS | `pet_app.api.ai_assist.*`, `pet_app.api.clinical_decision_support.evaluate_visit`. AI translation is currently a placeholder that echoes text. |

---

## 4. Business Logic

### Main Clinic Workflow

Standard flow:

```text
Guardian registration -> pet registration/approval -> appointment or walk-in
-> case sheet/intake -> queue/check-in -> visit/consultation
-> diagnoses + vitals + prescriptions + orders
-> lab/imaging/procedure/service execution
-> follow-up/care plan as needed
-> billable rows -> Sales Invoice -> Payment Entry
```

Detailed flow:

1. Guardian registers by phone and OTP.
2. Guardian creates a Pet; backend creates `PetAddRequest`.
3. Staff approves the request; backend creates `PetGuardian` and `Pet Medical Profile`.
4. Reception books an `Appointment` or creates a walk-in case sheet.
5. Appointment check-in can create `Pet Queue Ticket`.
6. Intake form creates `Vet Case Sheet`.
7. Doctor starts visit through `start_visit` or workspace `convert_to_visit`.
8. Doctor records vitals, notes, diagnoses, treatment plan, medications, orders, consults, follow-up.
9. `create_orders` creates linked `Lab`, `Imaging`, `PetCareService`, or `Pet Procedure` records.
10. Linked records sync billable rows to the visit.
11. `create_sales_invoice` creates/submits ERPNext Sales Invoice and locks the visit.
12. Cashier collects payment through Payment Entry/POS/cashier APIs.

### Key Business Rules And Validations

Identity:

- A pet must be linked to a guardian through `PetGuardian` for clinical/boarding flows.
- Guardian owns the billing `Customer`; Customer must match `Guardian.customer_id`.
- Case Sheet pulls Guardian/Customer/Pet data from Appointment and Pet.
- Visit pulls Guardian/Customer/Pet from Case Sheet.
- Visit identity fields (`case_sheet`, `guardian`, `customer`, `animal_patient`) cannot change after creation.
- One Case Sheet can map to only one Vet Visit.
- Deceased pets are blocked from new appointments, case sheets, visits, boarding, care services, and procedures through hooks.

Clinical status:

- `Vet Case Sheet`: `Draft -> Waiting Practitioner -> In Consultation -> Converted to Visit -> Closed`.
- `Vet Visit`: `Draft -> In Progress -> Follow-up Needed -> Completed`; `Cancelled` is terminal.
- `Visit Order`: `Draft -> Ordered -> In Progress -> Completed`; `Cancelled` is terminal.
- `Lab`: `Pending/Ordered/Sample Collected/In Progress/Result Entered -> Released`; `Completed`/`Cancelled` terminal.
- `Imaging`: `Pending/Ordered/Scheduled/In Progress/Reported -> Released`; `Completed`/`Cancelled` terminal.
- `Pet Procedure`: `Pending -> In Progress -> Completed -> Closed`; `Cancelled` terminal.
- `PetCareService`: lower-case `pending`, `overdue`, `completed`.

Visit completion and billing:

- Completing a Visit requires `illness`, `diagnosis`, `treatment_plan`, and `doctor_notes`.
- In strict mode, pending Lab/Imaging/Procedure records must be completed/cancelled before visit completion or invoice creation.
- Billed visits are locked. Do not edit visit clinical/billable data after `sales_invoice`/`billed` is set.
- Billable rows require `item_code`, `qty > 0`, and non-negative `rate`.
- Prescribed medications and selected care services create/update `Pet Billable Item` rows.
- Lab/imaging category care services must be added through Lab/Imaging order flows, not generic care service rows.
- Billed billable rows cannot be changed.
- Cancelling a Sales Invoice triggers visit billing rollback logic.

Boarding:

- Room must be active.
- Pet and Guardian must be linked.
- Only one active (`Reserved` or `Checked In`) boarding record per room.
- `Checked Out` sets boarding status `Closed`.
- Boarding can only be submitted after checkout and invoice creation.
- Totals are computed from non-cancelled billable rows; stay days are at least 1 after check-in.

Marketplace/order:

- Product Category syncs to ERPNext Item Group.
- Product publish creates/updates ERPNext Item and Item Price and can create initial stock.
- Order placement validates Guardian/Customer identity, item existence, stock in configured store warehouse, and item price.
- Only one coupon per order.
- Sales Order transitions:
  - `Draft -> Preparing -> Out for Delivery -> Cash Collected -> Completed`
  - `Draft/Preparing -> Cancelled`
  - `Out for Delivery -> Returned`
  - `Returned -> Out for Delivery` or `Cancelled`
- Side effects:
  - `Preparing`: Material Issue Stock Entry.
  - `Out for Delivery` with COD: driver debit Journal Entry.
  - `Returned`: reverse driver entry.
  - `Cash Collected`: collect driver cash Journal Entry.
  - `Cancelled`: reverse stock issue.

Permissions/restrictions:

- Backend security is Frappe-native: `Role`, `Role Profile`, `DocPerm`/`Custom DocPerm`, `User Permission`, and explicit permission checks.
- `Pet App Permission Rule` and `Pet App User Restriction` are deprecated and ignored for security.
- `get_current_access.fullAccess` is true only for `Administrator` in current code.
- Admin APIs generally allow `Administrator`, `System Manager`, or `Pet App Admin`.
- User restrictions map:
  - `warehouse` -> `Warehouse`
  - `cashier_profile` -> `POS Profile`
  - `practitioner` / `doctor` -> `Healthcare Practitioner`
  - `branch` -> `Branch`

### Roles And Role Profiles

Seeded operational roles include:

- `Pet App Admin`
- `Users`, `Setting`, `Guardians`, `Healthcare`, `Visit`, `Pet`, `E-commerce`, `Order`
- `Visit Read`, `Visit Admin`
- `Lab Read`, `Lab Admin`
- `Radiology Read`, `Radiology Admin`
- `Reception`, `Coordinator`
- `POS Cashier`, `POS Admin`
- `Warehouse Read`, `Warehouse Admin`
- `Accounting Read`, `Accounting Admin`
- `Ecommerce Admin`, `Audit Read`

Legacy/custom fixture roles also include broad names such as `Accounting`, `Doctor`, `E-commerce`, `Healthcare Practitioner`, `POS`, `Warehouse`, and corresponding `* User` roles. Keep them because DocPerm fixtures reference them.

Seeded role profiles include:

- `Alkokh App Owner`
- `Alkokh Admin`
- `Clinic Manager`
- `Veterinarian`
- `Clinic Reception`
- `Lab And Radiology Operator`
- `Visit Read Profile`, `Visit Admin Profile`
- `Lab Read Profile`, `Lab Admin Profile`
- `Radiology Read Profile`, `Radiology Admin Profile`
- `Reception Profile`, `Coordinator Profile`
- `POS Cashier`, `POS Cashier Profile`, `POS Supervisor`, `POS Admin Profile`
- `Accountant`, `Accounting Read Profile`, `Accounting Manager`, `Accounting Admin Profile`
- `Warehouse User`, `Warehouse Read Profile`, `Warehouse Manager`, `Warehouse Admin Profile`
- `Purchase Officer`
- `Ecommerce Admin Profile`
- `Auditor`, `Audit Read Profile`
- `HR Officer`, `HR Manager`
- `Module Viewer`

Frontend permission UI should manage Role Profiles, Role rows, User Role Profile assignments, and User Permission restrictions. Do not build new UI around deprecated app-resource permission matrices.

---

## 5. Frontend <-> Backend Contract

### Communication Pattern

The Vue app should call Frappe whitelisted methods over HTTP:

- Same-origin Frappe Desk/Site: `frappe.call({ method, args })` with session cookies.
- External/mobile/PWA: `fetch`/Axios to `/api/method/...` with `Authorization: token <api_key>:<api_secret>`.
- Use `multipart/form-data` for file upload methods.
- Use the normalized `v1` APIs where available, especially for new frontend code.

### Shared Constants

Clinical:

```ts
const VISIT_STATUSES = ["Draft", "In Progress", "Completed", "Cancelled", "Follow-up Needed"]
const CASE_SHEET_STATUSES = ["Draft", "Waiting Practitioner", "In Consultation", "Converted to Visit", "Closed"]
const VISIT_ORDER_STATUSES = ["Draft", "Ordered", "In Progress", "Completed", "Cancelled"]
const LAB_STATUSES = ["Pending", "Ordered", "Sample Collected", "In Progress", "Result Entered", "Released", "Completed", "Cancelled"]
const IMAGING_STATUSES = ["Pending", "Ordered", "Scheduled", "In Progress", "Reported", "Released", "Completed", "Cancelled"]
const PROCEDURE_STATUSES = ["Pending", "In Progress", "Completed", "Closed", "Cancelled"]
const SERVICE_STATUSES = ["pending", "overdue", "completed"]
```

Care episodes:

```ts
const ACTIVE_EPISODE_STATUSES = [
  "Open", "Under Diagnosis", "Pending Diagnostics", "Under Treatment",
  "Monitoring", "Follow-up Scheduled", "Follow-up Due", "Referred"
]
const CLOSED_EPISODE_STATUSES = ["Resolved", "Closed", "Deceased", "Cancelled"]
```

Boarding:

```ts
const BOARDING_RECORD_STATUSES = ["Reserved", "Checked In", "Checked Out", "Cancelled"]
const BOARDING_STATUSES = ["Open", "Closed", "Cancelled"]
const BOARDING_BILLING_STATUSES = ["Unbilled", "Invoiced"]
```

Billing:

```ts
const BILLABLE_ITEM_STATUSES = ["Draft", "Billable", "Billed", "Cancelled"]
const BILLABLE_ITEM_TYPES = ["Room Stay", "Service", "Medication", "Lab", "Imaging", "Procedure", "Product", "Other"]
```

Orders:

```ts
const ORDER_STATUSES = ["Draft", "Preparing", "Out for Delivery", "Returned", "Cash Collected", "Completed", "Cancelled"]
const PAYMENT_METHODS = ["Cash on Delivery", "Qi Card", "Zain Cash", "Master Card"]
const PAYMENT_STATUSES = ["Pending", "Paid", "Failed", "Refunded"]
```

Mortality:

```ts
const DEATH_RECORD_STATUSES = ["Draft", "Reported", "Pending Confirmation", "Confirmed", "Finalized", "Cancelled"]
const MANAGER_REVIEW_STATUSES = ["Not Required", "Pending", "Approved", "Rejected"]
```

### Frontend Rules Of Thumb

- Use Guardian as the owner identity. Use Customer only for billing.
- Use Pet as the animal patient. Do not use Healthcare Patient.
- Use Healthcare Practitioner for doctors/staff clinical assignments. Do not recreate the removed custom `Doctor` DocType.
- Use workspace APIs for clinical work queues and aggregates; do not infer cross-record relationships in Vue.
- Use `Pet Medical Profile` for permanent pet medical dashboard and `Pet Care Episode` for active/closed cases.
- Use `Vet Visit` for the doctor's working record.
- Use Lab/Imaging/Procedure/Service records for ordered work, not free-form rows in the visit.
- Never edit a billed visit; show invoice/payment actions instead.
- Normalize API responses because legacy endpoints mix `message`, `data`, and `ok/data/meta/errors`.
- Derive route/sidebar visibility from local frontend route config plus `get_current_access.doctypes`, `roles`, `roleProfiles`, and `restrictions`. In current code, `modules`, `pages`, and `actions` are compatibility arrays and are empty.

---

## 6. Current State

### Built And Working In Backend Code

- Guardian phone/OTP registration, login, password reset, profile completion, phone change, API token issuance.
- Guardian-Customer projection and Customer identity validation.
- Pet creation, approval request, Guardian-Pet link, and medical profile creation.
- Appointment custom fields, check-in/walk-in helpers, queue ticket APIs.
- Case Sheet intake, pet context autofill, status validation, conversion to Visit.
- Visit workspace, clinical notes, diagnoses, vitals, consults, orders, follow-ups, care episodes, care plans.
- Lab, Imaging, PetCareService, Procedure records with workspace actions and billable sync.
- Visit medication billables, service billables, Sales Invoice creation, billed-visit locking.
- Boarding rooms, reservation, check-in, billables, checkout invoice, owner updates.
- Product Category <-> Item Group sync; Product <-> Item/Item Price/stock projection; product listing and stock info.
- Marketplace order placement with coupon validation, guarded Guardian/Customer identity, stock checks, and order status hooks.
- Driver provisioning/login/balance and delivery assignment/proof APIs.
- Cashier profile/runtime defaults, payment entry creation, cash settlement.
- Frappe-native role/permission refactor with Role Profiles, DocPerms, User Permission restrictions, and access snapshots.
- Notification engine schema, template/queue/log APIs, reminders, scheduler, dummy/dev WhatsApp channel, webhook storage.
- Mortality/death reason/death record APIs, death certificate flow, manager review, deceased-pet guards.
- Clinical reports, analytics, inventory alerts, audit logs, import jobs, offline request lookup.
- Tests exist for appointment identity, clinical hardening/P1 flows, hybrid permissions, notification engine, product category, growth/mortality.

### In Progress Or Needs Frontend Caution

- Many newer modules are untracked/uncommitted in the current worktree. Treat this brief as a snapshot of local state, not necessarily committed Git history.
- Several important DocTypes are created by patches rather than checked-in DocType JSON. A fresh site must run `bench migrate` / app install patches before those APIs work.
- `get_current_access()` currently returns empty `modules`, `pages`, and `actions`; it still returns roles, role profiles, DocType permissions, and restrictions. Frontend should not assume backend-provided page keys are populated.
- Notification delivery defaults to dry-run/dummy-style behavior until real provider credentials/settings are configured.
- Mortality schema is in the active patch list, but `seed_default_death_reasons.py` exists outside `patches.txt`; a fresh site may have an empty death-reason dropdown until that seed is run or added to migrations.
- AI assist endpoints are deterministic helper/placeholder logic; translation currently echoes text with a placeholder note.
- Some endpoints are legacy/raw and not normalized. Prefer `api/v1` wrappers where they cover the needed flow.
- `Pet Medical Profile.healthcare_patient` exists in schema but the current direction is Pet-native; do not expose or depend on Healthcare Patient.

### Known Bugs / Limitations

- `Pet.after_insert` sends `requested_at` when creating `PetAddRequest`, but the checked-in `PetAddRequest` DocType JSON does not define a `requested_at` field. If the UI needs request timestamps, add a real field or use system `creation`.
- `pet_app.api.order.place_order` writes `custom_delivery_lat` and `custom_delivery_lng`, while the custom field fixture defines `custom_delivery_latitude` and `custom_delivery_longitude`. Delivery coordinates may not persist unless the target site also has the shorter legacy fields.
- Product variant generation is not fully wired: `publish_product` returns an empty `variants` list in the current variant branch, and helper code references `Product Variant.item_variant`, which is not in the checked-in child table schema.
- The custom `Doctor` DocType is deleted in the working tree; use `Healthcare Practitioner`. Some roles and text still say Doctor for compatibility.
- `Pet.food_brand` links to ERPNext `Brand` while `FoodBrand` also exists. Use `Brand` for current Pet form compatibility.
- Response shapes are inconsistent across old/new APIs. Frontend must normalize.
- Some source comments/messages are Arabic. Do not treat comments as final user-facing copy without product localization review.

### Useful Existing Docs

- `docs/frontend-agent-pet-app-overview.md`
- `docs/frontend-healthcare-workspace-backend-contract.md`
- `docs/frontend-medical-profile-episode-visit-flow.md`
- `docs/boarding-frontend.md`
- `docs/death-record-frontend.md`
- `docs/product-api-docs.md`
- `docs/workspace-api.md`
- `docs/agent-handoff-frappe-native-permissions.md`
- `README_FRONTEND_ROLE_PROFILE_API.md`
