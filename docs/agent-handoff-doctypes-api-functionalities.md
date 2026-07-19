# Pet App Agent Handoff: DocTypes, APIs, and Functionalities

This is an agent-ready backend map for `apps/pet_app`. The app is a Frappe/ERPNext veterinary clinic, boarding, marketplace, POS/accounting, driver, and role-permission system.

Use exact DocType and method names from this document. Several names are non-standard/case-sensitive, for example `PetGuardian`, `PetCareService`, `CareService template`, and child table `custom services`.

## How To Call APIs

All whitelisted methods are called through Frappe:

```text
/api/method/<dotted.python.path>
```

Authenticated mobile/API calls generally use:

```http
Authorization: token <api_key>:<api_secret>
```

File upload endpoints are multipart form-data and read `frappe.request.files`.

## Core Relationships

- `Guardian` is the pet owner/mobile identity. It links to ERPNext `User` through `Guardian.user_id` and to billing `Customer` through `Guardian.customer_id`.
- `Pet` is the animal patient. Pet ownership/access is via `PetGuardian`.
- `PetAddRequest` is created when a Guardian creates a Pet. Approval creates `PetGuardian`, marks the Pet approved, and ensures a `Pet Medical Profile`.
- `Vet Case Sheet` is intake/triage. It validates Guardian-Pet ownership and can be converted to `Vet Visit`.
- `Vet Visit` is the clinical center. It links case sheet, pet, guardian, customer, doctor, diagnoses, orders, consults, medications, billable rows, follow-ups, and sales invoice.
- `Visit Order` rows can create linked `Lab`, `Imaging`, `PetCareService`, or `Pet Procedure` records.
- `Lab`, `Imaging`, and `Pet Procedure` sync billable rows back to the parent `Vet Visit`.
- `Pet Billable Item` is the shared child table for billable visit and boarding rows. Visit invoices are generated from active billable items.
- `Pet Boarding` links room, pet, guardian, customer, billable rows, and checkout invoice.
- `Product` projects to ERPNext `Item`, `Item Price`, and stock entries. `Product Category` syncs to ERPNext `Item Group`.
- Frappe-native permissions drive backend access: `Role`, `Role Profile`, Role Permission Manager / `DocPerm`, and `User Permission`. `get_current_access` returns a Vue compatibility snapshot for module/page visibility and DocType permission reflection. `actions` is intentionally empty. `Pet App Permission Rule` and `Pet App User Restriction` are deprecated and must not be used for security decisions.

## DocType Catalog

### Identity And Ownership

| DocType | Kind | Purpose / Key Fields | Controller Behavior |
|---|---|---|---|
| `Guardian` | DocType | Phone-first owner identity. Key fields: `phone`, `full_name`, `email_id`, `address_line1`, `city`, `country`, `guardian_image`, `otp_verified`, `is_active`, `user_id`, `customer_id`, OTP fields. | Validates Guardian-Customer mapping and syncs ERPNext Customer on update. |
| `Pet` | DocType | Animal profile. Key fields: `pet_name`, `animal_species`, `animal_type`, `breed`, `birth_date`, `gender`, `weight`, `status`, `pet_status`, `requested_by`, `pet_image`, food fields. | If a non-Administrator Guardian creates a Pet, creates a pending `PetAddRequest` and stamps `requested_by`. |
| `PetGuardian` | DocType | Pet-owner relationship. Key fields: `pet_id`, `guardian_id`, `role` (`primary_owner` / `owner`). | No custom controller. Used for access checks and relationship validation. |
| `PetAddRequest` | DocType | Guardian request to add/link a Pet. Key fields: `pet_id`, `guardian_id`, `status`, `approved_by`, `approval_date`, `rejection_reason`. | On first transition to `Approved`, sets `Pet.pet_status = Approved`, creates `PetGuardian`, and ensures `Pet Medical Profile`. |
| `Doctor` | DocType | Clinic practitioner. Key fields: `doctor_name`, `phone`, `user`, `specialization`, `photo`, `disabled`. | Admin-only maintenance. Creates/reuses a `User`, ensures Doctor/Healthcare roles, syncs user profile, disables user on delete. |

### Clinical EMR

