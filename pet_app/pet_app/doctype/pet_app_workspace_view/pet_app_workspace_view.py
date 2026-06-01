from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr


class PetAppWorkspaceView(Document):
	def validate(self):
		self.user = self.user or frappe.session.user
		self.view_name = cstr(self.view_name).strip()
		if not self.view_name:
			frappe.throw(_("View Name is required."))
		self._validate_json("filters_json")
		self._validate_json("columns_json")
		self._validate_duplicate()
		self._clear_other_defaults()

	def _validate_json(self, fieldname):
		value = cstr(self.get(fieldname)).strip()
		if not value:
			return
		try:
			json.loads(value)
		except Exception:
			frappe.throw(_("{0} must be valid JSON.").format(self.meta.get_label(fieldname)))

	def _validate_duplicate(self):
		if frappe.db.exists(
			"Pet App Workspace View",
			{"user": self.user, "view_name": self.view_name, "name": ["!=", self.name]},
		):
			frappe.throw(_("A workspace view with this name already exists for this user."))

	def _clear_other_defaults(self):
		if not cint(self.is_default) or self.is_new():
			return
		frappe.db.set_value(
			"Pet App Workspace View",
			{"user": self.user, "name": ["!=", self.name]},
			"is_default",
			0,
			update_modified=False,
		)

