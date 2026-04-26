# Pet App Frontend Agent Overview

This document is a product and integration brief for a frontend Codex agent working on `pet_app`.

It describes what the app is, how the main domain objects relate to each other, which screens the frontend should expect, and the important backend rules that the UI must respect.

## Product Summary

`pet_app` is a veterinary clinic management system built on Frappe/ERPNext.

The app combines:

- Veterinary EMR workflows for pets.
- Guardian/owner identity and mobile access.
- Clinical case sheets and doctor visits.
- Care services, medications, lab requests, and billable visit items.
- Boarding room reservation, check-in, checkout, and billing.
- Product/store inventory and sales order flows.
- Admin dashboards, users, roles, drivers, and order operations.

The frontend should treat the app as a clinic operations system, not only as a pet profile app.

## Main User Roles

### Clinic Admin / System Manager

Primary responsibilities:

- Manage pets, guardians, users, doctors, services, products, rooms, and settings.
- Approve or maintain pet/guardian relationships.
- Monitor dashboards, sales, stock, orders, and operational workflows.
- Configure billing sources such as products, services, and boarding item mappings.

### Doctor / Healthcare Practitioner

Primary responsibilities:

- Review pet context and medical history.
- Start or continue visits.
- Complete examination, assessment, diagnosis, treatment plan, and follow-up.
- Request services, medications, lab work, or imaging-style services where available.
- Create or trigger visit billing when the visit is complete.

### Guardian / Pet Owner

Primary responsibilities:

- Register/login by phone.
- Complete profile and address.
- View linked pets.
- View pet-related data exposed by the frontend/mobile app.
- Place marketplace orders if the frontend supports the sales flow.

### Driver

Primary responsibilities:

- Login with driver credentials.
- Handle delivery/order duties.
- Track cash/account balance where applicable.

## Core Domain Model

### Guardian

Guardian represents the pet owner identity inside the clinic/mobile system.

Important concepts:

- Guardian uses phone-based registration and OTP verification.
- Guardian links to an ERPNext Customer using `Guardian.customer_id`.
- That Customer is the billing identity for sales invoices/orders.
- Do not assume a pet directly owns a Customer.
- For pet ownership, use the Pet to Guardian relationship.

Frontend implications:

- Guardian screens should show phone, profile, customer link, active/verified state, and address/profile completion.
- When a workflow asks for a billing customer, the backend usually resolves it from Guardian.

### Pet

Pet is the animal patient.

Important fields/concepts:

- Pet name, species/type, breed, image, color, sex, weight, birth date.
- Pet status and registry status.
- Pet can have food preferences and general notes.
- Pet may be visible to one or many Guardians through `PetGuardian`.

Frontend implications:

- Pet profile should be the center of medical history.
- Show pet image prominently.
- Keep clinical data, ownership data, and product/order data visually separate.

### PetGuardian

PetGuardian links pets to guardians.

Important concepts:

- This is the ownership/access relationship.
- One pet can have multiple guardians.
- A guardian can have multiple pets.
- Roles include primary owner and owner.

Frontend implications:

- When selecting a pet and guardian together, the UI should only allow valid linked combinations.
- Boarding and clinical workflows should respect the selected pet's guardian relationship.

### Customer

Customer is the ERPNext billing entity.

Important concepts:

- Customer is linked from Guardian.
- Sales Invoice and Sales Order use Customer.
- Guardian remains the clinic/mobile owner identity.

Frontend implications:

- Do not expose Customer as the primary owner model in pet UI.
- Use Guardian for owner UX, Customer for billing references.

## Veterinary EMR Flow

The clinical workflow is built around two main objects:

- Vet Case Sheet
- Vet Visit

### Vet Case Sheet

The case sheet is the intake and clinical context before or at the start of a visit.

It captures:

- Guardian/customer context.
- Animal/pet context.
- Chief complaint and symptom duration.
- Symptom checklist.
- Vaccination and background history.
- Chronic disease and medication background.
- Appetite, water intake, urination, defecation, and activity.
- Lifestyle and intake notes.
- Priority and case status.

Typical statuses:

- Draft
- Waiting Doctor
- In Consultation
- Converted to Visit
- Closed

Frontend page idea:

- Case sheet list for reception/triage.
- Case sheet detail with tabs:
  - Basic Info
  - Intake Checklist
  - Symptoms
  - Background History
  - Daily Condition
  - Notes
- Primary action: Start Visit.

### Vet Visit

Visit is the main doctor-facing clinical container.

It captures:

- Visit date/time.
- Visit status.
- Linked case sheet.
- Customer/guardian billing identity.
- Animal/pet.
- Doctor.
- Visit type.
- Weight and vitals.
- Examination notes.
- Case summary.
- Diagnosis and differential diagnosis.
- Treatment plan.
- Doctor notes.
- Prescribed medications.
- Requested services/procedures.
- Lab requests.
- Follow-up flag/date.
- Billing status and linked Sales Invoice.

Typical statuses:

- Draft
- In Progress
- Completed
- Cancelled
- Follow-up Needed

Frontend page idea:

- Doctor worklist for active visits.
- Visit detail as a clinical workspace:
  - Header: pet, owner/guardian, visit status, doctor, billing status.
  - Clinical tabs: Examination, Assessment, Plan, Orders/Actions, Follow-up.
  - Action buttons: complete visit, create invoice, open invoice.

Important UI rule:

- Visit is clinical. Billing should be visible, but it should not dominate the visit screen.

## Services And Billing Concepts

### Care Service

Care Service is the clinic's service/pricing catalog.

It represents things such as:

- Consultation
- Vaccination
- Grooming/service procedures
- Lab/imaging style service items
- Other billable clinic services

Important concepts:

- Care Service can link to an Item for billing.
- Care Service can have default price and category.
- Care Service is not the same as a clinical result.

Frontend implications:

- Use Care Service for service selection and pricing.
- Do not store clinical findings only inside service rows.

### Pet Care Service

Pet Care Service is an instance of a care service for a specific pet/context.

Frontend implications:

- Useful for pending/upcoming pet services.
- Can be shown in pet profile or visit context depending on workflow.

### Visit Billable Items

Vet Visit uses child tables for:

- Prescribed medications.
- Requested services/procedures.
- Lab requests.

The backend can create a Sales Invoice from visit rows.

Frontend implications:

- Make it easy for doctors to add billable items during visit.
- Show item, quantity, rate, amount, and notes.
- Keep the invoice action explicit.

## Boarding System

Boarding is a room-based healthcare/clinic operation.

Main objects:

- Service Room
- Pet Boarding
- Pet Billable Item
- Pet Boarding Settings

### Service Room

Service Room is the physical room/unit/cage/boarding space.

Fields/concepts:

- Room code.
- Room name.
- Room type.
- Image.
- Active/Inactive status.
- Notes.

Strict rule:

- Service Room does not store occupancy.
- Service Room does not store price.
- Service Room must not have daily rate.

Frontend implications:

- Use standard CRUD for Service Room management.
- Do not build manual occupancy toggles on Service Room.
- Room cards should display derived occupancy from the boarding API.

### Pet Boarding

Pet Boarding is the reservation/stay record.

Important fields/concepts:

- Service room.
- Pet.
- Guardian.
- Resolved customer.
- Boarding type: Travel or Treatment.
- Record status.
- Timing: reserved at, check-in, check-out.
- Stay days.
- Billing status.
- Sales invoice.
- Billable items.
- Notes.

Record statuses:

- Reserved
- Checked In
- Checked Out
- Cancelled

Occupancy mapping:

- No active boarding: Available.
- Reserved boarding: Reserved.
- Checked In boarding: Occupied.
- Checked Out or Cancelled: history only, room is available again.

Frontend pages:

- Boarding Rooms: operational room board for reservation/check-in/check-out.
- Boarding Records: current and historical boarding list.

Frontend behavior:

- Boarding Rooms must use the boarding API, not standard Service Room list only.
- Empty room detail should still open so a user can reserve it.
- Reserve must send room, pet, guardian, optional check-in/check-out, note, and optional boarding type.
- Check-in only applies to Reserved records.
- Checkout only applies to Checked In records.
- Checkout creates invoice and finalizes the boarding record.

### Pet Billable Item

Generic billable child row.

Fields/concepts:

- Item name.
- Item code.
- Item type.
- Quantity.
- Rate.
- Amount.
- Status.
- Note.
- Linked service id.

Frontend implications:

- Amount is always quantity times rate.
- Let backend be source of truth for final totals.
- Use this pattern for boarding and possibly future visit/item tracking.

### Pet Boarding Settings

Settings for boarding billing.

Important concepts:

- Travel boarding item.
- Treatment boarding item.
- Default boarding type.

Frontend implications:

- Admin/settings UI should allow selecting the two item mappings.
- Checkout will fail if the required boarding item is not configured.

## Product, Stock, And Marketplace Area

The app includes a product/store module connected to ERPNext stock and selling.

### Product

Product is a custom catalog object projected into ERPNext Item/Item Price/Website Item.

Important concepts:

- Product name.
- SKU.
- Barcode.
- Description.
- Image.
- Price and discounted price.
- Category.
- Vendor.
- Status.
- Variant rows.
- Linked Item, Item Price, Website Item.

Frontend pages:

- Product catalog/list.
- Product create/edit.
- Product publish flow.
- Stock info panel.
- Restock action.

Important frontend rule:

- Do not assume Product itself is the only stock source.
- Live stock comes from ERPNext Bin/Stock Entry through backend helpers or standard ERPNext APIs.

### Warehouse And Stock

Warehouse and stock should use standard ERPNext REST APIs where documented.

Frontend implications:

- Warehouse lists must be paginated.
- Do not load all warehouses or bins at once.
- Group warehouses are folders and should not be selected for stock operations.
- Stock Entry changes stock only after submit.
- Bin is read-only current quantity.

### Sales Orders And Drivers

The app includes order placement and delivery/driver operations.

Important concepts:

- Guardian/customer identity is resolved for orders.
- Sales Order is used for marketplace/order flow.
- Drivers can be managed and linked to system users.
- Driver cash/accounting behavior exists in backend hooks.

Frontend pages:

- Orders list/detail.
- Create/place order.
- Driver management.
- Driver login/mobile workflow if needed.
- Driver balance view.

## Authentication And Profile

### Guardian Mobile Auth

Main concepts:

- Register guardian by phone/password/full name.
- Send/resend OTP.
- Verify OTP.
- Login.
- Complete profile.
- Change phone with OTP.
- Forgot/reset password.
- Logout.

Frontend implications:

- Phone number is central.
- OTP verification is part of onboarding.
- A guardian may exist before a linked Customer is created or synced.
- Profile completion should be treated as a separate step.

### Admin/User Management

The app includes APIs for users and role profiles.

Frontend implications:

- Admin users can browse users with role profiles.
- Role profile screens should be paginated and permission aware.

## Files And Images

The app supports file/image handling for pets and other doctypes.

Main concepts:

- Upload single or multiple files.
- Get pet images.
- Set default file.
- Delete multiple files.
- Sync user image from Guardian/Doctor image where applicable.

Frontend implications:

- Pet profile should support image gallery and default image.
- Upload UI should show progress, file type, file size, and failure messages.
- Avoid assuming one image only.

## Dashboard Area

Dashboard APIs cover:

- Order status counts.
- General statistics.
- Revenue report.
- Best seller.
- Profit and expenses.
- Orders by item group.

Frontend implications:

- Dashboard should be role-aware.
- Use cards/charts with loading states.
- Do not mix dashboard analytics into clinical visit screens.