| DocType | Kind | Purpose / Key Fields | Controller Behavior |
|---|---|---|---|
| `Vet Case Sheet` | DocType | Intake/triage. Key fields: `status`, `priority`, `case_sheet_date`, `guardian`, `customer`, `animal_patient`, pet snapshot fields, `chief_complaint`, symptoms, vaccination/background/daily-condition fields, `vet_visit`. | Sets defaults, populates pet/guardian/customer snapshot, validates Guardian-Pet link and conditional fields, syncs status with linked Visit. |
| `Vet Visit` | DocType | Main doctor workspace. Key fields: `status`, `priority`, `visit_datetime`, `case_sheet`, `guardian`, `customer`, `animal_patient`, `doctor`, `visit_type`, vitals, clinical notes, diagnosis/plan fields, child tables `diagnoses`, `care_services`, `prescribed_medications`, `orders`, `billable_items`, follow-up fields, `consult_requests`, billing fields. | Pulls case sheet identity, locks identity after create, blocks edits after invoice/billing, syncs medication/service billables, validates completion requirements, creates Sales Invoice via API. |
| `Visit Diagnosis` | Child Table | Diagnosis rows on Visit. Key fields: `disease`, `diagnosis_text`, `is_primary`, `severity`, `note`. | Stored under `Vet Visit.diagnoses`. |
| `Disease` | DocType | Diagnosis master. Key fields: `disease_name`, `species`, `category`, `active`. | Used by workspace diagnosis actions; inactive handling is mostly UI/API responsibility. |
| `Visit Consult Request` | Child Table | Consult request rows on Visit. Key fields: `requested_doctor`, `requested_by`, `reason`, `status`, `consult_note`, `requested_at`, `completed_at`. | Created/completed through workspace actions. |
| `Visit Order` | Child Table | Order rows on Visit. Key fields: `order_id`, `kind`, `title`, `template_id`, `status`, `priority`, `qty`, `price`, `linked_doctype`, `linked_name`. | Workspace `create_orders` creates linked Lab/Imaging/Service/Procedure and stamps linked row. |
| `Vet Visit Medication Item` | Child Table | Prescribed medication rows. Key fields: `medication_item`, `medication`, `qty`, `rate`, `amount`, `dosage`, `frequency`, `duration_days`, `instructions`. | Visit validation converts rows to `Pet Billable Item` rows. |
| `Medication` | DocType | Medication master and inventory bridge. Key fields: `medication_name`, `code`, `linked_item`, `default_warehouse`, `default_price`, dosage defaults, usage counters. | Finds/creates ERPNext `Item`, syncs standard rate and Item Price, validates warehouse restrictions, refreshes usage counters from visits. |

### Services, Diagnostics, Procedures

| DocType | Kind | Purpose / Key Fields | Controller Behavior |
|---|---|---|---|
| `CategoryCareServices` | DocType | Care service category. Key fields: `category_name`, `description`. | No custom controller. |
| `CareService template` | DocType | Service/pricing catalog. Key fields: `animal_species`, `frequency`, `category_id`, `service_name`, `item_code`, `default_price`, `price_list`, diagnostics metadata. | Validates service name, item code, default price, defaults `price_list` through the veterinary selling price list resolver. |
| `custom services` | Child Table | Selected service rows under Visit. Key fields: `pet_care_service_id`, `care_service_id`. | Visit validation converts non-lab/non-imaging selected services into billable rows. |
| `PetCareService` | Submittable DocType | Scheduled/performed service instance. Key fields: `pet_service_name`, `pet_id`, `care_service_id`, `status`, `doctor`, `visit`, `order_id`, `provider`, `user`, dates. | No custom controller; workspace actions update start/end/status. |
| `Lab` | DocType | Lab request/result linked to visit. Key fields: `visit`, `order_id`, `pet`, `doctor`, `care_service`, `item_code`, `status`, `result`. | Pulls pet/doctor from Visit, blocks changes after visit billing, pulls item/rate from Care Service, syncs visit billable item. |
| `Imaging` | DocType | Radiology/imaging request/result linked to visit. Key fields: `visit`, `order_id`, `pet`, `doctor`, `care_service`, `item_code`, `rate`, `status`, `report`, `image`. | Same pattern as Lab; syncs visit billable item. |
| `Procedure Template` | DocType | Procedure master. Key fields: `procedure_name`, `code`, `species`, `category`, `billing_care_service`, `default_duration_minutes`, `consent_required`, `anesthesia_required`, `active`, `steps`. | No custom controller. |
| `Procedure Template Step` | Child Table | Template checklist row. Key fields: `step_title`, `required`, `default_note`, `sort_order`. | Copied into `Pet Procedure.checklist`. |
| `Pet Procedure` | DocType | Procedure execution record linked to visit/order. Key fields: `visit`, `order_id`, `pet`, `guardian`, `doctor`, `provider`, `procedure_template`, `care_service`, `item_code`, `rate`, `status`, timing fields, consent/anesthesia, `checklist`, notes/findings/outcome/aftercare. | Pulls identity from Visit, pulls care service from template, copies checklist, validates required consent/checklist on completion, syncs/cancels billable item. |
| `Procedure Checklist Item` | Child Table | Procedure execution checklist. Key fields: `step_title`, `required`, `done`, `note`. | Used by procedure completion validation. |

### Boarding

