from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, now_datetime

from pet_app.notifications import engine
from pet_app.notifications import designer
from pet_app.notifications.context import build_document_context, normalize_phone, recipient_phone
from pet_app.notifications.inbox import get_or_create_conversation, notify_inbox_users


EXECUTORS = set(designer.EXECUTORS)
OPERATORS = set(designer.OPERATORS)
ALLOWED_UPDATE_VALUES = {
	source: {
		fieldname: set(registration["update_values"])
		for fieldname, registration in config["fields"].items()
		if registration["update_values"]
	}
	for source, config in designer.SOURCE_REGISTRY.items()
}


def validate_action_rule(doc, method=None):
	if doc.doctype != "Pet App WhatsApp Action Rule":
		return
	if doc.source_doctype in designer.SOURCE_REGISTRY and not (
		doc.trigger_event == "Manual" and doc.recipient_type == "Manual"
	):
		normalized = designer.raise_for_invalid_rule(designer.canonical_rule(doc))
		doc.risk_level = normalized["risk_level"]
		doc.requires_approval = normalized["requires_approval"]
		for fieldname, value in designer.serialize_rule_sections(normalized).items():
			doc.set(fieldname, value)
		return

	# Temporary compatibility for existing manual rules. The designer API never exposes this path.
	if doc.executor not in EXECUTORS:
		frappe.throw(_("Unsupported WhatsApp action executor."))
	if not frappe.db.exists("DocType", doc.source_doctype):
		frappe.throw(_("Source DocType does not exist."))
	meta = frappe.get_meta(doc.source_doctype)
	if doc.recipient_field and not meta.has_field(doc.recipient_field):
		frappe.throw(_("Recipient field {0} does not exist on {1}.").format(doc.recipient_field, doc.source_doctype))

	conditions = _json(doc.condition_json, {})
	for condition in _condition_rows(conditions):
		fieldname = cstr(condition.get("field")).strip()
		operator = cstr(condition.get("operator")).strip()
		if not fieldname or not meta.has_field(fieldname):
			frappe.throw(_("Condition field {0} does not exist on {1}.").format(fieldname, doc.source_doctype))
		if operator not in OPERATORS:
			frappe.throw(_("Condition operator {0} is not supported.").format(operator))

	response = _json(doc.response_config_json, {})
	options = response.get("options") or []
	if doc.response_type in {"Buttons", "List"} and not options:
		frappe.throw(_("Interactive actions require response options."))
	if doc.response_type == "Buttons" and len(options) > 3:
		frappe.throw(_("WhatsApp supports at most three reply buttons."))
	if doc.response_type == "List" and len(options) > 10:
		frappe.throw(_("WhatsApp supports at most ten list options."))
	keys = set()
	for option in options:
		key = cstr(option.get("key")).strip()
		label = cstr(option.get("label")).strip()
		if not key or not label or key in keys:
			frappe.throw(_("Every response option needs a unique key and a label."))
		keys.add(key)

	config = _json(doc.executor_config_json, {})
	if doc.executor == "Update Allowed Field":
		fieldname = config.get("field")
		allowed = ALLOWED_UPDATE_VALUES.get(doc.source_doctype, {}).get(fieldname)
		if not allowed:
			frappe.throw(_("That field is not allowlisted for WhatsApp updates."))
		for value in (config.get("values") or {}).values():
			if value not in allowed:
				frappe.throw(_("Value {0} is not allowlisted for {1}.").format(value, fieldname))
	if doc.executor == "Create Rating" and cint(config.get("rating_scale") or 5) != 5:
		frappe.throw(_("The current Rating DocType supports the 1-5 scale."))


def after_insert(doc, method=None):
	_evaluate_hook(doc, "After Insert")


def on_update(doc, method=None):
	_evaluate_hook(doc, "On Update")


def on_submit(doc, method=None):
	_evaluate_hook(doc, "On Submit")


