# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class PetBillableItem(Document):
	def get_invalid_links(self, is_submittable=False):
		"""Don't fail the parent's save over a Sales Invoice that no longer exists.

		About 298 draft Sales Invoices were hard-deleted on 2026-09-14 without releasing
		the rows billed onto them. Those rows keep `sales_invoice` and their Billed status
		by decision - the charge is recorded, and rewriting it would lose that record - so
		roughly 295 Vet Visits carry a row pointing at an invoice that is gone. Frappe
		re-checks every link of every child row on each parent save, so any save of those
		visits died with "Could not find Row #1: Sales Invoice: ACC-SINV-...", including
		saves made as a side effect of an unrelated action.

		Only that one case is dropped: the field is `sales_invoice`, it has a value, and no
		such invoice exists. A value naming an invoice that DOES exist is still validated,
		every other link on the row is still validated, and the stored value is untouched -
		so Frappe still refuses to delete an invoice while a row points at it.

		The value has to be put back by hand. Frappe's own check writes the resolved name
		onto the field as it goes (frappe/model/base_document.py:1049), which for a missing
		link means blanking it - so without the restore below, dropping the error would
		silently clear `sales_invoice` on exactly the rows this is protecting.
		"""
		invoice = self.get("sales_invoice")
		dangling = bool(invoice) and not frappe.db.exists("Sales Invoice", invoice)

		invalid_links, cancelled_links = super().get_invalid_links(is_submittable=is_submittable)

		if dangling:
			invalid_links = [link for link in invalid_links if link[0] != "sales_invoice"]
			self.sales_invoice = invoice

		return invalid_links, cancelled_links