| DocType | Kind | Purpose / Key Fields | Controller Behavior |
|---|---|---|---|
| `Service Room` | DocType | Boarding/service room inventory. Key fields: `room_code`, `room_name`, `room_type`, `status`, `image`, `notes`. | No custom controller. |
| `Pet Boarding` | Submittable DocType | Boarding reservation/check-in/check-out. Key fields: `service_room`, `pet`, `guardian`, `customer`, `boarding_type`, `record_status`, `status`, `reserved_at`, `check_in`, `check_out`, `stay_hours`, `stay_days`, `total_cost`, `deposit`, `balance`, `billing_status`, `sales_invoice`, `billable_items`. | Defaults status/customer, validates active room and Guardian-Pet link, enforces one active boarding per room, computes elapsed `stay_hours` and duration-based `stay_days`, can only submit when checked out and invoiced. Room stay billing uses `qty = stay_days`; `stay_hours` remains the underlying measurement. |
| `Pet Boarding Settings` | Single | Boarding billing defaults. Key fields: `travel_boarding_item`, `treatment_boarding_item`, `default_boarding_type`. | Used by boarding flow to choose default boarding item/type. |
| `Pet Billable Item` | Child Table | Shared billing row. Key fields: `item_name`, `item_code`, `item_type`, `qty`, `rate`, `amount`, `status`, `note`, `linked_service_id`. | Amount/status managed by Visit/Boarding and billing utilities. |

### Products And Commerce

| DocType | Kind | Purpose / Key Fields | Controller Behavior |
|---|---|---|---|
| `Product` | DocType | Marketplace product wrapper over ERPNext Item. Key fields: `product_name`, `sku`, `barcode`, `description`, `price`, `discounted_price`, `vendor`, `category`, `status`, `image`, `tags`, `item`, `item_group`, `item_price`, `has_variants`, `product_variant`, `brand`. | Validation applies `Product Category` and synced Item Group. Product API publishes to Item/Item Price and can create Stock Entry. |
| `Product Category` | NestedSet DocType | Category tree synced to Item Group. Key fields: `category_name`, `parent_product_category`, `is_group`, `enabled`, `display_order`, `image`, `description`, `item_group`, `lft`, `rgt`. | Creates/syncs/deletes linked ERPNext Item Group under `Pet Supplies`; prevents deletion if Products/Items use it. |
| `Product Variant` | Child Table | Simple variant metadata. Key fields: `options`, `value`, `price`. | Used by product API. |
| `FoodBrand` | DocType | Pet food brand master. Key fields: `brand_name`, `description`, `image`. | No custom controller. Note: `Pet.food_brand` currently links to ERPNext `Brand`, not `FoodBrand`. |
| `FoodType` | DocType | Pet food type master. Key fields: `type_name`, `description`. | No custom controller. |

### Accounting, Access, Settings

| DocType | Kind | Purpose / Key Fields | Controller Behavior |
|---|---|---|---|
| `Pet App Accounting Settings` | Single | App accounting defaults. Key fields: `treasury_cash_account`, `default_company`, `default_cash_mode_of_payment`. | Validates cash account/company/mode of payment. |
| `Pet App Cashier Settlement` | Submittable DocType | Cashier treasury transfer record. Key fields: `posting_date`, `cashier_profile`, `cashier_user`, `company`, `cash_account`, `treasury_cash_account`, `expected_cash`, `counted_cash`, `transfer_amount`, difference fields, `payment_entry`, `status`. | Computes difference/status, validates accounts/access, creates/links Payment Entry through cashier API. |
| `Role Profile` | Frappe DocType | Business-facing role bundle assigned to users. | Contains normal Frappe `Role` rows; this is what the frontend should manage for staff permission bundles. |
| `User Permission` | Frappe DocType | Standard Frappe record restrictions. Used for `Warehouse`, `POS Profile`, `Healthcare Practitioner`, and `Branch`. | Drives warehouse/cashier/practitioner/branch restrictions in backend APIs and `get_current_access.restrictions`. |
| `Pet App Permission Rule` | Deprecated DocType | Old hybrid frontend resource matrix. | Preserved only for old site data. Do not read or write this as a security source. |
| `Pet App User Restriction` | Deprecated DocType | Old custom record restriction storage. | Preserved only for old site data. Use Frappe `User Permission` instead. |

### ERPNext / External DocTypes Used Heavily

`User`, `Customer`, `Address`, `File`, `Appointment`, `Item`, `Item Group`, `Item Price`, `Warehouse`, `Bin`, `Stock Entry`, `Sales Order`, `Sales Invoice`, `Payment Entry`, `Journal Entry`, `POS Profile`, `Driver`, `Company`, `Mode of Payment`, `Account`, `Coupon Code`, `Pricing Rule`.

