"""Make the scoped ``branch`` fields selectable rather than read-only.

Unrestricted users (Administrator, back-office staff with no Branch User Permission)
have no single branch to infer, so they must be able to choose one on create. The field
therefore cannot be ``read_only``.

Security does not rest on the field being read-only: ``stamp_branch_on_insert`` calls
``assert_can_write_to_branch``, which rejects any branch the caller has no claim to. A
restricted user still cannot file a record into another clinic, whatever they submit.
"""

from __future__ import annotations

import frappe


SCOPED_DOCTYPES = (
	"Vet Visit",
	"Vet Case Sheet",
	"Appointment",
	"Pet Queue Ticket",
	"Pet Boarding",
)


def execute():
	for doctype in SCOPED_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		_make_selectable(doctype)
		frappe.clear_cache(doctype=doctype)


def _make_selectable(doctype: str):
	name = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": "branch"}, "name")
	if name:
		frappe.db.set_value(
			"Custom Field",
			name,
			{
				"read_only": 0,
				"allow_on_submit": 0,
				"description": "Clinic that owns this record. Filled automatically from your branch; choose one only if your user covers several.",
			},
			update_modified=False,
		)
		return

	# Pet Queue Ticket carries a standard DocField, converted from Data by
	# worklist_branch_scope.
	docfield = frappe.db.get_value("DocField", {"parent": doctype, "fieldname": "branch"}, "name")
	if docfield:
		frappe.db.set_value("DocField", docfield, "read_only", 0, update_modified=False)
