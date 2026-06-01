from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class VetVisitAddendum(Document):
	def before_insert(self):
		self._set_defaults()

	def validate(self):
		self._set_defaults()
		self._validate_visit_is_billed()

	def _set_defaults(self):
		if not self.addendum_datetime:
			self.addendum_datetime = now_datetime()
		if self.meta.has_field("created_at") and not self.created_at:
			self.created_at = self.addendum_datetime
		if not self.authored_by:
			self.authored_by = frappe.session.user
		if self.meta.has_field("author") and not self.author:
			self.author = self.authored_by
		if self.meta.has_field("author_role") and not self.author_role:
			roles = [role for role in frappe.get_roles(frappe.session.user) if role not in {"All", "Guest"}]
			self.author_role = roles[0] if roles else None
		if self.meta.has_field("status") and not self.status:
			self.status = "Submitted"
		if not self.visit:
			frappe.throw(_("Vet Visit is required."))

		visit = frappe.db.get_value(
			"Vet Visit",
			self.visit,
			["guardian", "customer", "animal_patient", "doctor"],
			as_dict=True,
		)
		if not visit:
			frappe.throw(_("Vet Visit {0} was not found.").format(frappe.bold(self.visit)))

		self.guardian = self.guardian or visit.guardian
		self.customer = self.customer or visit.customer
		self.pet = self.pet or visit.animal_patient
		self.doctor = self.doctor or visit.doctor

	def _validate_visit_is_billed(self):
		visit = frappe.db.get_value("Vet Visit", self.visit, ["billed", "sales_invoice"], as_dict=True)
		if not visit or not (visit.billed or visit.sales_invoice):
			frappe.throw(_("Addenda are only allowed after a Vet Visit has been billed."))
		if not self.note:
			frappe.throw(_("Addendum Note is required."))