Custom Appointment fields loaded from `pet_app/pet_app/custom/appointment.json`:

- `custom_appointment_type`: `visit`, `follow_up`, `showering`, `barbering`
- `custom_customer`, `custom_guardian`, `custom_pet`
- `custom_follow_up_of_visit_id`
- `custom_linked_visit_id`
- `custom_linked_service_id`
- `custom_converted_target`
- `custom_converted_at`

## Whitelisted APIs

### Authentication And Guardian Mobile

| Method | Auth | Functionality |
|---|---|---|
| `pet_app.api.auth_api.login_and_get_session(usr, pwd)` | Guest | Frappe session login; returns `sid`, user, full name. |
| `pet_app.api.auth_api.login_and_get_api_keys(usr, pwd)` | Guest | Login and return API key/secret. |
| `pet_app.api.auth_api.login_and_get_oauth_token(usr=None, pwd=None, username=None, password=None, client_id=None, scope=None, grant_type=None)` | Guest POST | Password grant wrapper that returns OAuth bearer token. Username can be phone if verified Guardian exists. |
| `pet_app.api.auth_mobile.register_guardian(phone, password, full_name)` | Guest | Create or update unverified Guardian, store pending password hash, send OTP. Debug mode returns OTP. |
| `pet_app.api.auth_mobile.send_otp(phone)` | Guest, rate-limited | Resend OTP for unverified Guardian. |
| `pet_app.api.auth_mobile.resend_otp(phone)` | Guest | Alias for `send_otp`. |
| `pet_app.api.auth_mobile.verify_otp(phone, otp, password)` | Guest, rate-limited | Validate OTP/password, create Customer and User, activate Guardian, return `token <key>:<secret>`. |
| `pet_app.api.auth_mobile.login(phone, password)` | Guest, rate-limited | Phone/password login for verified Guardian; returns token. |
| `pet_app.api.auth_mobile.complete_profile(full_name, city, address_line1, email_id=None, country="Iraq")` | Token | Update Guardian, Customer, primary Address, and User name. |
| `pet_app.api.auth_mobile.request_guardian_phone_change_otp(new_phone, user_id=None, guardian_id=None)` | Token | Begin verified phone-change flow. |
| `pet_app.api.auth_mobile.change_guardian_phone(new_phone, otp_code, user_id=None, guardian_id=None)` | Token | Atomically update Guardian phone and linked Customer mobile. |
| `pet_app.api.auth_mobile.get_guardian_profile(guardian_id)` | Token | Return Guardian, Customer, address, order totals, and coupon usage. |
| `pet_app.api.auth_mobile.forgot_password(phone)` | Guest, rate-limited | Send OTP for password reset. |
| `pet_app.api.auth_mobile.reset_password(phone, otp, new_password)` | Guest, rate-limited | Reset linked User password using OTP. |
| `pet_app.api.auth_mobile.logout()` | Token | Stateless logout acknowledgement; client deletes token. |

### Pets And Files

| Method | Functionality |
|---|---|
| `pet_app.api.pet.upload_multiple_files()` | Multipart upload to any DocType/doc with an `Attach Image` field. Max 500 KB/file, duplicate SHA1 detection, default image selection. |
| `pet_app.api.pet.delete_multiple_files(file_names)` | Delete attached files after checking write permission; reassigns default image if needed. |
| `pet_app.api.pet.set_default_file(file_id, doctype, docname)` | Marks file as default and updates the DocType's first `Attach Image` field. |
| `pet_app.api.pet.get_pet_images(doctype, docname)` | List image files for a doc's first `Attach Image` field. |
| `pet_app.api.pet.list_pets(page=1, page_size=10, search=None)` | Paginated pet list with images. Guardian users only see linked pets. |
| `pet_app.api.pet.get_pet(pet_id)` | Single pet detail with images. Guardian users must be linked via `PetGuardian`. |
| `pet_app.api.pet.upload_single_file()` | Multipart single-image replace with duplicate detection; syncs Guardian/Practitioner image to `User.user_image` where linked. |
| `pet_app.api.medical_file.get_pet_medical_summary(pet=None, pet_id=None)` | Returns the pet medical profile, latest visit, and latest vitals. Profile payload uses `pet` as the clinical patient and does not expose legacy external patient links. |
| `pet_app.api.medical_file.update_pet_medical_profile(pet=None, pet_id=None, data=None, **kwargs)` | Clinical/admin POST endpoint to update profile notes and alerts: `microchip_no`, `allergies`, `chronic_conditions`, `special_alerts`, `diet_notes`, `behavior_notes`, `vaccination_notes`, `deworming_notes`. |
| `pet_app.api.medical_file.get_pet_medical_timeline(pet=None, pet_id=None, limit=50)` | Returns visit, case sheet, diagnosis, medication, follow-up, diagnostics, procedure, vaccination, deworming, boarding, and death events for a pet. |

