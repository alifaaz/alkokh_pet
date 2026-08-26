from __future__ import annotations

import json
import re
from contextlib import contextmanager
from copy import deepcopy

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, get_time, getdate

from pet_app.notifications import engine
from pet_app.notifications.context import (
	build_document_context,
	mask_phone,
	normalize_phone,
	recipient_phone,
)
from pet_app.notifications.renderer import render_preview


SCHEMA_VERSION = 1
MAX_CONDITION_DEPTH = 3
MAX_CONDITIONS = 20
MAX_EXPIRY_HOURS = 720
MAX_BUTTONS = 3
MAX_LIST_OPTIONS = 10
GENERAL_TEMPLATE_VARIABLES = {"otp"}

TRIGGER_EVENTS = ("After Insert", "On Update", "On Submit", "Manual")
INTERACTION_TYPES = ("None", "Buttons", "List", "Typed Reply")
DUPLICATE_POLICIES = ("Once Per Source", "Once Per Rule And Recipient", "Allow Repeats")
RISK_LEVELS = ("Low", "Medium", "High", "Sensitive")
OPERATORS = ("equals", "not_equals", "in", "not_in", "is_set", "is_not_set", "changed", "changed_to")
EXECUTORS = (
	"Create Rating",
	"Update Allowed Field",
	"Create Staff Task",
	"Record Acknowledgement",
	"Record Intent",
	"Require Staff Review",
)

RULE_FIELDS = (
	"name",
	"rule_name",
	"enabled",
	"source_doctype",
	"trigger_event",
	"recipient_type",
	"recipient_field",
	"template_key",
	# Both must be here or canonical_rule and coerce_rule_payload drop them, and a
	# rule would forget its Meta template every time the designer saved it.
	"template_source",
	"meta_template",
	"response_type",
	"expiry_hours",
	"duplicate_policy",
	"executor",
	"risk_level",
	"requires_approval",
)

SECTION_FIELDS = {
	"condition": ("condition", "condition_json", {"all": []}),
	"response_config": ("response_config", "response_config_json", {"options": []}),
	"executor_config": ("executor_config", "executor_config_json", {}),
}

_SELECT_OPERATORS = ("equals", "not_equals", "in", "not_in", "changed", "changed_to")
_LINK_OPERATORS = ("equals", "not_equals", "is_set", "is_not_set", "changed", "changed_to")
_VALUE_OPERATORS = ("equals", "not_equals", "in", "not_in", "is_set", "is_not_set", "changed", "changed_to")
_RISK_INDEX = {value: index for index, value in enumerate(RISK_LEVELS)}
_SAFE_RESPONSE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class DesignerError(frappe.ValidationError):
	def __init__(self, message, code="VALIDATION_ERROR", details=None):
		super().__init__(message)
		self.code = code
		self.details = details or {}


def _field(operators, *, values=None, recipient_types=None, update_values=None, description=None):
	return {
		"operators": tuple(operators),
		"values": tuple(values or ()),
		"recipient_types": tuple(recipient_types or ()),
		"update_values": tuple(update_values or ()),
		"description": description,
	}


