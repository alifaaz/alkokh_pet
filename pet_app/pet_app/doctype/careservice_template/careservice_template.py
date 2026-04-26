# Copyright (c) 2025, solvers and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class CareServicetemplate(Document):
	def validate(self):
		self.service_name = (self.service_name or "").strip()

		if not self.service_name:
			frappe.throw(_("Service Name is required."))
		if not self.item_code:
			frappe.throw(_("Item Code is required for Care Service."))
		if self.default_price in (None, ""):
			frappe.throw(_("Default Price is required for Care Service."))
		if not self.price_list:
			self.price_list = "Clinic"