### Healthcare Workspace

| Method | Functionality |
|---|---|
| `pet_app.api.workspace.get_my_workspace(mode=None, user=None, search=None, priority=None, status=None, limit=50, cursor=0)` | Returns daily queue items, metrics, pagination for modes: `doctor`, `service`, `coordinator`, `diagnostics`, `accounting`, `management`, `all`. |
| `pet_app.api.workspace.get_record(source_type, name)` | Returns aggregate detail for Visit/Case Sheet/Appointment/Service/Procedure/Lab/Radiology/Invoice/Payment. Linked records usually resolve to parent Visit aggregate. |
| `pet_app.api.workspace.perform_action(source_type, name, action, payload=None)` | Main workflow action router. See action list below. |
| `pet_app.api.workspace.add_note(source_type, name, note=None, content=None, payload=None)` | Adds Frappe comment and returns updated aggregate. |
| `pet_app.api.workspace.attach_file(source_type, name, payload=None, file_url=None, file_name=None, filedata=None, is_private=1)` | Attaches file by URL or base64/filedata and returns updated aggregate. |

Supported `source_type` aliases:

| UI Alias | Backend DocType |
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

Supported workspace actions:

- Doctor/clinical: `start_consultation`, `save_clinical_note`, `save_diagnoses`, `create_orders`, `complete_case`, `request_follow_up`, `request_consult`, `complete_consult`
- Procedure: `start_procedure`, `save_procedure_note`, `complete_procedure`, `close_procedure`, `cancel_procedure`
- Service: `start_service`, `finish_service`, `close_service`
- Diagnostics: `start_test`, `save_result`, `release`
- Coordinator: `assign`, `reassign`, `convert_to_visit`, `convert_follow_up_to_visit`, `convert_to_service`
- Accounting: `submit_invoice`, `mark_follow_up`

### Clinical DocType Methods

| Method | Functionality |
|---|---|
| `pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet.get_pet_context(pet_name)` | Returns pet snapshot plus primary Guardian/Customer/phone for case sheet autofill. |
| `pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet.start_visit(case_sheet_name)` | Converts Case Sheet to Visit for current Doctor after permission/restriction checks. |
| `pet_app.pet_app.doctype.vet_visit.vet_visit.get_item_billing_details(item_code)` | Returns item name/uom/rate for visit billing. |
| `pet_app.pet_app.doctype.vet_visit.vet_visit.get_care_service_billing_details(care_service_name)` | Returns Care Service item/rate/price list. |
| `pet_app.pet_app.doctype.vet_visit.vet_visit.create_sales_invoice(visit_name)` | Creates and submits Sales Invoice from Visit billable rows, then locks Visit as billed. |

### Care Services

| Method | Functionality |
|---|---|
| `pet_app.api.care_service.get_pet_and_services(pet_id, filters=None, limit_start=0, limit_page_length=20)` | Returns pending `PetCareService` rows plus matching master care services for the pet species. Note caveat below about `CareService` vs `CareService template`. |

### Boarding

| Method | Functionality |
|---|---|
| `pet_app.api.healthcare.boarding.list_boarding_units(search=None, occupancy=None, date=None)` | Active rooms with current occupancy/reservation info. |
| `pet_app.api.healthcare.boarding.get_boarding_detail(boarding_id=None, room_id=None, name=None)` | Detail by boarding or room, including active boarding if room is occupied/reserved. |
| `pet_app.api.healthcare.boarding.list_boarding_records(search=None, status=None, pet_id=None, guardian_id=None, date_from=None, date_to=None, limit_start=0, limit_page_length=10, order_by="modified desc")` | Search/filter/paginate boarding records. |
| `pet_app.api.healthcare.boarding.reserve_room(roomId, petId, guardianId, checkIn=None, checkOut=None, note=None, boardingType=None)` | Reserve room with locking and Guardian-Pet validation. |
| `pet_app.api.healthcare.boarding.check_in_boarding(boarding_id)` | Reserved -> Checked In. |
| `pet_app.api.healthcare.boarding.check_out_boarding(boarding_id)` | Checked In -> invoice, close, submit boarding. Creates Sales Invoice. |
| `pet_app.api.healthcare.boarding.sync_billable_items(boarding_id=None, billable_items=None, name=None, boardingId=None, billableItems=None)` | Replace/update open boarding billable rows and recompute totals. |

### Product, Category, Stock

