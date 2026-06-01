from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document


ALLOWED_PRACTITIONER_ADMIN_ROLES = ("System Manager", "Healthcare Administrator")
BASE_PRACTITIONER_USER_ROLES = ("Healthcare", "Healthcare Practitioner")
PRACTITIONER_TYPE_ROLES = {
	"Doctor": ("Doctor",),
	"Nurse": ("Nursing User",),
	"Service Provider": ("Service Provider",),
	"Coordinator": ("Coordinator",),
	"Other": (),
}
PRACTITIONER_TYPES = tuple(PRACTITIONER_TYPE_ROLES)


class HealthcarePractitioner(Document):
	def before_insert(self):
		self._enforce_admin_access()

	def validate(self):
		self._enforce_admin_access()
		self.practitioner_name = (self.practitioner_name or "").strip()
		self.practitioner_type = (self.practitioner_type or "Doctor").strip()
		self.phone = (self.phone or "").strip()

		if not self.practitioner_name:
			frappe.throw(_("Practitioner Name is required."))
		if self.practitioner_type not in PRACTITIONER_TYPES:
			frappe.throw(
				_("Practitioner Type must be one of: {0}.").format(", ".join(PRACTITIONER_TYPES))
			)
		if not self.phone:
			frappe.throw(_("Phone is required."))

		self.user_id = self._resolve_or_create_user()
		self._validate_user_mapping()

	def on_update(self):
		self._enforce_admin_access()
		self._sync_user_profile()

	def on_trash(self):
		self._enforce_admin_access()
		if self.user_id and frappe.db.exists("User", self.user_id):
			frappe.db.set_value("User", self.user_id, "enabled", 0, update_modified=False)

	def _enforce_admin_access(self):
		if frappe.session.user == "Administrator":
			return
		if frappe.flags.in_install or frappe.flags.in_patch:
			return
		frappe.only_for(ALLOWED_PRACTITIONER_ADMIN_ROLES)

	def _resolve_or_create_user(self) -> str:
		if self.user_id:
			if not frappe.db.exists("User", self.user_id):
				frappe.throw(_("User {0} was not found.").format(frappe.bold(self.user_id)))
			self._ensure_user_roles(self.user_id)
			self._sync_user_profile(self.user_id)
			return self.user_id

		user_id = self._generated_user_id()
		if frappe.db.exists("User", user_id):
			self._ensure_user_roles(user_id)
			self._sync_user_profile(user_id)
			return user_id

		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": user_id,
				"first_name": self.practitioner_name,
				"full_name": self.practitioner_name,
				"mobile_no": self.phone,
				"enabled": 1,
				"user_type": "System User",
				"send_welcome_email": 0,
			}
		)
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
		self._ensure_user_roles(user.name)
		return user.name

	def _sync_user_profile(self, user_id: str | None = None):
		user_id = user_id or self.user_id
		if not user_id or not frappe.db.exists("User", user_id):
			return

		self._ensure_user_roles(user_id)

		updates = {}
		current = frappe.db.get_value(
			"User",
			user_id,
			["first_name", "full_name", "mobile_no", "enabled"],
			as_dict=True,
		) or {}
		if current.get("first_name") != self.practitioner_name:
			updates["first_name"] = self.practitioner_name
		if current.get("full_name") != self.practitioner_name:
			updates["full_name"] = self.practitioner_name
		if current.get("mobile_no") != self.phone:
			updates["mobile_no"] = self.phone
		if current.get("enabled") != 1 and not self.disabled:
			updates["enabled"] = 1

		if updates:
			frappe.db.set_value("User", user_id, updates, update_modified=False)

	def _ensure_user_roles(self, user_id: str):
		required_roles = list(BASE_PRACTITIONER_USER_ROLES)
		required_roles.extend(PRACTITIONER_TYPE_ROLES.get(self.practitioner_type or "Doctor", ()))
		for role in required_roles:
			self._ensure_role(role)

		existing_roles = set(frappe.get_roles(user_id) or [])
		missing_roles = [role for role in required_roles if role not in existing_roles]
		if not missing_roles:
			return

		user = frappe.get_doc("User", user_id)
		for role in missing_roles:
			user.append("roles", {"role": role})
		user.save(ignore_permissions=True)

	def _ensure_role(self, role: str):
		if frappe.db.exists("Role", role):
			return
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role,
				"desk_access": 1,
				"is_custom": 1,
			}
		).insert(ignore_permissions=True)

	def _validate_user_mapping(self):
		if not self.user_id:
			frappe.throw(_("User is required."))

		other_practitioner = frappe.db.get_value(
			"Healthcare Practitioner",
			{"user_id": self.user_id, "name": ["!=", self.name or ""]},
			"name",
		)
		if other_practitioner:
			frappe.throw(
				_("User {0} is already linked to Healthcare Practitioner {1}.").format(
					frappe.bold(self.user_id), frappe.bold(other_practitioner)
				)
			)

	def _generated_user_id(self) -> str:
		digits = re.sub(r"\D+", "", self.phone or "")
		if digits:
			return f"practitioner.{digits}@petapp.local"

		slug = frappe.scrub(self.practitioner_name or "practitioner")
		if not slug:
			slug = "practitioner"
		return f"{slug}@petapp.local"
