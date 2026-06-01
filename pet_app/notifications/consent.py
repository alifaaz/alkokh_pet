from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, now_datetime


AUTHENTICATION_CATEGORY = "Authentication"


def assert_consent_allowed(
	*,
	channel: str,
	category: str | None = None,
	recipient_type: str | None = None,
	recipient_name: str | None = None,
	phone: str | None = None,
	require_opt_in: bool = False,
):
	if category == AUTHENTICATION_CATEGORY:
		return
	if not frappe.db.exists("DocType", "Pet App Communication Consent"):
		return
	filters = {"channel": channel}
	if phone:
		filters["phone"] = phone
	if recipient_name:
		filters["party"] = recipient_name
	if recipient_type:
		filters["party_type"] = recipient_type
	rows = frappe.get_all(
		"Pet App Communication Consent",
		filters=filters,
		fields=["name", "opt_in", "marketing_allowed", "utility_allowed"],
		order_by="modified desc",
		limit_page_length=1,
		ignore_permissions=True,
	)
	if rows and not cint(rows[0].opt_in):
		frappe.throw(_("Recipient has opted out of this channel."), frappe.PermissionError)
	if require_opt_in and not rows:
		frappe.throw(_("Recipient consent is required before sending this message."), frappe.PermissionError)
	if category == "Marketing" and rows and not cint(rows[0].marketing_allowed):
		frappe.throw(_("Recipient has not opted into marketing messages."), frappe.PermissionError)


def opt_out_phone(phone: str, channel: str = "WhatsApp", reason: str | None = None):
	if not phone or not frappe.db.exists("DocType", "Pet App Communication Consent"):
		return None
	name = frappe.db.get_value("Pet App Communication Consent", {"phone": phone, "channel": channel}, "name")
	if name:
		doc = frappe.get_doc("Pet App Communication Consent", name)
	else:
		doc = frappe.new_doc("Pet App Communication Consent")
		doc.party_type = "Manual"
		doc.party = phone
		doc.phone = phone
		doc.channel = channel
	doc.opt_in = 0
	doc.opt_out_at = now_datetime()
	doc.opt_out_reason = reason or "Opt-out keyword"
	doc.save(ignore_permissions=True)
	return doc