| Method | Functionality |
|---|---|
| `pet_app.api.product.publish_product(product_id=None, **kwargs)` | Create/update Product, validate, ensure ERPNext Item and Item Price, optionally initial stock, set Product active/in stock. |
| `pet_app.api.product.restock_product(product_id, qty, warehouse=None, item_variant=None)` | Creates submitted Material Receipt Stock Entry for the product item. |
| `pet_app.api.product.get_stock_info(product_id, warehouse=None)` | Returns stock qty, last restock, lifetime stock receipt total, variants metadata. |
| `pet_app.api.product.get_products(filters=None, fields=None, order_by="creation desc", limit_start=0, limit_page_length=20, search_term=None)` | Product list enriched with qty, images, category summary, brand data. |
| `pet_app.api.product_category.list_product_categories(parent=None, search=None, enabled_only=1)` | List Product Category tree rows. |
| `pet_app.api.product_category.get_product_category(name)` | Single category plus child/product counts. |
| `pet_app.api.product_category.save_product_category(data=None, **kwargs)` | Create/update/rename category and synced Item Group with hybrid permissions. |
| `pet_app.api.product_category.delete_product_category(name)` | Delete category and linked Item Group if safe. |

### Orders, Sales, Coupons

| Method | Functionality |
|---|---|
| `pet_app.api.order.place_order(customer=None, items=None, payment_method="Cash on Delivery", delivery_lat=None, delivery_lng=None, shipping_address_name=None, coupon_code=None, shipping_rule=None, guardian=None)` | Guardian/staff marketplace order. Validates Guardian-Customer identity, stock in `Stores - H`, item prices, optional coupon, creates and submits Sales Order. |
| `pet_app.api.sales.create_sales_invoice_for_guardian(guardian, items, posting_date=None, due_date=None, is_pos=1, pos_profile=None, customer=None)` | Guarded POS/billing invoice for a Guardian; resolves Customer, validates items/rates, creates Sales Invoice. |

Order status transitions implemented in `pet_app.api.order` helpers:

```text
Draft -> Preparing -> Out for Delivery -> Cash Collected -> Completed
Draft/Preparing -> Cancelled
Out for Delivery -> Returned
Returned -> Out for Delivery or Cancelled
```

Side effects:

- `Preparing`: Material Issue Stock Entry.
- `Out for Delivery` with COD: driver debit Journal Entry.
- `Returned`: reverse driver entry.
- `Cash Collected`: collect driver cash Journal Entry.
- `Cancelled`: reverse stock issue.

`pet_app.api.coupons` contains non-whitelisted coupon helpers used by `place_order`: pricing rule creation/update, validation, usage increment, and conflict logging.

### Drivers

| Method | Functionality |
|---|---|
| `pet_app.api.driver.create_driver(full_name, phone, custom_username=None, custom_email=None, email=None, status="Active", license_number=None, license_expiry=None, address_line1=None, address_line2=None, city=None, country="Iraq")` | Creates ERPNext Driver plus linked User, Address, and cash Account; returns one-time temporary password. |
| `pet_app.api.driver.delete_driver(driver_id)` | Hard deletes driver/user/address/cash account if no history; otherwise soft disables driver and user. |
| `pet_app.api.driver.driver_login(phone, password)` | Guest POST driver login; returns token and driver metadata. |
| `pet_app.api.driver.get_driver_balance(driver_id)` | Reads GL balance of driver's cash account with self/admin access rules. |

### Accounting And Cashier

| Method | Functionality |
|---|---|
| `pet_app.api.accounting.settings.get_accounting_settings()` | Returns app accounting defaults. |
| `pet_app.api.accounting.settings.update_accounting_settings(data=None, **kwargs)` | Updates and validates default company, treasury cash account, default cash mode. |
| `pet_app.api.accounting.cashier.list_cashier_profiles_for_user(user=None, company=None, search=None, include_disabled=0)` | POS Profiles visible to user, filtered by cashier restrictions. |
| `pet_app.api.accounting.cashier.get_cashier_profile_detail(pos_profile=None, profile=None, cashier_profile=None)` | Full authorized POS Profile payload. |
| `pet_app.api.accounting.cashier.get_cashier_runtime_defaults(pos_profile=None, company=None)` | Settings, profiles, active profile, current cashier context. |
| `pet_app.api.accounting.cashier.save_cashier_profile(profile=None, data=None, **kwargs)` | Create/update POS Profile, users, payments, cash account, defaults. |
| `pet_app.api.accounting.cashier.create_payment_entry_with_cashier_context(pos_profile=None, data=None, submit=1, **kwargs)` | Create/submit Payment Entry stamped with cashier/POS context and references. |
| `pet_app.api.accounting.cashier.get_cashier_settlement_snapshot(pos_profile=None, profile=None, cashier_profile=None, posting_date=None, from_date=None, to_date=None)` | Cash activity, current balance, expected cash, recent entries, suggested settlement. |
| `pet_app.api.accounting.cashier.settle_cashier_to_treasury(pos_profile=None, profile=None, cashier_profile=None, amount=None, posting_date=None, submit=1, **kwargs)` | Create internal transfer Payment Entry and `Pet App Cashier Settlement`. |
| `pet_app.api.accounting.cashier.list_cashier_settlements(profile=None, cashier_profile=None, from_date=None, to_date=None, difference_type=None, status=None, limit_start=0, limit_page_length=50)` | Settlement list with filters. |
| `pet_app.api.accounting.cashier.get_cashier_settlement_detail(name)` | Settlement detail plus linked Payment Entry. |

