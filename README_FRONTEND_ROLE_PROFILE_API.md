# Frontend Roles and Role Profiles

## Overview

Frontend `Permission` means backend `Role Profile`.

`Role Profile` is the frontend permission bundle.

`Role` is the backend module access unit.

Each user should have one selected `Role Profile`.

Each `Role Profile` can contain any combination of module roles.

Flow:

`User -> Role Profile -> Roles -> Permissions + Modules`

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
- Backend enforces actual access through role permissions and API checks

## Recommended Frontend Flow

1. Fetch available module roles from `Role`
2. Create a custom `Role Profile`
3. Add any combination of module roles into that profile
4. Assign one `Role Profile` to the user
5. Use `get_roles` and `get_users_with_role_profile` for runtime UI decisions