def _evaluate_hook(doc, event):
	if (
		getattr(frappe.flags, "in_migrate", False)
		or getattr(frappe.flags, "pet_app_whatsapp_action_execution", False)
		or doc.doctype.startswith("Pet App WhatsApp")
		or doc.doctype in {"Error Log", "Notification Log", "Version", "Comment", "Activity Log", "DocType", "DocField", "Custom Field", "Property Setter", "Module Def", "Page", "Workspace"}
		or not frappe.db.exists("DocType", "Pet App WhatsApp Action Rule")
	):
		return
	try:
		evaluate_document_rules(doc, event)
	except Exception:
		frappe.logger("pet_app.whatsapp_actions").error(
			"WhatsApp action rules failed for %s %s\n%s", doc.doctype, doc.name, frappe.get_traceback()
		)


def evaluate_document_rules(doc, event):
	if doc.doctype not in designer.SOURCE_REGISTRY:
		return []
	if event not in designer.SOURCE_REGISTRY[doc.doctype]["trigger_events"]:
		return []
	rules = frappe.get_all(
		"Pet App WhatsApp Action Rule",
		filters={"enabled": 1, "source_doctype": doc.doctype, "trigger_event": event},
		pluck="name",
		ignore_permissions=True,
	)
	created = []
	for name in rules:
		rule = frappe.get_doc("Pet App WhatsApp Action Rule", name)
		if conditions_match(rule, doc):
			result = create_action_request(rule, doc)
			if result:
				created.append(result)
	return created


def conditions_match(rule, doc) -> bool:
	conditions = _json(rule.condition_json, {})
	matched, _results, _warnings = designer.evaluate_conditions(conditions, doc, doc.get_doc_before_save())
	return matched


def create_action_request(rule, source_doc, *, recipient=None, context=None, process_now=False):
	validate_action_rule(rule)
	recipient_name, phone = _resolve_recipient(rule, source_doc, recipient)
	if not phone:
		frappe.throw(_("Could not resolve a WhatsApp recipient for {0}.").format(source_doc.name))
	account = engine.resolve_whatsapp_account(
		template=engine.resolve_template(template_key=rule.template_key), settings=engine.get_settings()
	)
	conversation = get_or_create_conversation(phone, account)
	key = _request_key(rule, source_doc, conversation.normalized_phone)
	existing = frappe.db.get_value("Pet App WhatsApp Action Request", {"idempotency_key": key}, "name")
	if existing:
		return frappe.get_doc("Pet App WhatsApp Action Request", existing)

	expiry_hours = cint(rule.expiry_hours) or cint(engine.get_settings().get("action_request_expiry_hours")) or 168
	rule_snapshot = designer.canonical_rule(rule)
	rule_snapshot["version_modified"] = cstr(rule.get("modified"))
	validation = designer.validate_rule(rule_snapshot)
	request_data = {
			"doctype": "Pet App WhatsApp Action Request",
			"rule": rule.name,
			"status": "Waiting Reply",
			"delivery_stage": "Queued",
			"conversation": conversation.name,
			"source_doctype": source_doc.doctype,
			"source_name": source_doc.name,
			"recipient_type": rule.recipient_type,
			"recipient_name": recipient_name,
			"recipient_phone": conversation.normalized_phone,
			"expires_at": now_datetime() + timedelta(hours=expiry_hours),
			"idempotency_key": key,
		}
	request_meta = frappe.get_meta("Pet App WhatsApp Action Request")
	if request_meta.has_field("rule_snapshot_json"):
		request_data["rule_snapshot_json"] = json.dumps(rule_snapshot, default=str)
	if request_meta.has_field("validation_json"):
		request_data["validation_json"] = json.dumps(validation, default=str)
	if request_meta.has_field("resolved_recipient_json"):
		request_data["resolved_recipient_json"] = json.dumps(
			{"type": rule.recipient_type, "name": recipient_name, "phone": phone}, default=str
		)
	try:
		request = frappe.get_doc(request_data).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		existing = frappe.db.get_value("Pet App WhatsApp Action Request", {"idempotency_key": key}, "name")
		if not existing:
			raise
		return frappe.get_doc("Pet App WhatsApp Action Request", existing)
	create_action_event(
		request,
		"Created",
		details={"rule_snapshot": rule_snapshot, "validation": validation, "resolved_recipient": {"type": rule.recipient_type, "name": recipient_name}},
	)

	message_context = build_document_context(source_doc.doctype, source_doc.name, context)
	interactive = build_interactive_payload(rule, request)
	result = engine.queue_notification(
		event_key=f"whatsapp.action.{rule.name}",
		recipient_type=rule.recipient_type,
		recipient_name=recipient_name,
		to_phone=conversation.normalized_phone,
		context=message_context,
		source_doctype=source_doc.doctype,
		source_name=source_doc.name,
		template_key=rule.template_key,
		channel="WhatsApp",
		idempotency_key=f"action-message:{key}",
		conversation=conversation.name,
		action_request=request.name,
		message_type="Interactive" if interactive else "Text",
		interactive=interactive,
	)
	if not result.get("ok"):
		return mark_action_failed(request, _result_error(result), details=result)

	queue_name = result["data"]["queue"]["name"]
	request.notification_queue = queue_name
	request.save(ignore_permissions=True)
	if process_now:
		engine.process_notification_queue(queue_name)
	else:
		frappe.enqueue(
			"pet_app.notifications.engine.process_notification_queue",
			queue="short",
			enqueue_after_commit=True,
			queue_name=queue_name,
		)
	return request