### Permissions, Role Profiles, User Restrictions

| Method | Functionality |
|---|---|
| `pet_app.api.permissions.get_current_access()` | Current user's roles, role profile, full-access status, allowed modules/pages, empty compatibility actions, DocType perms, restrictions. |
| `pet_app.api.permissions.get_access_matrix()` | Deprecated read-only modules/pages matrix with an empty actions list. |
| `pet_app.api.permissions.update_access_matrix(matrix=None, data=None, matrix_json=None, replace_roles=1, **kwargs)` | Admin-only update resource definitions and allowed roles. |
| `pet_app.api.permissions.register_frontend_resources(resources=None, data=None, matrix=None, resources_json=None, matrix_json=None, **kwargs)` | Admin-only register frontend resources as Draft/defined resources. |
| `pet_app.api.permissions.get_user_restrictions(user=None)` | User can read own restrictions; admin can read others. |
| `pet_app.api.permissions.update_user_restrictions(user=None, restrictions=None, data=None, **kwargs)` | Admin-only replace restrictions for a user. |

Important resource prefixes:

- Modules: `module.*`
- Pages: `page.*`
- Actions: `action.*`

Important restriction types:

- `warehouse`
- `cashier_profile`
- `doctor`
- `branch`

Seeded role profiles include:

- `Alkokh App Owner`
- `Alkokh Admin`
- `Clinic Manager`
- `Veterinarian`
- `Clinic Reception`
- `Lab And Radiology Operator`
- `POS Cashier`
- `POS Supervisor`
- `Accountant`
- `Accounting Manager`
- `Warehouse User`
- `Warehouse Manager`
- `Purchase Officer`
- `Auditor`
- `HR Officer`
- `HR Manager`
- `Module Viewer`

### Users And Dashboards

| Method | Functionality |
|---|---|
| `pet_app.api.users.get_users_with_role_profile(limit_start=0, limit_page_length=20, enabled_only=0, filters=None)` | Admin/user-management list of users with role profile/status. |
| `pet_app.api.users.get_all_role_profiles_with_roles(limit_page_length=999)` | Role profiles with their roles, excluding standard/empty profiles. |
| `pet_app.api.users.change_user_password(user, new_password)` | Admin/user-management password reset for another user. |
| `pet_app.api.dashboard.get_order_status_counts()` | Order status counts. |
| `pet_app.api.dashboard.get_statistics()` | Sales, customer, product, revenue stats. |
| `pet_app.api.dashboard.get_revenue_report(fiscal_year="2026")` | Monthly sales invoice earnings vs purchase invoice expenses. |
| `pet_app.api.dashboard.get_best_seller()` | Highest spending customer this month. |
| `pet_app.api.dashboard.get_profit_and_expenses()` | Current month profit/expense/growth. |
| `pet_app.api.dashboard.get_orders_by_item_group()` | Sales order item group totals for charts. |
| `pet_app.pet_app.page.alkohk_dashboard.alkohk_dashboard.get_dashboard_payload()` | Desk dashboard payload. |

## Main Workflows

### Guardian Registration

1. `register_guardian(phone, password, full_name)` creates inactive/unverified `Guardian` and OTP.
2. `verify_otp(phone, otp, password)` validates OTP, creates or resolves `Customer`, creates `User`, links both to Guardian, returns API token.
3. `complete_profile(...)` updates Guardian, Customer, Address, and User display name.
4. Login uses `auth_mobile.login(phone, password)` or OAuth/session alternatives in `auth_api.py`.

### Pet Approval / Linking

1. Guardian creates `Pet`.
2. `Pet.after_insert` creates `PetAddRequest(status="Pending")`.
3. Admin approves request.
4. `PetAddRequest.on_update` marks Pet approved, creates `PetGuardian(role="primary_owner")`, and ensures `Pet Medical Profile`.

### Case Sheet To Visit

1. Reception/coordinator creates `Vet Case Sheet` with valid Guardian/Pet.
2. Backend fills customer, phone, species, breed, sex, age, weight.
3. `start_visit(case_sheet_name)` or workspace `convert_to_visit` creates `Vet Visit`.
4. Visit inherits identity and locks case/pet/guardian/customer after creation.

### Visit Orders And Billing

