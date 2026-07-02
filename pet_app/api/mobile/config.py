from __future__ import annotations

import functools

import frappe
from frappe.utils import cstr

from pet_app.api.mobile.response import error, ok

SETTINGS_DOCTYPE = "Pet App Mobile Settings"


def _mobile_config_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			return ok(fn(*args, **kwargs))
		except Exception as exc:
			return error(getattr(exc, "code", None) or exc.__class__.__name__, cstr(exc), 400)

	return wrapper


def _mobile_settings():
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return None
	try:
		return frappe.get_single(SETTINGS_DOCTYPE)
	except Exception:
		return None


def _setting(settings, fieldname: str, fallback=None):
	if settings and settings.meta.has_field(fieldname):
		value = settings.get(fieldname)
		if value not in (None, ""):
			return value
	return fallback


def _csv(value, fallback: list[str]) -> list[str]:
	if not value:
		return fallback
	if isinstance(value, list):
		return [cstr(item).strip() for item in value if cstr(item).strip()]
	return [item.strip() for item in cstr(value).split(",") if item.strip()] or fallback


def _currency() -> str:
	return (
		frappe.db.get_single_value("Global Defaults", "default_currency")
		or frappe.db.get_default("currency")
		or "IQD"
	)


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_config_endpoint
def get_config(**kwargs):
	settings = _mobile_settings()
	return {
		"currency": _setting(settings, "currency", _currency()),
		"supported_locales": _csv(_setting(settings, "supported_locales"), ["en", "ar"]),
		"default_locale": _setting(settings, "default_locale", "en"),
		"feature_flags": {
			"auth": True,
			"orders": True,
			"catalog": True,
			"addresses": True,
			"pets": True,
			"favorites": True,
			"reviews": True,
			"recent_searches": True,
			"devices": True,
			"file_uploads": True,
			"pet_medical_records": True,
			"cart": False,
			"payments": False,
			"push_notifications": False,
		},
		"min_versions": {
			"android": _setting(settings, "min_android_version"),
			"ios": _setting(settings, "min_ios_version"),
		},
	}


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_config_endpoint
def support_contact(**kwargs):
	settings = _mobile_settings()
	return {
		"name": _setting(settings, "support_name", "Alkokh Support"),
		"phone": _setting(settings, "support_phone"),
		"email": _setting(settings, "support_email"),
		"whatsapp": _setting(settings, "support_whatsapp"),
	}


@frappe.whitelist(allow_guest=True, methods=["GET"])
@_mobile_config_endpoint
def content(key=None, **kwargs):
	key = cstr(key).strip()
	settings = _mobile_settings()
	content_map = {
		"privacy": {
			"key": "privacy",
			"title": _setting(settings, "privacy_title", "Privacy Policy"),
			"body": _setting(settings, "privacy_body", ""),
		},
		"terms": {
			"key": "terms",
			"title": _setting(settings, "terms_title", "Terms of Service"),
			"body": _setting(settings, "terms_body", ""),
		},
		"faq": {
			"key": "faq",
			"title": _setting(settings, "faq_title", "FAQ"),
			"body": _setting(settings, "faq_body", ""),
		},
	}
	return content_map.get(key) or {"key": key, "title": key.replace("_", " ").title(), "body": ""}
