from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr


class PetConsentTemplate(Document):
	def validate(self):
		self.template_name = cstr(self.template_name).strip()
		self.version = cstr(self.version).strip() or "1"
		if not self.template_name:
			frappe.throw(_("Template Name is required."))
		if not cstr(self.consent_text).strip():
			frappe.throw(_("Consent Text is required."))