def send_interactive_for_request(request):
	if isinstance(request, str):
		request = frappe.get_doc("Pet App WhatsApp Action Request", request)
	if request.status != "Waiting Reply":
		return request
	rule = _rule_for_request(request)
	source = frappe.get_doc(request.source_doctype, request.source_name)
	context = build_document_context(source.doctype, source.name)
	result = engine.queue_notification(
		event_key=f"whatsapp.action.session.{rule.name}",
		recipient_type=request.recipient_type,
		recipient_name=request.recipient_name,
		to_phone=request.recipient_phone,
		context=context,
		source_doctype=request.source_doctype,
		source_name=request.source_name,
		template_key=rule.template_key,
		idempotency_key=f"action-interactive:{request.name}",
		conversation=request.conversation,
		action_request=request.name,
		message_type="Interactive",
		interactive=build_interactive_payload(rule, request),
	)
	if result.get("ok"):
		engine.process_notification_queue(result["data"]["queue"]["name"])
	return request


def process_inbound_action(message, conversation):
	requests = frappe.get_all(
		"Pet App WhatsApp Action Request",
		filters={"conversation": conversation.name, "status": "Waiting Reply"},
		fields=["name", "expires_at", "delivery_stage", "creation"],
		order_by="creation desc",
		ignore_permissions=True,
	)
	if not requests:
		return None

	for row in requests:
		if row.expires_at and get_datetime(row.expires_at) <= now_datetime():
			_mark_expired(row.name)
	requests = [row for row in requests if not row.expires_at or get_datetime(row.expires_at) > now_datetime()]
	if not requests:
		return None

	request = _interactive_request(message, requests) or frappe.get_doc("Pet App WhatsApp Action Request", requests[0].name)
	frappe.db.sql(
		"select name from `tabPet App WhatsApp Action Request` where name = %s for update",
		request.name,
	)
	request.reload()
	if request.status != "Waiting Reply":
		return request if request.status == "Completed" else None
	if request.delivery_stage == "Awaiting Comment" and not message.interactive_id:
		return _store_rating_comment(request, message)
	rule = _rule_for_request(request)
	match, ambiguous = match_response(rule, request, message)
	if ambiguous:
		request.status = "Needs Review"
		request.matched_message = message.name
		request.save(ignore_permissions=True)
		message.action_request = request.name
		message.save(ignore_permissions=True)
		create_action_event(request, "Needs Review", message=message, details={"reason": "Ambiguous reply"})
		notify_inbox_users(conversation, message, subject=f"WhatsApp action needs review: {rule.rule_name}")
		return request
	if not match:
		if request.delivery_stage == "Awaiting Session":
			send_interactive_for_request(request)
		return None

	request.response_key = match["key"]
	request.response_value = cstr(match.get("value", match["key"]))
	request.matched_message = message.name
	request.status = "Matched"
	request.save(ignore_permissions=True)
	message.action_request = request.name
	message.save(ignore_permissions=True)
	create_action_event(
		request,
		"Reply Matched",
		message=message,
		response_id=message.interactive_id,
		response_text=message.body,
		matched_alias=match.get("matched_alias"),
	)
	if _requires_review(rule):
		request.status = "Pending Review"
		request.save(ignore_permissions=True)
		notify_inbox_users(conversation, message, subject=f"WhatsApp action approval required: {rule.rule_name}")
		return request
	return execute_action(request)


