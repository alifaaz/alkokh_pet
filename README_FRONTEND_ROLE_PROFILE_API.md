# Frontend Roles and Role Profiles

## Overview

Frontend `Permission` means backend `Role Profile`.

`Role Profile` is the frontend permission bundle.

`Role` is the backend permission unit used by Frappe DocPerm.

Each user should have one selected `Role Profile`.

Each `Role Profile` can contain any combination of approved Frappe roles.

Flow:

`User -> Role Profile -> Roles -> Frappe DocPerm + User Permission`

Vue page/module/action visibility is resolved through:

- Frappe `Role`
- Frappe `Role Profile`
- Frappe DocPerm / Role Permission Manager
- Frappe `User Permission`
- `pet_app.api.permissions.get_current_access`

`Pet App Permission Rule` is deprecated and must not be used by the frontend as an access source.

DocType CRUD security comes from normal Frappe permissions (`Role Permission Manager`, `DocPerm`, `User Permission`, and backend `frappe.has_permission` checks). `get_current_access` only returns a Vue compatibility snapshot for navigation and DocType permission reflection.

## Module Roles

Use these backend `Role` records as the frontend module roles:

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

These are flexible building blocks.

Examples:

- `Admin`
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

- `Clinic`
  - `Healthcare`
  - `Pet`
  - `Guardians`

- `Accounting + POS`
  - `Accounting`
  - `POS`

## 1. Get Module Roles

Use this endpoint to fetch the frontend module roles:

```http
GET {{url}}/api/resource/Role?filters=[["name","in",["Setting","Guardians","Users","Order","E-commerce","Pet","Healthcare","POS","Accounting","Warehouse","Audit"]]]&fields=["name"]&limit_page_length=999
```

Example response:

```json
{
  "data": [
    { "name": "Accounting" },
    { "name": "Audit" },
    { "name": "E-commerce" },
    { "name": "Guardians" },
    { "name": "Order" },
    { "name": "POS" },
    { "name": "Pet" },
    { "name": "Setting" },
    { "name": "Users" },
    { "name": "Healthcare" },
    { "name": "Warehouse" }
  ]
}
```

## 2. Get Current User Roles

Use token auth:

```http
GET {{url}}/api/method/frappe.core.doctype.user.user.get_roles
```

This returns the roles resolved for the current logged-in user.

## 3. Get Users With Role Profile

Use this to search users and read their assigned `Role Profile`:

```http
GET {{url}}/api/method/pet_app.api.users.get_users_with_role_profile?filters=[["full_name","like","%ali%"]]&limit_start=0&limit_page_length=10
```

## 4. Update User With Role Profile

Assign or replace the user's selected `Role Profile`:

```http
PUT {{url}}/api/resource/User/yuosifraed@gmail.com
```

Example payload:

```json
{
  "role_profiles": [
    { "role_profile": "Modeer" }
  ]
}
```

## 5. Get Role Profiles

List all existing `Role Profile` records:

```http
GET {{url}}/api/resource/Role%20Profile?fields=["name"]&limit_page_length=999
```

## 6. Create Role Profile

Frontend creates custom permission bundles by creating `Role Profile` records:

```http
POST {{url}}/api/resource/Role%20Profile
```

Example payload:

```json
{
  "role_profile": "مدير اضافة المنتجات",
  "roles": [
    { "role": "E-commerce" },
    { "role": "Order" },
    { "role": "Setting" }
  ]
}
```

Admin-like example:

```json
{
  "role_profile": "Modeer",
  "roles": [
    { "role": "Healthcare" },
    { "role": "Pet" },
    { "role": "E-commerce" },
    { "role": "Order" },
    { "role": "POS" },
    { "role": "Accounting" },
    { "role": "Warehouse" },
    { "role": "Audit" },
    { "role": "Users" },
    { "role": "Guardians" },
    { "role": "Setting" }
  ]
}
```

## 7. Delete Role Profile

```http
DELETE {{url}}/api/resource/Role%20Profile/Management
```

## 8. Add or Delete Roles Inside Role Profile

Replace the roles inside a profile:

```http
PUT {{url}}/api/resource/Role%20Profile/Management
```

Example payload:

```json
{
  "roles": [
    { "role": "Healthcare" },
    { "role": "Pet" },
    { "role": "Order" }
  ]
}
```

Another example:

```json
{
  "roles": [
    { "role": "E-commerce" },
    { "role": "Order" },
    { "role": "Setting" }
  ]
}
```

## 9. Get Role Profiles With Resolved Roles

Use this endpoint to fetch profiles and the roles inside each profile:

```http
GET {{url}}/api/method/pet_app.api.users.get_all_role_profiles_with_roles?limit_page_length=999
```

## Updated Response Shape

This endpoint was cleaned.

It now returns only:

```json
{
  "data": [
    {
      "name": "Administrator",
      "role_profile": "Accounting Profile",
      "creation": "2026-04-25 18:25:49.141478",
      "roles": [
        "Accounting"
      ]
    },
    {
      "name": "Administrator",
      "role_profile": "Modeer",
      "creation": "2026-02-04 20:10:57.502487",
      "roles": [
        "Accounting",
        "Audit",
        "E-commerce",
        "Guardians",
        "Order",
        "POS",
        "Pet",
        "Setting",
        "Users",
        "Healthcare",
        "Warehouse"
      ]
    }
  ]
}
```

It does not return duplicated `message.data` anymore.

## Frontend Rules

- Frontend permission selector = backend `Role Profile`
- Frontend should create and manage `Role Profile`
- Each `Role Profile` can contain any combination of module roles
- Do not restrict profiles to one module
- Use module roles as building blocks
- Backend enforces actual access through Frappe DocPerm, User Permission, and API checks
- Vue route access comes from `pet_app.api.permissions.get_current_access`
- Vue management controls should use `doctypes[doctype]` (`read`, `create`, `write`, etc.)
- `Pet App Permission Rule` rows do not grant access
- `Pet App Admin` is a normal Frappe role and no longer bypasses permissions
- Only `Administrator` is reported as `fullAccess: true`

## Recommended Frontend Flow

1. Fetch available module roles from `Role`
2. Create a custom `Role Profile`
3. Add any combination of module roles into that profile
4. Assign one or more workflow rules to the `Role Profile` through `Pet App Role Profile Workflow Rule`
5. Assign one `Role Profile` to the user
6. Use Frappe Role Permission Manager / DocPerm for real DocType CRUD access
7. Use Frappe `User Permission` for Warehouse, POS Profile, Healthcare Practitioner, and Branch restrictions
8. Use `get_current_access` for runtime module/page visibility and `doctypes`-based controls

## Dynamic Page Scope

`Pet App Role Profile Workflow Rule` narrows frontend navigation for a profile.

The backend calculates visible resources as:

```text
Role Profile workflow pages/modules ∩ Frappe DocPerm permissions
```

That means workflow rules can hide pages, but they cannot grant access. A page only appears when it is inside the profile's workflow scope and the user still has the required DocType permission.

For a Visits-only profile:

- Create or reuse `Role Profile`: `Visit User`
- Add a narrow role such as `Visit Read` or `Visit Admin`
- Add `Pet App Role Profile Workflow Rule`:
  - `role_profile`: `Visit User`
  - `workflow_rule`: `Healthcare.ClinicalVisits`

Then `get_current_access` can include `page.healthcare.visits` without including standalone pages like `page.healthcare.labs`, even when `Lab` read permission exists as a visit dependency.

Avoid using the broad legacy `Visit` role for a truly Visits-only user because it grants wider DocType permissions than page visibility alone can safely remove.

## Vue Access Snapshot

Load current user access:

```http
GET {{url}}/api/method/pet_app.api.permissions.get_current_access
```

`get_current_access` keeps the Vue response shape:

```json
{
  "roles": ["Desk User", "Accounts User"],
  "roleProfile": "Accountant",
  "fullAccess": false,
  "fullAccessRoles": ["Administrator"],
  "modules": ["module.accounting"],
  "pages": ["page.accounting.sales_invoices"],
  "actions": [],
  "doctypes": {
    "Sales Invoice": ["read", "create"]
  },
  "restrictions": {
    "warehouse": [],
    "cashier_profile": [],
    "practitioner": [],
    "branch": [],
    "doctor": []
  },
  "loadedAt": "2026-05-11T00:00:00Z"
}
```

`modules` and `pages` are derived from Frappe DocPerm checks. `actions` is intentionally empty; use `doctypes` permissions for UI controls.

## Deprecated App Resource APIs

Admin access matrix:

```http
GET {{url}}/api/method/pet_app.api.permissions.get_access_matrix
```

This endpoint is now read-only compatibility output and includes:

```json
{
  "deprecated": true,
  "readOnly": true
}
```

Do not build a new frontend editor around this matrix.

Save app resource status and roles:

```http
POST {{url}}/api/method/pet_app.api.permissions.update_access_matrix
```

This endpoint is deprecated and is now a no-op. It does not create, update, grant, or revoke access.

Register Vue resources discovered by the frontend:

```http
POST {{url}}/api/method/pet_app.api.permissions.register_frontend_resources
```

This endpoint is deprecated and is now a no-op. Vue resources are recognized by the backend static access manifest.

## User Permissions

Use Frappe `User Permission` for record restrictions:

```json
{
  "user": "cashier@example.com",
  "allow": "POS Profile",
  "for_value": "Main POS",
  "apply_to_all_doctypes": 1
}
```

Supported restriction mappings:

- `warehouse` -> `Warehouse`
- `cashier_profile` -> `POS Profile`
- `practitioner` / `doctor` -> `Healthcare Practitioner`
- `branch` -> `Branch`

The backend still exposes:

```http
GET  /api/method/pet_app.api.permissions.get_user_restrictions
POST /api/method/pet_app.api.permissions.update_user_restrictions
```

These now read and write Frappe `User Permission` rows.
