from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

DRIVER_ROLE = "Driver"

FIELDS = (
    {
        "fieldname": "custom_username",
        "label": "Username",
        "fieldtype": "Data",
        "insert_after": "cell_number",
        "description": "Login username for the driver app. Must be unique across Drivers and Users.",
    },
    {
        "fieldname": "custom_email",
        "label": "Contact Email",
        "fieldtype": "Data",
        "options": "Email",
        "insert_after": "custom_username",
        "description": "Optional real email. The login User gets a generated system address instead.",
    },
    {
        "fieldname": "custom_cash_account",
        "label": "Cash Account",
        "fieldtype": "Link",
        "options": "Account",
        "insert_after": "custom_email",
        "reqd": 1,
        "description": (
            "Ledger account this driver's collected cash is held against. "
            "Chosen by staff - never derived. A driver without one cannot handle money."
        ),
    },
)


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
    """Driver role + the three custom fields driver.py has always read but never had.

    create_driver threw during validation and driver_login threw before returning,
    because custom_username, custom_email and custom_cash_account are read all over
    pet_app.api.driver and exist nowhere on the doctype.
    """
    _ensure_role()

    created = False
    for definition in FIELDS:
        if frappe.db.exists("Custom Field", {"dt": "Driver", "fieldname": definition["fieldname"]}):
            continue
        create_custom_field("Driver", {**definition, "module": "Pet App", "translatable": 0})
        created = True

    if created:
        frappe.clear_cache(doctype="Driver")

    frappe.db.commit()