1. Doctor uses workspace `save_clinical_note`, `save_diagnoses`, and `create_orders`.
2. `create_orders` creates `Visit Order` rows and linked records:
   - `kind=lab` -> `Lab`
   - `kind=radiology` -> `Imaging`
   - `kind=service` -> `PetCareService`
   - `kind=procedure` -> `Pet Procedure`
3. Linked records sync `Pet Billable Item` rows to the Visit.
4. Prescribed medications and selected care services also create billable rows during Visit validation.
5. `create_sales_invoice(visit_name)` creates/submits ERPNext `Sales Invoice`, marks billable rows Billed, sets `billed=1`, and locks Visit.

### Follow-Up And Consults

- `request_follow_up` creates/reuses an `Appointment`, links it to original Visit, and marks follow-up Scheduled.
- `convert_follow_up_to_visit` creates a new Case Sheet and `Vet Visit(visit_type="Follow-up")`, sets `follow_up_of_visit_id`, links appointment conversion fields, and marks original follow-up Seen.
- `request_consult` adds a row to `Vet Visit.consult_requests`; `complete_consult` requires a note and marks it Completed.

### Boarding Flow

1. `list_boarding_units` displays room occupancy.
2. `reserve_room` creates `Pet Boarding(record_status="Reserved")`.
3. `check_in_boarding` sets Checked In.
4. `sync_billable_items` updates optional/additional billables.
5. `check_out_boarding` ensures room stay billable, creates Sales Invoice, marks rows Billed, closes and submits boarding.

### Product / Marketplace Flow

1. `save_product_category` creates Product Category and synced Item Group.
2. `publish_product` creates/updates Product, Item, Item Price, optional Material Receipt.
3. `get_products` serves storefront/admin listing with images and stock qty.
4. `place_order` validates Guardian/Customer identity, stock, price, coupon, creates and submits Sales Order.
5. Sales Order status hooks/helpers handle stock issue/return and COD driver accounting.

### Cashier Flow

1. `get_cashier_runtime_defaults` resolves settings, assigned profiles, active profile, mode of payment.
2. `create_payment_entry_with_cashier_context` collects payments against Sales Invoices and stamps POS/cashier fields if present.
3. `get_cashier_settlement_snapshot` computes expected cash.
4. `settle_cashier_to_treasury` creates internal transfer Payment Entry plus `Pet App Cashier Settlement`.

## Permission Model Notes

- Full access roles: `Administrator`, `System Manager`, `Pet App Admin`.
- `require_app_permission(resource_key)` is deprecated compatibility only; backend APIs should check DocType permissions directly.
- `require_doctype_permission(doctype, ptype)` wraps Frappe DocType permission checks.
- `require_restriction_value(type, value)` blocks users outside assigned warehouse/cashier/doctor/branch restrictions.
- Frontend should call `get_current_access()` on boot, gate route/sidebar visibility with `modules` and `pages`, and show management controls from `doctypes[doctype]` permissions.

## Known Caveats For The Next Agent

- Actual service master DocType is `CareService template`, but `pet_app.api.care_service.get_pet_and_services` currently queries `CareService` in several places. Verify whether a compatibility DocType/alias exists on the target site before relying on that endpoint.
- `Pet.food_brand` links to ERPNext `Brand`, while the app also defines `FoodBrand`.
- Some custom fields used by flows are on ERPNext DocTypes (`Appointment`, `Sales Order`, `Sales Invoice`, `Payment Entry`, `Driver`, `File`). Check fixtures in `pet_app/fixtures/custom_field.json` and `pet_app/pet_app/custom/appointment.json` when adding frontend fields.
- `Vet Visit` is intentionally locked once `sales_invoice`/`billed` is set. Use payment/invoice APIs, not Visit edits, after billing.
- Many APIs set `frappe.response["data"]` instead of returning a value. Clients should read Frappe's normal response `message` and/or `data` depending on endpoint behavior.
- Some source files include Arabic comments/messages. Do not treat them as user-facing English copy unless the product intentionally supports Arabic.

## Useful Existing Docs

- `docs/frontend-agent-pet-app-overview.md`
- `docs/frontend-healthcare-workspace-backend-contract.md`
- `docs/workspace-api.md`
- `docs/boarding-frontend.md`
- `docs/product-api-docs.md`
- `docs/README_API.md`

## High-Value Tests

- `pet_app/tests/test_hybrid_permissions.py`
- `pet_app/tests/test_product_category.py`
- `pet_app/tests/test_appointment_identity.py`
- `pet_app/pet_app/doctype/pet_boarding/test_pet_boarding.py`
- `pet_app/pet_app/doctype/vet_case_sheet/test_vet_case_sheet.py`
- `pet_app/pet_app/doctype/medication/test_medication.py`
