from __future__ import annotations

import json

import frappe
from frappe.permissions import add_permission, update_permission_property
from frappe.tests import IntegrationTestCase

from pet_app.api import permissions
from pet_app.patches import operational_healthcare_roles


SNAPSHOT_PTYPES = (
	"read",
	"create",
	"write",
	"delete",
	"submit",
	"cancel",
	"amend",
	"print",
	"email",
	"export",
	"import",
	"report",
	"share",
)


class TestDynamicDocPermSnapshot(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		frappe.reload_doc("pet_app", "doctype", "pet_app_page_access")
		frappe.reload_doc("pet_app", "doctype", "pet_app_access_settings")
		operational_healthcare_roles.execute()

	def setUp(self):
		frappe.set_user("Administrator")
		self._clear_page_access_settings()

	def tearDown(self):
		self._clear_page_access_settings()
		frappe.set_user("Administrator")

	def test_current_access_keeps_shape_with_no_page_registry_rows(self):
		frappe.set_user("Administrator")
		access = permissions.get_current_access()

		for key in (
			"roles",
			"roleProfile",
			"roleProfiles",
			"fullAccess",
			"fullAccessRoles",
			"modules",
			"pages",
			"actions",
			"doctypes",
			"restrictions",
			"loadedAt",
		):
			self.assertIn(key, access)

		self.assertEqual([], access["modules"])
		self.assertEqual([], access["pages"])
		self.assertEqual([], access["actions"])
		self.assertIsInstance(access["doctypes"], dict)

	def test_page_access_sync_creates_rows_with_fallback_roles(self):
		role = self._ensure_role("Pet App Test Page Sync Role")

		response = permissions.sync_frontend_pages(
			[
				{
					"key": "page.accounting.sales_invoices",
					"label": "Sales Invoices",
					"moduleKey": "module.accounting",
					"doctype": "Sales Invoice",
					"paths": ["/accounting/sales-invoices"],
					"pathPrefixes": ["/accounting/sales-invoices/"],
					"routeNames": ["accounting-sales-invoices"],
					"fallbackRoles": [role],
				}
			]
		)

		self.assertEqual(1, len(response["pages"]))
		page = response["pages"][0]
		self.assertEqual("page.accounting.sales_invoices", page["page_key"])
		self.assertEqual("Sales Invoices", page["label"])
		self.assertEqual("module.accounting", page["module_key"])
		self.assertEqual("Sales Invoice", page["doctype"])
		self.assertEqual(["/accounting/sales-invoices"], page["paths"])
		self.assertEqual(["/accounting/sales-invoices/"], page["path_prefixes"])
		self.assertEqual(["accounting-sales-invoices"], page["route_names"])
		self.assertEqual(1, page["enabled"])
		self.assertEqual([role], page["roles"])

	def test_page_access_sync_preserves_assignments_reorders_and_deletes_missing_pages(self):
		role_a = self._ensure_role("Pet App Test Page Preserve Role A")
		role_b = self._ensure_role("Pet App Test Page Preserve Role B")
		permissions.sync_frontend_pages(
			{
				"pages": [
					{"key": "page.test.a", "label": "Page A", "moduleKey": "module.old", "fallbackRoles": [role_a]},
					{"key": "page.test.b", "label": "Page B", "moduleKey": "module.old", "fallbackRoles": [role_a]},
				]
			}
		)
		permissions.update_page_access_settings([{"page_key": "page.test.b", "enabled": 0, "roles": [role_b]}])

		response = permissions.sync_frontend_pages(
			[
				{
					"key": "page.test.b",
					"label": "Page B Updated",
					"moduleKey": "module.updated",
					"paths": ["/updated"],
					"fallbackRoles": [role_a],
				},
				{"key": "page.test.c", "label": "Page C", "moduleKey": "module.new", "fallbackRoles": [role_a]},
			]
		)

		self.assertEqual(["page.test.b", "page.test.c"], [page["page_key"] for page in response["pages"]])
		page_b = response["pages"][0]
		page_c = response["pages"][1]
		self.assertEqual("Page B Updated", page_b["label"])
		self.assertEqual("module.updated", page_b["module_key"])
		self.assertEqual(["/updated"], page_b["paths"])
		self.assertEqual(0, page_b["enabled"])
		self.assertEqual([role_b], page_b["roles"])
		self.assertEqual(1, page_c["enabled"])
		self.assertEqual([role_a], page_c["roles"])

	def test_page_access_update_validates_roles_and_only_changes_assignment_fields(self):
		role_a = self._ensure_role("Pet App Test Page Update Role A")
		role_b = self._ensure_role("Pet App Test Page Update Role B")
		permissions.sync_frontend_pages(
			[
				{
					"key": "page.test.update",
					"label": "Original Label",
					"moduleKey": "module.original",
					"paths": ["/original"],
					"fallbackRoles": [role_a],
				}
			]
		)

		response = permissions.update_page_access_settings(
			json.dumps({"pages": [{"page_key": "page.test.update", "enabled": 0, "roles": [role_b]}]})
		)

		page = response["pages"][0]
		self.assertEqual("Original Label", page["label"])
		self.assertEqual("module.original", page["module_key"])
		self.assertEqual(["/original"], page["paths"])
		self.assertEqual(0, page["enabled"])
		self.assertEqual([role_b], page["roles"])

		with self.assertRaises(frappe.ValidationError):
			permissions.update_page_access_settings(
				[{"page_key": "page.test.update", "enabled": 1, "roles": ["Pet App Missing Page Role"]}]
			)

	def test_page_access_snapshot_uses_page_roles_for_visibility_only(self):
		doctype = self._ensure_test_doctype("Pet App Test Page Visibility DocType")
		page_role = self._ensure_role("Pet App Test Page Visibility Role")
		doc_role = self._ensure_role("Pet App Test Page Visibility Doc Role")
		page_user = self._ensure_user("page.visibility@example.com", roles=[page_role])
		doc_user = self._ensure_user("page.doctype.only@example.com", roles=[doc_role])
		self._grant_doctype_permission(doctype, doc_role, ("read",))
		permissions.sync_frontend_pages(
			[
				{
					"key": "page.test.visibility",
					"label": "Visibility",
					"moduleKey": "module.visibility",
					"doctype": doctype,
					"fallbackRoles": [page_role],
				}
			]
		)

		page_snapshot = permissions.build_access_snapshot(page_user)
		doc_snapshot = permissions.build_access_snapshot(doc_user)

		self.assertEqual(["page.test.visibility"], page_snapshot["pages"])
		self.assertEqual(["module.visibility"], page_snapshot["modules"])
		self.assertNotIn("page.test.visibility", doc_snapshot["pages"])
		self.assertEqual([], doc_snapshot["modules"])
		self.assertIn("read", doc_snapshot["doctypes"].get(doctype, []))

	def test_page_access_full_access_roles_receive_all_enabled_pages_only(self):
		page_role = self._ensure_role("Pet App Test Page Full Access Role")
		system_manager = self._ensure_user("page.system.manager@example.com", roles=["System Manager"])
		pet_app_admin = self._ensure_user("page.pet.app.admin@example.com", roles=["Pet App Admin"])
		permissions.sync_frontend_pages(
			[
				{"key": "page.test.enabled", "label": "Enabled", "moduleKey": "module.full", "fallbackRoles": [page_role]},
				{"key": "page.test.disabled", "label": "Disabled", "moduleKey": "module.hidden", "fallbackRoles": [page_role]},
			]
		)
		permissions.update_page_access_settings([{"page_key": "page.test.disabled", "enabled": 0, "roles": [page_role]}])

		for user in ("Administrator", system_manager, pet_app_admin):
			snapshot = permissions.build_access_snapshot(user)
			self.assertIn("page.test.enabled", snapshot["pages"])
			self.assertNotIn("page.test.disabled", snapshot["pages"])
			self.assertIn("module.full", snapshot["modules"])
			self.assertNotIn("module.hidden", snapshot["modules"])

	def test_read_docperm_appears_dynamically(self):
		doctype = self._ensure_test_doctype("Pet App Test Snapshot Read")
		role = self._ensure_role("Pet App Test Snapshot Read Role")
		user = self._ensure_user("snapshot.read@example.com", roles=[role])
		self._grant_doctype_permission(doctype, role, ("read",))

		snapshot = permissions.build_access_snapshot(user)

		self.assertIn("read", snapshot["doctypes"].get(doctype, []))
		self.assertNotIn("create", snapshot["doctypes"].get(doctype, []))

	def test_missing_docperm_does_not_report_doctype(self):
		doctype = self._ensure_test_doctype("Pet App Test Snapshot Missing")
		user = self._ensure_user("snapshot.missing@example.com")

		snapshot = permissions.build_access_snapshot(user)

		self.assertNotIn("read", snapshot["doctypes"].get(doctype, []))

	def test_create_and_write_appear_without_backend_registry_entries(self):
		doctype = self._ensure_test_doctype("Pet App Test Snapshot Manage")
		role = self._ensure_role("Pet App Test Snapshot Manage Role")
		user = self._ensure_user("snapshot.manage@example.com", roles=[role])
		self._grant_doctype_permission(doctype, role, ("read", "create", "write"))

		snapshot = permissions.build_access_snapshot(user)

		self.assertEqual({"read", "create", "write"}, set(snapshot["doctypes"].get(doctype, [])))

	def test_role_profile_removal_immediately_removes_composed_docperms(self):
		doctype = self._ensure_test_doctype("Pet App Test Snapshot Profile")
		role = self._ensure_role("Pet App Test Snapshot Profile Role")
		profile = self._ensure_role_profile("Pet App Test Snapshot Profile", [role])
		user = self._ensure_user("snapshot.profile@example.com")
		self._grant_doctype_permission(doctype, role, ("read",))

		self._assign_role_profile(user, profile)
		self.assertIn(profile, permissions.get_user_role_profiles(user))
		self.assertIn("read", permissions.build_access_snapshot(user)["doctypes"].get(doctype, []))

		user_doc = frappe.get_doc("User", user)
		user_doc.set("role_profiles", [])
		if user_doc.meta.has_field("role_profile_name"):
			user_doc.role_profile_name = None
		user_doc.save(ignore_permissions=True)
		frappe.clear_cache(user=user)

		self.assertEqual([], permissions.get_user_role_profiles(user))
		self.assertNotIn(role, permissions.get_user_roles(user))
		self.assertNotIn("read", permissions.build_access_snapshot(user)["doctypes"].get(doctype, []))

	def test_seed_role_profiles_do_not_hard_sync_existing_profiles(self):
		role_a = self._ensure_role("Pet App Test Seed Role A")
		role_b = self._ensure_role("Pet App Test Seed Role B")
		profile = "Visit Read Profile"
		self._ensure_role_profile(profile, [role_b])

		try:
			permissions.ensure_role_profiles()
			profile_doc = frappe.get_doc("Role Profile", profile)
			profile_roles = {row.role for row in profile_doc.roles}

			self.assertIn(role_b, profile_roles)
			self.assertNotIn(role_a, profile_roles)
		finally:
			self._ensure_role_profile(profile, ["Desk User", "Visit Read"])

	def test_legacy_app_resource_helpers_no_longer_grant_access(self):
		frappe.set_user("Administrator")

		self.assertEqual({}, permissions.MODULE_RULES)
		self.assertEqual({}, permissions.PAGE_RULES)
		self.assertEqual({}, permissions.ACTION_RULES)
		self.assertEqual([], permissions.get_allowed_resources("Administrator", "Action"))
		self.assertFalse(permissions.has_app_permission("action.anything", "Administrator"))
		with self.assertRaises(frappe.PermissionError):
			permissions.require_app_permission("action.anything", "Administrator")

		matrix = permissions.get_access_matrix()
		self.assertTrue(matrix["deprecated"])
		self.assertEqual([], matrix["modules"])
		self.assertEqual([], matrix["pages"])
		self.assertEqual([], matrix["actions"])

	def test_operational_read_roles_are_read_only(self):
		role_doctypes = {
			"Visit Read": ("Vet Visit",),
			"Lab Read": ("Lab",),
			"Radiology Read": ("Imaging",),
			"Warehouse Read": ("Stock Entry", "Purchase Invoice", "Item"),
			"Accounting Read": ("Sales Invoice", "Payment Entry", "Journal Entry"),
			"Audit Read": ("Version", "Activity Log"),
		}

		for role, doctypes in role_doctypes.items():
			snapshot = self._snapshot_for_role(role)
			for doctype in doctypes:
				if not self._doctype_exists(doctype):
					continue
				perms = set(snapshot["doctypes"].get(doctype, []))
				self.assertIn("read", perms, f"{role} should read {doctype}")
				self.assertFalse(
					{"create", "write", "delete", "submit", "cancel", "amend"} & perms,
					f"{role} should not mutate {doctype}: {sorted(perms)}",
				)

	def test_operational_admin_roles_receive_management_permissions(self):
		role_expectations = {
			"Visit Admin": {"Vet Visit": {"read", "create", "write"}},
			"Lab Admin": {"Lab": {"read", "create", "write"}},
			"Radiology Admin": {"Imaging": {"read", "create", "write"}},
			"POS Admin": {
				"Sales Invoice": {"read", "create", "write"},
				"POS Profile": {"read", "create", "write"},
			},
			"Warehouse Admin": {
				"Stock Entry": {"read", "create", "write"},
				"Purchase Invoice": {"read", "create", "write"},
				"Item": {"read", "create", "write", "delete"},
			},
			"Accounting Admin": {
				"Payment Entry": {"read", "create", "write"},
				"Journal Entry": {"read", "create", "write"},
				"Account": {"read", "create", "write", "delete"},
			},
			"Ecommerce Admin": {
				"Item": {"read", "create", "write", "delete"},
				"Sales Order": {"read", "create", "write"},
			},
			"Service Provider Manager": {
				"PetCareService": {"read", "write", "delete"},
				"CareService template": {"read", "write", "delete"},
			},
		}

		for role, doctype_expectations in role_expectations.items():
			snapshot = self._snapshot_for_role(role)
			for doctype, expected in doctype_expectations.items():
				if not self._doctype_exists(doctype):
					continue
				perms = set(snapshot["doctypes"].get(doctype, []))
				self.assertTrue(expected <= perms, f"{role} missing {expected - perms} on {doctype}: {sorted(perms)}")
				if "submit" in operational_healthcare_roles.OPERATIONAL_HEALTHCARE_ROLE_PERMISSIONS[role].get(doctype, ()):
					if operational_healthcare_roles.is_submittable(doctype):
						self.assertIn("submit", perms, f"{role} should submit {doctype}")

	def test_operational_supporting_dependencies_are_covered(self):
		expectations = {
			"POS Cashier": {
				"POS Profile": {"read"},
				"Item Price": {"read"},
				"Item Group": {"read"},
				"Warehouse": {"read"},
				"Account": {"read"},
			},
			"Warehouse Admin": {
				"File": {"read", "create", "delete"},
				"Payment Entry": {"read", "create", "write"},
				"Supplier": {"read"},
				"Account": {"read"},
			},
			"Accounting Read": {
				"Address": {"read"},
				"POS Profile": {"read"},
				"Item Price": {"read"},
			},
			"Reception": {
				"Address": {"read", "create", "write"},
				"File": {"read", "create", "delete"},
				"Care Service Billing Option": {"read"},
				"Item Price": {"read"},
			},
			"Service Provider Manager": {
				"Care Service Billing Option": {"read"},
				"CategoryCareServices": {"read"},
				"Item": {"read"},
				"Item Price": {"read"},
				"Price List": {"read"},
			},
		}

		for role, doctype_expectations in expectations.items():
			snapshot = self._snapshot_for_role(role)
			for doctype, expected in doctype_expectations.items():
				if not self._doctype_exists(doctype):
					continue
				perms = set(snapshot["doctypes"].get(doctype, []))
				self.assertTrue(expected <= perms, f"{role} missing {expected - perms} on {doctype}: {sorted(perms)}")

	def test_service_provider_manager_updates_and_deletes_without_create(self):
		snapshot = self._snapshot_for_role("Service Provider Manager")

		for doctype in ("PetCareService", "CareService template"):
			if not self._doctype_exists(doctype):
				continue
			perms = set(snapshot["doctypes"].get(doctype, []))
			self.assertTrue({"read", "write", "delete"} <= perms, f"missing update/delete on {doctype}: {sorted(perms)}")
			self.assertNotIn("create", perms, f"Service Provider Manager should not create {doctype}: {sorted(perms)}")

		for doctype in ("Item", "Item Price", "Price List"):
			if not self._doctype_exists(doctype):
				continue
			perms = set(snapshot["doctypes"].get(doctype, []))
			self.assertIn("read", perms, f"Service Provider Manager should read {doctype}")
			self.assertFalse(
				{"create", "write", "delete", "submit", "cancel", "amend"} & perms,
				f"Service Provider Manager should only read {doctype}: {sorted(perms)}",
			)

	def test_healthcare_roles_cover_clinical_master_read_dependencies(self):
		roles = (
			"Visit Read",
			"Visit Admin",
			"Lab Read",
			"Lab Admin",
			"Radiology Read",
			"Radiology Admin",
			"Reception",
			"Coordinator",
		)
		doctypes = (
			"Procedure Template",
			"CareService template",
			"CategoryCareServices",
			"Care Service Billing Option",
			"Disease",
			"Medication",
			"Healthcare Practitioner",
		)

		for role in roles:
			snapshot = self._snapshot_for_role(role)
			for doctype in doctypes:
				if not self._doctype_exists(doctype):
					continue
				perms = set(snapshot["doctypes"].get(doctype, []))
				self.assertIn("read", perms, f"{role} should read clinical master {doctype}")
				if role.endswith("Read"):
					self.assertFalse(
						{"create", "write", "delete", "submit", "cancel", "amend"} & perms,
						f"{role} should only read clinical master {doctype}: {sorted(perms)}",
					)

	def test_operational_role_snapshots_have_no_pages_without_registry_rows(self):
		snapshot = self._snapshot_for_role("POS Admin")

		self.assertEqual([], snapshot["modules"])
		self.assertEqual([], snapshot["pages"])
		self.assertEqual([], snapshot["actions"])

	def _ensure_test_doctype(self, doctype: str) -> str:
		frappe.set_user("Administrator")
		if frappe.db.exists("DocType", doctype):
			return doctype

		doc = frappe.get_doc(
			{
				"doctype": "DocType",
				"name": doctype,
				"module": "Pet App",
				"custom": 1,
				"istable": 0,
				"fields": [
					{
						"fieldname": "title",
						"label": "Title",
						"fieldtype": "Data",
						"in_list_view": 1,
					}
				],
				"permissions": [],
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.clear_cache(doctype=doctype)
		return doctype

	def _ensure_role(self, role_name: str) -> str:
		frappe.set_user("Administrator")
		if not frappe.db.exists("Role", role_name):
			frappe.get_doc(
				{
					"doctype": "Role",
					"role_name": role_name,
					"desk_access": 1,
					"is_custom": 1,
				}
			).insert(ignore_permissions=True)
		return role_name

	def _ensure_role_profile(self, profile_name: str, roles: list[str]) -> str:
		frappe.set_user("Administrator")
		if frappe.db.exists("Role Profile", profile_name):
			profile = frappe.get_doc("Role Profile", profile_name)
		else:
			profile = frappe.get_doc({"doctype": "Role Profile", "role_profile": profile_name, "roles": []})

		profile.set("roles", [])
		for role in roles:
			self._ensure_role(role)
			profile.append("roles", {"role": role})

		if profile.is_new():
			profile.insert(ignore_permissions=True)
		else:
			profile.save(ignore_permissions=True)
		return profile_name

	def _ensure_user(self, email: str, roles: list[str] | None = None) -> str:
		frappe.set_user("Administrator")
		roles = roles or []
		if frappe.db.exists("User", email):
			user = frappe.get_doc("User", email)
		else:
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@", 1)[0].replace(".", " ").title(),
					"enabled": 1,
					"user_type": "System User",
					"send_welcome_email": 0,
				}
			)

		user.enabled = 1
		user.user_type = "System User"
		if user.meta.has_field("role_profile_name"):
			user.role_profile_name = None
		if user.meta.has_field("role_profiles"):
			user.set("role_profiles", [])
		user.set("roles", [])
		for role in roles:
			self._ensure_role(role)
			user.append("roles", {"role": role})

		if user.is_new():
			user.insert(ignore_permissions=True)
		else:
			user.save(ignore_permissions=True)
		frappe.clear_cache(user=email)
		return email

	def _assign_role_profile(self, user: str, profile: str):
		user_doc = frappe.get_doc("User", user)
		user_doc.set("role_profiles", [])
		if user_doc.meta.has_field("role_profile_name"):
			user_doc.role_profile_name = None
		user_doc.append("role_profiles", {"role_profile": profile})
		user_doc.save(ignore_permissions=True)
		frappe.clear_cache(user=user)

	def _grant_doctype_permission(self, doctype: str, role: str, rights: tuple[str, ...]):
		frappe.set_user("Administrator")
		self._ensure_role(role)
		for name in frappe.get_all(
			"Custom DocPerm",
			filters={"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0},
			pluck="name",
			ignore_permissions=True,
		):
			frappe.delete_doc("Custom DocPerm", name, ignore_permissions=True, force=True)

		add_permission(doctype, role, 0)
		for ptype in SNAPSHOT_PTYPES:
			if frappe.get_meta("Custom DocPerm").has_field(ptype):
				update_permission_property(doctype, role, 0, ptype, 1 if ptype in rights else 0)
		frappe.clear_cache(doctype=doctype)

	def _snapshot_for_role(self, role: str) -> dict:
		user = self._ensure_user(f"snapshot.{role.lower().replace(' ', '.')}@example.com", roles=[role])
		return permissions.build_access_snapshot(user)

	def _doctype_exists(self, doctype: str) -> bool:
		return bool(frappe.db.exists("DocType", doctype))

	def _clear_page_access_settings(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("DocType", permissions.PAGE_ACCESS_SETTINGS_DOCTYPE):
			return
		settings = frappe.get_single(permissions.PAGE_ACCESS_SETTINGS_DOCTYPE)
		settings.set("pages", [])
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=permissions.PAGE_ACCESS_SETTINGS_DOCTYPE)
