# pet_app — Care Services & Pricing Sync (feature/care-services)

This branch contains the **CareService** catalog and per-pet service tracking, with pricing aligned to ERPNext accounting concepts.

## What changed in this branch
- Updated **CareService** DocType:
  - Stores service metadata (category/species/frequency, etc.).
  - Stores **default_price** and price list selection (as configured in your system).
- Updated **PetCareService** DocType:
  - Tracks services assigned to a specific pet.
  - Supports statuses such as: `Pending`, `Completed`, `Overdue`.
- Pricing integration direction:
  - **CareService default price** is the source used to set pricing for accounting/invoicing (via Item/Item Price workflow in your implementation).

## Why this exists
- Keep **CareService** as the clinic’s “service catalog” (easy to manage).
- Keep **PetCareService** as the per-pet operational tracker (what is due / completed).
- Enable clean billing later (invoice rate can be overridden per visit without changing the default catalog price).

## Quick verification checklist
1. Create a **CareService** (e.g., “General Examination”) with a default price.
2. Assign it to a pet via **PetCareService**.
3. Confirm:
   - Service appears under the pet with correct status and due date behavior.
4. Verify that the default price is available for the UI to prefill rates during visit/invoice creation.

## Notes
- If you depend on Server Scripts for automation (e.g., syncing Item/Item Price),
  ensure your bench allows server scripts (v15+ disables them by default).
- Prefer implementing core automation inside the app code when possible for stability.

## Export UI Customizations (recommended)
From bench:
```bash
cd ~/frappe-bench
bench --site YOUR_SITE export-customizations
bench --site YOUR_SITE export-fixtures