SOURCE_REGISTRY = {
	"Appointment": {
		"label": "Appointment",
		"description": "A scheduled clinic appointment",
		"trigger_events": ("After Insert", "On Update", "Manual"),
		"executors": ("Update Allowed Field", "Create Staff Task", "Record Acknowledgement", "Require Staff Review"),
		"fields": {
			"status": _field(
				_SELECT_OPERATORS,
				values=("Open", "Unverified", "Closed", "Cancelled"),
				update_values=("Open", "Unverified", "Closed", "Cancelled"),
				description="Current appointment status",
			),
			"custom_guardian": _field(_LINK_OPERATORS, recipient_types=("Guardian",)),
			"custom_customer": _field(_LINK_OPERATORS, recipient_types=("Customer",)),
			"custom_pet": _field(_LINK_OPERATORS),
			"scheduled_time": _field(_VALUE_OPERATORS),
			"custom_appointment_type": _field(
				_SELECT_OPERATORS,
				values=("visit", "follow_up", "showering", "barbering", "surgery", "examination", "lab", "radiology", "advance"),
			),
			"custom_doctor": _field(_LINK_OPERATORS),
			"custom_room": _field(_LINK_OPERATORS),
			"custom_duration_minutes": _field(_VALUE_OPERATORS),
			"custom_linked_visit_id": _field(_LINK_OPERATORS),
			"custom_linked_service_id": _field(_LINK_OPERATORS),
		},
	},
	"Pet Boarding": {
		"label": "Pet boarding",
		"description": "A boarding stay",
		# Check-in is not creation. On every booking sampled, check_in is stamped
		# between 17 seconds and 24 hours after the row is inserted, so a check-in
		# message is an On Update rule conditioned on record_status changing to
		# "Checked In" - not an After Insert one. After Insert is kept for rules that
		# genuinely mean "a booking was made".
		"trigger_events": ("After Insert", "On Update", "Manual"),
		# "Update Allowed Field" is deliberately absent. Nothing on a boarding record
		# should be writable from a guardian's WhatsApp reply - not the room, not the
		# lifecycle, not the billing - so no field here carries update_values either.
		"executors": ("Create Rating", "Create Staff Task", "Record Acknowledgement", "Record Intent", "Require Staff Review"),
		"fields": {
			# Recipient paths. Same mechanism every other source uses: the registered
			# field holds a link, and resolve_rule_recipient feeds it to
			# context.recipient_phone.
			"guardian": _field(_LINK_OPERATORS, recipient_types=("Guardian",), description="The stay's guardian"),
			"customer": _field(_LINK_OPERATORS, recipient_types=("Customer",)),
			# Lifecycle. record_status is the real one - status is a coarse
			# Open/Closed/Cancelled flag and does not distinguish a reservation from a
			# pet that has actually arrived.
			"record_status": _field(
				_SELECT_OPERATORS,
				values=("Pending Room", "Reserved", "Checked In", "Checked Out"),
				description="Where the stay is in its lifecycle",
			),
			"status": _field(_SELECT_OPERATORS, values=("Open", "Closed", "Cancelled")),
			"boarding_type": _field(_SELECT_OPERATORS, values=("Travel", "Treatment")),
			# Outcome and the death flag are guards, not triggers: a rule that messages
			# a guardian must be able to say "and the pet did not die here".
			"boarding_outcome": _field(_SELECT_OPERATORS, values=("", "Completed", "Death", "Transferred", "Cancelled", "Other")),
			"death_during_boarding": _field(_VALUE_OPERATORS, values=(0, 1)),
			"pet": _field(_LINK_OPERATORS),
			"practitioner": _field(_LINK_OPERATORS),
			"visit": _field(_LINK_OPERATORS),
			"service_room": _field(_LINK_OPERATORS),
			"branch": _field(_LINK_OPERATORS),
			"check_in": _field(_VALUE_OPERATORS),
			"check_out": _field(_VALUE_OPERATORS),
			"expected_check_out": _field(_VALUE_OPERATORS),
			# Billing, for conditions only. None of these is exposed as a message
			# variable - VARIABLE_FIELDS deliberately excludes deposit - but a rule may
			# legitimately test them, and a condition never reaches a customer.
			"billing_status": _field(_SELECT_OPERATORS, values=("Unbilled", "Invoiced")),
			"deposit": _field(_VALUE_OPERATORS),
			"total_cost": _field(_VALUE_OPERATORS),
			"balance": _field(_VALUE_OPERATORS),
		},
	},
	"PetCareService": {
		"label": "Pet care service",
		"description": "A booked pet-care service",
		"trigger_events": ("After Insert", "On Update", "Manual"),
		"executors": EXECUTORS,
		"fields": {
			"status": _field(
				_SELECT_OPERATORS,
				values=("pending", "completed", "overdue", "cancelled", "Completed", "Cancelled"),
				update_values=("pending", "completed", "overdue", "cancelled", "Completed", "Cancelled"),
				description="Current service status",
			),
			"guardian_id": _field(_LINK_OPERATORS, recipient_types=("Guardian",)),
			"pet_id": _field(_LINK_OPERATORS),
			"category": _field(_LINK_OPERATORS),
			"care_service_id": _field(_LINK_OPERATORS),
			"service_option": _field(_LINK_OPERATORS),
			"item_code": _field(_LINK_OPERATORS),
			"doctor": _field(_LINK_OPERATORS),
			"provider": _field(_LINK_OPERATORS),
			"due_date": _field(_VALUE_OPERATORS),
			"start_date": _field(_VALUE_OPERATORS),
			"end_date": _field(_VALUE_OPERATORS),
			"send_reminder": _field(_VALUE_OPERATORS, values=(0, 1)),
			"price": _field(_VALUE_OPERATORS),
		},
	},
	"Pet": {
		"label": "Pet",
		"description": "A pet profile",
		"trigger_events": ("After Insert", "On Update", "Manual"),
		"executors": ("Create Rating", "Create Staff Task", "Record Acknowledgement", "Require Staff Review"),
		"fields": {
			"status": _field(
				_SELECT_OPERATORS,
				values=("Available", "Adopted", "Fostered", "Missing", "Found", "Deceased", "Undertreatment", "Reserved"),
			),
			"pet_status": _field(_SELECT_OPERATORS, values=("Pending", "Approved", "Rejected", "Archived")),
			"requested_by": _field(_LINK_OPERATORS, recipient_types=("Guardian",)),
			"animal_species": _field(_LINK_OPERATORS),
			"animal_type": _field(_LINK_OPERATORS),
			"breed": _field(_LINK_OPERATORS),
			"birth_date": _field(_VALUE_OPERATORS),
			"gender": _field(_SELECT_OPERATORS),
		},
	},
	"Sales Invoice": {
		"label": "Sales invoice",
		"description": "A customer sales invoice",
		"trigger_events": ("On Update", "On Submit", "Manual"),
		"executors": ("Create Rating", "Create Staff Task", "Record Acknowledgement", "Record Intent", "Require Staff Review"),
		"fields": {
			"customer": _field(_LINK_OPERATORS, recipient_types=("Customer",)),
			"status": _field(
				_SELECT_OPERATORS,
				values=(
					"Draft", "Return", "Credit Note Issued", "Submitted", "Paid", "Partly Paid",
					"Unpaid", "Unpaid and Discounted", "Overdue and Discounted", "Overdue", "Cancelled",
					"Internal Transfer",
				),
			),
			"posting_date": _field(_VALUE_OPERATORS),
			"due_date": _field(_VALUE_OPERATORS),
			"currency": _field(_LINK_OPERATORS),
			"grand_total": _field(_VALUE_OPERATORS),
			"outstanding_amount": _field(_VALUE_OPERATORS),
		},
	},
	"Pet Death Record": {
		"label": "Pet death record",
		"description": "A documented pet death and guardian acknowledgement",
		"trigger_events": ("After Insert", "On Update", "Manual"),
		"executors": ("Create Staff Task", "Record Acknowledgement", "Require Staff Review"),
		"fields": {
			"pet": _field(_LINK_OPERATORS),
			"guardian": _field(_LINK_OPERATORS, recipient_types=("Guardian",)),
			"customer": _field(_LINK_OPERATORS, recipient_types=("Customer",)),
			"status": _field(
				_SELECT_OPERATORS,
				values=("Draft", "Reported", "Pending Confirmation", "Confirmed", "Finalized", "Cancelled"),
			),
			"death_datetime": _field(_VALUE_OPERATORS),
			"reported_datetime": _field(_VALUE_OPERATORS),
			"death_reason_category": _field(_LINK_OPERATORS),
			"requires_manager_review": _field(_VALUE_OPERATORS, values=(0, 1)),
			"certificate_issued": _field(_VALUE_OPERATORS, values=(0, 1)),
			"body_handling_option": _field(_SELECT_OPERATORS),
			"necropsy_status": _field(_SELECT_OPERATORS),
		},
	},
	"Guardian": {
		"label": "Guardian",
		"description": "A guardian account",
		"trigger_events": ("After Insert", "On Update", "Manual"),
		"executors": ("Create Staff Task", "Record Acknowledgement", "Require Staff Review"),
		"fields": {
			"customer_id": _field(_LINK_OPERATORS, recipient_types=("Customer",)),
			"user_id": _field(_LINK_OPERATORS, recipient_types=("User",)),
			"is_active": _field(_VALUE_OPERATORS, values=(0, 1)),
			"otp_verified": _field(_VALUE_OPERATORS, values=(0, 1)),
			"full_name": _field(_VALUE_OPERATORS),
			"email_id": _field(_VALUE_OPERATORS),
			"city": _field(_VALUE_OPERATORS),
			"country": _field(_VALUE_OPERATORS),
		},
	},
}


