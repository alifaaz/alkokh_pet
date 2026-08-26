from __future__ import annotations

import datetime
import json

import frappe
from frappe.utils import cstr


SENSITIVE_KEYS = {"otp", "otp_code", "code", "password", "token", "api_secret", "access_token", "app_secret"}

VARIABLE_FIELDS = {
	"guardian": (
		"name", "display_name", "full_name", "phone", "email_id", "address_line1", "city", "country",
	),
	"pet": (
		"name", "display_name", "pet_name", "animal_species", "animal_type", "breed", "birth_date", "registration_date",
		"color", "gender", "weight", "status", "pet_status",
	),
	"invoice": (
		"name", "customer", "customer_name", "posting_date", "due_date", "currency", "total", "net_total",
		"grand_total", "rounded_total", "outstanding_amount", "status",
	),
	"death_record": (
		"name", "pet", "guardian", "death_datetime", "reported_datetime", "death_reason",
		"death_reason_category", "cause_of_death_text", "guardian_visible_summary", "death_location",
		"death_location_detail", "status", "certificate_number", "certificate_issued_at",
	),
	"pet_service": (
		"name", "pet_service_name", "pet_id", "category", "guardian_id", "care_service_id", "service_option",
		"item_code", "price", "status", "doctor", "visit", "start_date", "end_date", "due_date", "provider",
	),
	"appointment": (
		"name", "customer_name", "customer_phone_number", "customer_email", "scheduled_time", "status",
		"party", "appointment_with", "service", "description", "custom_appointment_type", "custom_pet",
		"custom_guardian", "custom_customer", "custom_doctor", "custom_room", "custom_duration_minutes",
	),
	"clinic": ("business_name", "phone_number", "display_phone_number", "branch"),
	# Pet Boarding. room_name and room_arabic_name are not Pet Boarding columns - they
	# are fetched from the linked Service Room, because service_room holds a docname
	# and Service Room is autonamed field:room_code, so the raw value is a code like
	# "A4". A code is not a room name and must never reach a customer's message.
	#
	# deposit and branch are deliberately absent. See _safe_values for what is fetched.
	"boarding": (
		"name", "pet_name", "guardian_name", "service_room", "room_name", "room_arabic_name",
		"check_in", "check_out", "expected_check_out", "status",
	),
}

SOURCE_NAMESPACES = {
	"Guardian": "guardian",
	"Pet": "pet",
	"Sales Invoice": "invoice",
	"Pet Death Record": "death_record",
	"PetCareService": "pet_service",
	"Appointment": "appointment",
	"Pet Boarding": "boarding",
}


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


def list_template_variables(source_doctype: str | None = None) -> list[dict]:
	"""Return the supported Jinja variables; never expose arbitrary document fields."""
	namespaces = set(VARIABLE_FIELDS)
	if source_doctype:
		namespace = SOURCE_NAMESPACES.get(source_doctype)
		namespaces = {"clinic", "guardian", "pet"}
		if namespace:
			namespaces.add(namespace)

	variables = []
	for namespace in VARIABLE_FIELDS:
		if namespace not in namespaces:
			continue
		for fieldname in VARIABLE_FIELDS[namespace]:
			variables.append(
				{
					"key": f"{namespace}.{fieldname}",
					"token": "{{ " + f"{namespace}.{fieldname}" + " }}",
					"namespace": namespace,
					"fieldname": fieldname,
				}
			)
	return variables


def build_document_context(source_doctype: str, source_name: str, extra: dict | None = None) -> dict:
	if not source_doctype or not source_name or not frappe.db.exists(source_doctype, source_name):
		frappe.throw("A valid source document is required to build WhatsApp context.")

	doc = frappe.get_doc(source_doctype, source_name)
	context = {}
	namespace = SOURCE_NAMESPACES.get(source_doctype)
	if namespace:
		context[namespace] = _safe_values(doc, namespace)

	guardian_name, pet_name = _related_guardian_and_pet(doc)
	if guardian_name and frappe.db.exists("Guardian", guardian_name):
		context["guardian"] = _safe_values(frappe.get_doc("Guardian", guardian_name), "guardian")
	if pet_name and frappe.db.exists("Pet", pet_name):
		context["pet"] = _safe_values(frappe.get_doc("Pet", pet_name), "pet")

	account_name = frappe.db.get_single_value("Pet App Notification Settings", "default_whatsapp_account")
	if account_name and frappe.db.exists("Pet App WhatsApp Account", account_name):
		context["clinic"] = _safe_values(frappe.get_doc("Pet App WhatsApp Account", account_name), "clinic")
	else:
		context["clinic"] = {key: "" for key in VARIABLE_FIELDS["clinic"]}
	context["clinic"]["business_name"] = context["clinic"].get("business_name") or "Pet App"

	_merge_extra_context(context, extra or {})
	return context


