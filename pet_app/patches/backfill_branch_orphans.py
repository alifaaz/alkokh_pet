"""Catch any scoped record left without a branch.

``worklist_branch_scope`` backfilled child records from their parent visit, but a child
whose parent was itself unstamped at that moment fell through both passes. This patch is
idempotent and safe to re-run: it re-resolves from the parent, then from the record's own
practitioner, then falls back to the default branch.

Kept as a standalone patch rather than folded into the original because a NULL branch is
visible to every clinic, so it is worth a re-runnable sweep after any future schema work.
"""

from __future__ import annotations

import frappe


SCOPED = ("Vet Visit", "Vet Case Sheet", "Appointment", "Pet Queue Ticket", "Pet Boarding")
PARENT_LINKS = {
	"Vet Case Sheet": ("vet_visit", "Vet Visit"),
	"Pet Queue Ticket": ("visit", "Vet Visit"),
	"Pet Boarding": ("visit", "Vet Visit"),
}


def execute():
	default_branch = _default_branch()
	if not default_branch:
		return

	for doctype in SCOPED:
		if not frappe.db.exists("DocType", doctype) or not frappe.db.has_column(doctype, "branch"):
			continue

		link_field, parent = PARENT_LINKS.get(doctype, (None, None))
		if link_field and parent and frappe.db.has_column(doctype, link_field):
			frappe.db.sql(
				f"""
				update `tab{doctype}` c
				join `tab{parent}` p on p.name = c.`{link_field}`
				set c.branch = p.branch
				where ifnull(c.branch, '') = '' and ifnull(p.branch, '') != ''
				"""
			)

		for field in ("practitioner", "doctor"):
			if frappe.db.has_column(doctype, field):
				frappe.db.sql(
					f"""
					update `tab{doctype}` c
					join `tabHealthcare Practitioner` hp on hp.name = c.`{field}`
					set c.branch = hp.clinic_branch
					where ifnull(c.branch, '') = '' and ifnull(hp.clinic_branch, '') != ''
					"""
				)

		frappe.db.sql(
			f"update `tab{doctype}` set branch = %s where ifnull(branch, '') = ''",
			(default_branch,),
		)


def _default_branch() -> str | None:
	rows = frappe.get_all("Branch", pluck="name", order_by="creation asc", limit_page_length=1)
	return rows[0] if rows else None