EXECUTOR_REGISTRY = {
	"Create Rating": {
		"label": "Create rating",
		"description": "Create at most one Rating for the source and optionally collect a comment",
		"icon": "tabler-star",
		"minimum_risk": "Low",
		"requires_approval": False,
		"config_fields": (
			{"key": "rating_scale", "label": "Rating scale", "type": "number", "required": True, "min": 1, "max": 5},
			{"key": "questionnaire", "label": "Questionnaire", "type": "link", "link_doctype": "Rating Questionnaire", "required": False},
			{"key": "request_comment", "label": "Ask for an optional comment", "type": "boolean"},
			{"key": "comment_prompt", "label": "Comment prompt", "type": "text"},
		),
	},
	"Update Allowed Field": {
		"label": "Update an allowed field",
		"description": "Map a validated reply to an allowlisted field value",
		"icon": "tabler-edit-circle",
		"minimum_risk": "Medium",
		"requires_approval": True,
		"config_fields": (
			{"key": "field", "label": "Allowed field", "type": "field", "field_scope": "writable", "required": True},
		),
	},
	"Create Staff Task": {
		"label": "Create a staff task",
		"description": "Create a task for an allowed active User",
		"icon": "tabler-checklist",
		"minimum_risk": "Low",
		"requires_approval": False,
		"config_fields": (
			{"key": "allocated_to", "label": "Assigned user", "type": "user", "required": True},
			{"key": "description", "label": "Task description", "type": "textarea", "required": True},
		),
	},
	"Record Acknowledgement": {
		"label": "Record acknowledgement",
		"description": "Store an audited acknowledgement without changing the source record",
		"icon": "tabler-message-check",
		"minimum_risk": "Low",
		"requires_approval": False,
		"config_fields": (),
	},
	"Record Intent": {
		"label": "Record intent",
		"description": "Store an audited intent without changing payment or business state",
		"icon": "tabler-receipt",
		"minimum_risk": "Low",
		"requires_approval": False,
		"config_fields": (),
	},
	"Require Staff Review": {
		"label": "Require staff review",
		"description": "Hold the matched response until a staff member approves it",
		"icon": "tabler-user-check",
		"minimum_risk": "Medium",
		"requires_approval": True,
		"config_fields": (),
	},
}


def get_designer_schema(source_doctype=None):
	schema = {
		"version": SCHEMA_VERSION,
		"source_tables": [_source_summary(name, config) for name, config in SOURCE_REGISTRY.items()],
		"recipient_types": [
			{"value": "Guardian", "label": "Guardian", "description": "Guardian linked from the source record"},
			{"value": "Customer", "label": "Customer", "description": "Customer linked from the source record"},
			{"value": "User", "label": "User", "description": "User linked from the source record"},
		],
		"interaction_types": [
			{"value": "None", "label": "No reply", "description": "Send an informational message", "max_options": 0},
			{"value": "Buttons", "label": "Quick replies", "description": "Show up to three reply buttons", "max_options": MAX_BUTTONS},
			{"value": "List", "label": "Choice list", "description": "Show up to ten list choices", "max_options": MAX_LIST_OPTIONS},
			{"value": "Typed Reply", "label": "Typed answers", "description": "Match configured typed aliases", "max_options": MAX_LIST_OPTIONS},
		],
		"executors": [_executor_schema(name) for name in EXECUTORS],
		"duplicate_policies": [{"value": value, "label": value} for value in DUPLICATE_POLICIES],
		"risk_levels": [{"value": value, "label": value} for value in RISK_LEVELS],
		"limits": {
			"buttons": MAX_BUTTONS,
			"list": MAX_LIST_OPTIONS,
			"conditions": MAX_CONDITIONS,
			"condition_depth": MAX_CONDITION_DEPTH,
			"expiry_hours_max": MAX_EXPIRY_HOURS,
		},
	}
	if source_doctype:
		schema["selected_source"] = get_source_schema(source_doctype)
	return schema


def get_source_schema(source_doctype):
	config = _source_config(source_doctype)
	meta = frappe.get_meta(source_doctype)
	fields = []
	for fieldname, registration in config["fields"].items():
		df = meta.get_field(fieldname)
		if not df:
			continue
		values = list(registration["values"])
		if df.fieldtype == "Select" and not values:
			values = [value for value in cstr(df.options).splitlines() if value]
		row = {
			"fieldname": fieldname,
			"label": cstr(df.label or fieldname),
			"fieldtype": df.fieldtype,
			"allowed_operators": list(registration["operators"]),
			"recipient_types": list(registration["recipient_types"]),
			"can_update": bool(registration["update_values"]),
			"required": bool(cint(df.reqd)),
		}
		if registration.get("description") or df.description:
			row["description"] = cstr(registration.get("description") or df.description)
		if df.fieldtype == "Link":
			row.update(_link_metadata(df.options))
		if df.fieldtype == "Select":
			row["options"] = [{"label": value, "value": value} for value in values]
		if df.fieldtype == "Check":
			row["options"] = [{"label": "No", "value": 0}, {"label": "Yes", "value": 1}]
		if registration["update_values"]:
			row["allowed_values"] = list(registration["update_values"])
		elif values:
			row["allowed_values"] = values
		fields.append(row)

	return {
		**_source_summary(source_doctype, config),
		"fields": fields,
		"templates": compatible_templates(source_doctype),
		"executors": [_executor_schema(name) for name in config["executors"]],
	}


def compatible_templates(source_doctype):
	_source_config(source_doctype)
	rows = frappe.get_all(
		"Pet App WhatsApp Template",
		filters={"enabled": 1},
		fields=["name", "template_key", "template_name", "source_doctype", "delivery_mode", "recipient_type"],
		order_by="template_key asc",
		ignore_permissions=True,
	)
	return [
		{
			"value": row.template_key,
			"label": row.template_key,
			"name": row.name,
			"template_name": row.template_name,
			"source_doctype": row.source_doctype,
			"delivery_mode": row.delivery_mode,
			"recipient_type": row.recipient_type,
		}
		for row in rows
		if not row.source_doctype or row.source_doctype == source_doctype
	]


def validate_template_content(template, source_doctype):
	errors = template_content_errors(template, source_doctype)
	if errors:
		raise DesignerError(errors[0], "TEMPLATE_NOT_COMPATIBLE", {"errors": errors})


def template_content_errors(template, source_doctype):
	from pet_app.notifications.context import list_template_variables

	allowed = {row["key"] for row in list_template_variables(source_doctype)}
	if not source_doctype:
		allowed.update(GENERAL_TEMPLATE_VARIABLES)
	as_dict = getattr(template, "as_dict", None)
	data = as_dict() if callable(as_dict) else dict(template or {})
	content = "\n".join(cstr(data.get(fieldname)) for fieldname in ("body_preview", "footer_text") if data.get(fieldname))
	errors = []
	if re.search(r"<\s*/?\s*[A-Za-z][^>]*>", content):
		errors.append(_("WhatsApp message content must be plain text, not HTML."))
	if "{%" in content or "{#" in content:
		errors.append(_("WhatsApp templates may contain allowlisted variables, not Jinja statements."))
	for expression in re.findall(r"{{\s*([^{}]+?)\s*}}", content):
		key = expression.strip()
		if key not in allowed:
			errors.append(_("Template variable {0} is not allowlisted for this source table.").format(key))
	return list(dict.fromkeys(errors))


def canonical_rule(rule):
	as_dict = getattr(rule, "as_dict", None)
	data = as_dict() if callable(as_dict) else dict(rule or {})
	result = {field: data.get(field) for field in RULE_FIELDS}
	result["enabled"] = cint(result.get("enabled"))
	result["expiry_hours"] = cint(result.get("expiry_hours"))
	result["requires_approval"] = cint(result.get("requires_approval"))
	parse_errors = {}
	for section, (canonical_key, legacy_key, default) in SECTION_FIELDS.items():
		value = data.get(canonical_key) if canonical_key in data else data.get(legacy_key)
		parsed, error = parse_config_section(value, default, section)
		result[canonical_key] = parsed if not error else None
		if error:
			parse_errors[section] = error
	if parse_errors:
		result["parse_errors"] = parse_errors
	return result


