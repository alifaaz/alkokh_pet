from __future__ import annotations

import json

import frappe
from frappe.utils import cstr


SENSITIVE_KEYS = {"otp", "otp_code", "code", "password", "token", "api_secret", "access_token", "app_secret"}


def coerce_context(context=None) -> dict:
	if not context:
		return {}
	if isinstance(context, str):
		return json.loads(context)
	return dict(context)


def mask_sensitive_context(context: dict | None) -> dict:
	masked = {}
	for key, value in (context or {}).items():
		if key.lower() in SENSITIVE_KEYS:
			masked[key] = mask_value(value)
		elif isinstance(value, dict):
			masked[key] = mask_sensitive_context(value)
		elif isinstance(value, list):
			masked[key] = [mask_sensitive_context(row) if isinstance(row, dict) else row for row in value]
		else:
			masked[key] = value
	return masked


def mask_value(value) -> str:
	text = cstr(value)
	if not text:
		return ""
	if len(text) <= 2:
		return "*" * len(text)
	return f"{text[:1]}{'*' * max(len(text) - 2, 1)}{text[-1:]}"


def mask_phone(phone) -> str:
	text = cstr(phone)
	if len(text) <= 4:
		return "*" * len(text)
	return f"{'*' * max(len(text) - 4, 0)}{text[-4:]}"


def recipient_phone(recipient_type: str, recipient_name: str | None = None, to_phone: str | None = None) -> str | None:
	if to_phone:
		return to_phone
	if not recipient_name:
		return None
	if recipient_type == "Guardian":
		return frappe.db.get_value("Guardian", recipient_name, "phone")
	if recipient_type == "Customer":
		return frappe.db.get_value("Customer", recipient_name, "mobile_no") or frappe.db.get_value("Customer", recipient_name, "phone")
	if recipient_type == "User":
		return frappe.db.get_value("User", recipient_name, "mobile_no") or frappe.db.get_value("User", recipient_name, "phone")
	if recipient_type == "Doctor":
		return frappe.db.get_value("Healthcare Practitioner", recipient_name, "phone")
	return to_phone


def recipient_email(recipient_type: str, recipient_name: str | None = None, to_email: str | None = None) -> str | None:
	if to_email:
		return to_email
	if not recipient_name:
		return None
	if recipient_type == "Guardian":
		return frappe.db.get_value("Guardian", recipient_name, "email_id")
	if recipient_type == "Customer":
		return frappe.db.get_value("Customer", recipient_name, "email_id")
	if recipient_type == "User":
		return frappe.db.get_value("User", recipient_name, "email")
	return to_email


def normalize_phone(phone: str | None, default_country_code: str | None = None) -> str | None:
	text = cstr(phone).strip().replace(" ", "").replace("-", "")
	if not text:
		return None
	if text.startswith("+"):
		text = text[1:]
	if text.startswith("00"):
		text = text[2:]
	country = cstr(default_country_code).strip().lstrip("+")
	if country and text.startswith("0"):
		return country + text[1:]
	return text

