from __future__ import annotations

from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_url_to_form, strip_html

from pet_app.notifications import engine


FRONTEND_ROUTE_PREFIX_BY_DOCTYPE = {
	"Vet Visit": "/healthcare/visits",
	"Vet Case Sheet": "/healthcare/case-sheets",
	"Appointment": "/healthcare/appointments",
	"PetCareService": "/healthcare/services",
	"Lab": "/healthcare/labs",
	"Imaging": "/healthcare/radiology",
	"Pet Procedure": "/healthcare/procedures",
	"Sales Invoice": "/accounting/sales-invoices",
}


def mirror_notification_log(doc, method=None):
	if getattr(frappe.flags, "in_migrate", False) or getattr(frappe.flags, "in_install", False):
		return
	if not _should_mirror_notification_log(doc):
		return

	try:
		queue_names = []
		queue_name = _queue_mirrored_push(doc, doc.for_user, f"frappe-notification-log:{doc.name}:push")
		if queue_name:
			queue_names.append(queue_name)
		if doc.for_user != "Administrator" and _can_push_to_administrator():
			queue_name = _queue_mirrored_push(doc, "Administrator", f"frappe-notification-log:{doc.name}:push:Administrator")
			if queue_name:
				queue_names.append(queue_name)
		_enqueue_mirrored_push_queues(queue_names)
	except Exception:
		frappe.logger("pet_app.push").error(
			"Failed to mirror Notification Log %s to OneSignal push\n%s",
			doc.name,
			frappe.get_traceback(),
		)


def _should_mirror_notification_log(doc) -> bool:
	if not doc.get("for_user") or cint(doc.get("read")):
		return False
	if not frappe.db.exists("User", doc.for_user):
		return False
	if not cint(frappe.db.get_value("User", doc.for_user, "enabled")):
		return False
	if not frappe.db.exists("DocType", "Pet App Notification Settings"):
		return False
	settings = engine.get_settings()
	return bool(
		cint(settings.get("enabled"))
		and cint(settings.get("onesignal_enabled"))
		and cint(settings.get("onesignal_mirror_frappe_notifications"))
	)


def _can_push_to_administrator() -> bool:
	return bool(frappe.db.exists("User", "Administrator") and cint(frappe.db.get_value("User", "Administrator", "enabled")))


def _enqueue_mirrored_push_queues(queue_names: list[str]) -> None:
	if not queue_names:
		return
	if frappe.in_test:
		process_mirrored_push_queues(queue_names)
		return
	frappe.enqueue(
		"pet_app.notifications.push.process_mirrored_push_queues",
		queue="short",
		enqueue_after_commit=True,
		queue_names=queue_names,
	)


def process_mirrored_push_queues(queue_names: list[str]) -> None:
	for queue_name in queue_names or []:
		engine.process_notification_queue(queue_name)


def _queue_mirrored_push(doc, user: str, idempotency_key: str) -> str | None:
	result = engine.queue_push_notification(
		user=user,
		title=_notification_title(doc),
		body=_notification_body(doc),
		url=_notification_url(doc),
		data={
			"notification_log": doc.name,
			"notification_for_user": doc.for_user,
			"type": doc.type,
			"document_type": doc.document_type,
			"document_name": doc.document_name,
		},
		event_key="frappe.notification_log",
		source_doctype="Notification Log",
		source_name=doc.name,
		idempotency_key=idempotency_key,
	)
	if result.get("ok") and not result.get("meta", {}).get("duplicate"):
		queue_name = result["data"]["queue"]["name"]
		if result["data"]["queue"]["status"] == "Queued":
			return queue_name
	return None


def _notification_title(doc) -> str:
	return strip_html(cstr(doc.get("subject")))[:192] or _("New Notification")


def _notification_body(doc) -> str:
	body = strip_html(cstr(doc.get("email_content")))
	if body:
		return body[:1000]
	if doc.get("document_type") and doc.get("document_name"):
		return _("{0} {1}").format(doc.document_type, doc.document_name)
	return _notification_title(doc)


def _notification_url(doc) -> str | None:
	frontend_url = _frontend_notification_url(doc)
	if frontend_url:
		return frontend_url
	if doc.get("link"):
		return doc.link
	if doc.get("document_type") and doc.get("document_name"):
		return get_url_to_form(doc.document_type, doc.document_name)
	return "/app/List/Notification Log"


def _frontend_notification_url(doc) -> str | None:
	base_url = _frontend_base_url()
	if not base_url or not doc.get("document_type") or not doc.get("document_name"):
		return None
	route_prefix = FRONTEND_ROUTE_PREFIX_BY_DOCTYPE.get(cstr(doc.document_type))
	if not route_prefix:
		return None
	return f"{base_url}{route_prefix}/{quote(cstr(doc.document_name), safe='')}"


def _frontend_base_url() -> str | None:
	base_url = cstr(engine.get_settings().get("push_frontend_base_url")).strip().rstrip("/")
	if not base_url:
		return None
	if not base_url.startswith(("http://", "https://")):
		base_url = f"https://{base_url.lstrip('/')}"
	return base_url