def coerce_rule_payload(payload, existing=None):
	payload = dict(payload or {})
	if isinstance(payload.get("rule"), dict):
		payload = dict(payload["rule"])
	as_dict = getattr(existing, "as_dict", None)
	existing_data = as_dict() if callable(as_dict) else dict(existing or {})
	result = {field: payload.get(field, existing_data.get(field)) for field in RULE_FIELDS}
	parse_errors = {}
	for section, (canonical_key, legacy_key, default) in SECTION_FIELDS.items():
		existing_value = existing_data.get(canonical_key) if canonical_key in existing_data else existing_data.get(legacy_key)
		_existing_parsed, existing_error = parse_config_section(existing_value, default, section)
		if canonical_key in payload:
			value = payload.get(canonical_key)
		elif legacy_key in payload:
			value = payload.get(legacy_key)
		elif canonical_key in existing_data:
			value = existing_data.get(canonical_key)
		else:
			value = existing_data.get(legacy_key)
		parsed, error = parse_config_section(value, default, section)
		if existing_error and not _explicit_valid_section_replacement(payload, canonical_key, legacy_key, default, section):
			parsed, error = None, existing_error
		result[canonical_key] = parsed if not error else None
		if error:
			parse_errors[section] = error
	if parse_errors:
		result["parse_errors"] = parse_errors
	return result


def _explicit_valid_section_replacement(payload, canonical_key, legacy_key, default, section):
	if canonical_key in payload:
		value = payload.get(canonical_key)
	elif legacy_key in payload:
		value = payload.get(legacy_key)
	else:
		return False
	parsed, error = parse_config_section(value, default, section)
	return value not in (None, "") and not error and isinstance(parsed, dict)


def parse_config_section(value, default, section):
	if value in (None, ""):
		return deepcopy(default), None
	parsed = value
	try:
		for decode_index in range(3):
			if not isinstance(parsed, str):
				break
			parsed = json.loads(parsed)
	except (TypeError, ValueError, json.JSONDecodeError):
		return None, _("The stored {0} configuration is malformed.").format(section.replace("_", " "))
	if section == "condition" and isinstance(parsed, list):
		parsed = {"all": parsed}
	if not isinstance(parsed, dict):
		return None, _("The stored {0} configuration must be an object.").format(section.replace("_", " "))
	return parsed, None


def serialize_rule_sections(rule):
	return {
		legacy_key: json.dumps(rule[canonical_key], ensure_ascii=True, separators=(",", ":"), default=str)
		for canonical_key, legacy_key, _default in SECTION_FIELDS.values()
	}


def validate_rule(rule, existing=None):
	candidate = coerce_rule_payload(rule, existing)
	issues = []
	for section, message in (candidate.get("parse_errors") or {}).items():
		_issue(issues, _designer_section(section), section, "LEGACY_SECTION_MALFORMED", message)
	if issues:
		return _validation_result(candidate, issues, None)

	normalized = deepcopy(candidate)
	normalized.pop("parse_errors", None)
	normalized["rule_name"] = cstr(normalized.get("rule_name")).strip()
	normalized["source_doctype"] = cstr(normalized.get("source_doctype")).strip()
	normalized["trigger_event"] = cstr(normalized.get("trigger_event") or "Manual").strip()
	normalized["recipient_type"] = cstr(normalized.get("recipient_type")).strip()
	normalized["recipient_field"] = cstr(normalized.get("recipient_field")).strip()
	normalized["template_key"] = cstr(normalized.get("template_key")).strip()
	normalized["response_type"] = cstr(normalized.get("response_type") or "None").strip()
	normalized["duplicate_policy"] = cstr(normalized.get("duplicate_policy") or "Once Per Source").strip()
	normalized["executor"] = cstr(normalized.get("executor")).strip()
	normalized["risk_level"] = cstr(normalized.get("risk_level") or "Low").strip()
	normalized["enabled"] = cint(normalized.get("enabled", 1))
	normalized["requires_approval"] = cint(normalized.get("requires_approval"))
	normalized["expiry_hours"] = cint(normalized.get("expiry_hours") or 168)

	if not normalized["rule_name"]:
		_issue(issues, "when", "rule_name", "VALUE_REQUIRED", _("Rule name is required."))

	config = SOURCE_REGISTRY.get(normalized["source_doctype"])
	if not config:
		_issue(issues, "when", "source_doctype", "SOURCE_NOT_ALLOWED", _("Source table is not allowlisted."))
	else:
		if normalized["trigger_event"] not in config["trigger_events"]:
			_issue(issues, "when", "trigger_event", "TRIGGER_NOT_ALLOWED", _("Trigger event is not allowed for this source table."))
		normalized["condition"] = _validate_conditions(
			normalized.get("condition") or {"all": []},
			normalized["trigger_event"],
			normalized["source_doctype"],
			config,
			issues,
		)
		_validate_recipient(normalized, config, issues)
		_validate_template(normalized, issues)
		_validate_response(normalized, issues)
		_validate_executor(normalized, config, issues)

	if normalized["expiry_hours"] < 1 or normalized["expiry_hours"] > MAX_EXPIRY_HOURS:
		_issue(issues, "reply", "expiry_hours", "VALUE_OUT_OF_RANGE", _("Expiry must be between 1 and {0} hours.").format(MAX_EXPIRY_HOURS))
	if normalized["duplicate_policy"] not in DUPLICATE_POLICIES:
		_issue(issues, "then", "duplicate_policy", "VALUE_NOT_ALLOWED", _("Duplicate policy is not allowed."))
	if normalized["risk_level"] not in RISK_LEVELS:
		_issue(issues, "then", "risk_level", "VALUE_NOT_ALLOWED", _("Risk level is not allowed."))
	else:
		executor_policy = EXECUTOR_REGISTRY.get(normalized["executor"], {})
		minimum_risk = executor_policy.get("minimum_risk", "Low")
		if _RISK_INDEX[normalized["risk_level"]] < _RISK_INDEX[minimum_risk]:
			normalized["risk_level"] = minimum_risk
		if normalized["risk_level"] != "Low" or executor_policy.get("requires_approval"):
			normalized["requires_approval"] = 1

	valid = not any(issue["severity"] == "error" for issue in issues)
	return _validation_result(normalized, issues, normalized if valid else None)


def raise_for_invalid_rule(rule, existing=None):
	validation = validate_rule(rule, existing)
	if validation["valid"]:
		return validation["normalized_rule"]
	first = next(issue for issue in validation["issues"] if issue["severity"] == "error")
	raise DesignerError(first["message"], first["code"], {"issues": validation["issues"]})


