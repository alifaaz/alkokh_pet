# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import check_password, update_password

from pet_app.api.users import change_user_password


class TestUsersApi(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.target_user = cls._ensure_user("password-target@example.com")
        cls.regular_user = cls._ensure_user("password-regular@example.com")
        update_password(cls.target_user, "InitialPass123!")
        update_password(cls.regular_user, "InitialPass123!")

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        for user in ("password-target@example.com", "password-regular@example.com"):
            if frappe.db.exists("User", user):
                frappe.delete_doc("User", user, force=True)
        frappe.db.commit()
        super().tearDownClass()

    def tearDown(self):
        frappe.set_user("Administrator")
        update_password(self.target_user, "InitialPass123!")
        update_password(self.regular_user, "InitialPass123!")

    @staticmethod
    def _ensure_user(email):
        if frappe.db.exists("User", email):
            return email

        frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": email.split("@", 1)[0],
                "send_welcome_email": 0,
                "enabled": 1,
            }
        ).insert(ignore_permissions=True)
        return email

    def test_change_user_password_allows_administrator(self):
        frappe.set_user("Administrator")

        response = change_user_password(self.target_user, "NewStrongPass123!")

        self.assertEqual(response, {"message": "Password updated successfully"})
        self.assertTrue(check_password(self.target_user, "NewStrongPass123!"))

    def test_change_user_password_denies_non_admin_user(self):
        frappe.set_user(self.regular_user)

        with self.assertRaises(frappe.PermissionError):
            change_user_password(self.target_user, "BlockedPass123!")

    def test_change_user_password_requires_user(self):
        frappe.set_user("Administrator")

        with self.assertRaises(frappe.ValidationError):
            change_user_password("", "MissingUserPass123!")
