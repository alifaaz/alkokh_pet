from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, now_datetime


class PetConsentForm(Document):
	def validate(self):
		self._set_identity_from_procedure()
		self._set_text_from_template()
		if self.status == "Signed":
			if not self.signed_by:
				frappe.throw(_("Signed By is required for a signed consent form."))
			if not self.signed_at:
				self.signed_at = now_datetime()
		if self.template and not frappe.db.exists("Pet Consent Template", self.template):
			frappe.throw(_("Consent Template {0} was not found.").format(frappe.bold(self.template)))

	def _set_identity_from_procedure(self):
		if not self.procedure:
			return
		procedure = frappe.db.get_value(
			"Pet Procedure",
			self.procedure,
			["visit", "pet", "guardian", "doctor", "procedure_template"],
			as_dict=True,
		)
		if not procedure:
			frappe.throw(_("Pet Procedure {0} was not found.").format(frappe.bold(self.procedure)))
		self.visit = self.visit or procedure.visit
		self.pet = self.pet or procedure.pet
		self.guardian = self.guardian or procedure.guardian
		self.doctor = self.doctor or procedure.doctor
		if not self.template and procedure.procedure_template:
			self.template = frappe.db.get_value(
				"Procedure Template", procedure.procedure_template, "consent_template"
			)

	def _set_text_from_template(self):
		if not self.template or cstr(self.consent_text).strip():
			return
		self.consent_text = frappe.db.get_value("Pet Consent Template", self.template, "consent_text")