def evaluate_conditions(condition, doc, old_doc=None, *, simulation=False):
	results = []
	warnings = []

	def evaluate(node):
		if not node:
			return True
		if "all" in node or "any" in node:
			mode = "any" if "any" in node else "all"
			values = [evaluate(child) for child in node.get(mode) or []]
			return any(values) if mode == "any" else all(values)
		fieldname = node.get("field")
		operator = node.get("operator")
		target = node.get("value")
		actual = doc.get(fieldname)
		previous = old_doc.get(fieldname) if old_doc else None
		values = target if isinstance(target, list) else [target]
		if operator == "equals":
			passed = _equal(actual, target)
		elif operator == "not_equals":
			passed = not _equal(actual, target)
		elif operator == "in":
			passed = any(_equal(actual, value) for value in values)
		elif operator == "not_in":
			passed = all(not _equal(actual, value) for value in values)
		elif operator == "is_set":
			passed = actual not in (None, "")
		elif operator == "is_not_set":
			passed = actual in (None, "")
		elif operator == "changed":
			passed = old_doc is not None and not _equal(actual, previous)
		elif operator == "changed_to":
			passed = (old_doc is not None and not _equal(actual, previous) and any(_equal(actual, value) for value in values))
			if simulation and old_doc is None:
				passed = any(_equal(actual, value) for value in values)
				warnings.append(_("Previous document state is unavailable; simulation assumes the configured transition occurred."))
		else:
			passed = False
		results.append(
			{
				"label": _condition_label(fieldname, operator, target),
				"field": fieldname,
				"operator": operator,
				"passed": bool(passed),
				"actual": actual,
				"previous": previous,
			}
		)
		return bool(passed)

	matched = evaluate(condition or {"all": []})
	return matched, results, list(dict.fromkeys(warnings))


def resolve_rule_recipient(rule, source_doc):
	config = _source_config(rule.get("source_doctype") if isinstance(rule, dict) else rule.source_doctype)
	recipient_type = rule.get("recipient_type") if isinstance(rule, dict) else rule.recipient_type
	recipient_field = rule.get("recipient_field") if isinstance(rule, dict) else rule.recipient_field
	registration = config["fields"].get(recipient_field)
	if not registration or recipient_type not in registration["recipient_types"]:
		raise DesignerError(_("Recipient path is not allowlisted."), "RECIPIENT_PATH_NOT_ALLOWED")
	name = source_doc.get(recipient_field)
	phone = recipient_phone(recipient_type, name)
	phone = normalize_phone(phone, engine.get_settings().get("default_country_code"))
	display_name = _recipient_display_name(recipient_type, name)
	return {"type": recipient_type, "name": name, "display_name": display_name, "phone": phone}


def simulate_rule(rule, source_name):
	with simulation_context():
		return _simulate_rule(rule, source_name)


def _simulate_rule(rule, source_name):
	validation = validate_rule(rule)
	if not validation["valid"]:
		first = next(issue for issue in validation["issues"] if issue["severity"] == "error")
		raise DesignerError(first["message"], first["code"], {"validation": validation})
	normalized = validation["normalized_rule"]
	source_doctype = normalized["source_doctype"]
	if not source_name or not frappe.db.exists(source_doctype, source_name):
		raise DesignerError(_("Source record was not found."), "SOURCE_RECORD_NOT_FOUND")
	doc = frappe.get_doc(source_doctype, source_name)
	matched, condition_results, warnings = evaluate_conditions(normalized["condition"], doc, simulation=True)
	recipient = resolve_rule_recipient(normalized, doc)
	if not recipient["phone"]:
		warnings.append(_("The selected recipient has no WhatsApp phone number."))
	context = build_document_context(source_doctype, source_name)
	rendered, delivery_mode, would_send = _simulate_message(normalized, context, warnings)
	return {
		"matched": bool(matched),
		"source_name": source_name,
		"condition_results": condition_results,
		"recipient": {
			**recipient,
			"phone": _display_phone(recipient["phone"]),
		},
		"rendered_message": rendered,
		# False when the send would be refused before reaching Meta. Deliberately NOT
		# expressed as ok:false on the envelope - the simulation succeeded; it is the
		# send that would not. The reason is in warnings, carrying the real code.
		"would_send": would_send,
		"delivery_mode": delivery_mode,
		"interaction": {
			"type": normalized["response_type"],
			"options": deepcopy(normalized["response_config"].get("options") or []),
		},
		"executor_preview": _executor_preview(normalized, source_name),
		"warnings": warnings,
	}


META_DELIVERY_MODE = "Meta Template"


def _refusal_warning(exc) -> str:
    """A caught refusal, as a warning that names the real code and slot.

    A bare "could not resolve" is the false-pass failure mode this project keeps
    hitting, so the code an operator would see on a real send is carried verbatim.
    """
    details = getattr(exc, "details", None) or {}
    code = cstr(getattr(exc, "exc_type", "")) or "META_TEMPLATE_NOT_SENDABLE"
    slot = details.get("slot")
    where = _("Slot {0}: ").format(slot) if slot else ""
    return f"[{code}] {where}{cstr(exc)}"


def _simulate_message(normalized, context, warnings):
    """(rendered_message, delivery_mode, would_send) for either template source.

    MetaTemplateNotSendable is caught **here only**. This is the dry-simulation path:
    its whole purpose is to show an operator the problem, so a refusal becomes a loud
    warning plus would_send=False rather than an exception that hides the rest of the
    result. Every real send path still treats the same refusal as a refusal.
    """
    from pet_app.notifications.channels.whatsapp_meta import build_template_message
    from pet_app.notifications.meta_templates import (
        MetaTemplateNotSendable,
        resolve_meta_send_identity,
        resolve_send_identity,
    )
    if _uses_meta_template(normalized):
        try:
            target = resolve_meta_send_identity(cstr(normalized.get("meta_template")).strip())
        except MetaTemplateNotSendable as exc:
            warnings.append(_refusal_warning(exc))
            return "", META_DELIVERY_MODE, False
        rendered, would_send = _preview_meta_message(target, context, warnings)
        return rendered, META_DELIVERY_MODE, would_send

    # Local rules keep their existing preview, rendered from the local body_preview.
    template = _get_template(normalized["template_key"])
    rendered = render_preview(template, context, mask_sensitive=False)
    would_send = True
    try:
        # The local path still resolves its identity through the bound mirror row, so a
        # rule whose template is unbound or unapproved refuses at send. A would_send
        # that ignored that would be a lie in exactly the direction that hurts.
        target = resolve_send_identity(template)
    except MetaTemplateNotSendable as exc:
        warnings.append(_refusal_warning(exc))
        return rendered, cstr(template.delivery_mode), False
    try:
        build_template_message("<simulated>", target, dict(context))
    except MetaTemplateNotSendable as exc:
        warnings.append(_refusal_warning(exc))
        would_send = False
    return rendered, cstr(template.delivery_mode), would_send


