from __future__ import annotations

import frappe

DRIVER_ROLE = "Driver"


def _ensure_role():
    """Create the Driver role.

    Every append of {"role": "Driver"} in pet_app.api.driver failed link validation
    because this row never existed, which is why no driver has ever been provisioned.

    desk_access = 0 is the point, not an afterthought: drivers are API clients. Frappe
    derives User.user_type from whether ANY held role has desk access, so a role with
    desk_access = 0 keeps every driver a Website User and out of /app.
    """
    if frappe.db.exists("Role", DRIVER_ROLE):
        # Never silently widen an existing role, but do report a wrong value.
        if frappe.db.get_value("Role", DRIVER_ROLE, "desk_access"):
            frappe.log_error(
                title="DRIVER_ROLE_HAS_DESK_ACCESS",
                message="Role 'Driver' exists with desk_access=1; drivers must be API-only.",
            )
        return

    role = frappe.new_doc("Role")
    role.role_name = DRIVER_ROLE
    role.desk_access = 0
    role.disabled = 0
    role.flags.ignore_permissions = True
    role.insert(ignore_permissions=True)


def execute():
    """Driver role, required for every pet_app.api.driver provisioning call.

    Every append of {"role": "Driver"} in pet_app.api.driver failed link validation
    until this ran, because the role never existed.

    custom_username, custom_email and custom_cash_account are fixture-owned
    (fixtures/custom_field.json, allow-listed in hooks.py) - not this patch's concern.
    """
    _ensure_role()
    frappe.db.commit()
