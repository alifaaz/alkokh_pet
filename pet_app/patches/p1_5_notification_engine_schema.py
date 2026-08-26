from __future__ import annotations

import frappe


MODULE = "Pet App"


def execute():
	ensure_notification_schema()


def ensure_notification_schema():
	for doctype, spec in DOCTYPES.items():
		ensure_doctype(doctype, spec)
	if should_seed_notification_defaults():
		seed_defaults()
	frappe.clear_cache()


def should_seed_notification_defaults() -> bool:
	return bool(frappe.conf.get("pet_app_seed_notification_defaults"))


def ensure_doctype(doctype_name: str, spec: dict):
	if frappe.db.exists("DocType", doctype_name):
		doc = frappe.get_doc("DocType", doctype_name)
		changed = False
		if spec.get("issingle") and not doc.issingle:
			doc.issingle = 1
			changed = True
		if spec.get("istable") and not doc.istable:
			doc.istable = 1
			changed = True
		existing = {row.fieldname: row for row in doc.fields}
		for field in spec.get("fields", []):
			if field["fieldname"] not in existing:
				doc.append("fields", field_doc(field))
				changed = True
				continue
			existing_field = existing[field["fieldname"]]
			if "length" in field and existing_field.length != field["length"]:
				existing_field.length = field["length"]
				changed = True
		if changed:
			doc.save(ignore_permissions=True)
		return

	doc = frappe.get_doc(
		{
			"doctype": "DocType",
			"name": doctype_name,
			"module": MODULE,
			"custom": 1,
			"issingle": 1 if spec.get("issingle") else 0,
			# Child tables carry no permissions of their own - they inherit the parent's.
			"istable": 1 if spec.get("istable") else 0,
			"autoname": spec.get("autoname", "hash"),
			"title_field": spec.get("title_field"),
			"track_changes": spec.get("track_changes", 1),
			"allow_rename": spec.get("allow_rename", 1),
			"fields": [field_doc(field) for field in spec.get("fields", [])],
			"permissions": [] if spec.get("istable") else permissions(spec.get("roles")),
			"sort_field": spec.get("sort_field", "modified"),
			"sort_order": spec.get("sort_order", "DESC"),
		}
	)
	doc.insert(ignore_permissions=True)


def seed_defaults():
	settings = {
		"enabled": 1,
		"dry_run": 1,
		"default_channel": "WhatsApp",
		"default_country_code": "964",
		"default_language": "en",
		"timezone": "Etc/UTC",
		"allow_marketing_messages": 0,
		"require_opt_in": 0,
		"respect_quiet_hours": 0,
		"quiet_hours_start": "21:00:00",
		"quiet_hours_end": "08:00:00",
		"fallback_to_sms": 0,
		"fallback_to_email": 0,
		"max_retries": 3,
		"retry_after_minutes": 5,
		"max_messages_per_minute": 60,
		"max_messages_per_day": 1000,
		"duplicate_window_minutes": 60,
		"otp_expiry_minutes": 10,
		"otp_cooldown_seconds": 60,
		"otp_max_attempts": 5,
		"otp_length": 6,
		"hash_otp": 0,
		"reminder_batch_size": 100,
		"reminder_lookahead_minutes": 1440,
		"reminder_duplicate_window_hours": 24,
		"log_payload": 0,
		"log_rendered_message": 1,
		"mask_sensitive_values": 1,
	}
	for fieldname, value in settings.items():
		frappe.db.set_single_value("Pet App Notification Settings", fieldname, value)

	if not frappe.db.exists("Pet App WhatsApp Account", {"is_default": 1}):
		account = frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Account",
				"account_name": "Dummy / Dev",
				"enabled": 1,
				"is_default": 1,
				"provider": "Dummy / Dev",
				"business_name": "Pet App Dev",
				"default_language": "en",
			}
		).insert(ignore_permissions=True)
	else:
		account = frappe.get_doc("Pet App WhatsApp Account", frappe.db.get_value("Pet App WhatsApp Account", {"is_default": 1}, "name"))

	if not frappe.db.exists("Pet App WhatsApp Template", {"template_key": "auth_otp"}):
		template = frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Template",
				"template_key": "auth_otp",
				"enabled": 1,
				"template_name": "auth_otp",
				"language": "en",
				"category": "Authentication",
				"provider_account": account.name,
				"event_key": "auth.otp",
				"recipient_type": "Guardian",
				"body_preview": "Your verification code is {{ otp }}.",
				"requires_opt_in": 0,
				"allow_during_quiet_hours": 1,
				"priority": "urgent",
			}
		).insert(ignore_permissions=True)
	else:
		template = frappe.get_doc("Pet App WhatsApp Template", frappe.db.get_value("Pet App WhatsApp Template", {"template_key": "auth_otp"}, "name"))

	frappe.db.set_single_value("Pet App Notification Settings", "default_whatsapp_account", account.name)
	frappe.db.set_single_value("Pet App Notification Settings", "otp_template", template.template_key)