def _preview_meta_message(target, context, warnings):
    """Preview text for a Meta-addressed rule, and whether the send would go."""
    from pet_app.notifications.channels.whatsapp_meta import build_template_message
    from pet_app.notifications.meta_templates import MetaTemplateNotSendable
    from pet_app.notifications.renderer import preview_declared_message

    try:
        payload = build_template_message("<simulated>", target, dict(context))
    except MetaTemplateNotSendable as exc:
        # A per-slot refusal is reported by the per-slot pass below, which finds every
        # failing slot rather than just the first one the builder tripped on. Only a
        # whole-template refusal (a stale map, an unsupported shape) is reported here,
        # or the same slot would be warned about twice.
        if not (getattr(exc, "details", None) or {}).get("slot"):
            warnings.append(_refusal_warning(exc))
        return preview_declared_message(target.components, _best_effort_values(target, context, warnings)), False

    emitted = (payload.get("template") or {}).get("components") or []
    values = [
        cstr(parameter.get("text"))
        for component in emitted
        for parameter in component.get("parameters") or []
    ]
    return preview_declared_message(target.components, values), True


def _best_effort_values(target, context, warnings):
    """Resolve each slot on its own so the ones that work still show.

    Uses resolve_slots itself, one slot at a time, rather than a second lenient
    resolver - the rules for what resolves must not be able to differ between preview
    and send. A slot that refuses keeps its literal {{n}} in the preview, so nobody can
    look at the output and mistake a hole for a value.
    """
    from pet_app.notifications.meta_templates import MetaTemplateNotSendable, resolve_slots, stored_slot_map

    values = []
    for row in stored_slot_map(target.meta_template_id):
        slot = {"slot": cint(row.get("slot")), "variable_key": cstr(row.get("variable_key")), "fallback": cstr(row.get("fallback"))}
        try:
            values.extend(
                resolve_slots([slot], context, label=target.label, meta_template_id=target.meta_template_id)
            )
        except MetaTemplateNotSendable as exc:
            warnings.append(_refusal_warning(exc))
            values.append("{{%d}}" % slot["slot"])
    return values


@contextmanager
def simulation_context():
	previous_flag = getattr(frappe.flags, "pet_app_whatsapp_simulation", False)
	frappe.flags.pet_app_whatsapp_simulation = True
	try:
		yield
	finally:
		frappe.flags.pet_app_whatsapp_simulation = previous_flag


def _validate_conditions(condition, trigger_event, source_doctype, source_config, issues):
	count = 0

	def visit(node, depth, path):
		nonlocal count
		if not isinstance(node, dict):
			_issue(issues, "if", path, "EXECUTOR_CONFIG_INVALID", _("Each condition must be an object."))
			return {}
		groups = [key for key in ("all", "any") if key in node]
		if groups:
			if len(groups) != 1 or len(node) != 1:
				_issue(issues, "if", path, "EXECUTOR_CONFIG_INVALID", _("A condition group must contain exactly one of all or any."))
			if depth >= MAX_CONDITION_DEPTH:
				_issue(issues, "if", path, "VALUE_OUT_OF_RANGE", _("Condition nesting is too deep."))
			mode = groups[0]
			children = node.get(mode)
			if not isinstance(children, list):
				_issue(issues, "if", path, "EXECUTOR_CONFIG_INVALID", _("Condition group rows must be a list."))
				return {mode: []}
			return {mode: [visit(child, depth + 1, f"{path}.{mode}.{index}") for index, child in enumerate(children)]}

		count += 1
		if count > MAX_CONDITIONS:
			_issue(issues, "if", path, "VALUE_OUT_OF_RANGE", _("A rule may contain at most {0} conditions.").format(MAX_CONDITIONS))
		fieldname = cstr(node.get("field")).strip()
		operator = cstr(node.get("operator")).strip()
		registration = source_config["fields"].get(fieldname)
		if not registration:
			_issue(issues, "if", f"{path}.field", "FIELD_NOT_ALLOWED", _("Condition field is not allowlisted."))
			return {"field": fieldname, "operator": operator, "value": node.get("value")}
		if operator not in registration["operators"]:
			_issue(issues, "if", f"{path}.operator", "OPERATOR_NOT_ALLOWED", _("Condition operator is not allowed for this field."))
		if operator in {"changed", "changed_to"} and trigger_event != "On Update":
			_issue(issues, "if", f"{path}.operator", "TRIGGER_NOT_ALLOWED", _("Change operators require the On Update trigger."))
		value = node.get("value")
		if operator in {"in", "not_in", "changed_to"} and not isinstance(value, list):
			_issue(issues, "if", f"{path}.value", "VALUE_NOT_ALLOWED", _("This operator requires a list of values."))
		elif operator not in {"is_set", "is_not_set", "changed"}:
			_validate_condition_values(
				fieldname,
				value,
				registration,
				frappe.get_meta(source_doctype).get_field(fieldname),
				issues,
				f"{path}.value",
			)
		return {"field": fieldname, "operator": operator, "value": value}

	return visit(condition, 0, "condition")


def _validate_condition_values(fieldname, value, registration, df, issues, path):
	values = value if isinstance(value, list) else [value]
	if not values or any(item is None for item in values):
		_issue(issues, "if", path, "VALUE_NOT_ALLOWED", _("Condition value is required."))
		return
	allowed = registration["values"]
	if df and df.fieldtype == "Select" and not allowed:
		allowed = tuple(entry for entry in cstr(df.options).splitlines() if entry)
	if df and df.fieldtype == "Check" and not allowed:
		allowed = (0, 1)
	if allowed:
		for item in values:
			if item not in allowed and cstr(item) not in {cstr(entry) for entry in allowed}:
				_issue(issues, "if", path, "VALUE_NOT_ALLOWED", _("Value {0} is not allowed for {1}.").format(item, fieldname))
	if df:
		for item in values:
			if not _valid_field_value(df.fieldtype, item):
				_issue(issues, "if", path, "VALUE_NOT_ALLOWED", _("Value {0} is not valid for the {1} field type.").format(item, df.fieldtype))


def _validate_recipient(rule, source_config, issues):
	fieldname = rule["recipient_field"]
	registration = source_config["fields"].get(fieldname)
	if not registration or rule["recipient_type"] not in registration["recipient_types"]:
		_issue(issues, "send", "recipient_field", "RECIPIENT_PATH_NOT_ALLOWED", _("Recipient type and field are not an eligible registered path."))


def _uses_meta_template(rule) -> bool:
	return cstr(rule.get("template_source")).strip().lower() == "meta" or bool(
		cstr(rule.get("meta_template")).strip()
	)


