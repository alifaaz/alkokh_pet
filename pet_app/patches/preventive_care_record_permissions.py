"""Give `Preventive Care Record` the role grants its predecessors held.

WHY IT NEEDS ONE. The doctype was created with the permission block `Lab`'s JSON carries -
System Manager only. That is fine for a doctype nothing reaches yet and wrong the moment the
frontend does: every doctor, coordinator and receptionist would get `Not permitted` on a
worklist they are supposed to own.

THE GRANTS ARE COPIED, NOT INVENTED. Three sources, unioned, and nothing beyond them:

    `Pet Vaccination Record`   Doctor, Pet App Admin  (read/write/create, no delete)
    `Pet Deworming Record`     the same two
    `PetCareService`           every role that could read, write or create a care service,
                               because preventive care WAS a care service and the same people
                               do the same work

A role's flags are the strongest it held on any source: a role that could create a
PetCareService can create a dose, and one that could only read keeps only read. `delete` is
the exception - it is NOT copied from anything. Neither record doctype granted it, and a
deletable clinical record is how history quietly disappears; System Manager keeps it from the
doctype's own JSON and that is enough.

IT RUNS BEFORE THE PURGE, which deletes the superseded doctypes' permission rows. Reading
them afterwards would find nothing and silently grant only what PetCareService had - which
would leave Doctor with read and no write, because `PetCareService.Doctor` is read-only while
`Pet Vaccination Record.Doctor` was not.

IDEMPOTENT. Existing rows are updated in place rather than duplicated, so a re-run cannot
produce two grants for one role.
"""

from __future__ import annotations

import frappe

DOCTYPE = "Preventive Care Record"
SOURCES = ("Pet Vaccination Record", "Pet Deworming Record", "PetCareService")
# Copied flags. `delete` is deliberately absent - see the module docstring.
FLAGS = ("read", "write", "create", "report", "export", "print", "email", "share")


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		frappe.log_error(
			title="PREVENTIVE_CARE_PERMISSIONS_NO_DOCTYPE",
			message=f"{DOCTYPE} does not exist; run preventive_care_record_schema first.",
		)
		return

	granted: dict[str, dict] = {}
	for source in SOURCES:
		if not frappe.db.exists("DocType", source):
			continue
		for row in frappe.get_all(
			"Custom DocPerm",
			filters={"parent": source, "permlevel": 0},
			fields=["role", *FLAGS],
			ignore_permissions=True,
		):
			target = granted.setdefault(row.role, {flag: 0 for flag in FLAGS})
			for flag in FLAGS:
				# Strongest wins: a role that could create on any source can create here.
				target[flag] = max(target[flag], int(row.get(flag) or 0))

	# A role that ends up with nothing grants nothing; skip it rather than writing an empty row.
	granted = {role: flags for role, flags in granted.items() if any(flags.values())}
	if not granted:
		frappe.log_error(
			title="PREVENTIVE_CARE_PERMISSIONS_NO_SOURCES",
			message=(
				f"No Custom DocPerm rows found on any of {SOURCES}. {DOCTYPE} keeps System Manager only, "
				"so every other role will be refused. Grant them by hand."
			),
		)
		return

	created = updated = 0
	for role, flags in sorted(granted.items()):
		if not frappe.db.exists("Role", role):
			continue
		existing = frappe.db.get_value("Custom DocPerm", {"parent": DOCTYPE, "role": role, "permlevel": 0}, "name")
		if existing:
			frappe.db.set_value("Custom DocPerm", existing, flags, update_modified=False)
			updated += 1
			continue
		doc = frappe.new_doc("Custom DocPerm")
		doc.parent = DOCTYPE
		doc.role = role
		doc.permlevel = 0
		for flag, value in flags.items():
			doc.set(flag, value)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		created += 1

	frappe.clear_cache(doctype=DOCTYPE)
	frappe.logger("pet_app.migrate").info(
		{"event": "PREVENTIVE_CARE_PERMISSIONS_GRANTED", "created": created, "updated": updated, "roles": sorted(granted)}
	)
