# Pet App

`pet_app` is a custom Frappe / ERPNext application for a pet and veterinary business. It combines:

- guardian onboarding and mobile authentication
- pet registration and guardian-to-pet linking
- catalog and stock-backed product selling
- care service cataloging and scheduled pet services
- ERPNext sales order / sales invoice integration
- healthcare integration through `Patient` and `Patient Encounter`
- a new veterinary intake and doctor-visit workflow through `Vet Case Sheet` and `Vet Visit`

The app is implemented under the `Pet App` module and relies heavily on standard ERPNext masters such as `Customer`, `Item`, `Item Price`, `Sales Order`, `Sales Invoice`, `Supplier`, `Item Group`, `Patient`, and `Healthcare Practitioner`.

## Repository Layout

```text
apps/pet_app/
├── docs/
│   ├── README_API.md
│   ├── pet-guardian-linking-flow-v1.md
│   └── product-api-docs.md
├── pet_app/
│   ├── api/
│   ├── integrations/
│   ├── utils/
│   ├── hooks.py
│   └── pet_app/doctype/
└── README.md
```

## Main Business Areas

### 1. Guardian and Mobile Identity

The mobile identity flow is implemented in `pet_app/api/auth_mobile.py`.

Core behavior:
- guardian registration starts with phone + password + OTP
- `Guardian` is created first
- after OTP verification, the app creates the linked `User` and `Customer`
- profile completion updates `Customer` and primary `Address`
- login returns API credentials for mobile usage

This is the core owner onboarding model in the current app.

### 2. Pet Master and Ownership

The app currently uses:

- `Pet`: main pet master
- `Guardian`: owner profile linked to `User` and `Customer`
- `PetGuardian`: pet-to-guardian link table with role such as `primary_owner`
- `PetAddRequest`: approval workflow for guardian-submitted pets

Important behavior:
- when a guardian creates a pet, a `PetAddRequest` is created automatically
- once approved, the app creates the `PetGuardian` link
- the app also creates or links an ERPNext `Patient` record for the pet

The patient-linking utility lives in `pet_app/utils/patient_linking.py`.

### 3. Care Services

The service side of the app is centered on:

- `CareService`: reusable clinic service master
- `CategoryCareServices`: category master for services
- `PetCareService`: scheduled or due service for a specific pet

`CareService` already contains the pricing shape the app uses today:

- `service_name`
- `item_code` -> ERPNext `Item`
- `default_price`
- `price_list`
- `category_id`

The service API in `pet_app/api/care_service.py` merges:
- pending `PetCareService` rows for a pet
- master `CareService` rows filtered by animal species

### 4. Products, Stock, and E-commerce-style Selling

The product subsystem is implemented through:

- `Product`
- `Product Variant`
- `pet_app/api/product.py`

This code synchronizes custom product records with ERPNext:

- `Item`
- `Item Price`
- `Website Item`
- `Stock Entry`
- `Bin`

Pricing for products is authoritative in ERPNext:
- the app derives the selling rate from `price` / `discounted_price`
- it creates or updates `Item Price`
- stock operations go through ERPNext stock transactions

### 5. Orders and Invoicing

The order flow is implemented in `pet_app/api/order.py`.

Current behavior:
- `place_order` creates a `Sales Order`
- item price is always fetched from `Item Price`
- stock availability is checked from `Bin`
- `complete_order` creates and submits a `Sales Invoice`
- `cancel_order` cancels the order
- a hook updates sales order payment status when order status becomes completed

### 6. Healthcare / Clinical Integration

The healthcare integration is already active through `hooks.py`:

- `Patient Encounter.before_insert` -> `pet_app.integrations.patient_encounter.before_insert`
- `Patient Encounter.after_insert` -> `pet_app.integrations.patient_encounter.after_insert`
- `Sales Order.on_change` -> `pet_app.api.order.on_sales_order_update`

`pet_app/integrations/patient_encounter.py` does the following:
- resolves or creates `Patient` from `Pet` + `Guardian`
- resolves session user to `Healthcare Practitioner`
- converts custom services into invoiceable lines
- creates and submits a `Sales Invoice`
- marks `PetCareService` rows as completed

This is the most important existing billing pattern in the app.

## Custom DocTypes

### Operational / Master Data

- `Guardian`
- `Pet`
- `PetGuardian`
- `PetAddRequest`
- `FoodBrand`
- `FoodType`
- `Product`
- `Product Variant`
- `CareService`
- `CategoryCareServices`
- `PetCareService`
- `Pet Boarding`
- `Custom Services`

