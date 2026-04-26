# pet_app

## Project Overview

`pet_app` is a production Frappe / ERPNext backend for a veterinary operations and commerce platform.

It combines:

- Veterinary workflow
- Pet registry
- E-commerce
- Order management
- POS
- Accounting
- Warehouse
- Audit
- User and guardian management

The system is built on:

- Frappe
- ERPNext

The frontend controls module visibility. The backend enforces real permissions and security.

## Architecture

Permission flow:

`User -> Role Profile -> Roles -> Permissions`

Meaning:

- `Role Profile` = frontend permission bundle
- `Roles` = backend enforcement
- `Roles` represent modules
- `User` should have one selected `Role Profile`

Core rule:

- frontend selects the `Role Profile`
- backend checks the resolved `Roles`
- `Role Profile` is only a label or group
- `Roles` are the real authority

## Module Roles

Final module roles:

- `Healthcare`
- `Pet`
- `E-commerce`
- `Order`
- `POS`
- `Accounting`
- `Warehouse`
- `Audit`
- `Users`
- `Guardians`
- `Setting`

Notes:

- `Healthcare` is the module role for clinic and visit workflows
- each role controls one backend module
- roles are reusable building blocks
- backend APIs and DocType permissions are mapped to these roles

## Role Profile System

`Role Profile` is a flexible combination of module roles.

Rules:

- a user should have one `Role Profile`
- a profile can contain any combination of module roles
- profiles are not limited to one module
- profile names are business labels only

Examples:

### Admin

- `Healthcare`
- `Pet`
- `E-commerce`
- `Order`
- `POS`
- `Accounting`
- `Warehouse`
- `Audit`
- `Users`
- `Guardians`
- `Setting`

### Clinic

- `Healthcare`
- `Pet`
- `Guardians`

### Finance

- `Accounting`
- `POS`

This model supports profiles such as:

- `Admin`
- `Clinic`
- `Finance`
- `Accounting + POS`
- any custom mix required by business

## Core Backend Flows

### Vet Flow

Flow:

`Vet Case Sheet -> Vet Visit -> Lab / Imaging / CareService template -> billable items -> Sales Invoice`

Rules:

- billing comes only from visit `billable_items`
- invoice creation is controlled
- direct random billing is blocked
- visit billing is audited and logged

Main objects:

- `Vet Case Sheet`
- `Vet Visit`
- `Lab`
- `Imaging`
- `CareService template`
- `CategoryCareServices`
- `Service Room`
- `Pet Boarding`

### Doctor System

Doctor model:

- `Doctor` is linked to one `User`
- `1 Doctor = 1 User`
- one user cannot be linked to multiple doctors

Behavior:

- Doctor is manageable through standard Frappe REST
- if `user` is missing, backend auto-creates a `User`
- system assigns:
  - `Doctor`
  - `Healthcare`
- linked user is synced on update
- deleting doctor does not delete user
- linked user is disabled instead

Standard REST:

- `POST /api/resource/Doctor`
- `GET /api/resource/Doctor`
- `PUT /api/resource/Doctor/{name}`
- `DELETE /api/resource/Doctor/{name}`

### Billing System

Billing rules:

- `Sales Invoice` is created only from controlled billing flows
- direct invoice creation is blocked
- billing endpoints are role-protected
- duplicate billing is blocked
- audit comments and logs exist for sensitive billing actions

Protected flows include:

- visit invoice creation
- boarding checkout billing
- guardian invoice creation
- payment-related accounting actions

### Order System

Order flow uses:

- `place_order`
- product pricing logic
- coupon handling
- safe totals calculation

Security behavior:

- role-protected access
- controlled pricing flow
- coupon usage hardened against concurrency abuse
- rate limiting on order entry points

## Security Model

The backend uses strict role-based security.

Current model:

- no sensitive custom API is open without role checks
- document permission checks are enforced
- direct public misuse is restricted
- financial operations are protected
- guardian data is isolated

Security measures applied:

- role-based access checks
- document-level permission checks
- protected billing and invoice flows
- rate limiting on sensitive endpoints
- safer input validation
- audit logging for sensitive actions
- guardian self/other-guardian isolation
- pet access scoping
- dashboard access protection

Public guest exposure is limited to authentication flows only.

## Permission System (Final State)

Final state goals:

- no intentional over-permission in custom module roles
- no known missing permission in audited custom app flows
- backend role checks aligned to the final module-role model
- standard profiles normalized to final module role names

Standard profiles:

- `Healthcare Profile` -> `Healthcare`, `Doctor`
- `Pet Profile` -> `Pet`
- `Ecommerce Profile` -> `E-commerce`
- `Order Profile` -> `Order`
- `POS Profile` -> `POS`
- `Accounting Profile` -> `Accounting`
- `Warehouse Profile` -> `Warehouse`
- `Audit Profile` -> `Audit`
- `Users Profile` -> `Users`
- `Guardians Profile` -> `Guardians`, `Guardian`
- `Settings Profile` -> `Setting`

Notes:

- `Doctor` and `Guardian` remain business roles
- module control is based on the final module roles
- legacy old role names were normalized for managed profiles

## Permission Audit

Live permission verification exists in:

- `pet_app/utils/live_permission_audit.py`

Purpose:

- create test users
- assign one profile per user
- validate backend behavior on a live site

It checks:

- DocType access
- API access
- allow and deny rules
- guardian data isolation
- pet isolation
- profile-to-role correctness

Run:

```bash
bench execute pet_app.utils.live_permission_audit.run
```

Use this after permission changes to confirm the live site still matches the intended security model.

## API Design Rules

System rules:

- no API per permission
- no role per page
- roles map to modules
- profiles combine roles
- APIs are secured by roles
- frontend decides visibility
- backend decides access

Implications:

- frontend should use `Role Profile` for UI and module visibility
- backend must never trust frontend visibility alone
- APIs must always enforce role and permission checks

## Setup and Run

Use after code or permission updates:

```bash
bench migrate
bench clear-cache
bench restart
```

Recommended after permission or patch updates:

```bash
bench --site your-site execute pet_app.patches.role_profile_access_setup.execute
bench clear-cache
bench restart
```

## Key Notes

- frontend controls UI visibility
- backend controls security
- `Role Profile` names are labels only
- `Roles` are the real authority
- module roles are the stable permission building blocks
- business roles like `Doctor` and `Guardian` remain separate where needed
- invoice and financial flows must go through controlled backend logic
- live permission audit should be rerun after permission changes

## Frontend Integration Summary

Frontend should:

1. fetch available module roles
2. create custom `Role Profile`
3. assign any combination of module roles
4. assign one `Role Profile` to the user
5. use role and profile data only for visibility
6. rely on backend for real authorization

Frontend should not:

- create one role per page
- assume UI visibility equals access
- bypass controlled billing or accounting flows
- treat `Role Profile` as the real permission engine

## Final Principle

The system is built around this model:

`User -> Role Profile -> Roles -> Permissions`

Frontend chooses what to show.

Backend decides what is allowed.
