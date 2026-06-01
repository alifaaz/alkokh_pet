from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, now_datetime


class VetCaseSheetResponse(Document):
	def validate(self):
		if not self.case_sheet:
			frappe.throw(_("Case Sheet is required."))
		if not self.field_key:
			frappe.throw(_("Field Key is required."))
		if not self.recorded_at:
			self.recorded_at = now_datetime()
		if not self.recorded_by:
			self.recorded_by = frappe.session.user
		self.field_key = cstr(self.field_key).strip()
		if self.value_json:
			try:
				json.loads(self.value_json)
			except Exception:
				frappe.throw(_("Value JSON must be valid JSON."))

