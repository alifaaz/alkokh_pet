from __future__ import annotations

import frappe
from frappe.utils import cstr

# Seed CareService template.performing_branch from the free-text service_area that the
# catalogue already carries.
#
# service_area was never read by anything - the seeder wrote it, the guardian portal
# serialises it, nothing acts on it - but it is the only place on this site that records
# where a service is physically performed, and it is right: "Hotel" on X-Ray, Contrast
# X-Ray, Regular Boarding and Treatment Boarding; "1F" on the five lab tests; "GF" on
# Grooming and Bathing.
#
# Only values that resolve to a real Branch are used, matched case-insensitively because
# the strings say "Hotel" while the Branch is named "hotel". "1F" and "GF" are floors of a
# building, not branches, and no Branch of either name exists - so they are LEFT BLANK
# rather than assumed to mean "main". Blank is the safe answer: it means "performed
# wherever it was ordered", which is exactly the behaviour those services have today.
#
# Existing orders are NOT backfilled. performing_branch is a snapshot of where the work
# happened, and stamping today's catalogue onto work already done would be the retroactive
# re-attribution that snapshot_performing_branch exists to prevent. The 106 Imaging records
# already on this site keep a blank performing_branch and keep billing exactly as before.

TEMPLATE = "CareService template"
FIELD = "performing_branch"


def _branches_by_lowercase_name() -> dict[str, str]:
    return {cstr(name).strip().lower(): name for name in frappe.get_all("Branch", pluck="name")}


def execute():
    if not frappe.db.exists("DocType", TEMPLATE):
        return

    frappe.reload_doc("pet_app", "doctype", "careservice_template")
    frappe.reload_doc("pet_app", "doctype", "procedure_template")
    for slug in ("lab", "imaging", "pet_procedure", "petcareservice"):
        frappe.reload_doc("pet_app", "doctype", slug)

    branches = _branches_by_lowercase_name()
    if not branches:
        return

    mapped, unmapped = [], []
    for row in frappe.get_all(TEMPLATE, fields=["name", "service_name", "service_area", FIELD]):
        if cstr(row.get(FIELD)).strip():
            # Never overwrite an answer somebody has already given by hand.
            continue
        key = cstr(row.get("service_area")).strip().lower()
        branch = branches.get(key)
        if not branch:
            if key:
                unmapped.append(f"{row.name} ({row.service_name}): service_area={row.service_area!r}")
            continue
        frappe.db.set_value(TEMPLATE, row.name, FIELD, branch, update_modified=False)
        mapped.append(f"{row.name} ({row.service_name}) -> {branch}")

    if mapped:
        frappe.logger("pet_app.branch").info({"event": "PERFORMING_BRANCH_SEEDED", "mapped": mapped})
    if unmapped:
        # Not an error. These are locations the site knows about that are not Branches;
        # somebody has to decide whether they ever become one.
        frappe.logger("pet_app.branch").info({"event": "PERFORMING_BRANCH_UNMAPPED", "rows": unmapped})

    frappe.clear_cache(doctype=TEMPLATE)
    frappe.db.commit()
