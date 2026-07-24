from __future__ import annotations

import json

import frappe

from pet_app.patches.p1_5_notification_engine_schema import (
	check,
	code,
	dt,
	dyn_link,
	ensure_doctype,
	link,
	select,
	text,
	txt,
)


ACTION_STATUSES = (
	"Waiting Reply",
	"Matched",
	"Pending Review",
	"Needs Review",
	"Completed",
	"Rejected",
	"Expired",
	"Failed",
)
MESSAGE_TYPES = ("Text", "Image", "Document", "Video", "Audio", "Sticker", "Location", "Contacts", "Interactive", "Unsupported")


def execute():
	ensure_whatsapp_action_schema()
	seed_pet_service_rating_rule()
	frappe.clear_cache()


def ensure_whatsapp_action_schema():
	for doctype, spec in DOCTYPES.items():
		ensure_doctype(doctype, spec)

	_update_select_options("Pet App Notification Queue", "status", (
		"Draft", "Queued", "Processing", "Waiting For Session", "Sent", "Delivered", "Read", "Failed",
		"Retry Scheduled", "Cancelled", "Skipped",
	))
	_update_select_options("Pet App Notification Log", "status", (
		"Draft", "Queued", "Processing", "Waiting For Session", "Sent", "Delivered", "Read", "Received",
		"Action Required", "Action Completed", "Failed", "Retry Scheduled", "Cancelled", "Skipped",
	))


def seed_pet_service_rating_rule():
	account = frappe.db.get_value("Pet App WhatsApp Account", {"is_default": 1, "enabled": 1}, "name")
	fallback = frappe.db.get_value("Pet App WhatsApp Template", {"template_key": "meta_feedback", "enabled": 1}, "name")

	if not frappe.db.exists("Pet App WhatsApp Template", "pet_service_rating_request"):
		frappe.get_doc(
			{
				"doctype": "Pet App WhatsApp Template",
				"template_key": "pet_service_rating_request",
				"enabled": 1,
				"template_name": "pet_service_rating_request",
				"language": "en",
				"category": "Service",
				"provider_account": account,
				"event_key": "pet_service.rating_request",
				"source_doctype": "PetCareService",
				"recipient_type": "Guardian",
				"delivery_mode": "Hybrid",
				"fallback_template_key": fallback,
				"body_preview": (
					"Thank you for visiting {{ clinic.business_name }}.\n"
					"Please rate {{ pet.pet_name }}'s {{ pet_service.pet_service_name }} service from 1 to 5."
				),
				"footer_text": "Your feedback helps us improve.",
				"priority": "default",
			}
		).insert(ignore_permissions=True)

	if frappe.db.exists("Pet App WhatsApp Action Rule", "PetCareService Completed Rating"):
		return

	responses = {
		"button_text": "Choose a rating",
		"section_title": "Rating",
		"options": [
			{"key": str(score), "label": f"{score} / 5", "value": score, "aliases": [str(score)]}
			for score in range(1, 6)
		],
	}
	frappe.get_doc(
		{
			"doctype": "Pet App WhatsApp Action Rule",
			"rule_name": "PetCareService Completed Rating",
			"enabled": 1,
			"source_doctype": "PetCareService",
			"trigger_event": "On Update",
			"condition_json": json.dumps(
				{"all": [{"field": "status", "operator": "changed_to", "value": ["completed", "Completed"]}]}
			),
			"recipient_type": "Guardian",
			"recipient_field": "guardian_id",
			"template_key": "pet_service_rating_request",
			"response_type": "List",
			"response_config_json": json.dumps(responses),
			"expiry_hours": 168,
			"duplicate_policy": "Once Per Source",
			"executor": "Create Rating",
			"executor_config_json": json.dumps({"rating_scale": 5, "request_comment": 0}),
			"risk_level": "Low",
			"requires_approval": 0,
		}
	).insert(ignore_permissions=True)


