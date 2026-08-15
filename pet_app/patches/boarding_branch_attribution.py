from __future__ import annotations

import frappe

# Boarding revenue belongs to the boarding facility, not to whoever processes the stay.
# Pet Boarding carried no branch, so invoice_reuse.resolve_branch() fell through to the
# acting user's branch and a hotel stay checked out by a clinic coordinator was posted to
# the clinic's invoice. branch is a live accounting dimension, so that reached the ledger.
#
# Attribution only. Pet Boarding stays OUT of branch.SCOPED_DOCTYPES so the 37 Service
# Rooms remain one shared pool and every clinic can still see and check out every stay.

SETTINGS = "Pet App Accounting Settings"  # unused; kept for grep symmetry with siblings
BOARDING_SETTINGS = "Pet Boarding Settings"
FIELD = "boarding_branch"


def _pick_boarding_branch() -> str | None:
    """Seed the setting only when the answer is unambiguous.

    Exactly two Branches exist here - the clinic ("main", which carries every visit and
    every invoice) and the boarding facility. If the shape is anything other than that,
    leave it blank: checkout throws with an actionable message, which is safer than a
    guess that silently misstates revenue.
    """
    branches = frappe.get_all("Branch", pluck="name")
    if len(branches) != 2:
        return None
    others = [b for b in branches if b != "main"]
    return others[0] if len(others) == 1 else None


def execute():
    if not frappe.db.exists("DocType", BOARDING_SETTINGS):
        return

    frappe.reload_doc("pet_app", "doctype", "pet_boarding_settings")

    if not frappe.db.get_single_value(BOARDING_SETTINGS, FIELD):
        picked = _pick_boarding_branch()
        if picked:
            frappe.db.set_single_value(BOARDING_SETTINGS, FIELD, picked)

    # The stamp is for attribution and must never filter what a clinic can see. The
    # field links to Branch, and Branch User Permissions exist with
    # apply_to_all_doctypes = 1, so without this a stay stamped "hotel" could disappear
    # from a clinic user's list - exactly the shared-pool breakage the SCOPED_DOCTYPES
    # comment warns about.
    if frappe.db.exists("Custom Field", {"dt": "Pet Boarding", "fieldname": "branch"}):
        frappe.make_property_setter(
            {
                "doctype": "Pet Boarding",
                "fieldname": "branch",
                "property": "ignore_user_permissions",
                "value": "1",
                "property_type": "Check",
            },
            is_system_generated=False,
        )

    # Existing stays keep their (null) branch: no retroactive re-attribution.
    frappe.clear_cache(doctype="Pet Boarding")
    frappe.db.commit()
