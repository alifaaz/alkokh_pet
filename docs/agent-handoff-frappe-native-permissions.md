# Agent Handoff: Frappe-Native Permissions Refactor

This handoff documents the permission refactor from the old hybrid app-resource matrix to normal Frappe permissions.

## Current Architecture

Frappe is the only backend security authority.

Use:

- `Role`
- `Role Profile`
- Role Permission Manager / `DocPerm`
- `User Permission`
- explicit backend checks such as `frappe.has_permission(...)` and `require_doctype_permission(...)`

Do not use `Pet App Permission Rule` or `Pet App User Restriction` for security decisions. They may still exist in old sites for data preservation, but they are deprecated and ignored for access enforcement.

## Vue Compatibility Snapshot

Vue should keep calling:

```text
GET /api/method/pet_app.api.permissions.get_current_access
```

The response shape is preserved:

```json
{
  "roles": [],
  "roleProfile": null,
  "modules": [],
  "pages": [],
  "actions": [],
  "doctypes": {},
  "restrictions": {
    "warehouse": [],
    "cashier_profile": [],
    "practitioner": [],
    "branch": [],
    "doctor": []
  },
  "loadedAt": "..."
}
```

Important: `modules` and `pages` are only for Vue sidebar/routes. They are derived from Page Access settings, not from table read permission. `actions` is kept as an empty compatibility field; button and management controls should use `doctypes[doctype]` permissions instead.

Current page visibility comes from `Pet App Access Settings`:

- Frontend syncs its `page.*` catalog through `pet_app.api.permissions.sync_frontend_pages`.
- Admins update enabled state and assigned Frappe roles through `update_page_access_settings`.
- `get_current_access().pages` contains enabled page keys assigned to the current user's roles.
- `get_current_access().modules` is derived from those allowed page rows.
- Full page-visibility roles are `Administrator`, `System Manager`, and `Pet App Admin`.
- This registry does not grant data access; Frappe DocPerms and backend permission checks still control records and actions.

## Deprecated Matrix APIs

These endpoints remain only for compatibility and must not be used as an editable security source:

```text
pet_app.api.permissions.get_access_matrix
pet_app.api.permissions.update_access_matrix
pet_app.api.permissions.register_frontend_resources
```

They return a deprecated/read-only response. Frontend permission management should use normal Frappe `Role Profile`, `Role`, and `User Permission` records.

## Record Restrictions

Use Frappe `User Permission`.

Mappings:

| Vue key | Frappe allow DocType |
| --- | --- |
| `warehouse` | `Warehouse` |
| `cashier_profile` | `POS Profile` |
| `doctor` | `Healthcare Practitioner` |
| `practitioner` | `Healthcare Practitioner` |
| `branch` | `Branch` |

`doctor` is kept as a frontend alias for `practitioner`.

The compatibility APIs are:

```text
GET  /api/method/pet_app.api.permissions.get_user_restrictions
POST /api/method/pet_app.api.permissions.update_user_restrictions
```

They read/write standard `User Permission` rows.

## Backend Enforcement Rules

For new or changed APIs:

- Use `require_doctype_permission("<DocType>", "<ptype>")` before writes that later use `ignore_permissions=True`.
- Use `require_restriction_value(...)` for selected warehouse, POS profile, practitioner, or branch values.
- Use `filter_restricted_values(...)` for lists returned to the frontend.
- Never add `require_app_permission(...)` to business APIs. It remains only as a deprecated compatibility helper in `pet_app.api.permissions`.
- Never read `Pet App Permission Rule` to grant access.
- Never read `Pet App User Restriction`; use `User Permission`.

Examples:

```py
require_doctype_permission("Sales Invoice", "create")
require_doctype_permission("Sales Invoice", "submit")
require_restriction_value("warehouse", warehouse)
```

## Frontend Guidance

The frontend role/permission UI should manage Frappe-native records:

- Create/update `Role Profile`.
- Assign allowed `Role` rows to a `Role Profile`.
- Assign page visibility with Page Access settings; these narrow Vue pages/modules but never grant data access.
- Assign a `Role Profile` to `User`.
- Manage `User Permission` rows for warehouse, POS profile, practitioner, and branch restrictions.

The frontend should not expose or edit:

- `Pet App Permission Rule`
- `Pet App User Restriction`
- low-level `DocPerm` unless building an admin-only developer tool

## Verification Commands

Use these checks after permission work:

```bash
rg -n "require_app_permission|Pet App Permission Rule|Pet App User Restriction" pet_app --glob '*.py' --glob '!pet_app/tests/*.py'
python -m py_compile pet_app/api/permissions.py
bench --site frappe.localhost run-tests --app pet_app --module pet_app.tests.test_hybrid_permissions
```

Expected grep result:

- `require_app_permission` appears only as the deprecated helper in `pet_app/api/permissions.py`.
- `Pet App Permission Rule` and `Pet App User Restriction` should not appear in production Python enforcement code.