def _update_select_options(doctype, fieldname, options):
	row = frappe.db.get_value("DocField", {"parent": doctype, "fieldname": fieldname}, "name")
	if not row:
		return
	value = "\n".join(options)
	if frappe.db.get_value("DocField", row, "options") != value:
		frappe.db.set_value("DocField", row, "options", value, update_modified=False)


DOCTYPES = {
	"Pet App Notification Settings": {
		"issingle": 1,
		"fields": [
			link("whatsapp_feedback_user", "User"),
			txt("whatsapp_inbox_notification_users"),
			{"fieldname": "action_request_expiry_hours", "fieldtype": "Int", "default": 168},
		],
	},
	"Pet App WhatsApp Template": {
		"fields": [
			select("delivery_mode", ("Meta Template", "App Styled", "Hybrid"), default="Meta Template", in_list_view=1),
			link("fallback_template_key", "Pet App WhatsApp Template"),
			select("media_type", ("None", "Image", "Document", "Video", "Audio"), default="None"),
			link("media_file", "File"),
		],
	},
	"Pet App Notification Queue": {
		"fields": [
			link("conversation", "Pet App WhatsApp Conversation"),
			link("action_request", "Pet App WhatsApp Action Request"),
			select("message_type", MESSAGE_TYPES, default="Text"),
			code("interactive_json"),
			link("media_file", "File"),
			{"fieldname": "delivery_mode", "fieldtype": "Data"},
		],
	},
	"Pet App WhatsApp Conversation": {
		"autoname": "WA-CONV-.YYYY.-.#####",
		"title_field": "display_name",
		"fields": [
			{"fieldname": "conversation_key", "fieldtype": "Data", "reqd": 1, "unique": 1},
			link("provider_account", "Pet App WhatsApp Account", in_standard_filter=1),
			{"fieldname": "normalized_phone", "fieldtype": "Data", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
			{"fieldname": "display_name", "fieldtype": "Data", "in_list_view": 1},
			link("guardian", "Guardian", in_standard_filter=1),
			link("customer", "Customer"),
			select("status", ("Open", "Closed", "Blocked"), default="Open", in_list_view=1),
			dt("last_inbound_at"),
			dt("last_outbound_at"),
			dt("session_expires_at", in_list_view=1),
			{"fieldname": "unread_count", "fieldtype": "Int", "default": 0, "in_list_view": 1},
			text("last_message_preview"),
			select("last_message_direction", ("Inbound", "Outbound")),
			link("assigned_to", "User"),
		],
	},
	"Pet App WhatsApp Message": {
		"autoname": "WA-MSG-.YYYY.-.#####",
		"title_field": "body",
		"fields": [
			link("conversation", "Pet App WhatsApp Conversation", reqd=1, in_list_view=1, in_standard_filter=1),
			select("direction", ("Inbound", "Outbound"), reqd=1, in_list_view=1),
			select("message_type", MESSAGE_TYPES, default="Text", in_list_view=1),
			text("body"),
			text("caption"),
			link("file", "File"),
			{"fieldname": "provider_media_id", "fieldtype": "Data"},
			{"fieldname": "provider_message_id", "fieldtype": "Data", "unique": 1, "in_standard_filter": 1},
			{"fieldname": "interactive_id", "fieldtype": "Data"},
			{"fieldname": "interactive_title", "fieldtype": "Data"},
			select("status", ("Received", "Queued", "Sent", "Delivered", "Read", "Failed"), default="Received", in_list_view=1),
			dt("message_at", in_list_view=1),
			dt("read_at"),
			link("notification_queue", "Pet App Notification Queue"),
			link("action_request", "Pet App WhatsApp Action Request"),
			link("source_doctype", "DocType"),
			dyn_link("source_name", "source_doctype"),
			code("raw_json"),
			txt("media_error"),
		],
	},
	"Pet App WhatsApp Action Rule": {
		"autoname": "field:rule_name",
		"title_field": "rule_name",
		"fields": [
			{"fieldname": "rule_name", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
			check("enabled", default=1, in_list_view=1),
			link("source_doctype", "DocType", reqd=1, in_list_view=1, in_standard_filter=1),
			select("trigger_event", ("After Insert", "On Update", "On Submit", "Manual"), default="Manual", in_list_view=1),
			code("condition_json"),
			select("recipient_type", ("Guardian", "Customer", "User", "Manual"), default="Guardian"),
			{"fieldname": "recipient_field", "fieldtype": "Data"},
			link("template_key", "Pet App WhatsApp Template", reqd=1),
			select("response_type", ("None", "Buttons", "List", "Typed Reply"), default="Typed Reply"),
			code("response_config_json"),
			{"fieldname": "expiry_hours", "fieldtype": "Int", "default": 168},
			select("duplicate_policy", ("Once Per Source", "Once Per Rule And Recipient", "Allow Repeats"), default="Once Per Source"),
			select("executor", ("Create Rating", "Update Allowed Field", "Record Acknowledgement", "Create Staff Task", "Record Intent", "Require Staff Review"), reqd=1, in_list_view=1),
			code("executor_config_json"),
			select("risk_level", ("Low", "Medium", "High", "Sensitive"), default="Low", in_list_view=1),
			check("requires_approval"),
		],
	},
	"Pet App WhatsApp Action Request": {
		"autoname": "WA-ACTION-.YYYY.-.#####",
		"title_field": "rule",
		"fields": [
			link("rule", "Pet App WhatsApp Action Rule", reqd=1, in_list_view=1, in_standard_filter=1),
			select("status", ACTION_STATUSES, default="Waiting Reply", in_list_view=1, in_standard_filter=1),
			select("delivery_stage", ("Queued", "Awaiting Session", "Interactive Sent", "Awaiting Comment", "Complete"), default="Queued"),
			link("conversation", "Pet App WhatsApp Conversation", reqd=1, in_standard_filter=1),
			link("source_doctype", "DocType", reqd=1),
			dyn_link("source_name", "source_doctype", reqd=1),
			select("recipient_type", ("Guardian", "Customer", "User", "Manual"), default="Guardian"),
			{"fieldname": "recipient_name", "fieldtype": "Data"},
			{"fieldname": "recipient_phone", "fieldtype": "Data", "in_standard_filter": 1},
			link("notification_queue", "Pet App Notification Queue"),
			link("outbound_message", "Pet App WhatsApp Message"),
			link("matched_message", "Pet App WhatsApp Message"),
			dt("expires_at", in_list_view=1),
			{"fieldname": "idempotency_key", "fieldtype": "Data", "reqd": 1, "unique": 1},
			{"fieldname": "response_key", "fieldtype": "Data"},
			text("response_value"),
			link("approved_by", "User"),
			dt("approved_at"),
			text("approval_note"),
			link("result_doctype", "DocType"),
			dyn_link("result_name", "result_doctype"),
			code("result_json"),
			txt("error_message"),
		],
	},
	"Pet App WhatsApp Action Event": {
		"autoname": "WA-ACTION-EVT-.YYYY.-.#####",
		"title_field": "event_type",
		"fields": [
			link("action_request", "Pet App WhatsApp Action Request", reqd=1, in_list_view=1, in_standard_filter=1),
			select("event_type", ("Created", "Sent", "Reply Matched", "Needs Review", "Approved", "Rejected", "Executed", "Failed", "Expired"), in_list_view=1),
			link("message", "Pet App WhatsApp Message"),
			{"fieldname": "response_id", "fieldtype": "Data"},
			text("response_text"),
			{"fieldname": "matched_alias", "fieldtype": "Data"},
			link("performed_by", "User"),
			dt("event_at", in_list_view=1),
			code("details_json"),
		],
	},
}
