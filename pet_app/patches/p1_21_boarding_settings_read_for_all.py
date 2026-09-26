"""Let every account read Pet Boarding Settings, so multi-pet booking stops vanishing.

``max_pets_per_booking`` is set to 7 on this site and the backend already enforces it:
get_max_pets_per_booking reads the Single with frappe.db.get_single_value, a DB-level
read that never consults permissions. The client had no such luxury - its only source
was a raw ``frappe.client.get("Pet Boarding Settings")``, which runs
check_permission("read"). Administrator short-circuits has_permission and always
passes; everyone else needs an explicit read row on the Single, which a settings
doctype does not normally grant. The 403 was swallowed, capacity fell back to its safe
floor of 1, and at 1 the pet picker is designed to behave as single-select - so the
feature disappeared without an error for every account except Administrator.

Why the JSON alone is not enough
--------------------------------
pet_boarding_settings.json now carries the ``All`` read row, which covers fresh
installs. It does nothing here. frappe.permissions.get_all_perms discards EVERY
standard DocPerm for a doctype that has any Custom DocPerm row, and this one has six -
System Manager, Pet App Admin, Settings Read, Settings Update, Settings Create, and an
ad-hoc lowercase "pet boarding settings read". So on an existing site the grant has to
be written as a Custom DocPerm or it is silently ignored.

There were TWO blockers, not one
--------------------------------
The DocPerm grant below is necessary and was not sufficient. Read also failed at the
DOCUMENT level, and for a different reason: this Single carries Link fields, and
``boarding_branch`` holds "hotel", while front-desk users carry a User Permission
scoping them to Branch "main". frappe.permissions.has_user_permission walks a doc's
Link fields and refuses the whole document when one names a value the user is not
allowed - so a System Manager with a branch scope was refused too, and no amount of
DocPerm granting would have changed it. That is fixed on the DocType itself, by
``ignore_user_permissions`` on the Single's Link fields (see pet_boarding_settings.json,
which syncs on migrate and needs no code here). It is the right call on the merits as
well as the mechanics: the field's own description says the boarding branch "is never
taken from the user processing the stay", so scoping the reader by it was always wrong.

Why ``All`` rather than a role list
-----------------------------------
Granting role by role is what has already failed. Three of the six rows above grant
read, and front-desk staff hold none of them - the list was written once and the next
role to be created was not on it. Nothing on this Single is sensitive: a category link,
a branch, two deprecated item links, a default type and a capacity integer. Write and
create stay on System Manager; only read widens.

Idempotent: frappe.permissions.add_permission returns early when a row for this
doctype/role/permlevel already exists.
"""

from __future__ import annotations

import frappe
from frappe.permissions import add_permission

DOCTYPE = "Pet Boarding Settings"
ROLE = "All"
PERMLEVEL = 0


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		print(f"{DOCTYPE} does not exist; nothing to grant.")
		return

	existing = frappe.db.get_value(
		"Custom DocPerm",
		{"parent": DOCTYPE, "role": ROLE, "permlevel": PERMLEVEL, "if_owner": 0},
		"read",
	)
	if existing:
		print(f"{DOCTYPE}: role {ROLE} already has read at level {PERMLEVEL}.")
		return

	# Defaults to ptype="read" and calls validate_permissions_for_doctype itself. Its
	# setup_custom_perms call is a no-op here - Custom DocPerms already exist for this
	# doctype, which is the whole reason the JSON row does not take effect on its own.
	add_permission(DOCTYPE, ROLE, PERMLEVEL)
	frappe.clear_cache()

	print(f"{DOCTYPE}: granted read to {ROLE} at level {PERMLEVEL}.")
