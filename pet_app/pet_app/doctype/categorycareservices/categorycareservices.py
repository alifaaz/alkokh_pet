# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr


class CategoryCareServices(Document):
	def validate(self):
		self.category_name = cstr(self.category_name).strip()
		if not self.category_name:
			frappe.throw(_("Category Name is required."))
		if self.active in (None, ""):
			self.active = 1
		if frappe.db.exists(
			"CategoryCareServices",
			{"category_name": self.category_name, "name": ["!=", self.name]},
		):
			frappe.throw(_("Care Service category {0} already exists.").format(frappe.bold(self.category_name)))