def execute_action(request, *, approved_by=None, approval_note=None):
	if isinstance(request, str):
		request_name = request
	else:
		request_name = request.name
	frappe.db.sql(
		"select name from `tabPet App WhatsApp Action Request` where name = %s for update",
		request_name,
	)
	request = frappe.get_doc("Pet App WhatsApp Action Request", request_name)
	if request.status == "Completed":
		return request
	if request.expires_at and get_datetime(request.expires_at) <= now_datetime():
		_mark_expired(request.name)
		raise designer.DesignerError(_("This WhatsApp action has expired."), "ACTION_EXPIRED")
	if request.status not in {"Matched", "Pending Review"}:
		raise designer.DesignerError(_("This WhatsApp action is no longer executable."), "ACTION_STATUS_CONFLICT")
	rule = _rule_for_request(request)
	if rule.source_doctype in designer.SOURCE_REGISTRY and rule.recipient_type != "Manual":
		designer.raise_for_invalid_rule(designer.canonical_rule(rule))
	if _requires_review(rule) and not approved_by:
		raise designer.DesignerError(_("Staff approval is required before this action can execute."), "APPROVAL_REQUIRED")
	if approved_by:
		request.approved_by = approved_by
		request.approved_at = now_datetime()
		request.approval_note = approval_note
		create_action_event(request, "Approved", performed_by=approved_by, details={"note": approval_note})
	previous_execution_flag = getattr(frappe.flags, "pet_app_whatsapp_action_execution", False)
	try:
		frappe.flags.pet_app_whatsapp_action_execution = True
		result = _executor(rule.executor)(request, rule)
		if request.delivery_stage != "Awaiting Comment":
			request.status = "Completed"
			request.delivery_stage = "Complete"
		request.result_json = json.dumps(result or {}, default=str)
		request.error_message = None
		request.save(ignore_permissions=True)
		create_action_event(request, "Executed", details=result)
		_notify_action_result(request, rule, "completed")
		return request
	except Exception as exc:
		request.status = "Failed"
		request.error_message = cstr(exc)[:500]
		request.save(ignore_permissions=True)
		create_action_event(request, "Failed", details={"error": cstr(exc)})
		_notify_action_result(request, rule, "failed")
		raise
	finally:
		frappe.flags.pet_app_whatsapp_action_execution = previous_execution_flag


def reject_action(request, note=None):
	if isinstance(request, str):
		request_name = request
	else:
		request_name = request.name
	frappe.db.sql(
		"select name from `tabPet App WhatsApp Action Request` where name = %s for update",
		request_name,
	)
	request = frappe.get_doc("Pet App WhatsApp Action Request", request_name)
	if request.status not in {"Waiting Reply", "Matched", "Pending Review", "Needs Review"}:
		frappe.throw(_("Only pending WhatsApp actions can be rejected."))
	request.status = "Rejected"
	request.approved_by = frappe.session.user
	request.approved_at = now_datetime()
	request.approval_note = note
	request.save(ignore_permissions=True)
	create_action_event(request, "Rejected", performed_by=frappe.session.user, details={"note": note})
	return request


def mark_action_failed(request, error, *, details=None):
	if isinstance(request, str):
		request = frappe.get_doc("Pet App WhatsApp Action Request", request)
	if request.status in {"Completed", "Rejected", "Expired", "Failed"}:
		return request
	request.status = "Failed"
	request.error_message = cstr(error)[:500]
	request.save(ignore_permissions=True)
	create_action_event(request, "Failed", details=details or {"error": cstr(error)})
	rule = frappe.get_doc("Pet App WhatsApp Action Rule", request.rule)
	_notify_action_result(request, rule, "failed")
	return request


def expire_due_actions(limit=500):
	rows = frappe.get_all(
		"Pet App WhatsApp Action Request",
		filters={
			"status": ["in", ["Waiting Reply", "Matched", "Pending Review", "Needs Review"]],
			"expires_at": ["<=", now_datetime()],
		},
		pluck="name",
		limit_page_length=cint(limit) or 500,
		ignore_permissions=True,
	)
	for name in rows:
		_mark_expired(name)
	return len(rows)