def _validate_meta_template(rule, issues):
	"""Validate a Meta-addressed rule against the mirror row it names.

	Whether the mirror row is *bound* to a local template is irrelevant here - that
	binding exists for the local send path. What matters is that Meta will accept the
	send: the template must be APPROVED, and if it carries a slot map that map must
	still match the number of variables the template declares. Both are things that
	would otherwise pass validation and refuse at send time, in front of an operator.
	"""
	from pet_app.notifications.meta_templates import (
		MIRROR_DOCTYPE,
		SENDABLE_STATUS,
		_target_from_row,
		_TARGET_FIELDS,
		stored_slot_map,
	)
	from pet_app.notifications.renderer import declared_parameter_count

	meta_template = cstr(rule.get("meta_template")).strip()
	if not meta_template:
		_issue(issues, "message", "meta_template", "VALUE_REQUIRED", _("Meta template is required."))
		return

	row = frappe.db.get_value(MIRROR_DOCTYPE, meta_template, list(_TARGET_FIELDS), as_dict=True)
	if not row:
		_issue(
			issues, "message", "meta_template", "TEMPLATE_NOT_COMPATIBLE",
			_("Meta template was not found. Run a sync from the Meta Templates tab."),
		)
		return

	if cint(row.missing_on_meta):
		_issue(
			issues, "message", "meta_template", "TEMPLATE_NOT_COMPATIBLE",
			_("Meta template {0} no longer exists on Meta.").format(row.template_name),
		)
		return

	if cstr(row.status) != SENDABLE_STATUS:
		_issue(
			issues, "message", "meta_template", "TEMPLATE_NOT_COMPATIBLE",
			_("Meta template {0} is in status {1} and can only be sent once Meta reports it as {2}.").format(
				row.template_name, cstr(row.status) or _("(none)"), SENDABLE_STATUS
			),
		)
		return

	target = _target_from_row(row)
	try:
		declared = declared_parameter_count(target.components)
	except Exception as exc:
		_issue(issues, "message", "meta_template", "TEMPLATE_NOT_COMPATIBLE", cstr(exc))
		return

	mapped = len(stored_slot_map(meta_template))
	if mapped and mapped != declared:
		_issue(
			issues, "message", "meta_template", "TEMPLATE_NOT_COMPATIBLE",
			_("Meta template {0} has {1} variable(s) but its saved variable map has {2}.").format(
				row.template_name, declared, mapped
			),
		)
	elif declared and not mapped:
		_issue(
			issues, "message", "meta_template", "TEMPLATE_NOT_COMPATIBLE",
			_(
				"Meta template {0} has {1} variable(s) and no saved variable map, so a rule "
				"cannot fill them. Map its variables first."
			).format(row.template_name, declared),
		)


def _validate_template(rule, issues):
	if _uses_meta_template(rule):
		_validate_meta_template(rule, issues)
		return
	if not rule["template_key"]:
		_issue(issues, "message", "template_key", "VALUE_REQUIRED", _("Message template is required."))
		return
	try:
		template = _get_template(rule["template_key"])
	except frappe.DoesNotExistError:
		_issue(issues, "message", "template_key", "TEMPLATE_NOT_COMPATIBLE", _("Message template was not found."))
		return
	if not cint(template.enabled) or (template.source_doctype and template.source_doctype != rule["source_doctype"]):
		_issue(issues, "message", "template_key", "TEMPLATE_NOT_COMPATIBLE", _("Message template is disabled or incompatible with the source table."))
	for message in template_content_errors(template, rule["source_doctype"]):
		_issue(issues, "message", "template_key", "TEMPLATE_NOT_COMPATIBLE", message)


def _validate_response(rule, issues):
	response_type = rule["response_type"]
	config = rule.get("response_config") or {}
	options = config.get("options") or []
	if response_type not in INTERACTION_TYPES:
		_issue(issues, "reply", "response_type", "VALUE_NOT_ALLOWED", _("Interaction type is not allowed."))
		return
	if not isinstance(options, list):
		_issue(issues, "reply", "response_config.options", "EXECUTOR_CONFIG_INVALID", _("Response options must be a list."))
		return
	limit = 0 if response_type == "None" else MAX_BUTTONS if response_type == "Buttons" else MAX_LIST_OPTIONS
	if len(options) > limit:
		_issue(issues, "reply", "response_config.options", "INTERACTION_LIMIT_EXCEEDED", _("This interaction supports at most {0} options.").format(limit))
	if response_type != "None" and not options:
		_issue(issues, "reply", "response_config.options", "VALUE_REQUIRED", _("A reply interaction requires response options."))
	if response_type == "None" and options:
		_issue(issues, "reply", "response_config.options", "INTERACTION_LIMIT_EXCEEDED", _("No-reply messages cannot contain response options."))

	keys = set()
	aliases = {}
	for index, option in enumerate(options):
		path = f"response_config.options.{index}"
		if not isinstance(option, dict):
			_issue(issues, "reply", path, "EXECUTOR_CONFIG_INVALID", _("Every response option must be an object."))
			continue
		key = cstr(option.get("key")).strip()
		label = cstr(option.get("label")).strip()
		if not key or not _SAFE_RESPONSE_KEY.fullmatch(key):
			_issue(issues, "reply", f"{path}.key", "VALUE_NOT_ALLOWED", _("Response key must use 1-64 safe identifier characters."))
		if key in keys:
			_issue(issues, "reply", f"{path}.key", "DUPLICATE_RESPONSE_KEY", _("Response keys must be unique."))
		keys.add(key)
		max_label = 20 if response_type == "Buttons" else 24
		if not label or len(label) > max_label:
			_issue(issues, "reply", f"{path}.label", "VALUE_OUT_OF_RANGE", _("Response label must contain 1-{0} characters.").format(max_label))
		if option.get("value") in (None, ""):
			_issue(issues, "reply", f"{path}.value", "VALUE_REQUIRED", _("Response value is required."))
		option_aliases = option.get("aliases") or []
		if not isinstance(option_aliases, list) or any(not isinstance(alias, (str, int, float)) for alias in option_aliases):
			_issue(issues, "reply", f"{path}.aliases", "EXECUTOR_CONFIG_INVALID", _("Aliases must be a list of text or numeric values."))
			continue
		for alias in [key, label, *option_aliases]:
			normalized_alias = _normalize_alias(alias)
			if normalized_alias and normalized_alias in aliases and aliases[normalized_alias] != key:
				_issue(issues, "reply", f"{path}.aliases", "AMBIGUOUS_RESPONSE_ALIAS", _("Alias {0} matches more than one response and will require review.").format(alias), severity="warning")
			aliases[normalized_alias] = key


