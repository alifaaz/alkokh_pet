from __future__ import annotations

from types import SimpleNamespace

import frappe
from frappe.auth import CookieManager
from frappe.tests.utils import FrappeTestCase
from werkzeug.datastructures import Headers
from werkzeug.wrappers import Response

from pet_app.api import session_guard


class TestApiSessionGuard(FrappeTestCase):
	def setUp(self):
		self._original_user = frappe.session.user
		self._original_cookie_manager = getattr(frappe.local, "cookie_manager", None)
		self._original_request = getattr(frappe.local, "request", None)
		frappe.local.cookie_manager = CookieManager()

	def tearDown(self):
		frappe.set_user(self._original_user)
		if self._original_cookie_manager is None:
			self._clear_local("cookie_manager")
		else:
			frappe.local.cookie_manager = self._original_cookie_manager
		if self._original_request is None:
			self._clear_local("request")
		else:
			frappe.local.request = self._original_request

	def test_after_request_removes_queued_guest_identity_cookies(self):
		frappe.local.cookie_manager.set_cookie("sid", "Guest")
		frappe.local.cookie_manager.set_cookie("user_id", "Guest")
		frappe.local.cookie_manager.set_cookie("full_name", "Guest")
		frappe.local.cookie_manager.set_cookie("system_user", "no")
		frappe.local.cookie_manager.set_cookie("user_lang", "en")

		session_guard.after_request(response=Response(), request=self._request("/api/resource/Appointment"))

		for key in session_guard.GUEST_IDENTITY_COOKIE_KEYS:
			self.assertNotIn(key, frappe.local.cookie_manager.cookies)

	def test_after_request_keeps_real_session_cookies(self):
		frappe.local.cookie_manager.set_cookie("sid", "real-session")
		frappe.local.cookie_manager.set_cookie("user_id", "doctor@example.com")
		frappe.local.cookie_manager.set_cookie("full_name", "Doctor User")
		frappe.local.cookie_manager.set_cookie("system_user", "yes")

		session_guard.after_request(response=Response(), request=self._request("/api/resource/Appointment"))

		self.assertEqual("real-session", frappe.local.cookie_manager.cookies["sid"]["value"])
		self.assertEqual("doctor@example.com", frappe.local.cookie_manager.cookies["user_id"]["value"])

	def test_protected_resource_is_401_for_guest(self):
		frappe.set_user("Guest")
		frappe.local.request = self._request("/api/resource/Appointment")

		with self.assertRaises(frappe.AuthenticationError):
			session_guard.enforce_authenticated_api_access()

	def test_get_current_access_is_401_for_guest(self):
		frappe.set_user("Guest")
		frappe.local.request = self._request("/api/method/pet_app.api.permissions.get_current_access")

		with self.assertRaises(frappe.AuthenticationError):
			session_guard.enforce_authenticated_api_access()

	def test_public_guest_methods_stay_public(self):
		frappe.set_user("Guest")
		for path in (
			"/api/method/pet_app.api.whatsapp.webhook",
			"/api/method/pet_app.api.auth_api.login_and_get_oauth_token",
			"/api/method/pet_app.api.mobile.auth.sign_in",
			"/api/method/pet_app.api.mobile.config.get_config",
		):
			frappe.local.request = self._request(path, method="POST" if "auth" in path else "GET")
			session_guard.enforce_authenticated_api_access()

	def test_api_preflight_gets_short_cache_window(self):
		response = Response()
		request = self._request(
			"/api/resource/Appointment",
			method="OPTIONS",
			headers={"Origin": "https://clinic.kokh-vet.com"},
		)

		session_guard.after_request(response=response, request=request)

		self.assertEqual("600", response.headers.get("Access-Control-Max-Age"))

	@staticmethod
	def _clear_local(key: str):
		try:
			delattr(frappe.local, key)
		except AttributeError:
			pass

	@staticmethod
	def _request(path: str, method: str = "GET", headers: dict | None = None):
		return SimpleNamespace(path=path, method=method, headers=Headers(headers or {}))