## Suggested Frontend Navigation

Recommended top-level sections:

- Dashboard
- Pets
- Guardians
- Healthcare
- Boarding
- Services
- Products
- Orders
- Drivers
- Users and Settings

Healthcare subpages:

- Case Sheets
- Visits
- Doctors
- Services

Boarding subpages:

- Boarding Rooms
- Boarding Records
- Service Rooms
- Boarding Settings

Products/stock subpages:

- Products
- Categories
- Warehouses
- Stock Entries / Restock

## Important Data Relationship Rules

Use these rules throughout the frontend:

- Pet ownership/access is Pet to Guardian through PetGuardian.
- Guardian to billing Customer is through Guardian.customer_id.
- Visit belongs to a Case Sheet.
- Visit references pet and customer.
- Visit billable rows can produce a Sales Invoice.
- Care Service is a pricing/service catalog, not a clinical result.
- Service Room is physical room master data only.
- Boarding occupancy is derived from active Pet Boarding records only.
- Product is custom catalog data; Item/Bin/Stock Entry are ERPNext inventory data.

## API Areas For Frontend Agent

Custom method APIs:

- `pet_app.api.auth_mobile`
- `pet_app.api.auth_api`
- `pet_app.api.pet`
- `pet_app.api.care_service`
- `pet_app.api.product`
- `pet_app.api.order`
- `pet_app.api.sales`
- `pet_app.api.driver`
- `pet_app.api.users`
- `pet_app.api.dashboard`
- `pet_app.api.healthcare.boarding`
- `pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet`
- `pet_app.pet_app.doctype.vet_visit.vet_visit`

Standard Frappe resource APIs are also expected for normal CRUD doctypes:

- Pet
- Guardian
- PetGuardian
- Doctor
- Vet Case Sheet
- Vet Visit
- CareService
- CategoryCareServices
- PetCareService
- Product
- Product Variant
- Service Room
- Pet Boarding
- Pet Boarding Settings

Frappe response reminder:

- Custom method responses are usually wrapped by HTTP as `message`.
- Standard resource list responses are usually returned as `data`.

## Frontend Design Principles

Use a clinic operations mindset:

- Make lists dense, searchable, and paginated.
- Keep clinical screens focused and readable.
- Put patient context in headers: pet, owner, species/type, age, weight, status.
- Use clear status badges.
- Use action buttons only for valid transitions.
- Keep billing visible but secondary in clinical workflows.
- Use modals/drawers for quick actions when the user should not lose context.
- Avoid loading full tables client-side.
- Avoid building manual UI controls that contradict backend lifecycle rules.

## MVP Priorities For Frontend

Build these first:

- Guardian login/register/profile flow.
- Pet list and pet profile.
- Case Sheet list/detail/start visit.
- Visit detail with clinical sections and invoice action.
- Care Service selector in visit.
- Boarding Rooms page.
- Boarding Records page.
- Product list/detail and stock info.
- Basic dashboard cards.

Then expand:

- Advanced owner/pet approval views.
- Rich pet medical timeline.
- Lab and imaging dedicated modules.
- Product publishing/restock administration.
- Driver operations and delivery tracking.

## Known Backend-Side Constraints To Respect

- Some features use standard Frappe resource CRUD, not custom methods.
- Some workflows depend on server-side validation and should not be simulated only on frontend.
- Existing APIs may have mixed naming conventions because the app evolved over time.
- Boarding APIs intentionally accept mixed frontend key names like `roomId`, `petId`, `guardianId`, `checkIn`, `checkOut`.
- The frontend should be tolerant of nullable legacy data in older records.

## Related Docs

- `docs/boarding-frontend.md`
- `docs/pet-guardian-linking-flow-v1.md`
- `docs/product-api-docs.md`
- `docs/warehouse-stock-frontend-readme.md`
- `docs/README_API.md`
- `AGENTS.md`
