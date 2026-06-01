# Response Shape Changes

## pet_app.api.workspace.get_my_workspace

Endpoint: `pet_app.api.workspace.get_my_workspace`

Old shape:
- `items[]` used nested link summaries such as `pet: { id, name_label, pet_name }`, `guardian: { id, name_label, full_name }`, and `assignee: { id, name_label }`.
- Flat display keys were absent or inconsistent across item types.
- `PetCareService` rows exposed some ad hoc fields such as `doctor_name`, `provider_name`, `practitioner_name`, and `guardian_id.full_name`, but not the same flat keys as visits/appointments/procedures.

New shape:
- Existing nested `pet`, `guardian`, and `assignee` objects are retained for backward compatibility.
- Every workspace list item now also includes flat aliases where data is available:
  - `pet_id`: linked Pet ID
  - `pet_name`: Pet display name
  - `guardian_id`: linked Guardian ID
  - `guardian_name`: Guardian display name
  - `doctor`: linked Healthcare Practitioner ID when the item has a doctor/assignee
  - `doctor_name`: Healthcare Practitioner display name
- `PetCareService` rows keep `provider_name`, `practitioner_name`, and `guardian_id.full_name`; their `doctor` and `doctor_name` fields now refer to the actual service doctor, while provider assignment remains represented by `assignee` and `provider_name`.

## pet_app.api.healthcare.boarding.list_boarding_units

Endpoint: `pet_app.api.healthcare.boarding.list_boarding_units`

Old shape:
- Occupied/reserved room rows exposed top-level `pet` and `guardian` IDs.
- Display names were available only through nested `active_boarding.pet_name` and `active_boarding.guardian_name`, so callers had to fall back into the nested active-boarding object.

New shape:
- Existing top-level IDs and nested `active_boarding` object are retained for backward compatibility.
- Occupied/reserved room rows now also include top-level aliases:
  - `pet_name`: Pet display name
  - `guardian_name`: Guardian display name

## pet_app.api.follow_up.list_due_follow_ups

Endpoint: `pet_app.api.follow_up.list_due_follow_ups`

Old shape:
- `follow_ups[]` exposed the Vet Visit link fields as raw IDs: `animal_patient`, `guardian`, and `doctor`.
- There was no flat `pet` alias and no display-name fields, so callers had to enrich Pet, Guardian, and Healthcare Practitioner labels separately.

New shape:
- Existing raw ID fields are retained for backward compatibility: `animal_patient`, `guardian`, and `doctor`.
- Each follow-up row now also includes flat list aliases:
  - `pet`: linked Pet ID, copied from `animal_patient`
  - `pet_name`: Pet display name
  - `guardian_name`: Guardian display name
  - `doctor_name`: Healthcare Practitioner display name

## pet_app.api.visit_workbench.get_visit_workbench

Endpoint: `pet_app.api.visit_workbench.get_visit_workbench`

Old shape:
- `data.visit` was the raw `Vet Visit` document and exposed `animal_patient`, `guardian`, and `doctor` IDs.
- Pet display name was available only through the nested root `data.pet.pet_name`; guardian and doctor display names depended on fetched fields in the raw visit document.

New shape:
- Existing `data.visit` fields and root nested `pet` and `guardian` documents are retained for backward compatibility.
- `data.visit` now also includes flat aliases:
  - `pet`: linked Pet ID, copied from `animal_patient`
  - `pet_id`: linked Pet ID
  - `pet_name`: Pet display name
  - `guardian_id`: linked Guardian ID
  - `guardian_name`: Guardian display name
  - `doctor_name`: Healthcare Practitioner display name

## Full remaining API response-shape sweep