def select_review_response(request, response_key):
	if isinstance(request, str):
		request = frappe.get_doc("Pet App WhatsApp Action Request", request)
	rule = _rule_for_request(request)
	options = _json(rule.response_config_json, {}).get("options") or []
	option = next((row for row in options if cstr(row.get("key")) == cstr(response_key)), None)
	if not option:
		frappe.throw(_("Choose a valid response before approving this action."))
	request.response_key = cstr(option.get("key"))
	request.response_value = cstr(option.get("value", option.get("key")))
	request.status = "Matched"
	request.save(ignore_permissions=True)
	return request


def match_response(rule, request, message):
	options = (_json(rule.response_config_json, {}).get("options") or [])
	if message.interactive_id:
		prefix = f"wa:{request.name}:"
		if message.interactive_id.startswith(prefix):
			key = message.interactive_id[len(prefix):]
			for option in options:
				if cstr(option.get("key")) == key:
					return dict(option), False
		return None, False

	value = _normalize_reply(message.body or message.interactive_title)
	matches = []
	for option in options:
		aliases = [option.get("key"), option.get("label"), *(option.get("aliases") or [])]
		if value and value in {_normalize_reply(alias) for alias in aliases if alias is not None}:
			matched = dict(option)
			matched["matched_alias"] = value
			matches.append(matched)
	return (matches[0], False) if len(matches) == 1 else (None, len(matches) > 1)


def build_interactive_payload(rule, request):
	if rule.response_type not in {"Buttons", "List"}:
		return None
	config = _json(rule.response_config_json, {})
	return {
		"type": rule.response_type,
		"button_text": config.get("button_text"),
		"section_title": config.get("section_title"),
		"footer": config.get("footer"),
		"options": [
			{
				"id": f"wa:{request.name}:{cstr(option.get('key'))}",
				"label": cstr(option.get("label")),
				"description": cstr(option.get("description")),
			}
			for option in (config.get("options") or [])
		],
	}


def create_action_event(request, event_type, *, message=None, response_id=None, response_text=None, matched_alias=None, performed_by=None, details=None):
	return frappe.get_doc(
		{
			"doctype": "Pet App WhatsApp Action Event",
			"action_request": request.name,
			"event_type": event_type,
			"message": message.name if message else None,
			"response_id": response_id,
			"response_text": response_text,
			"matched_alias": matched_alias,
			"performed_by": performed_by,
			"event_at": now_datetime(),
			"details_json": json.dumps(details or {}, default=str),
		}
	).insert(ignore_permissions=True)


def _executor(name):
	return {
		"Create Rating": _create_rating,
		"Update Allowed Field": _update_allowed_field,
		"Record Acknowledgement": _record_acknowledgement,
		"Create Staff Task": _create_staff_task,
		"Record Intent": _record_intent,
		"Require Staff Review": _record_acknowledgement,
	}[name]


def _create_rating(request, rule):
	config = _json(rule.executor_config_json, {})
	score = cint(request.response_value)
	scale = cint(config.get("rating_scale") or 5)
	if score < 1 or score > scale or scale > 5:
		frappe.throw(_("Rating must be within the configured 1-{0} scale.").format(scale))
	_source_row_lock(request.source_doctype, request.source_name)
	filters = {"reference_doctype": request.source_doctype, "reference_name": request.source_name}
	if config.get("questionnaire"):
		filters["questionnaire"] = config["questionnaire"]
	existing = frappe.db.get_value("Rating", filters, "name")
	if existing:
		rating = frappe.get_doc("Rating", existing)
	else:
		conversation = frappe.get_doc("Pet App WhatsApp Conversation", request.conversation)
		rated_by = frappe.db.get_value("Guardian", conversation.guardian, "user_id") if conversation.guardian else None
		rated_by = rated_by or engine.get_settings().get("whatsapp_feedback_user") or "Administrator"
		rating = frappe.get_doc(
			{
				"doctype": "Rating",
				"reference_doctype": request.source_doctype,
				"reference_name": request.source_name,
				"questionnaire": config.get("questionnaire"),
				"overall_rating": score,
				"rated_by": rated_by,
				"rated_at": now_datetime(),
			}
		).insert(ignore_permissions=True)
	request.result_doctype = "Rating"
	request.result_name = rating.name
	if cint(config.get("request_comment")):
		request.status = "Waiting Reply"
		request.delivery_stage = "Awaiting Comment"
		from pet_app.notifications.inbox import send_conversation_message

		send_conversation_message(request.conversation, config.get("comment_prompt") or "Thank you. Would you like to add a comment?", action_request=request.name)
	return {"rating": rating.name, "overall_rating": score, "awaiting_comment": bool(cint(config.get("request_comment")))}


