from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr


class VetCaseSheetTemplate(Document):
	def validate(self):
		self.template_name = cstr(self.template_name).strip()
		self.species = cstr(self.species).strip()
		if not self.template_name:
			frappe.throw(_("Template Name is required."))
		if not self.visit_type:
			frappe.throw(_("Visit Type is required."))
		self._validate_duplicate_default()
		self._normalize_items()

	def _validate_duplicate_default(self):
		if not cint(self.is_default) or not cint(self.active):
			return
		filters = {
			"visit_type": self.visit_type,
			"species": self.species or "",
			"is_default": 1,
			"active": 1,
			"name": ["!=", self.name],
		}
		if frappe.db.exists("Vet Case Sheet Template", filters):
			frappe.throw(_("Only one active default template is allowed for a visit type and species."))

	def _normalize_items(self):
		seen = set()
		for row in self.items or []:
			row.field_key = cstr(row.field_key).strip()
			row.label = cstr(row.label).strip()
			row.section = cstr(row.section).strip()
			if not row.field_key:
				frappe.throw(_("Template item row {0} needs a Field Key.").format(row.idx))
			if row.field_key in seen:
				frappe.throw(_("Duplicate template field key {0}.").format(frappe.bold(row.field_key)))
			seen.add(row.field_key)
			if not row.label:
				row.label = row.field_key.replace("_", " ").title()

