"""Make boarding global and attribute it to a practitioner instead.

Boarding was briefly branch scoped. That was wrong: the 37 ``Service Room`` records are
a single shared pool with no branch of their own, so scoping the booking let one clinic
reserve a room that another clinic could then neither see nor check out.

This patch:
1. adds ``Pet Boarding.practitioner`` (the doctype had no practitioner link at all --
   only ``boarded_by``, a User, and ``service_room``),
2. clears ``Pet Boarding.branch`` so the field cannot be mistaken for an access control.

``practitioner`` is **optional and frontend-owned**: it is deliberately left empty and
never auto-filled, so an existing stay is not attributed to a practitioner who was only
inferred. Clearing the branch column is safe precisely because boarding is no longer in
``SCOPED_DOCTYPES`` -- nothing filters on it, so a NULL here grants nothing.
"""

from __future__ import annotations

import frappe


DOCTYPE = "Pet Boarding"


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	ensure_practitioner_field()
	clear_branch()
	frappe.clear_cache(doctype=DOCTYPE)


def ensure_practitioner_field():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	if frappe.db.has_column(DOCTYPE, "practitioner"):
		return

	create_custom_field(
		DOCTYPE,
		{
			"fieldname": "practitioner",
			"label": "Responsible Practitioner",
			"fieldtype": "Link",
			"options": "Healthcare Practitioner",
			"in_standard_filter": 1,
			"insert_after": "guardian",
			"description": "Practitioner responsible for this stay. Boarding is shared across clinics; this records who is accountable, not who may see it.",
		},
	)


def clear_branch():
	if frappe.db.has_column(DOCTYPE, "branch"):
		frappe.db.sql(f"update `tab{DOCTYPE}` set branch = null where ifnull(branch, '') != ''")