def _validate_executor(rule, source_config, issues):
	executor = rule["executor"]
	config = rule.get("executor_config") or {}
	options = rule.get("response_config", {}).get("options") or []
	if executor not in source_config["executors"]:
		_issue(issues, "then", "executor", "EXECUTOR_NOT_ALLOWED", _("Executor is not enabled for this source table."))
		return
	if rule["response_type"] == "None":
		_issue(issues, "reply", "response_type", "EXECUTOR_CONFIG_INVALID", _("Configured actions require a reply interaction."))

	if executor == "Create Rating":
		scale = cint(config.get("rating_scale"))
		if scale < 1 or scale > 5:
			_issue(issues, "then", "executor_config.rating_scale", "VALUE_OUT_OF_RANGE", _("Rating scale must be between 1 and 5."))
		for option in options:
			value = cint(option.get("value"))
			if cstr(option.get("value")).strip() == "" or value < 1 or value > scale:
				_issue(issues, "then", "response_config.options.value", "VALUE_OUT_OF_RANGE", _("Every rating response must be within the configured rating scale."))
		questionnaire = cstr(config.get("questionnaire")).strip()
		if questionnaire:
			row = frappe.db.get_value("Rating Questionnaire", questionnaire, ["active", "applies_to_doctype"], as_dict=True)
			if not row or not cint(row.active) or (row.applies_to_doctype and row.applies_to_doctype != rule["source_doctype"]):
				_issue(issues, "then", "executor_config.questionnaire", "EXECUTOR_CONFIG_INVALID", _("Questionnaire is inactive or incompatible with the source table."))
		if cint(config.get("request_comment")) and not cstr(config.get("comment_prompt")).strip():
			_issue(issues, "then", "executor_config.comment_prompt", "VALUE_REQUIRED", _("Comment prompt is required when comment collection is enabled."))
	elif executor == "Update Allowed Field":
		fieldname = cstr(config.get("field")).strip()
		registration = source_config["fields"].get(fieldname)
		if not registration or not registration["update_values"]:
			_issue(issues, "then", "executor_config.field", "FIELD_NOT_ALLOWED", _("Updated field is not allowlisted."))
			return
		values = config.get("values")
		if not isinstance(values, dict):
			_issue(issues, "then", "executor_config.values", "EXECUTOR_CONFIG_INVALID", _("Reply-to-value mapping is required."))
			return
		for option in options:
			key = cstr(option.get("key"))
			if key not in values or values[key] not in registration["update_values"]:
				_issue(issues, "then", f"executor_config.values.{key}", "VALUE_NOT_ALLOWED", _("Every response must map to an allowlisted field value."))
	elif executor == "Create Staff Task":
		allocated_to = cstr(config.get("allocated_to")).strip()
		if not allocated_to or not frappe.db.exists("User", {"name": allocated_to, "enabled": 1}):
			_issue(issues, "then", "executor_config.allocated_to", "EXECUTOR_CONFIG_INVALID", _("Assigned user must be active."))
		if not cstr(config.get("description")).strip():
			_issue(issues, "then", "executor_config.description", "VALUE_REQUIRED", _("Task description is required."))
	elif config:
		_issue(issues, "then", "executor_config", "EXECUTOR_CONFIG_INVALID", _("This executor does not accept configuration values."))


def _validation_result(candidate, issues, normalized):
	return {
		"valid": not any(issue["severity"] == "error" for issue in issues),
		"normalized_rule": normalized,
		"summary": summarize_rule(normalized or candidate),
		"issues": issues,
	}


def summarize_rule(rule):
	source = SOURCE_REGISTRY.get(cstr(rule.get("source_doctype")), {})
	source_label = source.get("label") or cstr(rule.get("source_doctype") or "source record")
	event = cstr(rule.get("trigger_event") or "an event occurs").lower()
	recipient = cstr(rule.get("recipient_type") or "recipient")
	if _uses_meta_template(rule):
		meta_template = cstr(rule.get("meta_template")).strip()
		template = (
			cstr(frappe.db.get_value("Pet App WhatsApp Meta Template", meta_template, "template_name"))
			or meta_template
			or "a Meta template"
		)
	else:
		template = cstr(rule.get("template_key") or "a message")
	executor = cstr(rule.get("executor") or "the configured action")
	return _("When {0} reaches {1}, send {2} to its {3} and run {4} after a valid reply.").format(
		source_label, event, template, recipient, executor
	)


def _source_config(source_doctype):
	config = SOURCE_REGISTRY.get(cstr(source_doctype).strip())
	if not config:
		raise DesignerError(_("Source table is not allowlisted."), "SOURCE_NOT_ALLOWED", {"source_doctype": source_doctype})
	return config


def _source_summary(name, config):
	return {
		"value": name,
		"label": _(config["label"]),
		"description": _(config["description"]),
		"trigger_events": list(config["trigger_events"]),
	}


def _executor_schema(name):
	return {"value": name, **deepcopy(EXECUTOR_REGISTRY[name])}


def _link_metadata(link_doctype):
	meta = frappe.get_meta(link_doctype)
	search_fields = ["name"]
	search_fields.extend(field.strip() for field in cstr(meta.search_fields).split(",") if field.strip())
	return {
		"link_doctype": link_doctype,
		"link_label_field": meta.title_field or "name",
		"link_search_fields": list(dict.fromkeys(search_fields)),
	}


def _get_template(template_key):
	name = frappe.db.get_value("Pet App WhatsApp Template", {"template_key": template_key}, "name") or template_key
	return frappe.get_doc("Pet App WhatsApp Template", name)


def _recipient_display_name(recipient_type, name):
	if not name:
		return None
	field = {"Guardian": "full_name", "Customer": "customer_name", "User": "full_name"}.get(recipient_type)
	return frappe.db.get_value(recipient_type, name, field) if field else name


def _display_phone(phone):
	phone = cstr(phone)
	if not phone:
		return ""
	masked = mask_phone(phone)
	return f"+{masked}" if not masked.startswith("+") else masked


def _executor_preview(rule, source_name):
	executor = rule["executor"]
	if executor == "Create Rating":
		return _("Would create one Rating linked to {0} after a valid reply.").format(source_name)
	if executor == "Update Allowed Field":
		return _("Would request approval, then update the allowlisted {0} field.").format(rule["executor_config"].get("field"))
	if executor == "Create Staff Task":
		return _("Would create a staff task assigned to {0}.").format(rule["executor_config"].get("allocated_to"))
	if executor == "Require Staff Review":
		return _("Would hold the response for explicit staff review.")
	return _("Would record an audited {0} result without changing sensitive business state.").format(executor.lower())


def _issue(issues, section, field, code, message, severity="error"):
	issues.append({"section": section, "field": field, "code": code, "message": cstr(message), "severity": severity})


def _designer_section(section):
	return {"condition": "if", "response_config": "reply", "executor_config": "then"}.get(section, section)


def _condition_label(fieldname, operator, value):
	return f"{fieldname.replace('_', ' ').title()} {operator.replace('_', ' ')} {cstr(value)}".strip()


def _equal(left, right):
	if left == right:
		return True
	return cstr(left) == cstr(right)


def _normalize_alias(value):
	return " ".join(cstr(value).casefold().split())


def _valid_field_value(fieldtype, value):
	try:
		if fieldtype == "Check":
			return value in (0, 1, True, False, "0", "1")
		if fieldtype == "Int":
			return cstr(int(value)) == cstr(value).strip()
		if fieldtype in {"Float", "Currency", "Percent", "Duration"}:
			float(value)
			return True
		if fieldtype == "Date":
			getdate(value)
			return True
		if fieldtype == "Datetime":
			get_datetime(value)
			return True
		if fieldtype == "Time":
			get_time(value)
			return True
		if fieldtype in {"Link", "Data", "Select"}:
			return isinstance(value, str) and bool(value.strip())
		return isinstance(value, (str, int, float, bool))
	except (TypeError, ValueError, OverflowError):
		return False
