# pet_app — Pet & Guardian Flow (feature/pet-guardian)

This branch contains the core **Pet / Guardian** data model and linking flow used by the clinic/admin system.

## What changed in this branch
- Updated **Pet** DocType (including linkage readiness for clinic operations).
- Updated **Guardian** DocType.
- Added/updated relationship DocTypes:
  - **PetGuardian**: links a Guardian to one or more Pets.
  - **PetAddRequest**: request + approval flow to register a pet under a guardian.
- API / business logic updates related to Pet & Guardian flows:
  - `pet_app/api/auth_mobile.py`
  - `pet_app/api/pet.py`

## Why this exists
- Keep **Pet** as the primary entity for identity (name, image, status, etc.).
- Keep **Guardian** as the owner/customer-side entity.
- Provide a reliable relationship layer (**PetGuardian**) for fetching pets by guardian and enforcing ownership rules.

## Quick verification checklist
1. Create a **Guardian**.
2. Create a **PetAddRequest** and approve it (based on your current approval workflow).
3. Confirm:
   - A **Pet** record exists and is linked properly to the Guardian via **PetGuardian**.
4. Fetch pets for a guardian using Resource API on **PetGuardian** (filters by guardian_id) and verify results.

## Notes
- Do not commit site files (`sites/`), configs (`site_config.json`), logs, or private files.
- If you created Custom Fields via UI, export them (see below) and commit the generated files.

## Export UI Customizations (recommended)
From bench:
```bash
cd ~/frappe-bench
bench --site YOUR_SITE export-customizations
bench --site YOUR_SITE export-fixtures