def field_doc(field: dict) -> dict:
	data = {
		"fieldname": field["fieldname"],
		"fieldtype": field.get("fieldtype", "Data"),
		"label": field.get("label") or field["fieldname"].replace("_", " ").title(),
	}
	for key in (
		"options",
		"default",
		"reqd",
		"read_only",
		"in_list_view",
		"in_standard_filter",
		"unique",
		"depends_on",
		"mandatory_depends_on",
		"precision",
		"length",
	):
		if key in field:
			data[key] = field[key]
	return data


def permissions(roles=None):
	roles = roles or ("System Manager", "Healthcare Administrator")
	return [
		{
			"role": role,
			"read": 1,
			"write": 1,
			"create": 1,
			"delete": 1 if role == "System Manager" else 0,
			"print": 1,
			"email": 1,
			"export": 1,
			"report": 1,
			"share": 1,
		}
		for role in roles
	]


def link(fieldname: str, options: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Link", "options": options, **kwargs}


def dyn_link(fieldname: str, options_field: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Dynamic Link", "options": options_field, **kwargs}


def select(fieldname: str, options, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Select", "options": "\n".join(options), **kwargs}


def check(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Check", **kwargs}


def dt(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Datetime", **kwargs}


def date(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Date", **kwargs}


def time(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Time", **kwargs}


def txt(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Small Text", **kwargs}


def text(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Text", **kwargs}


def code(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Code", "options": "JSON", **kwargs}


def password(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Password", "length": 2048, **kwargs}


CHANNELS = ("WhatsApp", "SMS", "Email", "In App")
QUEUE_STATUSES = ("Draft", "Queued", "Processing", "Sent", "Delivered", "Read", "Failed", "Retry Scheduled", "Cancelled", "Skipped")
REMINDER_STATUSES = ("Draft", "Scheduled", "Queued", "Sent", "Cancelled", "Skipped", "Failed")
RECIPIENT_TYPES = ("Guardian", "Customer", "User", "Doctor", "Manual")
TEMPLATE_CATEGORIES = ("Authentication", "Utility", "Marketing", "Service")
PROVIDERS = ("Meta Cloud API", "Dummy / Dev", "Twilio", "360dialog", "Custom")


DOCTYPES = {
	"Pet App Notification Settings": {
		"issingle": 1,
		"roles": ("System Manager", "Healthcare Administrator"),
		"fields": [
			check("enabled", default=1),
			check("dry_run", default=1),
			select("default_channel", CHANNELS, default="WhatsApp"),
			{"fieldname": "default_country_code", "fieldtype": "Data", "default": "964"},
			{"fieldname": "default_language", "fieldtype": "Data", "default": "en"},
			{"fieldname": "timezone", "fieldtype": "Data", "default": "Etc/UTC"},
			check("allow_marketing_messages"),
			check("require_opt_in"),
			check("respect_quiet_hours"),
			time("quiet_hours_start", default="21:00:00"),
			time("quiet_hours_end", default="08:00:00"),
			link("default_whatsapp_account", "Pet App WhatsApp Account"),
			check("fallback_to_sms"),
			check("fallback_to_email"),
			{"fieldname": "max_retries", "fieldtype": "Int", "default": 3},
			{"fieldname": "retry_after_minutes", "fieldtype": "Int", "default": 5},
			{"fieldname": "max_messages_per_minute", "fieldtype": "Int", "default": 60},
			{"fieldname": "max_messages_per_day", "fieldtype": "Int", "default": 1000},
			{"fieldname": "duplicate_window_minutes", "fieldtype": "Int", "default": 60},
			{"fieldname": "otp_template", "fieldtype": "Data", "default": "auth_otp"},
			{"fieldname": "otp_expiry_minutes", "fieldtype": "Int", "default": 10},
			{"fieldname": "otp_cooldown_seconds", "fieldtype": "Int", "default": 60},
			{"fieldname": "otp_max_attempts", "fieldtype": "Int", "default": 5},
			{"fieldname": "otp_length", "fieldtype": "Int", "default": 6},
			check("hash_otp"),
			{"fieldname": "reminder_batch_size", "fieldtype": "Int", "default": 100},
			{"fieldname": "reminder_lookahead_minutes", "fieldtype": "Int", "default": 1440},
			{"fieldname": "reminder_duplicate_window_hours", "fieldtype": "Int", "default": 24},
			check("log_payload"),
			check("log_rendered_message", default=1),
			check("mask_sensitive_values", default=1),
		],
	},
	"Pet App WhatsApp Account": {
		"autoname": "field:account_name",
		"title_field": "account_name",
		"roles": ("System Manager", "Healthcare Administrator"),
		"fields": [
			{"fieldname": "account_name", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
			check("enabled", default=1, in_list_view=1),
			check("is_default", in_list_view=1),
			select("provider", PROVIDERS, default="Dummy / Dev", in_list_view=1),
			{"fieldname": "branch", "fieldtype": "Data"},
			{"fieldname": "business_name", "fieldtype": "Data"},
			{"fieldname": "whatsapp_business_account_id", "fieldtype": "Data"},
			{"fieldname": "phone_number_id", "fieldtype": "Data"},
			{"fieldname": "phone_number", "fieldtype": "Data"},
			{"fieldname": "display_phone_number", "fieldtype": "Data"},
			{"fieldname": "graph_api_version", "fieldtype": "Data", "default": "v20.0"},
			password("access_token", length=4096),
			password("app_secret", length=512),
			password("verify_token", length=512),
			{"fieldname": "default_language", "fieldtype": "Data", "default": "en"},
			{"fieldname": "quality_rating", "fieldtype": "Data"},
			{"fieldname": "messaging_limit_tier", "fieldtype": "Data"},
			dt("last_health_check_at"),
			txt("last_error"),
		],
	},
	"Pet App WhatsApp Template": {
		"autoname": "field:template_key",
		"title_field": "template_key",
		"roles": ("System Manager", "Healthcare Administrator"),
		"fields": [
			{"fieldname": "template_key", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
			check("enabled", default=1, in_list_view=1),
			{"fieldname": "template_name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
			{"fieldname": "language", "fieldtype": "Data", "default": "en"},
			select("category", TEMPLATE_CATEGORIES, default="Utility", in_list_view=1),
			link("provider_account", "Pet App WhatsApp Account"),
			txt("description"),
			{"fieldname": "event_key", "fieldtype": "Data", "in_standard_filter": 1},
			{"fieldname": "source_doctype", "fieldtype": "Data"},
			select("recipient_type", RECIPIENT_TYPES, default="Guardian"),
			select("header_type", ("None", "Text", "Image", "Document"), default="None"),
			text("body_preview"),
			txt("footer_text"),
			code("buttons_json"),
			code("components_json"),
			code("sample_context_json"),
			check("requires_opt_in"),
			check("allow_during_quiet_hours"),
			{"fieldname": "priority", "fieldtype": "Data", "default": "default"},
		],
	},
	"Pet App Notification Rule": {
		"autoname": "field:rule_name",
		"title_field": "rule_name",
		"fields": [
			{"fieldname": "rule_name", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
			check("enabled", default=1),
			{"fieldname": "event_key", "fieldtype": "Data", "reqd": 1, "in_standard_filter": 1},
			select("channel", CHANNELS, default="WhatsApp"),
			{"fieldname": "template_key", "fieldtype": "Data"},
			select("recipient_type", RECIPIENT_TYPES, default="Guardian"),
			{"fieldname": "source_doctype", "fieldtype": "Data"},
			select("send_timing", ("Immediate", "Delayed", "Scheduled", "Before Date", "After Date", "Manual Only"), default="Immediate"),
			{"fieldname": "delay_minutes", "fieldtype": "Int"},
			{"fieldname": "schedule_expression", "fieldtype": "Data"},
			code("condition_json"),
			{"fieldname": "context_builder", "fieldtype": "Data"},
			check("respect_quiet_hours", default=1),
			check("requires_opt_in"),
			{"fieldname": "priority", "fieldtype": "Data", "default": "default"},
			check("dedupe_enabled", default=1),
			{"fieldname": "dedupe_window_minutes", "fieldtype": "Int"},
		],
	},
	"Pet App Notification Queue": {
		"autoname": "NOTIF-Q-.YYYY.-.#####",
		"title_field": "event_key",
		"fields": [
			{"fieldname": "event_key", "fieldtype": "Data", "in_list_view": 1, "in_standard_filter": 1},
			select("channel", CHANNELS, default="WhatsApp", in_list_view=1),
			select("status", QUEUE_STATUSES, default="Queued", in_list_view=1, in_standard_filter=1),
			{"fieldname": "priority", "fieldtype": "Data", "default": "default"},
			select("recipient_type", RECIPIENT_TYPES, default="Guardian"),
			{"fieldname": "recipient_name", "fieldtype": "Data", "in_standard_filter": 1},
			{"fieldname": "to_phone", "fieldtype": "Data"},
			{"fieldname": "to_email", "fieldtype": "Data"},
			{"fieldname": "template_key", "fieldtype": "Data", "in_standard_filter": 1},
			{"fieldname": "template_name", "fieldtype": "Data"},
			{"fieldname": "language", "fieldtype": "Data"},
			code("context_json"),
			text("rendered_preview"),
			link("source_doctype", "DocType"),
			dyn_link("source_name", "source_doctype"),
			{"fieldname": "source_title", "fieldtype": "Data"},
			dt("scheduled_at", in_list_view=1),
			dt("queued_at"),
			dt("processing_at"),
			dt("sent_at"),
			dt("delivered_at"),
			dt("read_at"),
			dt("failed_at"),
			{"fieldname": "provider", "fieldtype": "Data"},
			link("provider_account", "Pet App WhatsApp Account"),
			{"fieldname": "provider_message_id", "fieldtype": "Data", "in_standard_filter": 1},
			code("provider_response_json"),
			{"fieldname": "provider_error_code", "fieldtype": "Data"},
			txt("provider_error_message"),
			{"fieldname": "retry_count", "fieldtype": "Int"},
			dt("next_retry_at"),
			{"fieldname": "idempotency_key", "fieldtype": "Data", "unique": 1},
			{"fieldname": "dedupe_key", "fieldtype": "Data", "in_standard_filter": 1},
			link("created_by_rule", "Pet App Notification Rule"),
			check("manual"),
		],
	},
	"Pet App Notification Log": {
		"autoname": "NOTIF-LOG-.YYYY.-.#####",
		"title_field": "event_key",
		"fields": [
			link("queue", "Pet App Notification Queue"),
			{"fieldname": "event_key", "fieldtype": "Data", "in_list_view": 1},
			select("channel", CHANNELS, default="WhatsApp"),
			select("status", QUEUE_STATUSES, default="Sent", in_list_view=1),
			select("recipient_type", RECIPIENT_TYPES, default="Guardian"),
			{"fieldname": "recipient_name", "fieldtype": "Data"},
			{"fieldname": "to_phone_masked", "fieldtype": "Data"},
			{"fieldname": "template_key", "fieldtype": "Data"},
			link("source_doctype", "DocType"),
			dyn_link("source_name", "source_doctype"),
			{"fieldname": "provider_message_id", "fieldtype": "Data"},
			dt("event_datetime", in_list_view=1),
			text("message"),
			code("details_json"),
		],
	},
	"Pet App WhatsApp Webhook Event": {
		"autoname": "WA-WEBHOOK-.YYYY.-.#####",
		"title_field": "provider_message_id",
		"fields": [
			link("provider_account", "Pet App WhatsApp Account"),
			{"fieldname": "event_type", "fieldtype": "Data", "in_list_view": 1},
			{"fieldname": "provider_message_id", "fieldtype": "Data", "in_standard_filter": 1},
			{"fieldname": "phone_number_id", "fieldtype": "Data"},
			{"fieldname": "from_phone", "fieldtype": "Data"},
			{"fieldname": "to_phone", "fieldtype": "Data"},
			{"fieldname": "status", "fieldtype": "Data", "in_list_view": 1},
			{"fieldname": "timestamp", "fieldtype": "Data"},
			code("payload_json"),
			check("processed"),
			dt("processed_at"),
			txt("processing_error"),
		],
	},
	"Pet App Communication Consent": {
		"autoname": "APP-CONSENT-.YYYY.-.#####",
		"title_field": "party",
		"fields": [
			select("party_type", RECIPIENT_TYPES, default="Guardian", in_list_view=1),
			{"fieldname": "party", "fieldtype": "Data", "in_list_view": 1},
			{"fieldname": "phone", "fieldtype": "Data", "in_standard_filter": 1},
			select("channel", CHANNELS, default="WhatsApp"),
			check("opt_in", default=1, in_list_view=1),
			{"fieldname": "opt_in_source", "fieldtype": "Data"},
			dt("opt_in_at"),
			dt("opt_out_at"),
			txt("opt_out_reason"),
			{"fieldname": "language", "fieldtype": "Data", "default": "en"},
			check("marketing_allowed"),
			check("utility_allowed", default=1),
			check("authentication_allowed", default=1),
		],
	},
	"Pet App Reminder": {
		"autoname": "APP-REM-.YYYY.-.#####",
		"title_field": "title",
		"fields": [
			select("reminder_type", ("Appointment Reminder", "Follow-up Due", "Vaccination Due", "Deworming Due", "Medication Refill", "Boarding Checkout", "Lab Result Released", "Invoice Due", "Food Reorder", "Death Certificate Ready"), in_list_view=1),
			select("status", REMINDER_STATUSES, default="Scheduled", in_list_view=1),
			select("channel", CHANNELS, default="WhatsApp"),
			{"fieldname": "template_key", "fieldtype": "Data"},
			link("guardian", "Guardian", in_standard_filter=1),
			link("customer", "Customer"),
			link("pet", "Pet", in_standard_filter=1),
			link("source_doctype", "DocType"),
			dyn_link("source_name", "source_doctype"),
			dt("due_datetime"),
			dt("send_at", in_list_view=1),
			dt("sent_at"),
			dt("cancelled_at"),
			{"fieldname": "title", "fieldtype": "Data", "in_list_view": 1},
			code("context_json"),
			{"fieldname": "dedupe_key", "fieldtype": "Data", "unique": 1},
			link("notification_queue", "Pet App Notification Queue"),
		],
	},
}
