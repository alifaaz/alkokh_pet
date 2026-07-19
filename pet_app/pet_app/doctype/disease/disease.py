# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr


class Disease(Document):
	def validate(self):
		self.disease_name = cstr(self.disease_name).strip()
		self.species = cstr(self.species).strip()
		self.category_a = cstr(self.category_a).strip()
		if not self.disease_name:
			frappe.throw(_("Disease Name is required."))
		if self.active in (None, ""):
			self.active = 1
		self._validate_duplicate()

	def _validate_duplicate(self):
		filters = {
			"disease_name": self.disease_name,
			"species": self.species,
			"name": ["!=", self.name],
		}
		if frappe.db.exists("Disease", filters):
			frappe.throw(_("A Disease already exists for this name and species."))