### Veterinary Module

The current veterinary implementation added in this branch introduces:

- `Vet Case Sheet`
- `Vet Visit`
- `Vet Visit Medication Item`
- `Vet Visit Service Item`
- `Vet Visit Lab Request Item`

Business intent:
- `Vet Case Sheet` is front-desk intake and triage
- `Vet Visit` is the doctor encounter
- one case sheet maps to exactly one visit
- the `Start Visit` action creates the visit from the case sheet

## Current API Surface

### Authentication
- `auth_api.py`
- `auth_mobile.py`

### Pets and Files
- `pet.py`

Key capabilities:
- list pets
- get single pet
- upload single image
- upload multiple images
- delete images
- set default image

### Services and Encounters
- `care_service.py`
- `visit.py`
- `integrations/patient_encounter.py`

### Commerce
- `product.py`
- `order.py`
- `coupons.py`

### Admin / Analytics
- `dashboard.py`
- `users.py`
- `driver.py`

## Pricing and Billing Model in the Current App

The app already uses two clear pricing patterns:

### A. Product pricing

For products, price is defined through:
- custom `Product.price` / `discounted_price`
- synchronized ERPNext `Item Price`

Billing then uses ERPNext item pricing.

### B. Service pricing

For clinic services, price is defined through:
- `CareService.default_price`
- `CareService.item_code`

The healthcare encounter integration uses `CareService.item_code` to create `Sales Invoice` items.

## Vet Visit Billing Model

`Vet Visit` now follows the same master-linked billing direction as the rest of the app.

### Billing state on `Vet Visit`

The visit now carries:
- `billed` -> read-only `Check`
- `sales_invoice` -> read-only `Link` to `Sales Invoice`
- `total_billable_amount` -> read-only `Currency`

Behavior:
- default `billed = 0`
- clicking `Create Invoice` creates a draft `Sales Invoice`
- once invoice is created, `billed = 1` and `sales_invoice` is stored
- duplicate invoice creation is blocked

### Billable row model

- medications -> link to ERPNext `Item`
- services / procedures -> link to `CareService`
- lab / imaging -> link to `CareService`

### Price source

- medication price comes from ERPNext `Item Price` in `Standard Selling`
- service and lab price come from `CareService.default_price`
- service and lab invoice item mapping comes from `CareService.item_code`

### Snapshot fields stored on rows

The visit child rows store:
- source master link
- `item_code` where applicable
- `qty`
- `rate`
- `amount`

This keeps the visit invoice-ready and preserves the billed snapshot.

### Operational requirement

For any service or lab row to be billable:
- the linked `CareService` must have `item_code`
- the linked `CareService` should have `default_price`

For medication rows:
- the linked `Item` should have selling price in `Item Price`
- use master price or copied row rate
- create one `Sales Invoice`
- set `billed = 1`
- set `sales_invoice = <invoice>`

## Recommended Vet Billing Rules

For the veterinary module specifically:

- `Vet Visit Medication Item` should be backed by `Item`
- `Vet Visit Service Item` should ideally be backed by `CareService`, not only `Item`
- `Vet Visit Lab Request Item` should remain free text only if it is not billable
- if lab requests need billing, they must point to a priced master

In other words:

- free text is fine for clinical notes
- free text is not enough for invoicing

## Current Gaps to Be Aware Of

- `Vet Visit` currently does not yet have `billed` or `sales_invoice`
- the current visit child tables are clinically useful, but not yet invoice-grade
- `Pet` permissions are currently narrow, so desk and doctor roles may need extra read permission in production
- `users.py` contains duplicated `get_all_role_profiles_with_roles` logic and should be cleaned later

## Setup / Migration

After new DocType changes:

```bash
bench --site <site-name> migrate
bench --site <site-name> clear-cache
```

If you are iterating on individual doctypes:

```bash
bench --site <site-name> reload-doc pet_app pet_app doctype vet_case_sheet
bench --site <site-name> reload-doc pet_app pet_app doctype vet_visit
```

## Suggested Next Step

Before coding the billing part of `Vet Visit`, align the billable model:

1. Decide whether lab requests are billable or purely clinical.
2. Decide whether visit procedures should use `CareService`.
3. Keep medications on `Item`.
4. Then add `billed`, `sales_invoice`, row rates, and invoice generation.

Once that decision is locked, the invoice implementation can be added cleanly in one pass.