Endpoints/files covered:
- `pet_app.api.scheduling.get_available_slots`
- `pet_app.api.scheduling.get_doctor_calendar`
- `pet_app.api.scheduling.book_appointment`
- `pet_app.api.scheduling.reschedule_appointment`
- `pet_app.api.care_plan.get_pet_active_plan`
- `pet_app.api.care_plan` plan item mutation responses
- `pet_app.api.guardian_portal.get_my_pets_dashboard`
- `pet_app.api.guardian_portal.get_upcoming_appointments`
- `pet_app.api.guardian_portal.get_pet_medical_timeline`
- `pet_app.api.guardian_portal` active boarding and owner-safe death records
- `pet_app.api.medical_profile.get_pet_medical_profile`
- `pet_app.api.medical_profile.get_active_case_snapshot`
- `pet_app.api.medical_file.get_pet_medical_summary`
- `pet_app.api.medical_file.get_pet_medical_timeline`
- `pet_app.api.mortality` death record responses
- `pet_app.api.appointment` queue ticket responses
- `pet_app.api.queue` queue ticket responses, through the shared appointment payload
- `pet_app.api.diagnostics` Lab and Imaging payloads
- `pet_app.api.membership.get_subscription_status`
- `pet_app.api.loyalty.get_guardian_points`
- `pet_app.api.loyalty.apply_points_to_invoice`
- `pet_app.api.delivery` assignment and task responses
- `pet_app.api.boarding_updates` owner update responses
- `pet_app.api.visit_addendum` addendum responses
- `pet_app.api.notifications.list_reminders`
- `pet_app.api.notifications.send_manual_reminder` legacy notification response
- `pet_app.api.healthcare.boarding.list_boarding_records`
- `pet_app.api.healthcare.boarding.get_boarding_detail`
- `pet_app.api.workspace.get_record`
- `pet_app.api.workspace.perform_action`
- `pet_app.api.visit_workbench.get_visit_workbench`
- `pet_app.api.printing.get_prescription`
- `pet_app.api.printing.get_lab_result`
- `pet_app.api.printing.get_imaging_report`
- `pet_app.api.printing.get_procedure_report`
- `pet_app.api.clinical_reports.clinical_daily_summary`
- `pet_app.api.clinical_reports.doctor_performance`
- `pet_app.api.clinical_reports.unbilled_visits`
- `pet_app.api.clinical_reports.follow_up_due`
- `pet_app.api.analytics.clinic_daily_report`
- `pet_app.api.analytics.doctor_performance`
- `pet_app.api.analytics.guardian_retention`
- `pet_app.api.analytics.unexpected_death_review`
- `pet_app.api.ai_assist.summarize_visit`
- `pet_app.api.pet.list_pets`
- `pet_app.api.pet.get_pet`
- `pet_app.api.follow_up` follow-up mutation responses
- `pet_app.api.pharmacy.list_pending_dispense`
- `pet_app.api.sales.create_sales_invoice_for_guardian`
- `pet_app.api.order.create_order`
- `pet_app.api.auth_mobile` guardian registration, OTP, verification, profile, and phone-change responses

Old shape:
- Many responses exposed raw link IDs only, using different field names depending on source DocType:
  - `pet`, `pet_id`, `animal_patient`, or `custom_pet`
  - `guardian`, `guardian_id`, `primary_guardian`, or `custom_guardian`
  - `doctor`, `custom_doctor`, or `practitioner`
  - `provider`
- Callers had to perform separate lookups for Pet, Guardian, and Healthcare Practitioner display names.
- Some list endpoints had names for one link type but not the others.

New shape:
- Existing fields are retained for backward compatibility.
- Rows and payloads that expose these clinical link IDs now add flat aliases where the linked ID is present:
  - `pet_id`: linked Pet ID
  - `pet_name`: Pet display name
  - `guardian_id`: linked Guardian ID
  - `guardian_name`: Guardian display name
  - `doctor`: linked Healthcare Practitioner ID
  - `doctor_name`: Healthcare Practitioner display name
  - `provider`: linked provider ID, retained from the old response
  - `provider_name`: provider display name when the provider is a Healthcare Practitioner, otherwise the provider ID fallback
- List endpoints use batched display-name lookups through `pet_app.api.link_aliases.enrich_link_aliases`.
- Single-payload endpoints use the same aliasing rules through `pet_app.api.link_aliases.with_link_aliases`.

## #16 Response Envelope Standardization

Target envelope:
- Success: `{ "ok": true, "data": { ... }, "meta": { ... }, "errors": [] }`
- Error: `{ "ok": false, "data": {}, "meta": { "code": "..." }, "errors": [ ... ] }`

Old shape:
- Several whitelisted endpoints returned raw dicts or lists directly.
- Several endpoints populated `frappe.response["data"]` and returned `None`.
- Some mutation endpoints used ad hoc shapes such as `{ "success": true, ... }`, `{ "status": "success", ... }`, `{ "created": [], "failed": [] }`, or direct list responses.

New shape:
- Covered endpoints now pass through the shared `standardize_response` wrapper from `pet_app.api.response`, which uses the existing response helpers.
- Existing standard envelopes from `ok`, `fail`, `api_success`, `api_error`, or `call_api` are not double-wrapped.
- Raw dict responses are now returned under `data`, and non-reserved legacy top-level keys are mirrored where possible. For example, callers can still read legacy keys such as `created`, `failed`, `total`, `status`, `message`, `items`, `profiles`, or `settings` when those keys do not conflict with envelope keys.
- Raw list responses are wrapped as `data.items` and `data.result`; a raw top-level list cannot be preserved together with the object envelope.
- Endpoints that previously used a top-level `data` key now use the envelope `data` key, so the previous payload is nested inside the envelope. The old top-level `data` field cannot be mirrored without replacing the envelope payload.
- Exceptions raised by wrapped endpoints are converted to the standard error envelope with `meta.code` and `errors[]`.

