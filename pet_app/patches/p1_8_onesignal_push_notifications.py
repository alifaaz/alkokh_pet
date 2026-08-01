from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import (
	check,
	code,
	dt,
	ensure_doctype,
	link,
	password,
	select,
	text,
	txt,
)
from pet_app.patches.p1_6_whatsapp_actions_inbox_schema import _update_select_options


CHANNELS = ("WhatsApp", "SMS", "Email", "In App", "Push")
QUEUE_STATUSES = (
	"Draft",
	"Queued",
	"Processing",
	"Waiting For Session",
	"Sent",
	"Delivered",
	"Read",
	"Failed",
	"Retry Scheduled",
	"Cancelled",
	"Skipped",
)
LOG_STATUSES = (
	"Draft",
	"Queued",
	"Processing",
	"Waiting For Session",
	"Sent",
	"Delivered",
	"Read",
	"Received",
	"Action Required",
	"Action Completed",
	"Failed",
	"Retry Scheduled",
	"Cancelled",
	"Skipped",
)


def execute():
	ensure_onesignal_schema()
	frappe.clear_cache()


def ensure_onesignal_schema():
	for doctype, spec in DOCTYPES.items():
		ensure_doctype(doctype, spec)

	for doctype in ("Pet App Notification Rule", "Pet App Notification Queue", "Pet App Notification Log", "Pet App Reminder", "Pet App Communication Consent"):
		_update_select_options(doctype, "channel", CHANNELS)
	_update_select_options("Pet App Notification Settings", "default_channel", CHANNELS)
	_update_select_options("Pet App Notification Queue", "status", QUEUE_STATUSES)
	_update_select_options("Pet App Notification Log", "status", LOG_STATUSES)


DOCTYPES = {
	"Pet App Notification Settings": {
		"issingle": 1,
		"fields": [
			check("onesignal_enabled"),
			check("onesignal_web_enabled", default=1),
			check("onesignal_mobile_enabled", default=1),
			check("onesignal_mirror_frappe_notifications", default=1),
			{"fieldname": "onesignal_app_id", "fieldtype": "Data"},
			password("onesignal_rest_api_key", length=4096),
			{"fieldname": "onesignal_service_worker_path", "fieldtype": "Data", "default": "OneSignalSDKWorker.js"},
			{"fieldname": "onesignal_service_worker_scope", "fieldtype": "Data", "default": "/onesignal/"},
			{"fieldname": "push_frontend_base_url", "fieldtype": "Data", "label": "Push Frontend Base URL"},
		],
	},
	"Pet App Notification Queue": {
		"fields": [
			link("push_user", "User"),
			{"fieldname": "push_title", "fieldtype": "Data"},
			text("push_body"),
			txt("push_url"),
			code("push_data_json"),
		],
	},
	"Pet App Notification Log": {
		"fields": [
			link("push_user", "User"),
		],
	},
	"Pet App Push Subscription": {
		"autoname": "PUSH-SUB-.YYYY.-.#####",
		"title_field": "subscription_id",
		"roles": ("System Manager", "Healthcare Administrator"),
		"fields": [
			link("user", "User", reqd=1, in_list_view=1, in_standard_filter=1),
			link("guardian", "Guardian", in_standard_filter=1),
			{"fieldname": "onesignal_app_id", "fieldtype": "Data", "in_list_view": 1, "in_standard_filter": 1},
			{"fieldname": "subscription_id", "fieldtype": "Data", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
			{"fieldname": "onesignal_id", "fieldtype": "Data", "in_standard_filter": 1},
			select("platform", ("web", "android", "ios", "unknown"), default="unknown", in_list_view=1),
			{"fieldname": "device_id", "fieldtype": "Data"},
			txt("push_token"),
			check("opted_in"),
			{"fieldname": "permission", "fieldtype": "Data"},
			dt("last_seen_at", in_list_view=1),
			check("disabled"),
		],
	},
}