def _update_allowed_field(request, rule):
	config = _json(rule.executor_config_json, {})
	fieldname = config.get("field")
	value = (config.get("values") or {}).get(request.response_key)
	allowed = ALLOWED_UPDATE_VALUES.get(request.source_doctype, {}).get(fieldname) or set()
	if value not in allowed:
		frappe.throw(_("The requested update is not allowlisted."))
	doc = frappe.get_doc(request.source_doctype, request.source_name)
	actor = request.approved_by or frappe.session.user
	if not actor or actor == "Guest" or not frappe.has_permission(
		request.source_doctype, ptype="write", doc=doc, user=actor
	):
		raise frappe.PermissionError
	doc.set(fieldname, value)
	doc.save(ignore_permissions=True)
	return {"field": fieldname, "value": value}


def _record_acknowledgement(request, rule):
	return {**_audit_result(request, rule), "acknowledged": True}


def _create_staff_task(request, rule):
	config = _json(rule.executor_config_json, {})
	allocated_to = config.get("allocated_to") or "Administrator"
	description = config.get("description") or f"WhatsApp follow-up for {request.source_doctype} {request.source_name}: {request.response_value}"
	todo_data = {
			"doctype": "ToDo",
			"allocated_to": allocated_to,
			"description": description,
			"reference_type": request.source_doctype,
			"reference_name": request.source_name,
			"status": "Open",
		}
	todo_meta = frappe.get_meta("ToDo")
	if todo_meta.has_field("pet_app_whatsapp_conversation"):
		todo_data["pet_app_whatsapp_conversation"] = request.conversation
	if todo_meta.has_field("pet_app_whatsapp_action_request"):
		todo_data["pet_app_whatsapp_action_request"] = request.name
	todo = frappe.get_doc(todo_data).insert(ignore_permissions=True)
	request.result_doctype = "ToDo"
	request.result_name = todo.name
	return {"todo": todo.name}


def _record_intent(request, rule):
	return {**_audit_result(request, rule), "intent": request.response_key, "record_changed": False}


def _store_rating_comment(request, message):
	if not request.result_name or request.result_doctype != "Rating":
		return None
	rating = frappe.get_doc("Rating", request.result_name)
	rating.notes = cstr(message.body).strip()
	rating.save(ignore_permissions=True)
	request.matched_message = message.name
	request.status = "Completed"
	request.delivery_stage = "Complete"
	request.save(ignore_permissions=True)
	message.action_request = request.name
	message.save(ignore_permissions=True)
	create_action_event(request, "Executed", message=message, details={"rating_comment": True})
	return request


def _resolve_recipient(rule, source_doc, recipient=None):
	if recipient:
		return None, normalize_phone(recipient, engine.get_settings().get("default_country_code"))
	if rule.source_doctype in designer.SOURCE_REGISTRY and rule.recipient_type != "Manual":
		resolved = designer.resolve_rule_recipient(designer.canonical_rule(rule), source_doc)
		return resolved["name"], resolved["phone"]
	value = source_doc.get(rule.recipient_field) if rule.recipient_field else None
	if rule.recipient_type == "Manual":
		return None, normalize_phone(value, engine.get_settings().get("default_country_code"))
	return value, normalize_phone(recipient_phone(rule.recipient_type, value), engine.get_settings().get("default_country_code"))


def _request_key(rule, source_doc, phone):
	if rule.duplicate_policy == "Allow Repeats":
		value = f"{rule.name}|{source_doc.doctype}|{source_doc.name}|{phone}|{now_datetime()}|{frappe.generate_hash(length=8)}"
	elif rule.duplicate_policy == "Once Per Rule And Recipient":
		value = f"{rule.name}|{phone}"
	else:
		value = f"{rule.name}|{source_doc.doctype}|{source_doc.name}"
	return hashlib.sha1(value.encode()).hexdigest()


