from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from pet_app.api.permissions import ADMIN_ROLES, RESTRICTION_TYPES, _normalize_restriction_type


class PetAppUserRestriction(Document):
	def validate(self):
		self.restriction_type = _normalize_restriction_type(self.restriction_type)
		if self.restriction_type not in RESTRICTION_TYPES:
			frappe.throw(_("Invalid Restriction Type."))
		if not self.user:
			frappe.throw(_("User is required."))
		if not self.value:
			frappe.throw(_("Value is required."))
		self._validate_duplicate_active_restriction()

	def has_permission(self, ptype=None, user=None):
		if ptype in {"create", "write", "delete"}:
			return _is_admin_user(user)
		return None

	def _validate_duplicate_active_restriction(self):
		if not self.enabled:
			return
		if frappe.db.exists(
			self.doctype,
			{
				"enabled": 1,
				"user": self.user,
				"restriction_type": self.restriction_type,
				"value": self.value,
				"name": ["!=", self.name],
			},
		):
			frappe.throw(_("An enabled restriction already exists for this user, type, and value."))


def has_permission(doc=None, ptype=None, user=None):
	if ptype in {"create", "write", "delete"}:
		return _is_admin_user(user)
	return None


def _is_admin_user(user=None) -> bool:
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False
	roles = set(frappe.get_roles(user) or [])
	if user == "Administrator":
		roles.add("Administrator")
	return bool(roles & ADMIN_ROLES)
