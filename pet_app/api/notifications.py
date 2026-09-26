from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, formatdate, now_datetime
from frappe.utils.password import get_decrypted_password

from pet_app.api.permissions import require_doctype_permission
from pet_app.api.response import fail, ok, standardize_response
from pet_app.api.link_aliases import enrich_link_aliases, with_link_aliases
from pet_app.notifications import engine
from pet_app.notifications import actions as whatsapp_actions
from pet_app.notifications import designer as whatsapp_designer
from pet_app.notifications import inbox as whatsapp_inbox
from pet_app.notifications import meta_templates as whatsapp_meta_templates
from pet_app.notifications import send_targets as whatsapp_send_targets
from pet_app.notifications import template_categories
from pet_app.notifications.context import list_template_variables
from pet_app.notifications.scheduler import enqueue_due_reminders as _enqueue_due_reminders
from pet_app.utils.api_response import api_error, api_success

TEMPLATE_CATEGORY_DOCTYPE = "Pet App Template Category"


@frappe.whitelist()
def get_notification_settings():
	try:
		require_doctype_permission("Pet App Notification Settings", "read")
		return api_success({"settings": _settings_payload(frappe.get_single("Pet App Notification Settings"))})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def update_notification_settings(data=None, **kwargs):
	try:
		require_doctype_permission("Pet App Notification Settings", "write")
		payload = _payload(data, kwargs)
		doc = frappe.get_single("Pet App Notification Settings")
		for key, value in payload.items():
			if key == "onesignal_rest_api_key" and not cstr(value).strip():
				continue
			if key == "onesignal_rest_api_key_configured":
				continue
			if doc.meta.has_field(key):
				doc.set(key, value)
		doc.save(ignore_permissions=True)
		frappe.clear_cache(doctype="Pet App Notification Settings")
		return api_success({"settings": _settings_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


ACCESS_SETTINGS_DOCTYPE = "Pet App Access Settings"
SEND_DEFAULTS_FIELD = "send_defaults"
# surface_key is the identity and label is derived from the registry, so neither is
# writable. A client may send them - the settings screen round-trips whole rows - and
# both are ignored rather than refused.
SEND_DEFAULT_WRITABLE_FIELDS = ("enabled", "template_source", "template_key", "meta_template")


def _send_defaults_payload():
	"""One entry per REGISTERED surface, whether or not a row exists for it.

	The screen shows every surface the code knows, not only the ones somebody has
	already configured - otherwise a surface added in a release would be invisible
	until a patch seeded it. A surface with no row, a blank row and a disabled row all
	come back the same way, with ``configured: false``, because that is what they mean
	everywhere else.
	"""
	stored = {}
	doc = frappe.get_single(ACCESS_SETTINGS_DOCTYPE)
	for row in doc.get(SEND_DEFAULTS_FIELD) or []:
		stored[cstr(row.surface_key).strip()] = row

	configured = whatsapp_send_targets.configured_defaults()
	entries = []
	for surface in whatsapp_send_targets.registered_surfaces():
		row = stored.get(surface)
		entries.append(
			{
				"surface_key": surface,
				"label": whatsapp_send_targets.surface_label(surface),
				"enabled": bool(cint(row.enabled)) if row else True,
				"template_source": cstr(row.template_source) if row else "",
				"template_key": cstr(row.template_key) if row else "",
				"meta_template": cstr(row.meta_template) if row else "",
				# What the send path will actually do with it, which is not the same as
				# the row being filled in: a disabled row still remembers its template.
				"configured": configured[surface].configured,
			}
		)
	return entries


@frappe.whitelist()
def get_send_defaults():
	"""The default template each send surface offers.

	Pairs with update_send_defaults the way get/update_notification_settings do. The
	authority is Pet App Access Settings.send_defaults; there is no code-side fallback,
	so a surface listed here with configured: false sends nothing by default and its
	picker opens blank.
	"""
	try:
		require_doctype_permission(ACCESS_SETTINGS_DOCTYPE, "read")
		return api_success({"send_defaults": _send_defaults_payload()})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def update_send_defaults(data=None, **kwargs):
	"""Update the defaults for surfaces that already exist. Never creates or deletes one.

	Which surfaces exist is code - see SEND_SURFACES - so this endpoint cannot invent
	one, and an unregistered surface_key is refused rather than stored, because a row
	no surface reads is a default an operator can set and then watch have no effect.

	It cannot delete one either: a surface omitted from the payload is left exactly as
	it was. Partial payloads are therefore safe, and the screen cannot silently drop a
	default by sending a short list.

	Clearing a default is done by sending a blank template_source, not by omitting the
	row - the row's other half is cleared on save, so a row can never carry two answers.
	"""
	try:
		require_doctype_permission(ACCESS_SETTINGS_DOCTYPE, "write")
		payload = _payload(data, kwargs)
		rows = payload.get(SEND_DEFAULTS_FIELD)
		if rows is None and isinstance(payload.get("defaults"), list):
			rows = payload.get("defaults")
		if not isinstance(rows, list):
			return api_error(
				_("send_defaults must be a list of rows."), code="SEND_DEFAULTS_PAYLOAD_INVALID"
			)

		known = set(whatsapp_send_targets.registered_surfaces())
		unknown = sorted(
			{
				cstr((row or {}).get("surface_key")).strip()
				for row in rows
				if cstr((row or {}).get("surface_key")).strip() not in known
			}
		)
		if unknown:
			return api_error(
				_("Not a known send surface: {0}. Known surfaces: {1}.").format(
					", ".join(unknown), ", ".join(sorted(known))
				),
				code="SEND_SURFACE_UNKNOWN",
			)

		doc = frappe.get_single(ACCESS_SETTINGS_DOCTYPE)
		existing = {cstr(row.surface_key).strip(): row for row in doc.get(SEND_DEFAULTS_FIELD) or []}
		for incoming in rows:
			surface = cstr(incoming.get("surface_key")).strip()
			row = existing.get(surface)
			if row is None:
				# Materialised, not created: the surface is already registered in code and
				# simply has no row yet - a release can add a surface without a patch
				# before it can be configured.
				row = doc.append(SEND_DEFAULTS_FIELD, {"surface_key": surface})
				existing[surface] = row
			for field in SEND_DEFAULT_WRITABLE_FIELDS:
				if field in incoming:
					row.set(field, incoming.get(field))

		# validate() refuses an unregistered or duplicated surface, fills label from the
		# registry, and clears the half of the row the source does not name.
		doc.save(ignore_permissions=True)
		frappe.clear_cache(doctype=ACCESS_SETTINGS_DOCTYPE)
		return api_success({"send_defaults": _send_defaults_payload()})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def test_whatsapp_account(account=None):
	try:
		require_doctype_permission("Pet App WhatsApp Account", "write")
		name = account or frappe.db.get_value("Pet App WhatsApp Account", {"is_default": 1}, "name")
		doc = frappe.get_doc("Pet App WhatsApp Account", name)
		doc.last_health_check_at = now_datetime()
		doc.last_error = None if doc.enabled else _("Account is disabled.")
		doc.save(ignore_permissions=True)
		return api_success({"account": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_templates(event_key=None, enabled=None):
	try:
		require_doctype_permission("Pet App WhatsApp Template", "read")
		filters = {}
		if event_key:
			filters["event_key"] = event_key
		if enabled is not None:
			filters["enabled"] = int(enabled)
		rows = frappe.get_all(
			"Pet App WhatsApp Template",
			filters=filters,
			fields=["name", "template_key", "enabled", "template_name", "language", "category", "surface_category", "event_key", "source_doctype", "recipient_type", "delivery_mode", "priority"],
			order_by="template_key asc",
			ignore_permissions=True,
		)
		return api_success({"templates": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_template_categories():
	"""The screens a template can be assigned to, for the template editor's dropdown.

	Gated on read of Pet App WhatsApp Template - the same check as list_templates - and
	read past the category doctype's own permissions: anyone who may open the template
	list may see what its surface_category values mean. Checking the category doctype
	instead would refuse a template editor who simply has no row for it, and show an
	empty dropdown over a field they can still write.

	Enabled records only. A disabled category is not offered because save_template
	refuses it; a template that already carries one still returns it from
	list_templates, so the editor can show the stored value it cannot re-select.

	Every row carries ``page_key`` and ``surface_key`` - the page and the send dialog the
	category is bound to, each None when unbound - so a screen can resolve its own
	category from this one list, applying the order ``get_surface_category`` documents
	(surface match, then page match, then none). Each key is None on a site where its
	patch (p1_29, p1_30) has not run yet, rather than failing the dropdown.
	"""
	try:
		require_doctype_permission("Pet App WhatsApp Template", "read")
		rows = frappe.get_all(
			TEMPLATE_CATEGORY_DOCTYPE,
			filters={"enabled": 1},
			fields=list(template_categories.CATEGORY_FIELDS),
			order_by="category_name asc",
			ignore_permissions=True,
		)
		# Screens are many-to-many since p1_31: page_keys / surface_keys list every screen a
		# category is on; page_key / surface_key stay for one-value readers (see category_payload).
		bindings = template_categories.category_bindings()
		categories = [template_categories.category_payload(row, bindings) for row in rows]
		return api_success({"categories": categories}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_surface_category_for_page(page_key=None):
	"""The enabled categories this page's screen record lists. Read-only.

	What a screen calls to learn which ``surface_category`` to pass to
	``list_template_options`` - set in desk, so a new category needs no deploy.
	``categories`` is the full list, in the screen's order; [] means "no category for this
	page": the screen passes nothing and gets the full catalogue. A disabled category is not
	returned, for the same reason list_template_categories does not offer one.

	``category`` is the single-value field older callers read: the category when the page
	shows exactly one, None when it shows several. None makes an older screen ask for the
	full catalogue - a superset of what the page shows, never a subset.

	Same permission check as list_template_categories: read on Pet App WhatsApp Template.
	"""
	try:
		require_doctype_permission("Pet App WhatsApp Template", "read")
		key = cstr(page_key).strip()
		resolved = template_categories.resolve_category(page_key=key)
		return api_success({"page_key": key or None, "category": resolved["category"], "categories": resolved["categories"]})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_surface_key_options():
	"""The options for the ``surface_key`` picker on Pet App Template Category. Read-only.

	Every key in ``send_targets.SEND_SURFACES`` with its label, plus any value already
	stored on a category that the registry does not hold (``registered: false``), so
	the desk picker never blanks a value it does not recognise. Gated on read of the
	category doctype - this feeds the category form, and only its readers open that.
	"""
	try:
		require_doctype_permission(TEMPLATE_CATEGORY_DOCTYPE, "read")
		options = template_categories.surface_key_options()
		return api_success({"options": options}, meta={"total": len(options)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_page_key_options():
	"""The options for the ``page_key`` picker on Pet App Template Category. Read-only.

	Every page in Pet App Access Settings -> Page Access, by label with its key, plus any
	value already stored on a category that matches no page (``registered: false``), so the
	desk picker never blanks it. Same gate as list_surface_key_options.
	"""
	try:
		require_doctype_permission(TEMPLATE_CATEGORY_DOCTYPE, "read")
		options = template_categories.page_key_options()
		return api_success({"options": options}, meta={"total": len(options)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_surface_category(surface_key=None, page_key=None):
	"""The categories a screen should use, from its send surface and/or its page. Read-only.

	ORDER: the surface's screen record, if it lists any enabled category; otherwise the
	page's; otherwise none - the screen passes nothing to ``list_template_options`` and gets
	the full catalogue. The surface REPLACES the page, it does not add to it.

	``categories`` is the list to pass to ``list_template_options(surface_category=...)``
	(as a JSON array). ``resolved_by`` says which rule answered ("surface", "page" or None).
	When the surface overrides a page with a different set, the page's set is returned as
	``page_categories``.

	BACKWARD COMPATIBLE. ``category`` (and ``page_category``) keep their single-value shape:
	the category when there is exactly one, None when there are several. An older screen
	that passes ``category.name`` therefore gets the full catalogue for a multi-category
	screen - more than it should show, never less - and exactly what it got before for every
	single-category screen, which is every screen on the day p1_31 runs.

	Same permission check as list_template_categories: read on Pet App WhatsApp Template.
	``get_surface_category_for_page`` is unchanged and remains the page-only lookup.
	"""
	try:
		require_doctype_permission("Pet App WhatsApp Template", "read")
		return api_success(template_categories.resolve_category(surface_key=surface_key, page_key=page_key))
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_template(template_key=None, name=None):
	try:
		require_doctype_permission("Pet App WhatsApp Template", "read")
		doc = _get_template(template_key or name)
		return api_success({"template": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def save_template(data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		if payload.get("source_doctype"):
			whatsapp_designer.get_source_schema(payload["source_doctype"])
			require_doctype_permission(payload["source_doctype"], "read")
		name = payload.get("name") or frappe.db.get_value("Pet App WhatsApp Template", {"template_key": payload.get("template_key")}, "name")
		require_doctype_permission("Pet App WhatsApp Template", "write" if name else "create")
		_normalise_surface_category(payload)
		doc = frappe.get_doc("Pet App WhatsApp Template", name) if name else frappe.new_doc("Pet App WhatsApp Template")
		content_candidate = doc.as_dict()
		content_candidate.update(payload)
		whatsapp_designer.validate_template_content(
			content_candidate,
			content_candidate.get("source_doctype"),
		)
		for key, value in payload.items():
			if doc.meta.has_field(key):
				doc.set(key, value)
		doc.save(ignore_permissions=True)
		return api_success({"template": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def preview_template(template_key=None, context=None, data=None, **kwargs):
	try:
		require_doctype_permission("Pet App WhatsApp Template", "read")
		payload = _payload(data, kwargs)
		doc = _get_template(template_key or payload.get("template_key"))
		from pet_app.notifications.renderer import render_preview

		return api_success({"preview": render_preview(doc, _payload(context, {}) or payload.get("context") or {})})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
@standardize_response
def queue_notification(data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		if payload.get("manual"):
			require_doctype_permission("Pet App Notification Queue", "create")
		return engine.queue_notification(**payload)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
@standardize_response
def send_manual_notification(data=None, **kwargs):
	"""Send now, from either template source.

	The default path is unchanged: pass ``template_key`` and everything resolves
	from that local Pet App WhatsApp Template. Passing ``template_source: "meta"``
	with a ``meta_template`` (a Pet App WhatsApp Meta Template name) sends that
	mirror row directly - no local row, no binding required.
	"""
	try:
		require_doctype_permission("Pet App Notification Queue", "create")
		payload = _payload(data, kwargs)
		if payload.get("meta_template"):
			require_doctype_permission(whatsapp_meta_templates.MIRROR_DOCTYPE, "read")
		process_now = payload.pop("process_now", 1)
		payload["manual"] = True
		result = engine.queue_notification(**payload)
		if result.get("ok") and cint(process_now):
			return engine.process_notification_queue(result["data"]["queue"]["name"])
		return result
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
@standardize_response
def retry_notification(queue=None, name=None):
	try:
		require_doctype_permission("Pet App Notification Queue", "write")
		doc = frappe.get_doc("Pet App Notification Queue", queue or name)
		doc.status = "Queued"
		doc.save(ignore_permissions=True)
		return engine.process_notification_queue(doc.name)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def cancel_notification(queue=None, name=None):
	try:
		require_doctype_permission("Pet App Notification Queue", "write")
		doc = frappe.get_doc("Pet App Notification Queue", queue or name)
		doc.status = "Cancelled"
		doc.save(ignore_permissions=True)
		return api_success({"queue": engine.queue_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_notification_queue(status=None, limit=50):
	try:
		require_doctype_permission("Pet App Notification Queue", "read")
		filters = {}
		if status:
			filters["status"] = status
		rows = frappe.get_all(
			"Pet App Notification Queue",
			filters=filters,
			fields=["name", "event_key", "channel", "status", "recipient_type", "recipient_name", "template_key", "scheduled_at", "sent_at", "provider_message_id"],
			order_by="creation desc",
			limit_page_length=int(limit or 50),
			ignore_permissions=True,
		)
		return api_success({"queue": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_notification_queue(queue=None, name=None):
	try:
		require_doctype_permission("Pet App Notification Queue", "read")
		doc = frappe.get_doc("Pet App Notification Queue", queue or name)
		return api_success({"queue": engine.queue_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_notification_logs(queue=None, guardian=None, pet=None, limit=50):
	try:
		if frappe.db.exists("DocType", "Pet App Notification Log"):
			require_doctype_permission("Pet App Notification Log", "read")
			filters = {}
			if queue:
				filters["queue"] = queue
			rows = frappe.get_all(
				"Pet App Notification Log",
				filters=filters,
				fields=["name", "queue", "event_key", "channel", "status", "recipient_type", "recipient_name", "to_phone_masked", "template_key", "event_datetime", "message"],
				order_by="event_datetime desc, creation desc",
				limit_page_length=int(limit or 50),
				ignore_permissions=True,
			)
			return api_success({"logs": [dict(row) for row in rows]}, meta={"total": len(rows)})
		require_doctype_permission("Pet App Notification Queue", "read")
		return ok({"logs": []}, meta={"total": 0})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_notification_stats():
	try:
		require_doctype_permission("Pet App Notification Queue", "read")
		rows = frappe.db.sql(
			"""
			select status, count(*) as total
			from `tabPet App Notification Queue`
			group by status
			""",
			as_dict=True,
		)
		return api_success({"stats": [dict(row) for row in rows]})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def create_reminder(data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		doc = frappe.get_doc({"doctype": "Pet App Reminder", **payload})
		if not doc.send_at:
			doc.send_at = doc.due_datetime or now_datetime()
		doc.status = doc.status or "Scheduled"
		doc.save(ignore_permissions=True)
		return api_success({"reminder": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def cancel_reminder(reminder=None, name=None):
	try:
		doc = frappe.get_doc("Pet App Reminder", reminder or name)
		doc.status = "Cancelled"
		doc.cancelled_at = now_datetime()
		doc.save(ignore_permissions=True)
		return api_success({"reminder": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_reminders(status=None, limit=50):
	try:
		filters = {}
		if status:
			filters["status"] = status
		rows = frappe.get_all(
			"Pet App Reminder",
			filters=filters,
			fields=["name", "reminder_type", "status", "channel", "guardian", "pet", "source_doctype", "source_name", "due_datetime", "send_at", "notification_queue"],
			order_by="send_at asc, creation asc",
			limit_page_length=int(limit or 50),
			ignore_permissions=True,
		)
		reminders = [dict(row) for row in rows]
		enrich_link_aliases(reminders, pet_field="pet", guardian_field="guardian", include_doctor=False, include_provider=False)
		return api_success({"reminders": reminders}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
@standardize_response
def enqueue_due_reminders():
	return _enqueue_due_reminders()


@frappe.whitelist(methods=["POST"])
@standardize_response
def send_manual_reminder(reminder=None, guardian=None, pet=None, reminder_type=None, message=None, channel="In App", data=None, **kwargs):
	"""Compatibility wrapper for the older Pet Reminder API."""
	try:
		payload = _payload(data, kwargs)
		reminder_name = cstr(reminder or payload.get("reminder")).strip()
		reminder_doc = frappe.get_doc("Pet Reminder", reminder_name) if reminder_name and frappe.db.exists("Pet Reminder", reminder_name) else None
		guardian = guardian or payload.get("guardian") or (reminder_doc.guardian if reminder_doc else None)
		pet = pet or payload.get("pet") or (reminder_doc.pet if reminder_doc else None)
		if channel != "WhatsApp":
			return _send_legacy_reminder(reminder_doc, guardian, pet, reminder_type, message, channel, payload)
		# A WhatsApp send must name its template, from either source. This used to
		# default to "auth_otp" whenever the channel was WhatsApp, which is not a
		# template that exists on this site - so the branch never completed a send, it
		# only failed one step later with "Notification template was not found." and no
		# mention of the default that had been substituted.
		#
		# Either addressing is accepted: a local template_key, or the Meta pair. The
		# payload wins over the stored reminder so a caller can override what was
		# scheduled, which is what "manual" means here.
		template_key = cstr(payload.get("template_key")).strip() or cstr(
			reminder_doc.get("template_key") if reminder_doc else ""
		).strip()
		template_source = cstr(payload.get("template_source")).strip() or cstr(
			reminder_doc.get("template_source") if reminder_doc else ""
		).strip()
		meta_template = cstr(payload.get("meta_template")).strip() or cstr(
			reminder_doc.get("meta_template") if reminder_doc else ""
		).strip()
		if not template_key and not meta_template:
			return fail(
				_("A WhatsApp reminder must name the template to send. Pass template_key, or meta_template for a Meta template."),
				code="TEMPLATE_NOT_ADDRESSED",
			)
		result = engine.queue_notification(
			event_key=payload.get("event_key") or f"legacy_reminder.{reminder_type or (reminder_doc.reminder_type if reminder_doc else 'Manual')}",
			recipient_type="Guardian" if guardian else "Manual",
			recipient_name=guardian,
			to_phone=payload.get("to_phone"),
			context={"message": message or payload.get("message"), "pet": pet},
			source_doctype=reminder_doc.reference_doctype if reminder_doc else payload.get("reference_doctype"),
			source_name=reminder_doc.reference_name if reminder_doc else payload.get("reference_name"),
			template_key=template_key,
			template_source=template_source or None,
			meta_template=meta_template or None,
			channel="WhatsApp" if channel == "WhatsApp" else "In App",
			idempotency_key=payload.get("idempotency_key") or (f"{reminder_name}:{channel}" if reminder_name else None),
			manual=True,
		)
		if result.get("ok") and channel == "WhatsApp":
			result = engine.process_notification_queue(result["data"]["queue"]["name"])
		if reminder_doc:
			reminder_doc.status = "Sent"
			reminder_doc.save(ignore_permissions=True)
		return result
	except Exception as exc:
		return _error_response(exc)


def _send_legacy_reminder(reminder_doc, guardian, pet, reminder_type, message, channel, payload):
	if not guardian:
		return fail(_("Guardian is required."), code="VALIDATION_ERROR")
	reminder_name = reminder_doc.name if reminder_doc else None
	resolved_type = reminder_type or payload.get("reminder_type") or (reminder_doc.reminder_type if reminder_doc else "Manual")
	template = _legacy_template(resolved_type, channel)
	context = _legacy_context(reminder_doc, guardian, pet)
	subject = payload.get("subject") or _render(template.subject if template else None, context, resolved_type)
	body = message or payload.get("message") or _render(template.body if template else None, context, resolved_type)
	log = frappe.get_doc(
		{
			"doctype": "Pet Notification Log",
			"reminder": reminder_name,
			"template": template.name if template else None,
			"guardian": guardian,
			"pet": pet,
			"channel": channel,
			"recipient": _legacy_recipient(guardian, channel),
			"subject": subject,
			"message": body,
			"status": "Sent",
			"sent_at": now_datetime(),
			"reference_doctype": reminder_doc.reference_doctype if reminder_doc else payload.get("reference_doctype"),
			"reference_name": reminder_doc.reference_name if reminder_doc else payload.get("reference_name"),
			"idempotency_key": payload.get("idempotency_key") or (f"{reminder_name}:{channel}" if reminder_name else None),
		}
	)
	try:
		log.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		log = frappe.get_doc("Pet Notification Log", {"idempotency_key": log.idempotency_key})
	if reminder_doc:
		reminder_doc.status = "Sent"
		reminder_doc.save(ignore_permissions=True)
		_mark_source_reminded(reminder_doc)
	notification = {key: log.get(key) for key in ("name", "reminder", "guardian", "pet", "channel", "recipient", "subject", "message", "status", "sent_at")}
	return ok({"notification": with_link_aliases(notification, pet_field="pet", guardian_field="guardian", include_doctor=False, include_provider=False)})


def _legacy_template(reminder_type, channel):
	name = frappe.db.get_value("Pet Notification Template", {"reminder_type": reminder_type, "channel": channel, "active": 1}, "name")
	return frappe.get_doc("Pet Notification Template", name) if name else None


# Source doctypes that carry their own `reminder_status`, and are therefore worth telling
# that their reminder went out. Anything else (Vet Visit, Sales Invoice) has no such field
# and is left alone. One entry where there were two: `Preventive Care Record` holds both
# vaccination and deworming, and the reminder type already says which kind it was.
REMINDED_SOURCE_DOCTYPES = ("Preventive Care Record",)


def _mark_source_reminded(reminder_doc) -> None:
	"""Stamp `reminder_status = Sent` on the clinical record the reminder came from.

	Five readers show this field - medical_file, guardian_portal, mobile/pets - and nothing
	has ever written it, so every vaccination card has read "Pending" since the day it was
	created, including ones already reminded.

	"Sent" and not "Acknowledged": the options are Pending/Sent/Acknowledged/Cancelled, and
	the clinic has sent a message, not heard back. It also keeps
	`clinical_decision_support` correct, which counts a record as still overdue unless the
	status is "Cancelled" - a vaccination remains due after the reminder goes out.

	`update_modified=False` so a reminder does not churn the clinical record's timestamp;
	`modified` is surfaced to the medical file as the record's own last-touched date.
	"""
	doctype = cstr(reminder_doc.reference_doctype).strip()
	name = cstr(reminder_doc.reference_name).strip()
	if doctype not in REMINDED_SOURCE_DOCTYPES or not name:
		return
	if not frappe.db.has_column(doctype, "reminder_status"):
		return
	if not frappe.db.exists(doctype, name):
		return
	if cstr(frappe.db.get_value(doctype, name, "reminder_status")) in ("Sent", "Acknowledged", "Cancelled"):
		return
	frappe.db.set_value(doctype, name, "reminder_status", "Sent", update_modified=False)


def _legacy_context(reminder_doc, guardian, pet) -> dict:
	"""What a template may refer to. Names, not ids - this is read by a guardian."""
	pet_name = cstr(frappe.db.get_value("Pet", pet, "pet_name")) if pet else ""
	guardian_name = cstr(frappe.db.get_value("Guardian", guardian, "full_name")) if guardian else ""
	due_date = reminder_doc.due_date if reminder_doc else None
	return {
		"pet": pet_name or cstr(pet),
		"pet_name": pet_name or cstr(pet),
		"guardian": guardian_name or cstr(guardian),
		"guardian_name": guardian_name or cstr(guardian),
		"due_date": formatdate(due_date) if due_date else "",
		"reminder_type": cstr(reminder_doc.reminder_type) if reminder_doc else "",
		"note": cstr(reminder_doc.note) if reminder_doc else "",
	}


def _render(text, context: dict, fallback: str) -> str:
	"""Render a template body, or fall back to it unrendered rather than losing the send.

	Deliberately not a throw. This runs from the hourly `send_due_reminders` loop, so a
	typo in one template would otherwise fail that reminder every hour forever, and the
	reminder would stay Pending with nothing to show for it. The mistake is logged where an
	administrator sees it and the guardian still gets the message, unsubstituted.
	"""
	text = cstr(text).strip()
	if not text:
		return fallback
	if "{" not in text:
		return text
	try:
		return frappe.render_template(text, context)
	except Exception:
		frappe.log_error(
			title="PET_NOTIFICATION_TEMPLATE_RENDER_FAILED",
			message=f"{text[:200]}\n\n{frappe.get_traceback()}",
		)
		return text


def _legacy_recipient(guardian, channel):
	return frappe.db.get_value("Guardian", guardian, "email_id" if channel == "Email" else "phone") or guardian


class SurfaceCategoryError(frappe.ValidationError):
	"""A template named a screen category that cannot be assigned."""

	def __init__(self, message, code, details=None):
		super().__init__(message)
		self.exc_type = code
		self.details = details or {}


def _normalise_surface_category(payload: dict) -> None:
	"""Validate ``surface_category`` in place, only when the caller sent it.

	Three cases, and the difference between the first two is the point:

	* absent - untouched, so the save loop leaves the stored value alone. An editor that
	  never learned about the field must not wipe it on every save.
	* sent empty (``""`` / ``None``) - cleared. Uncategorised is a real value: the
	  template is offered on every screen.
	* sent a key - it must name an existing, ENABLED category. Link validation alone
	  would accept a disabled one, and a template filed under a screen nobody can
	  select any more is invisible in the editor's own dropdown.

	``category`` (Meta's MARKETING / UTILITY / AUTHENTICATION) is not read or written
	here.
	"""
	if "surface_category" not in payload:
		return
	value = cstr(payload.get("surface_category")).strip()
	if not value:
		payload["surface_category"] = None
		return
	enabled = frappe.db.get_value(TEMPLATE_CATEGORY_DOCTYPE, value, "enabled")
	if enabled is None:
		raise SurfaceCategoryError(
			_("Screen category {0} does not exist. Choose one from the list, or leave it empty to show the template on every screen.").format(value),
			code="SURFACE_CATEGORY_NOT_FOUND",
			details={"surface_category": value},
		)
	if not cint(enabled):
		raise SurfaceCategoryError(
			_("Screen category {0} is disabled. Choose an enabled category, or leave it empty to show the template on every screen.").format(value),
			code="SURFACE_CATEGORY_DISABLED",
			details={"surface_category": value},
		)
	payload["surface_category"] = value


def _get_template(name):
	if not name:
		frappe.throw(_("Template is required."))
	docname = frappe.db.get_value("Pet App WhatsApp Template", {"template_key": name}, "name") or name
	return frappe.get_doc("Pet App WhatsApp Template", docname)


def _doc_payload(doc) -> dict:
	return {field.fieldname: doc.get(field.fieldname) for field in doc.meta.fields}


def _settings_payload(doc) -> dict:
	data = _doc_payload(doc)
	data.pop("onesignal_rest_api_key", None)
	data["onesignal_rest_api_key_configured"] = bool(
		get_decrypted_password(
			"Pet App Notification Settings",
			"Pet App Notification Settings",
			"onesignal_rest_api_key",
			raise_exception=False,
		)
	)
	return data


def _payload(data, kwargs) -> dict:
	if isinstance(data, str) and data:
		return json.loads(data)
	if isinstance(data, dict):
		return data
	return kwargs or {}


def _error_response(exc):
	if isinstance(exc, frappe.PermissionError):
		return api_error(_("Not permitted"), code="PERMISSION_ERROR")
	return api_error(
		cstr(exc),
		code=getattr(exc, "code", None) or getattr(exc, "exc_type", None) or exc.__class__.__name__,
		details=getattr(exc, "details", None),
	)


def _require_whatsapp_designer_access():
	user = frappe.session.user
	if not user or user == "Guest":
		raise frappe.PermissionError
	roles = set(frappe.get_roles(user) or [])
	allowed_roles = {"System Manager", "Healthcare Administrator"}
	if frappe.db.exists("DocType", "Pet App Notification Settings"):
		meta = frappe.get_meta("Pet App Notification Settings")
		if meta.has_field("whatsapp_full_access_role"):
			configured_role = frappe.db.get_single_value("Pet App Notification Settings", "whatsapp_full_access_role")
			if configured_role:
				allowed_roles.add(configured_role)
	if user != "Administrator" and not roles.intersection(allowed_roles):
		raise frappe.PermissionError


@frappe.whitelist()
def list_whatsapp_template_variables(source_doctype=None):
	try:
		_require_whatsapp_designer_access()
		require_doctype_permission("Pet App WhatsApp Template", "read")
		if source_doctype:
			whatsapp_designer.get_source_schema(source_doctype)
			require_doctype_permission(source_doctype, "read")
		return api_success({"variables": list_template_variables(source_doctype)}, meta={"source_doctype": source_doctype})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_whatsapp_designer_schema(source_doctype=None):
	try:
		_require_whatsapp_designer_access()
		require_doctype_permission("Pet App WhatsApp Action Rule", "read")
		require_doctype_permission("Pet App WhatsApp Template", "read")
		if source_doctype:
			require_doctype_permission(source_doctype, "read")
		schema = whatsapp_designer.get_designer_schema(source_doctype)
		if not source_doctype:
			schema["source_tables"] = [
				row for row in schema["source_tables"] if frappe.has_permission(row["value"], ptype="read")
			]
		return api_success({"schema": schema})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_whatsapp_action_rules(source_doctype=None, enabled=None):
	try:
		_require_whatsapp_designer_access()
		require_doctype_permission("Pet App WhatsApp Action Rule", "read")
		if source_doctype:
			whatsapp_designer.get_source_schema(source_doctype)
			require_doctype_permission(source_doctype, "read")
		filters = {}
		if source_doctype:
			filters["source_doctype"] = source_doctype
		if enabled is not None:
			filters["enabled"] = cint(enabled)
		names = frappe.get_all(
			"Pet App WhatsApp Action Rule",
			filters=filters,
			pluck="name",
			order_by="rule_name asc",
			ignore_permissions=True,
		)
		rules = []
		for name in names:
			doc = frappe.get_doc("Pet App WhatsApp Action Rule", name)
			if doc.source_doctype in whatsapp_designer.SOURCE_REGISTRY and frappe.has_permission(doc.source_doctype, ptype="read"):
				rules.append(whatsapp_designer.canonical_rule(doc))
		return api_success({"rules": rules}, meta={"total": len(rules)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def save_whatsapp_action_rule(data=None, **kwargs):
	try:
		_require_whatsapp_designer_access()
		payload = _payload(data, kwargs)
		if isinstance(payload.get("rule"), dict):
			payload = payload["rule"]
		name = payload.get("name") or payload.get("rule_name")
		existing = frappe.db.exists("Pet App WhatsApp Action Rule", name) if name else None
		require_doctype_permission("Pet App WhatsApp Action Rule", "write" if existing else "create")
		doc = frappe.get_doc("Pet App WhatsApp Action Rule", existing) if existing else frappe.new_doc("Pet App WhatsApp Action Rule")
		normalized = whatsapp_designer.raise_for_invalid_rule(payload, doc if existing else None)
		require_doctype_permission(normalized["source_doctype"], "read")
		require_doctype_permission("Pet App WhatsApp Template", "read")
		for key in whatsapp_designer.RULE_FIELDS:
			if key != "name" and doc.meta.has_field(key):
				doc.set(key, normalized.get(key))
		for key, value in whatsapp_designer.serialize_rule_sections(normalized).items():
			doc.set(key, value)
		doc.save(ignore_permissions=True)
		return api_success({"rule": whatsapp_designer.canonical_rule(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def validate_whatsapp_action_rule(data=None, rule=None, **kwargs):
	try:
		_require_whatsapp_designer_access()
		require_doctype_permission("Pet App WhatsApp Action Rule", "read")
		payload = _payload(data, kwargs)
		candidate = _payload(rule, {}) if rule else payload.get("rule") or payload
		source_doctype = cstr(candidate.get("source_doctype")).strip()
		if source_doctype in whatsapp_designer.SOURCE_REGISTRY:
			require_doctype_permission(source_doctype, "read")
		require_doctype_permission("Pet App WhatsApp Template", "read")
		return api_success({"validation": whatsapp_designer.validate_rule(candidate)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def simulate_whatsapp_action_rule(data=None, rule=None, source_name=None, **kwargs):
	try:
		_require_whatsapp_designer_access()
		require_doctype_permission("Pet App WhatsApp Action Rule", "read")
		require_doctype_permission("Pet App WhatsApp Template", "read")
		payload = _payload(data, kwargs)
		candidate = _payload(rule, {}) if rule else payload.get("rule") or payload
		source_name = source_name or payload.get("source_name")
		source_doctype = cstr(candidate.get("source_doctype")).strip()
		whatsapp_designer.get_source_schema(source_doctype)
		if not source_name or not frappe.db.exists(source_doctype, source_name):
			raise whatsapp_designer.DesignerError(_("Source record was not found."), "SOURCE_RECORD_NOT_FOUND")
		try:
			require_doctype_permission(source_doctype, "read")
			source_doc = frappe.get_doc(source_doctype, source_name)
			if not source_doc.has_permission("read"):
				raise frappe.PermissionError
		except frappe.PermissionError:
			raise whatsapp_designer.DesignerError(_("You cannot read this source record."), "SOURCE_READ_FORBIDDEN")
		return api_success({"simulation": whatsapp_designer.simulate_rule(candidate, source_name)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def send_actionable_whatsapp_message(
	template_key=None,
	source_doctype=None,
	source_name=None,
	recipient=None,
	context=None,
	action_rule=None,
	data=None,
	**kwargs,
):
	try:
		payload = _payload(data, kwargs)
		template_key = template_key or payload.get("template_key")
		source_doctype = source_doctype or payload.get("source_doctype")
		source_name = source_name or payload.get("source_name")
		recipient = recipient or payload.get("recipient")
		action_rule = action_rule or payload.get("action_rule")
		require_doctype_permission("Pet App WhatsApp Action Request", "create")
		require_doctype_permission(source_doctype, "read")
		filters = {"enabled": 1, "source_doctype": source_doctype}
		action_rule = cstr(action_rule).strip()
		template_key = cstr(template_key).strip()
		if action_rule:
			filters["name"] = action_rule
		elif template_key:
			# Guarded against emptiness on purpose: a rule that addresses a Meta template
			# has a blank template_key, and an unguarded filter would match it by
			# accident and send a template nobody asked for.
			filters["template_key"] = template_key
		name = frappe.db.get_value("Pet App WhatsApp Action Rule", filters, "name", order_by="modified desc")
		if not name:
			if action_rule:
				frappe.throw(
					_("WhatsApp action rule {0} was not found, is disabled, or is not for {1}.").format(
						action_rule, source_doctype
					)
				)
			if template_key:
				frappe.throw(
					_(
						"No enabled WhatsApp action rule for {0} uses template {1}. Rules that send a "
						"Meta template cannot be found this way - pass action_rule instead."
					).format(source_doctype, template_key)
				)
			frappe.throw(
				_("Specify action_rule, or template_key for a rule that uses a local template.")
			)
		rule = frappe.get_doc("Pet App WhatsApp Action Rule", name)
		source = frappe.get_doc(source_doctype, source_name)
		request = whatsapp_actions.create_action_request(
			rule,
			source,
			recipient=recipient,
			context=_payload(context, {}) if context else None,
			process_now=True,
		)
		return api_success({"action": _doc_payload(request)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_pending_whatsapp_actions(status=None, limit=50):
	try:
		require_doctype_permission("Pet App WhatsApp Action Request", "read")
		filters = {"status": status} if status else {"status": ["in", ["Waiting Reply", "Matched", "Pending Review", "Needs Review"]]}
		rows = frappe.get_all(
			"Pet App WhatsApp Action Request",
			filters=filters,
			fields=[
				"name", "rule", "status", "delivery_stage", "conversation", "source_doctype", "source_name",
				"recipient_phone", "response_key", "response_value", "expires_at", "creation",
			],
			order_by="creation desc",
			limit_page_length=cint(limit) or 50,
			ignore_permissions=True,
		)
		return api_success({"actions": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def approve_whatsapp_action(action_request=None, note=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = action_request or payload.get("action_request") or payload.get("name")
		require_doctype_permission("Pet App WhatsApp Action Request", "write")
		doc = frappe.get_doc("Pet App WhatsApp Action Request", name)
		if doc.status not in {"Pending Review", "Needs Review", "Matched"}:
			frappe.throw(_("Only matched or review-pending actions can be approved."))
		if doc.status == "Needs Review":
			doc = whatsapp_actions.select_review_response(doc, payload.get("response_key"))
		doc = whatsapp_actions.execute_action(doc, approved_by=frappe.session.user, approval_note=note or payload.get("note"))
		return api_success({"action": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def reject_whatsapp_action(action_request=None, note=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		name = action_request or payload.get("action_request") or payload.get("name")
		require_doctype_permission("Pet App WhatsApp Action Request", "write")
		doc = whatsapp_actions.reject_action(name, note or payload.get("note"))
		return api_success({"action": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_whatsapp_conversations(status=None, limit=50):
	try:
		require_doctype_permission("Pet App WhatsApp Conversation", "read")
		filters = {"status": status} if status else {}
		rows = frappe.get_all(
			"Pet App WhatsApp Conversation",
			filters=filters,
			fields=[
				"name", "display_name", "normalized_phone", "guardian", "customer", "status", "last_inbound_at",
				"last_outbound_at", "session_expires_at", "unread_count", "last_message_preview", "last_message_direction",
			],
			order_by="modified desc",
			limit_page_length=cint(limit) or 50,
			ignore_permissions=True,
		)
		for row in rows:
			row["session_open"] = whatsapp_inbox.session_is_open(row)
		return api_success({"conversations": [dict(row) for row in rows]}, meta={"total": len(rows)})
	except Exception as exc:
		return _error_response(exc)


def _attach_file_names(messages):
	"""Give every message row the name of its attachment, as a name and not a docname.

	``file`` is a Link to File, so the row carries "8d92107521" and nothing else. The
	inbox renders ``file_name`` and falls back to ``file`` when it is missing, so every
	document in the thread was labelled with that docname - no extension, no meaning,
	and identical for two different reports. The bytes and what WhatsApp was told they
	are called were always right; only this read was blind.

	One extra query for the whole page, keyed on the distinct File names, and rows whose
	File row is gone simply keep no ``file_name`` - the caller's fallback still applies.
	"""
	names = {row.get("file") for row in messages if row.get("file")}
	if not names:
		return messages
	file_names = dict(
		frappe.get_all(
			"File",
			filters={"name": ["in", list(names)]},
			fields=["name", "file_name"],
			limit_page_length=0,
			ignore_permissions=True,
			as_list=True,
		)
	)
	for row in messages:
		resolved = file_names.get(row.get("file"))
		if resolved:
			row["file_name"] = resolved
	return messages


@frappe.whitelist()
def get_whatsapp_conversation(conversation=None, limit=100):
	"""A conversation, its most recent ``limit`` messages, and its action requests.

	THE NEWEST ``limit`` MESSAGES, oldest to newest. Fetched newest-first and reversed, so
	the thread still reads top to bottom. It used to sort oldest-first and then cut at the
	limit, which froze every thread longer than the limit at its OLDEST messages - staff saw
	a conversation from weeks ago and none of today's sends.

	``meta`` says whether older messages exist: ``has_more`` (from one extra row fetched,
	so it is a fact, not a guess), ``total_messages`` and ``returned``. ``data`` keeps its
	shape; a thread no longer than the limit returns exactly what it returned before.
	"""
	try:
		require_doctype_permission("Pet App WhatsApp Conversation", "read")
		doc = frappe.get_doc("Pet App WhatsApp Conversation", conversation)
		page_length = cint(limit) or 100
		messages = frappe.get_all(
			"Pet App WhatsApp Message",
			filters={"conversation": doc.name},
			fields=[
				"name", "direction", "message_type", "body", "caption", "file", "provider_message_id", "interactive_id",
				"interactive_title", "status", "message_at", "read_at", "action_request", "source_doctype", "source_name",
			],
			# The exact reverse of the display order, so reversing below restores it.
			order_by="message_at desc, creation desc",
			# One more than asked for: its presence is what has_more reports.
			limit_page_length=page_length + 1,
			ignore_permissions=True,
		)
		has_more = len(messages) > page_length
		messages = list(reversed(messages[:page_length]))
		_attach_file_names(messages)
		actions = frappe.get_all(
			"Pet App WhatsApp Action Request",
			filters={"conversation": doc.name},
			fields=[
				"name", "rule", "status", "delivery_stage", "source_doctype", "source_name",
				"response_key", "response_value", "result_doctype", "result_name", "error_message",
			],
			order_by="creation asc",
			ignore_permissions=True,
		)
		for action in actions:
			rule_config = frappe.db.get_value("Pet App WhatsApp Action Rule", action.rule, "response_config_json")
			action["response_options"] = (_payload(rule_config, {}).get("options") or []) if rule_config else []
		return api_success(
			{
				"conversation": {**_doc_payload(doc), "session_open": whatsapp_inbox.session_is_open(doc)},
				"messages": [dict(row) for row in messages],
				"actions": [dict(row) for row in actions],
			},
			meta={
				"has_more": has_more,
				"total_messages": frappe.db.count("Pet App WhatsApp Message", {"conversation": doc.name}),
				"returned": len(messages),
				"limit": page_length,
			},
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def reply_whatsapp_conversation(conversation=None, message=None, file=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		conversation = conversation or payload.get("conversation")
		message = message if message is not None else payload.get("message")
		file = file or payload.get("file")
		require_doctype_permission("Pet App WhatsApp Message", "create")
		doc = whatsapp_inbox.send_conversation_message(conversation, message, file)
		return api_success({"message": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def mark_whatsapp_conversation_read(conversation=None, data=None, **kwargs):
	try:
		payload = _payload(data, kwargs)
		conversation = conversation or payload.get("conversation")
		require_doctype_permission("Pet App WhatsApp Conversation", "write")
		doc = whatsapp_inbox.mark_conversation_read(conversation)
		return api_success({"conversation": _doc_payload(doc)})
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def download_whatsapp_media(message=None):
	try:
		require_doctype_permission("Pet App WhatsApp Message", "read")
		doc = frappe.get_doc("Pet App WhatsApp Message", message)
		if not doc.file:
			frappe.throw(_("This message has no downloaded attachment."))
		file_doc = frappe.get_doc("File", doc.file)
		return api_success({"file": {"name": file_doc.name, "file_name": file_doc.file_name, "file_url": file_doc.file_url, "is_private": file_doc.is_private}})
	except Exception as exc:
		return _error_response(exc)


# ---------------------------------------------------------------------------
# Meta template management
#
# These six sit on the rich error path: _error_response passes
# details=getattr(exc, "details", None), so Meta's code, error_subcode, type,
# fbtrace_id and http_status reach the caller intact. They must not be routed
# through the queue's _mark_failed, which flattens all of that to a string.
#
# Only sync and the three write methods call Graph. list/get read the mirror
# doctype, so the templates tab keeps working when Meta is unreachable.
# ---------------------------------------------------------------------------


@frappe.whitelist()
@standardize_response
def list_template_options(surface_category=None):
	"""Every Meta and local template as one list, in one shape, for a single picker.

	Replaces having to call list_templates and list_meta_templates and reconcile two
	different payloads - they return different keys under the same ``templates`` name.
	Both stay as they are; eight frontend surfaces still consume them, and they retire
	when those cut over.

	Unsendable templates are returned, marked ``sendable: false`` with the reasons that
	say why and which field to fix. Filtering them out would tell an operator a
	template had been deleted when it had only lost its slot map.

	Reads local data only - no Graph call - so it stays usable while Meta is
	unreachable, at a fixed query count regardless of how many templates exist.

	Requires read on both doctypes: an operator who may see only one source would
	otherwise get a silently half-populated list, which is the failure this endpoint
	exists to remove.

	``surface_category`` is optional and narrows the catalogue to one screen - a single
	key, or several as a list or JSON array. Omitting it returns the full catalogue,
	byte for byte what every current caller already receives; the frontend sends no
	parameters today and is unaffected until a surface chooses to pass one. A template
	with no surface category is returned to every screen, so a filtered picker is never
	emptier than the sorting done so far. Not to be confused with ``category`` in the
	payload, which is Meta's MARKETING / UTILITY / AUTHENTICATION and is unchanged.
	"""
	try:
		require_doctype_permission(whatsapp_meta_templates.MIRROR_DOCTYPE, "read")
		require_doctype_permission("Pet App WhatsApp Template", "read")
		payload = whatsapp_meta_templates.unified_template_options(surface_category=surface_category)
		# The envelope, not a bare array: standardize_response wraps this as
		# {"ok": true, "data": {...}}, so a transport failure and an empty catalogue are
		# never the same value on the wire. counts.total == 0 means genuinely none.
		return payload
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def list_meta_templates(status=None, language=None, limit=None, after=None):
	try:
		require_doctype_permission(whatsapp_meta_templates.MIRROR_DOCTYPE, "read")
		return api_success(whatsapp_meta_templates.list_mirror_templates(status, language, limit, after))
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_meta_template(meta_template_id=None, name=None):
	try:
		require_doctype_permission(whatsapp_meta_templates.MIRROR_DOCTYPE, "read")
		return api_success(whatsapp_meta_templates.get_mirror_template(meta_template_id or name))
	except Exception as exc:
		return _error_response(exc)


# Distinguishes "surface_category not sent" from "sent as null" - null clears, absent refuses.
_UNSENT = object()


@frappe.whitelist(methods=["POST"])
def set_meta_template_surface_category(meta_template=None, surface_category=_UNSENT, data=None, **kwargs):
	"""Set which of OUR screens offers a Meta template. Local only - Meta never sees it.

	`edit_meta_template` cannot do this: it always sends components and therefore always
	resubmits the template for Meta review. `surface_category` is an internal field, so this
	writes that one column and nothing else:

	* NO GRAPH CALL. Nothing in this path imports or calls the Graph client. Status, the
	  sync timestamps, components, raw_json and the slot map are not read or written.
	* `frappe.db.set_value`, not `doc.save()`. The mirror doctype has no controller and no
	  doctype hooks, and the wildcard `on_update` in notifications.actions skips every
	  `Pet App WhatsApp*` doctype, so a save would not reach Meta either - but set_value
	  writes exactly one column and cannot turn into a wider write if a hook is added later.
	* ANY STATUS. PENDING, REJECTED and DISABLED rows are accepted: those states block Meta
	  edits, and this is not one.
	* SAME VALIDATION AS `save_template` (`_normalise_surface_category`): "" or null clears;
	  an existing ENABLED category is stored; unknown -> SURFACE_CATEGORY_NOT_FOUND,
	  disabled -> SURFACE_CATEGORY_DISABLED.
	* A BOUND ROW IS REFUSED (SURFACE_CATEGORY_BOUND_TO_LOCAL). A bound pair is one template;
	  its category is set on the local template, whose value wins on both picker entries
	  (see `pair_surface_category`). Writing the mirror side would create a value the
	  picker ignores whenever the local side is set.

	Permission: `require_doctype_permission(<doctype>, "write")`, the check `save_template`
	makes, applied to the doctype this writes (the mirror), as every other mirror write here
	does.
	"""
	try:
		payload = _payload(data, kwargs)
		target = cstr(meta_template if meta_template is not None else payload.get("meta_template")).strip()
		if surface_category is not _UNSENT:
			payload["surface_category"] = surface_category
		require_doctype_permission(whatsapp_meta_templates.MIRROR_DOCTYPE, "write")
		if not target:
			frappe.throw(_("Meta template is required."))
		if "surface_category" not in payload:
			# Absent is not "clear" - that is what "" or null says. A request with no value
			# at all is a caller error, not an instruction.
			raise SurfaceCategoryError(
				_("surface_category is required. Send an empty value to clear it."),
				code="SURFACE_CATEGORY_REQUIRED",
			)
		row = frappe.db.get_value(
			whatsapp_meta_templates.MIRROR_DOCTYPE, target, ["name", "local_template"], as_dict=True
		)
		if not row:
			raise SurfaceCategoryError(
				_("Meta template {0} is not in the local mirror. Run a sync first.").format(target),
				code="META_TEMPLATE_NOT_FOUND",
				details={"meta_template": target},
			)
		if row.local_template:
			raise SurfaceCategoryError(
				_("This Meta template is bound to the local template {0}, and takes its screen category from it. Set the category on that template instead.").format(
					frappe.db.get_value("Pet App WhatsApp Template", row.local_template, "template_key") or row.local_template
				),
				code="SURFACE_CATEGORY_BOUND_TO_LOCAL",
				details={"meta_template": row.name, "local_template": row.local_template},
			)
		_normalise_surface_category(payload)
		frappe.db.set_value(
			whatsapp_meta_templates.MIRROR_DOCTYPE, row.name, "surface_category", payload["surface_category"]
		)
		return api_success(whatsapp_meta_templates.get_mirror_template(row.name))
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def create_meta_template(name=None, language=None, category=None, components=None, data=None, **kwargs):
	try:
		require_doctype_permission(whatsapp_meta_templates.MIRROR_DOCTYPE, "create")
		payload = _payload(data, kwargs)
		return api_success(
			whatsapp_meta_templates.create_meta_template(
				name=name if name is not None else payload.get("name"),
				language=language if language is not None else payload.get("language"),
				category=category if category is not None else payload.get("category"),
				components=components if components is not None else payload.get("components"),
				account=payload.get("account"),
			)
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def edit_meta_template(meta_template_id=None, components=None, data=None, **kwargs):
	try:
		require_doctype_permission(whatsapp_meta_templates.MIRROR_DOCTYPE, "write")
		payload = _payload(data, kwargs)
		return api_success(
			whatsapp_meta_templates.edit_meta_template(
				meta_template_id=meta_template_id if meta_template_id is not None else payload.get("meta_template_id"),
				components=components if components is not None else payload.get("components"),
				account=payload.get("account"),
			)
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def delete_meta_template(name=None, data=None, **kwargs):
	try:
		require_doctype_permission(whatsapp_meta_templates.MIRROR_DOCTYPE, "delete")
		payload = _payload(data, kwargs)
		return api_success(
			whatsapp_meta_templates.delete_meta_template(
				name=name if name is not None else payload.get("name"),
				account=payload.get("account"),
			)
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def sync_meta_templates(account=None, data=None, **kwargs):
	try:
		require_doctype_permission(whatsapp_meta_templates.MIRROR_DOCTYPE, "write")
		payload = _payload(data, kwargs)
		counts = whatsapp_meta_templates.sync_meta_templates(account or payload.get("account"))
		return api_success(
			{"synced": counts["synced"], "last_synced_at": now_datetime()},
			meta=counts,
		)
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist()
def get_meta_template_slot_map(meta_template=None, name=None, source_doctype=None):
	"""The stored meaning of each {{n}} slot on a Meta template.

	Returns the map plus the count the template actually declares, so an editor can
	show "4 variables in this message, 4 mapped" without a second call, and the
	allowlisted variables it may choose from.
	"""
	try:
		require_doctype_permission(whatsapp_meta_templates.MIRROR_DOCTYPE, "read")
		target = meta_template or name
		# Omitted reads the stored source; passing one previews that source's variables
		# without writing anything. An empty string is a legal preview of "no source".
		source = (
			whatsapp_meta_templates._UNSET if source_doctype is None else source_doctype
		)
		return api_success(whatsapp_meta_templates.slot_map_payload(target, source))
	except Exception as exc:
		return _error_response(exc)


@frappe.whitelist(methods=["POST"])
def save_meta_template_slot_map(meta_template=None, slots=None, source_doctype=None, data=None, **kwargs):
	"""Replace a Meta template's slot map.

	Validated against the template before it is stored: slots contiguous from 1, no
	duplicates, count equal to the declared variable count, and every variable_key on
	the allowlist. A map that disagrees with the template would otherwise surface as a
	refused send to a customer.
	"""
	try:
		require_doctype_permission(whatsapp_meta_templates.MIRROR_DOCTYPE, "write")
		payload = _payload(data, kwargs)
		target = meta_template if meta_template is not None else payload.get("meta_template")
		rows = slots if slots is not None else payload.get("slots")
		if isinstance(rows, str):
			rows = json.loads(rows)
		# Absent means "leave the stored source alone"; an empty string clears it. The
		# two are kept distinguishable so a caller that does not know about sources
		# cannot silently erase one.
		if source_doctype is not None:
			source = source_doctype
		elif "source_doctype" in payload:
			source = payload.get("source_doctype")
		else:
			source = whatsapp_meta_templates._UNSET
		whatsapp_meta_templates.save_slot_map(target, rows or [], source)
		# Projected through the same function the read endpoint uses, so the caller can
		# re-render straight from this response without a second request and without a
		# branch on which endpoint produced it.
		return api_success(whatsapp_meta_templates.slot_map_payload(target))
	except Exception as exc:
		return _error_response(exc)
