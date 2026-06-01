from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils.password import update_password


PASSWORD = "Test@12345"
ALLOWED_PERM_EXCEPTIONS = (
	"ValidationError",
	"MandatoryError",
	"DoesNotExistError",
	"LinkValidationError",
	"TypeError",
)

PROFILE_USERS = {
	"Healthcare Profile": "permtest.healthcare@petapp.local",
	"Pet Profile": "permtest.pet@petapp.local",
	"Ecommerce Profile": "permtest.ecommerce@petapp.local",
	"Order Profile": "permtest.order@petapp.local",
	"POS Profile": "permtest.pos@petapp.local",
	"Accounting Profile": "permtest.accounting@petapp.local",
	"Warehouse Profile": "permtest.warehouse@petapp.local",
	"Audit Profile": "permtest.audit@petapp.local",
	"Users Profile": "permtest.users@petapp.local",
	"Guardians Profile": "permtest.guardians@petapp.local",
	"Settings Profile": "permtest.settings@petapp.local",
}

EXPECTED_PROFILE_ROLES = {
	"Healthcare Profile": {"Healthcare", "Healthcare Practitioner"},
	"Pet Profile": {"Pet"},
	"Ecommerce Profile": {"E-commerce"},
	"Order Profile": {"Order"},
	"POS Profile": {"POS"},
	"Accounting Profile": {"Accounting"},
	"Warehouse Profile": {"Warehouse"},
	"Audit Profile": {"Audit"},
	"Users Profile": {"Users"},
	"Guardians Profile": {"Guardians", "Guardian"},
	"Settings Profile": {"Setting"},
}

DOCTYPE_TESTS = {
	"Healthcare Profile": {
		"allow": [("Vet Visit", "create"), ("Lab", "create"), ("Vet Case Sheet", "read")],
		"deny": [("Sales Invoice", "create"), ("Pet App Accounting Settings", "read")],
	},
	"Pet Profile": {
		"allow": [("Pet", "create"), ("PetGuardian", "create"), ("PetAddRequest", "create")],
		"deny": [("Sales Invoice", "create"), ("User", "read", "Administrator")],
	},
	"Ecommerce Profile": {
		"allow": [("Product", "create"), ("Product", "write"), ("Item", "read")],
		"deny": [("Sales Invoice", "create"), ("Vet Visit", "create")],
	},
	"Order Profile": {
		"allow": [("Sales Order", "create"), ("Delivery Note", "create"), ("Customer", "read")],
		"deny": [("Vet Visit", "create"), ("Pet App Accounting Settings", "read")],
	},
	"POS Profile": {
		"allow": [("Sales Invoice", "create"), ("Payment Entry", "create"), ("Mode of Payment", "read")],
		"deny": [("Vet Visit", "create"), ("User", "read", "Administrator")],
	},
	"Accounting Profile": {
		"allow": [("Sales Invoice", "create"), ("Payment Entry", "create"), ("Mode of Payment", "read")],
		"deny": [("Vet Visit", "create"), ("Pet Boarding", "create")],
	},
	"Warehouse Profile": {
		"allow": [("Stock Entry", "create"), ("Warehouse", "read"), ("Bin", "read")],
		"deny": [("Sales Invoice", "create"), ("Vet Visit", "create")],
	},
	"Audit Profile": {
		"allow": [("Audit Trail", "read"), ("Version", "read"), ("Error Log", "read")],
		"deny": [("Sales Invoice", "create"), ("Vet Visit", "create")],
	},
	"Users Profile": {
		"allow": [("User", "create"), ("Role Profile", "read"), ("Role", "read")],
		"deny": [("Sales Invoice", "create"), ("Vet Visit", "create")],
	},
	"Guardians Profile": {
		"allow": [("Guardian", "read"), ("Pet", "create"), ("PetGuardian", "read")],
		"deny": [("Sales Invoice", "create"), ("User", "read", "Administrator")],
	},
	"Settings Profile": {
		"allow": [("Pet App Accounting Settings", "read"), ("Pet Boarding Settings", "read"), ("Selling Settings", "write")],
		"deny": [("Sales Invoice", "create"), ("Vet Visit", "create")],
	},
}