def _condition_rows(conditions):
	if isinstance(conditions, list):
		return conditions
	if not isinstance(conditions, dict):
		return []
	return conditions.get("all") or conditions.get("any") or []


def _condition_matches(condition, doc, old_doc):
	fieldname = condition.get("field")
	operator = condition.get("operator")
	value = condition.get("value")
	current = doc.get(fieldname)
	previous = old_doc.get(fieldname) if old_doc else None
	values = value if isinstance(value, list) else [value]
	if operator == "equals":
		return current == value
	if operator == "not_equals":
		return current != value
	if operator == "in":
		return current in values
	if operator == "not_in":
		return current not in values
	if operator == "is_set":
		return current not in (None, "")
	if operator == "is_not_set":
		return current in (None, "")
	if operator == "changed":
		return old_doc is not None and current != previous
	if operator == "changed_to":
		return old_doc is not None and current != previous and current in values
	return False


def _interactive_request(message, rows):
	if not message.interactive_id or not message.interactive_id.startswith("wa:"):
		return None
	parts = message.interactive_id.split(":", 2)
	if len(parts) != 3 or parts[1] not in {row.name for row in rows}:
		return None
	return frappe.get_doc("Pet App WhatsApp Action Request", parts[1])


def _requires_review(rule):
	return bool(cint(rule.requires_approval) or rule.risk_level != "Low" or rule.executor == "Require Staff Review")


def _mark_expired(name):
	request = frappe.get_doc("Pet App WhatsApp Action Request", name)
	if request.delivery_stage == "Awaiting Comment" and request.result_doctype == "Rating" and request.result_name:
		request.status = "Completed"
		request.delivery_stage = "Complete"
		request.save(ignore_permissions=True)
		create_action_event(request, "Executed", details={"comment_skipped": True})
		return
	request.status = "Expired"
	request.save(ignore_permissions=True)
	create_action_event(request, "Expired")


def _notify_action_result(request, rule, state):
	conversation = frappe.get_doc("Pet App WhatsApp Conversation", request.conversation)
	message = frappe.get_doc("Pet App WhatsApp Message", request.matched_message) if request.matched_message else frappe._dict(body=request.response_value, message_type="Action")
	notify_inbox_users(conversation, message, subject=f"WhatsApp action {state}: {rule.rule_name}")


def _normalize_reply(value):
	return " ".join(unicodedata.normalize("NFKC", cstr(value)).casefold().split())


def _result_error(result):
	errors = result.get("errors") or []
	return result.get("message") or (errors[0].get("message") if errors else None) or _("WhatsApp notification could not be queued.")


def _json(value, default):
	if not value:
		return default
	if isinstance(value, (dict, list)):
		return value
	parsed = value
	try:
		for decode_index in range(3):
			if not isinstance(parsed, str):
				break
			parsed = json.loads(parsed)
		return parsed
	except (TypeError, ValueError):
		frappe.throw(_("WhatsApp action configuration must be valid JSON."))


def _rule_for_request(request):
	if request.meta.has_field("rule_snapshot_json") and request.rule_snapshot_json:
		try:
			snapshot = json.loads(request.rule_snapshot_json)
			rule = frappe._dict(snapshot)
			rule.condition_json = json.dumps(snapshot.get("condition") or {"all": []})
			rule.response_config_json = json.dumps(snapshot.get("response_config") or {"options": []})
			rule.executor_config_json = json.dumps(snapshot.get("executor_config") or {})
			return rule
		except (TypeError, ValueError):
			pass
	return frappe.get_doc("Pet App WhatsApp Action Rule", request.rule)


def _source_row_lock(source_doctype, source_name):
	if source_doctype not in designer.SOURCE_REGISTRY:
		raise designer.DesignerError(_("Source table is not allowlisted."), "SOURCE_NOT_ALLOWED")
	table = source_doctype.replace("`", "``")
	frappe.db.sql(f"select name from `tab{table}` where name = %s for update", source_name)


def _audit_result(request, rule):
	return {
		"source_doctype": request.source_doctype,
		"source_name": request.source_name,
		"rule": request.rule,
		"conversation": request.conversation,
		"recipient_type": request.recipient_type,
		"recipient_name": request.recipient_name,
		"response_key": request.response_key,
		"response_value": request.response_value,
	}
