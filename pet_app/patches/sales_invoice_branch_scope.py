"""Backfill ``Sales Invoice.branch`` so the invoice list can be scoped per clinic.

Scope note: this stamps the invoice **document** only. The customer, the receivable
account, Payment Entry and every balance stay global -- one Company means one set of
books, and a debt is owed to the business, not to a branch. Filtering a balance would
make a customer look settled at one clinic while owing money at another.

The ``branch`` field already exists on Sales Invoice as an ERPNext reporting dimension
(``pet_app/fixtures/custom_field.json``); this patch only populates it.

Resolution order:
1. the linked ``Vet Visit.branch`` -- the clinic that actually did the work
2. the default (oldest) Branch, as a catch-all so nothing is left NULL
"""

from __future__ import annotations

import frappe


DOCTYPE = "Sales Invoice"


def execute():
	if not frappe.db.exists("DocType", "Branch") or not frappe.db.has_column(DOCTYPE, "branch"):
		return

	default_branch = _default_branch()
	if not default_branch:
		return

	# Vet Visit points at the invoice, not the other way round.
	if frappe.db.has_column("Vet Visit", "sales_invoice") and frappe.db.has_column("Vet Visit", "branch"):
		frappe.db.sql(
			"""
			update `tabSales Invoice` si
			join `tabVet Visit` v on v.sales_invoice = si.name
			set si.branch = v.branch
			where ifnull(si.branch, '') = '' and ifnull(v.branch, '') != ''
			"""
		)

	# Boarding-sourced invoices, where Pet Boarding carries the link.
	if frappe.db.has_column("Pet Boarding", "sales_invoice") and frappe.db.has_column("Pet Boarding", "branch"):
		frappe.db.sql(
			"""
			update `tabSales Invoice` si
			join `tabPet Boarding` b on b.sales_invoice = si.name
			set si.branch = b.branch
			where ifnull(si.branch, '') = '' and ifnull(b.branch, '') != ''
			"""
		)

	# Anything left -- retail/POS sales with no clinical source -- goes to the default.
	# A NULL branch would be visible to every clinic, which is the opposite of intent.
	frappe.db.sql(
		"update `tabSales Invoice` set branch = %s where ifnull(branch, '') = ''",
		(default_branch,),
	)


def _default_branch() -> str | None:
	rows = frappe.get_all("Branch", pluck="name", order_by="creation asc", limit_page_length=1)
	return rows[0] if rows else None