API_TESTS = {
	"Healthcare Profile": {
		"allow": [("pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet.start_visit", ("MISSING-CASE-SHEET",), {})],
		"deny": [("pet_app.api.accounting.settings.get_accounting_settings", (), {})],
	},
	"Ecommerce Profile": {
		"allow": [("pet_app.api.product.get_products", (), {})],
		"deny": [("pet_app.api.accounting.settings.get_accounting_settings", (), {})],
	},
	"Order Profile": {
		"allow": [("pet_app.api.order.place_order", (), {"items": "[]", "guardian": "MISSING-GUARDIAN"})],
		"deny": [("pet_app.api.accounting.settings.get_accounting_settings", (), {})],
	},
	"POS Profile": {
		"allow": [("pet_app.api.accounting.cashier.list_cashier_profiles_for_user", (), {})],
		"deny": [("pet_app.api.accounting.settings.get_accounting_settings", (), {})],
	},
	"Accounting Profile": {
		"allow": [("pet_app.api.accounting.settings.get_accounting_settings", (), {})],
		"deny": [("pet_app.pet_app.doctype.vet_case_sheet.vet_case_sheet.start_visit", ("MISSING-CASE-SHEET",), {})],
	},
	"Users Profile": {
		"allow": [("pet_app.api.users.get_all_role_profiles_with_roles", (), {})],
		"deny": [("pet_app.api.accounting.settings.get_accounting_settings", (), {})],
	},
	"Guardians Profile": {
		"allow": [],
		"deny": [],
	},
}


@contextmanager
def user_context(user: str):
	previous_user = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous_user)


def _full_name_from_profile(profile_name: str) -> str:
	return profile_name.replace(" Profile", " Test User")


def _ensure_test_user(profile_name: str, email: str) -> dict:
	full_name = _full_name_from_profile(profile_name)

	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.new_doc("User")
		user.email = email
		user.first_name = full_name
		user.full_name = full_name
		user.enabled = 1
		user.user_type = "System User"
		user.send_welcome_email = 0

	user.first_name = full_name
	user.full_name = full_name
	user.enabled = 1
	user.user_type = "System User"
	user.send_welcome_email = 0
	user.set("role_profiles", [])
	user.append("role_profiles", {"role_profile": profile_name})
	user.set("roles", [])
	user.save(ignore_permissions=True)
	update_password(user.name, PASSWORD, logout_all_sessions=False)

	actual_roles = {row.role for row in user.roles if row.role}
	return {
		"user": user.name,
		"role_profile": profile_name,
		"roles": sorted(actual_roles),
		"role_match": actual_roles == EXPECTED_PROFILE_ROLES[profile_name],
	}


def _perm_result(user: str, doctype: str, ptype: str, expect: bool, doc: str | None = None) -> dict:
	with user_context(user):
		allowed = bool(frappe.has_permission(doctype, doc=doc, ptype=ptype, user=user))
	return {
		"kind": "doctype",
		"doctype": doctype,
		"doc": doc,
		"ptype": ptype,
		"expected": "PASS" if expect else "FAIL",
		"actual": "PASS" if allowed else "FAIL",
		"status": "PASS" if allowed == expect else "FAIL",
	}


def _api_result(user: str, dotted_path: str, args: tuple, kwargs: dict, expect: bool) -> dict:
	method = frappe.get_attr(dotted_path)
	with user_context(user):
		try:
			method(*args, **kwargs)
			outcome = True
			error = None
		except frappe.PermissionError as exc:
			outcome = False
			error = f"{exc.__class__.__name__}: {exc}"
		except Exception as exc:
			outcome = True if expect and exc.__class__.__name__ in ALLOWED_PERM_EXCEPTIONS else False
			error = f"{exc.__class__.__name__}: {exc}"

	return {
		"kind": "api",
		"method": dotted_path,
		"expected": "PASS" if expect else "FAIL",
		"actual": "PASS" if outcome else "FAIL",
		"status": "PASS" if outcome == expect else "FAIL",
		"detail": error,
	}


def _ensure_guardian_fixture():
	user = PROFILE_USERS["Guardians Profile"]
	other_user = "permtest.guardians.other@petapp.local"

	_ensure_test_user("Guardians Profile", other_user)

	guardian_name = _upsert_guardian(user, "07790000001", "Guardian Primary")
	other_guardian_name = _upsert_guardian(other_user, "07790000002", "Guardian Other")
	own_pet = _upsert_pet("Audit Own Pet", guardian_name)
	other_pet = _upsert_pet("Audit Other Pet", other_guardian_name)

	return {
		"guardian": guardian_name,
		"other_guardian": other_guardian_name,
		"own_pet": own_pet,
		"other_pet": other_pet,
	}


def _upsert_guardian(user: str, phone: str, full_name: str) -> str:
	name = frappe.db.get_value("Guardian", {"user_id": user}, "name")
	payload = {
		"phone": phone,
		"user_id": user,
		"full_name": full_name,
		"is_active": 1,
		"otp_verified": 1,
		"country": "Iraq",
	}
	if name:
		frappe.db.set_value("Guardian", name, payload, update_modified=False)
		return name
	doc = frappe.get_doc({"doctype": "Guardian", **payload})
	doc.insert(ignore_permissions=True)
	return doc.name