Endpoints wrapped in this pass:
- `pet_app.api.accounting.cashier`: `list_cashier_profiles_for_user`, `get_cashier_profile_detail`, `get_cashier_runtime_defaults`, `save_cashier_profile`, `create_payment_entry_with_cashier_context`, `get_cashier_settlement_snapshot`, `settle_cashier_to_treasury`, `list_cashier_settlements`, `get_cashier_settlement_detail`
- `pet_app.api.accounting.settings`: `get_accounting_settings`, `update_accounting_settings`
- `pet_app.api.auth_api`: `login_and_get_session`, `login_and_get_api_keys`
- `pet_app.api.auth_mobile`: `register_guardian`, `send_otp`, `resend_otp`, `verify_otp`, `login`, `complete_profile`, `request_guardian_phone_change_otp`, `change_guardian_phone`, `get_guardian_profile`, `forgot_password`, `reset_password`, `logout`
- `pet_app.api.care_service`: `get_providers_for_category`, `bulk_create_pet_care_services`
- `pet_app.api.dashboard`: `get_order_status_counts`, `get_statistics`, `get_revenue_report`, `get_best_seller`, `get_profit_and_expenses`, `get_orders_by_item_group`
- `pet_app.api.driver`: `create_driver`, `delete_driver`, `driver_login`, `get_driver_balance`
- `pet_app.api.healthcare.boarding`: `list_boarding_units`, `get_boarding_detail`, `list_boarding_records`, `reserve_room`, `check_in_boarding`, `check_out_boarding`, `sync_billable_items`
- `pet_app.api.order`: `place_order`
- `pet_app.api.permissions`: `get_current_access`, `get_user_restrictions`, `update_user_restrictions`, `get_page_access_settings`, `sync_frontend_pages`, `update_page_access_settings`, `get_access_matrix`, `update_access_matrix`, `register_frontend_resources`, `get_permission_integrity_report`
- `pet_app.api.pet`: `list_pet_breeds`, `upload_multiple_files`, `delete_multiple_files`, `set_default_file`, `get_pet_images`, `list_pets`, `get_pet`, `upload_single_file`
- `pet_app.api.product`: `publish_product`, `restock_product`, `get_stock_info`, `get_products`
- `pet_app.api.product_category`: `list_product_categories`, `get_product_category`, `save_product_category`, `delete_product_category`
- `pet_app.api.ratings`: `get_ratings`
- `pet_app.api.sales`: `create_sales_invoice_for_guardian`
- `pet_app.api.sidebar_config`: `get_sidebar_config`, `save_sidebar_config`
- `pet_app.api.users`: `get_users_with_role_profile`, `get_all_role_profiles_with_roles`, `change_user_password`
- `pet_app.api.workspace`: `get_my_workspace`, `get_record`, `perform_action`, `add_note`, `attach_file`
- Helper-return endpoints explicitly wrapped for consistency: `pet_app.api.analytics.pet_mortality_summary`, `pet_app.api.analytics.mortality_by_reason`, `pet_app.api.analytics.mortality_by_source`, `pet_app.api.delivery.mark_delivered`, `pet_app.api.delivery.mark_failed_delivery`, `pet_app.api.follow_up.mark_follow_up_contacted`, `pet_app.api.follow_up.mark_follow_up_missed`, `pet_app.api.medical_profile.get_pet_medical_timeline`, `pet_app.api.notifications.queue_notification`, `pet_app.api.notifications.send_manual_notification`, `pet_app.api.notifications.retry_notification`, `pet_app.api.notifications.enqueue_due_reminders`, `pet_app.api.notifications.send_manual_reminder`, `pet_app.api.scheduling.book_appointment`
- Non-API whitelisted methods covered too: `Vet Case Sheet.get_pet_context`, `Vet Case Sheet.start_visit`, `Vet Visit.get_item_billing_details`, `Vet Visit.get_care_service_billing_details`, `Vet Visit.create_sales_invoice`, `alkohk_dashboard.get_dashboard_payload`

Compatibility exceptions:
- `pet_app.api.auth_api.login_and_get_oauth_token` remains a raw OAuth token response because OAuth clients expect token fields such as `access_token` at the response root.
- `pet_app.api.whatsapp.webhook` keeps the GET verification challenge as a raw string because Meta webhook verification requires the exact challenge response. The POST webhook path already returns the existing standard API response from the notification webhook handler or `api_error`.

Migration/data impact:
- No database migration or data backfill is required for the envelope wrapper itself.
- `bench migrate` should still be run after the pass to keep the site schema/current patches aligned with the app state.
