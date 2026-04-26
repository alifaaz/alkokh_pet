from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document


ALLOWED_DOCTOR_ADMIN_ROLES = ("System Manager", "Healthcare Administrator")
DOCTOR_USER_ROLES = ("Doctor", "Healthcare")


class Doctor(Document):
	def before_insert(self):
		self._enforce_admin_access()

	def validate(self):
		self._enforce_admin_access()
		self.doctor_name = (self.doctor_name or "").strip()
		self.phone = (self.phone or "").strip()

		if not self.doctor_name:
			frappe.throw(_("Doctor Name is required."))
		if not self.phone:
			frappe.throw(_("Phone is required."))

		self.user = self._resolve_or_create_user()
		self._validate_user_mapping()

	def on_update(self):
		self._enforce_admin_access()
		self._sync_user_profile()

	def on_trash(self):
		self._enforce_admin_access()
		if self.user and frappe.db.exists("User", self.user):
			frappe.db.set_value("User", self.user, "enabled", 0, update_modified=False)

	def _enforce_admin_access(self):
		if frappe.session.user == "Administrator":
			return
		if frappe.flags.in_install or frappe.flags.in_patch:
			return
		frappe.only_for(ALLOWED_DOCTOR_ADMIN_ROLES)

	def _resolve_or_create_user(self) -> str:
		if self.user:
			if not frappe.db.exists("User", self.user):
				frappe.throw(_("User {0} was not found.").format(frappe.bold(self.user)))
			self._ensure_user_roles(self.user)
			self._sync_user_profile(self.user)
			return self.user

		user_id = self._generated_user_id()
		if frappe.db.exists("User", user_id):
			self._ensure_user_roles(user_id)
			self._sync_user_profile(user_id)
			return user_id

		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": user_id,
				"first_name": self.doctor_name,
				"full_name": self.doctor_name,
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
		user_id = user_id or self.user
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
		if current.get("first_name") != self.doctor_name:
			updates["first_name"] = self.doctor_name
		if current.get("full_name") != self.doctor_name:
			updates["full_name"] = self.doctor_name
		if current.get("mobile_no") != self.phone:
			updates["mobile_no"] = self.phone
		if current.get("enabled") != 1:
			updates["enabled"] = 1

		if updates:
			frappe.db.set_value("User", user_id, updates, update_modified=False)

	def _ensure_user_roles(self, user_id: str):
		existing_roles = set(frappe.get_roles(user_id) or [])
		missing_roles = [role for role in DOCTOR_USER_ROLES if role not in existing_roles]
		for role in missing_roles:
			user = frappe.get_doc("User", user_id)
			user.append("roles", {"role": role})
			user.save(ignore_permissions=True)

	def _validate_user_mapping(self):
		if not self.user:
			frappe.throw(_("User is required."))

		other_doctor = frappe.db.get_value(
			"Doctor",
			{"user": self.user, "name": ["!=", self.name or ""]},
			"name",
		)
		if other_doctor:
			frappe.throw(
				_("User {0} is already linked to Doctor {1}.").format(
					frappe.bold(self.user), frappe.bold(other_doctor)
				)
			)

	def _generated_user_id(self) -> str:
		digits = re.sub(r"\D+", "", self.phone or "")
		if digits:
			return f"doctor.{digits}@petapp.local"

		slug = frappe.scrub(self.doctor_name or "doctor")
		if not slug:
			slug = "doctor"
		return f"{slug}@petapp.local"