def _upsert_pet(pet_name: str, guardian_name: str) -> str:
	name = frappe.db.get_value("Pet", {"pet_name": pet_name, "requested_by": guardian_name}, "name")
	payload = {
		"pet_name": pet_name,
		"pet_status": "Approved",
		"requested_by": guardian_name,
	}
	if name:
		frappe.db.set_value("Pet", name, payload, update_modified=False)
		pet_name_value = name
	else:
		doc = frappe.get_doc({"doctype": "Pet", **payload})
		doc.insert(ignore_permissions=True)
		pet_name_value = doc.name

	link_name = frappe.db.get_value("PetGuardian", {"pet_id": pet_name_value, "guardian_id": guardian_name}, "name")
	if not link_name:
		frappe.get_doc(
			{
				"doctype": "PetGuardian",
				"pet_id": pet_name_value,
				"guardian_id": guardian_name,
				"role": "primary_owner",
			}
		).insert(ignore_permissions=True)

	return pet_name_value


def _guardian_api_results(user: str, fixture: dict) -> list[dict]:
	results = []
	results.append(
		_api_result(
			user,
			"pet_app.api.auth_mobile.get_guardian_profile",
			(fixture["guardian"],),
			{},
			True,
		)
	)
	results.append(
		_api_result(
			user,
			"pet_app.api.auth_mobile.get_guardian_profile",
			(fixture["other_guardian"],),
			{},
			False,
		)
	)
	results.append(
		_api_result(
			user,
			"pet_app.api.pet.get_pet",
			(fixture["own_pet"],),
			{},
			True,
		)
	)
	results.append(
		_api_result(
			user,
			"pet_app.api.pet.get_pet",
			(fixture["other_pet"],),
			{},
			False,
		)
	)
	return results


def run():
	report = {
		"site": frappe.local.site,
		"users": [],
		"mismatches": [],
	}

	guardian_fixture = None

	for profile_name, email in PROFILE_USERS.items():
		user_info = _ensure_test_user(profile_name, email)
		user_report = {
			"user": user_info["user"],
			"role_profile": profile_name,
			"roles": user_info["roles"],
			"role_match": user_info["role_match"],
			"visible_modules": [profile_name],
			"doctype_tests": [],
			"api_tests": [],
		}

		if not user_info["role_match"]:
			report["mismatches"].append(
				{
					"user": user_info["user"],
					"role_profile": profile_name,
					"issue": "Role set does not match expected profile roles",
					"actual_roles": user_info["roles"],
				}
			)

		for entry in DOCTYPE_TESTS.get(profile_name, {}).get("allow", []):
			doctype, ptype = entry[0], entry[1]
			doc = entry[2] if len(entry) > 2 else None
			result = _perm_result(user_info["user"], doctype, ptype, True, doc=doc)
			user_report["doctype_tests"].append(result)
			if result["status"] == "FAIL":
				report["mismatches"].append(
					{
						"user": user_info["user"],
						"role_profile": profile_name,
						"issue": f"Expected allow on {doctype}:{ptype}",
					}
				)

		for entry in DOCTYPE_TESTS.get(profile_name, {}).get("deny", []):
			doctype, ptype = entry[0], entry[1]
			doc = entry[2] if len(entry) > 2 else None
			result = _perm_result(user_info["user"], doctype, ptype, False, doc=doc)
			user_report["doctype_tests"].append(result)
			if result["status"] == "FAIL":
				report["mismatches"].append(
					{
						"user": user_info["user"],
						"role_profile": profile_name,
						"issue": f"Expected deny on {doctype}:{ptype}",
					}
				)

		for method, args, kwargs in API_TESTS.get(profile_name, {}).get("allow", []):
			result = _api_result(user_info["user"], method, args, kwargs, True)
			user_report["api_tests"].append(result)
			if result["status"] == "FAIL":
				report["mismatches"].append(
					{
						"user": user_info["user"],
						"role_profile": profile_name,
						"issue": f"Expected API allow on {method}",
						"detail": result.get("detail"),
					}
				)

		for method, args, kwargs in API_TESTS.get(profile_name, {}).get("deny", []):
			result = _api_result(user_info["user"], method, args, kwargs, False)
			user_report["api_tests"].append(result)
			if result["status"] == "FAIL":
				report["mismatches"].append(
					{
						"user": user_info["user"],
						"role_profile": profile_name,
						"issue": f"Expected API deny on {method}",
						"detail": result.get("detail"),
					}
				)

		if profile_name == "Guardians Profile":
			if guardian_fixture is None:
				guardian_fixture = _ensure_guardian_fixture()
			for result in _guardian_api_results(user_info["user"], guardian_fixture):
				user_report["api_tests"].append(result)
				if result["status"] == "FAIL":
					report["mismatches"].append(
						{
							"user": user_info["user"],
							"role_profile": profile_name,
							"issue": f"Guardian isolation test failed for {result.get('method')}",
							"detail": result.get("detail"),
						}
					)

		report["users"].append(user_report)

	frappe.response["message"] = report
	return report