def _merge_extra_context(context: dict, extra: dict) -> None:
	for key, value in extra.items():
		if (
			key in VARIABLE_FIELDS
			and isinstance(context.get(key), dict)
			and isinstance(value, dict)
		):
			context[key].update(value)
		elif key in VARIABLE_FIELDS and isinstance(context.get(key), dict):
			# Keep canonical namespaces as objects. Older callers sometimes sent
			# flat values like {"guardian": "Sara"}; replacing the object makes
			# {{ guardian.display_name }} render blank.
			continue
		else:
			context[key] = value


# Fixed output, deliberately not the site's date_format. What a customer sees must not
# change because somebody edited System Settings; these three are the contract.
DATE_OUTPUT_FORMAT = "%d-%m-%Y"
DATETIME_OUTPUT_FORMAT = "%d-%m-%Y %H:%M"
TIME_OUTPUT_FORMAT = "%H:%M"


def format_variable_value(value):
	"""Render a temporal value for a message; leave everything else exactly as it is.

	This is the one place a date becomes text for the whole variable stack. Without it
	a Datetime reached a customer as ``2026-08-24 10:45:29.132858`` - the raw repr,
	microseconds and all - because _safe_values handed back the object and whatever
	stringified it downstream just called cstr().

	``datetime`` is tested before ``date`` because datetime *subclasses* date: the
	other order silently formats every timestamp as a bare day and drops the time.

	None and "" are returned untouched. A missing date must stay missing - turning it
	into 01-01-1970, or any other invented value, would defeat the unresolved-slot
	refusal that exists to stop a hole reaching a customer.

	Strings are returned untouched and are never parsed. A value that merely looks like
	a date is not necessarily one, and re-reading it would be guessing.
	"""
	if isinstance(value, datetime.datetime):
		return value.strftime(DATETIME_OUTPUT_FORMAT)
	if isinstance(value, datetime.date):
		return value.strftime(DATE_OUTPUT_FORMAT)
	if isinstance(value, datetime.time):
		return value.strftime(TIME_OUTPUT_FORMAT)
	if isinstance(value, datetime.timedelta):
		# MariaDB hands a TIME column back as a timedelta, not a time, so a Time field
		# would otherwise render as "1 day, 9:00:00". No allowlisted variable is a Time
		# field today; this is here so that adding one is not a silent regression.
		total_minutes = int(value.total_seconds()) // 60
		return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"
	return value


def _safe_values(doc, namespace: str) -> dict:
	# Formatted here, at the single point every allowlisted value enters the context:
	# the slot-map resolver, the explicit-parameter builder, the preview renderer and
	# the Jinja body_preview path all read this dict, and formatting at any one of them
	# would leave the others raw.
	values = {
		fieldname: format_variable_value(doc.get(fieldname)) for fieldname in VARIABLE_FIELDS[namespace]
	}
	# Friendly display name so templates that reach for `.name` (the record id,
	# e.g. GUARDIAN-00001 / PET-00001) still render a human-readable value.
	if namespace == "guardian":
		values["display_name"] = doc.get("full_name") or doc.name
	elif namespace == "pet":
		values["display_name"] = doc.get("pet_name") or doc.name
	elif namespace == "boarding":
		values.update(_room_values(doc.get("service_room")))
	return values


def _room_values(service_room) -> dict:
	"""Human-readable names for the linked Service Room.

	``service_room`` holds a docname, and Service Room is autonamed
	``field:room_code``, so that value is a code - "A4", "B1". Both names are fetched
	so a template can pick the one matching its language; boarding_checkinn is Arabic
	and wants arabic_name, an English template wants room_name.

	A room that cannot be resolved yields blanks rather than the code. Emitting the
	code would be emitting a docname into a customer's message, which is the thing
	this fetch exists to prevent - and a blank lets the slot's fallback (or its
	refusal) decide, which is a decision an operator made deliberately.
	"""
	if not service_room:
		return {"room_name": "", "room_arabic_name": ""}
	row = frappe.db.get_value("Service Room", service_room, ["room_name", "arabic_name"], as_dict=True)
	if not row:
		return {"room_name": "", "room_arabic_name": ""}
	return {"room_name": cstr(row.room_name), "room_arabic_name": cstr(row.arabic_name)}


def _related_guardian_and_pet(doc) -> tuple[str | None, str | None]:
	pet_name = doc.get("pet_id") or doc.get("pet") or doc.get("custom_pet")
	guardian_name = doc.get("guardian_id") or doc.get("guardian") or doc.get("custom_guardian")
	if doc.doctype == "Guardian":
		guardian_name = doc.name
	elif doc.doctype == "Pet":
		pet_name = doc.name

	if not guardian_name and doc.doctype == "Sales Invoice":
		guardian_name = frappe.db.get_value("Guardian", {"customer_id": doc.get("customer")}, "name")
	if not guardian_name and pet_name:
		guardian_name = frappe.db.get_value(
			"PetGuardian", {"pet_id": pet_name, "role": "primary_owner"}, "guardian_id"
		) or frappe.db.get_value("PetGuardian", {"pet_id": pet_name}, "guardian_id")
	return guardian_name, pet_name
