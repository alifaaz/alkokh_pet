from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


MODULE = "Pet App"


def execute():
	ensure_p2_schema()


def ensure_p2_schema():
	for doctype_name, spec in DOCTYPES.items():
		ensure_doctype(doctype_name, spec)
	repair_existing_schema()
	create_custom_fields(CUSTOM_FIELDS, update=True)
	frappe.clear_cache()


def ensure_doctype(doctype_name: str, spec: dict):
	if frappe.db.exists("DocType", doctype_name):
		return

	fields = []
	for field in spec["fields"]:
		fields.append(field_doc(field))

	doc = frappe.get_doc(
		{
			"doctype": "DocType",
			"name": doctype_name,
			"module": MODULE,
			"custom": 1,
			"autoname": spec.get("autoname", "naming_series:"),
			"title_field": spec.get("title_field"),
			"track_changes": spec.get("track_changes", 1),
			"allow_rename": spec.get("allow_rename", 1),
			"fields": fields,
			"permissions": permissions(spec.get("roles")),
			"sort_field": spec.get("sort_field", "modified"),
			"sort_order": spec.get("sort_order", "DESC"),
		}
	)
	doc.insert(ignore_permissions=True)


def repair_existing_schema():
	if frappe.db.exists("DocType", "Pet Death Record"):
		field = frappe.db.get_value("DocField", {"parent": "Pet Death Record", "fieldname": "source_doctype"}, "name")
		if field:
			frappe.db.set_value("DocField", field, {"fieldtype": "Link", "options": "DocType", "default": None})


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
	):
		if key in field:
			data[key] = field[key]
	return data


def permissions(roles=None):
	roles = roles or ("System Manager",)
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


def naming_series(prefix: str) -> dict:
	return {
		"fieldname": "naming_series",
		"fieldtype": "Select",
		"label": "Naming Series",
		"options": f"{prefix}-.YYYY.-.#####",
		"default": f"{prefix}-.YYYY.-.#####",
		"reqd": 1,
		"read_only": 1,
	}


def link(fieldname: str, options: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Link", "options": options, **kwargs}


def dyn_link(fieldname: str, options_field: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Dynamic Link", "options": options_field, **kwargs}


def select(fieldname: str, options: list[str] | tuple[str, ...], **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Select", "options": "\n".join(options), **kwargs}


def check(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Check", **kwargs}


def dt(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Datetime", **kwargs}


def date(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Date", **kwargs}


def txt(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Small Text", **kwargs}


def code(fieldname: str, **kwargs) -> dict:
	return {"fieldname": fieldname, "fieldtype": "Code", "options": "JSON", **kwargs}


REMINDER_TYPES = (
	"Vaccination Due",
	"Deworming Due",
	"Follow-up Due",
	"Medication Refill",
	"Boarding Checkout",
	"Appointment Confirmation",
	"Lab Result Released",
	"Invoice Due",
	"Food Reorder",
)


DEATH_CATEGORIES = (
	"Natural Death",
	"Disease / Illness",
	"Emergency / Critical Case",
	"Accident / Trauma",
	"Procedure Complication",
	"Anesthesia Complication",
	"Boarding Incident",
	"Euthanasia",
	"Unknown / Found Dead",
	"External / Reported By Guardian",
	"Other",
)


SOURCE_DOCTYPES = (
	"Vet Visit",
	"Pet Procedure",
	"Pet Boarding",
	"PetCareService",
	"Appointment",
	"Lab",
	"Imaging",
)


DOCTYPES = {
	"Pet Reminder": {
		"autoname": "PET-REM-.YYYY.-.#####",
		"title_field": "reminder_type",
		"roles": ("System Manager", "Healthcare Administrator", "Healthcare Practitioner"),
		"fields": [
			select("reminder_type", REMINDER_TYPES, reqd=1, in_list_view=1, in_standard_filter=1),
			link("pet", "Pet", in_list_view=1, in_standard_filter=1),
			link("guardian", "Guardian", in_standard_filter=1),
			date("due_date", in_list_view=1, in_standard_filter=1),
			select("status", ("Pending", "Queued", "Sent", "Skipped", "Cancelled", "Failed"), default="Pending", in_list_view=1),
			select("channel", ("In App", "SMS", "Email", "WhatsApp"), default="In App"),
			link("reference_doctype", "DocType"),
			dyn_link("reference_name", "reference_doctype"),
			{"fieldname": "idempotency_key", "fieldtype": "Data", "unique": 1},
			txt("note"),
		],
	},
	"Pet Notification Template": {
		"autoname": "field:template_name",
		"title_field": "template_name",
		"roles": ("System Manager", "Healthcare Administrator"),
		"fields": [
			{"fieldname": "template_name", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
			select("reminder_type", REMINDER_TYPES, in_standard_filter=1),
			select("channel", ("In App", "SMS", "Email", "WhatsApp"), default="In App"),
			{"fieldname": "subject", "fieldtype": "Data"},
			{"fieldname": "body", "fieldtype": "Text Editor", "reqd": 1},
			check("active", default=1, in_list_view=1),
		],
	},
	"Pet Notification Log": {
		"autoname": "PET-NLOG-.YYYY.-.#####",
		"title_field": "subject",
		"fields": [
			link("reminder", "Pet Reminder"),
			link("template", "Pet Notification Template"),
			link("guardian", "Guardian", in_standard_filter=1),
			link("pet", "Pet", in_standard_filter=1),
			select("channel", ("In App", "SMS", "Email", "WhatsApp"), default="In App"),
			{"fieldname": "recipient", "fieldtype": "Data", "in_list_view": 1},
			{"fieldname": "subject", "fieldtype": "Data", "in_list_view": 1},
			{"fieldname": "message", "fieldtype": "Text"},
			select("status", ("Queued", "Sent", "Failed", "Skipped"), default="Queued", in_list_view=1),
			dt("sent_at"),
			link("reference_doctype", "DocType"),
			dyn_link("reference_name", "reference_doctype"),
			{"fieldname": "idempotency_key", "fieldtype": "Data", "unique": 1},
			txt("error"),
		],
	},
	"Pet Membership Plan": {
		"autoname": "field:plan_name",
		"title_field": "plan_name",
		"fields": [
			{"fieldname": "plan_name", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
			{"fieldname": "monthly_fee", "fieldtype": "Currency"},
			{"fieldname": "annual_fee", "fieldtype": "Currency"},
			{"fieldname": "discount_percent", "fieldtype": "Float"},
			{"fieldname": "points_multiplier", "fieldtype": "Float", "default": 1},
			check("active", default=1, in_list_view=1),
			txt("description"),
		],
	},
	"Pet Membership Subscription": {
		"autoname": "PET-MSUB-.YYYY.-.#####",
		"title_field": "guardian",
		"fields": [
			link("guardian", "Guardian", reqd=1, in_list_view=1, in_standard_filter=1),
			link("plan", "Pet Membership Plan", reqd=1, in_list_view=1),
			select("status", ("Active", "Paused", "Cancelled", "Expired"), default="Active", in_list_view=1),
			date("start_date", reqd=1),
			date("end_date"),
			link("sales_invoice", "Sales Invoice"),
			txt("note"),
		],
	},
	"Pet Loyalty Ledger": {
		"autoname": "PET-LOY-.YYYY.-.#####",
		"title_field": "guardian",
		"fields": [
			link("guardian", "Guardian", reqd=1, in_list_view=1, in_standard_filter=1),
			select("entry_type", ("Earn", "Redeem", "Expire", "Adjust"), reqd=1, in_list_view=1),
			{"fieldname": "points", "fieldtype": "Float", "reqd": 1, "in_list_view": 1},
			link("reference_doctype", "DocType"),
			dyn_link("reference_name", "reference_doctype"),
			dt("posting_datetime", in_list_view=1),
			txt("note"),
		],
	},
	"Pet Loyalty Rule": {
		"autoname": "field:rule_name",
		"title_field": "rule_name",
		"fields": [
			{"fieldname": "rule_name", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
			select("rule_type", ("Earn Per Amount", "Redeem Value", "Membership Bonus"), reqd=1),
			{"fieldname": "points_per_amount", "fieldtype": "Float"},
			{"fieldname": "amount_per_point", "fieldtype": "Currency"},
			{"fieldname": "min_invoice_amount", "fieldtype": "Currency"},
			check("active", default=1, in_list_view=1),
		],
	},
	"Practitioner Availability": {
		"autoname": "HCP-AVAIL-.YYYY.-.#####",
		"title_field": "practitioner",
		"fields": [
			link("practitioner", "Healthcare Practitioner", reqd=1, in_list_view=1, in_standard_filter=1),
			select("day_of_week", ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")),
			date("date"),
			{"fieldname": "start_time", "fieldtype": "Time", "reqd": 1},
			{"fieldname": "end_time", "fieldtype": "Time", "reqd": 1},
			{"fieldname": "slot_minutes", "fieldtype": "Int", "default": 30},
			check("active", default=1, in_list_view=1),
		],
	},
	"Clinic Working Hours": {
		"autoname": "CLINIC-HOURS-.#####",
		"title_field": "day_of_week",
		"fields": [
			select("day_of_week", ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"), in_list_view=1),
			date("date"),
			{"fieldname": "branch", "fieldtype": "Data", "in_standard_filter": 1},
			{"fieldname": "start_time", "fieldtype": "Time", "reqd": 1},
			{"fieldname": "end_time", "fieldtype": "Time", "reqd": 1},
			check("active", default=1),
		],
	},
	"Room Availability": {
		"autoname": "ROOM-AVAIL-.YYYY.-.#####",
		"title_field": "room",
		"fields": [
			link("room", "Service Room", reqd=1, in_list_view=1, in_standard_filter=1),
			select("day_of_week", ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")),
			date("date"),
			{"fieldname": "start_time", "fieldtype": "Time", "reqd": 1},
			{"fieldname": "end_time", "fieldtype": "Time", "reqd": 1},
			check("active", default=1),
		],
	},
	"Service Duration Rules": {
		"autoname": "DURATION-.#####",
		"title_field": "service_type",
		"fields": [
			select("service_type", ("Visit", "Care Service", "Procedure", "Boarding"), reqd=1, in_list_view=1),
			link("care_service", "CareService template"),
			link("procedure_template", "Procedure Template"),
			{"fieldname": "duration_minutes", "fieldtype": "Int", "default": 30, "reqd": 1},
			{"fieldname": "buffer_minutes", "fieldtype": "Int", "default": 0},
			check("active", default=1),
		],
	},
	"Clinical Alert Rule": {
		"autoname": "field:rule_name",
		"title_field": "rule_name",
		"fields": [
			{"fieldname": "rule_name", "fieldtype": "Data", "reqd": 1, "unique": 1},
			select("alert_type", ("Abnormal Vital", "Allergy", "Duplicate Medication", "Missing Weight", "Vaccination Overdue"), reqd=1, in_list_view=1),
			{"fieldname": "species", "fieldtype": "Data"},
			{"fieldname": "fieldname", "fieldtype": "Data"},
			select("operator", ("<", "<=", "=", ">=", ">"), default=">"),
			{"fieldname": "threshold", "fieldtype": "Float"},
			select("severity", ("Info", "Warning", "Critical"), default="Warning"),
			txt("message"),
			check("active", default=1),
		],
	},
	"Clinical Alert Log": {
		"autoname": "CALERT-.YYYY.-.#####",
		"title_field": "message",
		"fields": [
			link("visit", "Vet Visit", in_standard_filter=1),
			link("pet", "Pet", in_standard_filter=1),
			link("alert_rule", "Clinical Alert Rule"),
			select("alert_type", ("Abnormal Vital", "Allergy", "Duplicate Medication", "Missing Weight", "Vaccination Overdue"), in_list_view=1),
			select("severity", ("Info", "Warning", "Critical"), default="Warning"),
			txt("message", in_list_view=1),
			select("status", ("Open", "Acknowledged", "Dismissed"), default="Open", in_list_view=1),
		],
	},
	"Medication Dose Rule": {
		"autoname": "DOSE-RULE-.#####",
		"title_field": "medication",
		"fields": [
			link("medication", "Medication", reqd=1, in_list_view=1),
			{"fieldname": "species", "fieldtype": "Data", "in_standard_filter": 1},
			{"fieldname": "min_weight", "fieldtype": "Float"},
			{"fieldname": "max_weight", "fieldtype": "Float"},
			{"fieldname": "dose_per_kg", "fieldtype": "Float"},
			{"fieldname": "max_dose", "fieldtype": "Float"},
			{"fieldname": "unit", "fieldtype": "Data"},
			check("active", default=1),
		],
	},
	"Species Vital Range": {
		"autoname": "VITAL-RANGE-.#####",
		"title_field": "species",
		"fields": [
			{"fieldname": "species", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
			select("vital_field", ("temperature", "heart_rate", "respiratory_rate", "weight", "spo2"), reqd=1, in_list_view=1),
			{"fieldname": "min_value", "fieldtype": "Float"},
			{"fieldname": "max_value", "fieldtype": "Float"},
			select("severity", ("Info", "Warning", "Critical"), default="Warning"),
			check("active", default=1),
		],
	},
	"Inventory Alert Rule": {
		"autoname": "INV-RULE-.#####",
		"title_field": "item_code",
		"fields": [
			link("item_code", "Item", in_list_view=1),
			link("item_group", "Item Group"),
			link("warehouse", "Warehouse", in_standard_filter=1),
			select("alert_type", ("Low Stock", "Expiry"), reqd=1, in_list_view=1),
			{"fieldname": "min_qty", "fieldtype": "Float"},
			{"fieldname": "expiry_days", "fieldtype": "Int", "default": 30},
			check("active", default=1),
		],
	},
	"Inventory Alert Log": {
		"autoname": "INV-ALERT-.YYYY.-.#####",
		"title_field": "message",
		"fields": [
			link("rule", "Inventory Alert Rule"),
			link("item_code", "Item", in_list_view=1),
			link("warehouse", "Warehouse"),
			select("alert_type", ("Low Stock", "Expiry"), in_list_view=1),
			{"fieldname": "current_qty", "fieldtype": "Float"},
			date("expiry_date"),
			select("status", ("Open", "Acknowledged", "Closed"), default="Open", in_list_view=1),
			txt("message"),
		],
	},
	"Pet App Purchase Suggestion": {
		"autoname": "PUR-SUG-.YYYY.-.#####",
		"title_field": "item_code",
		"fields": [
			link("item_code", "Item", reqd=1, in_list_view=1),
			link("warehouse", "Warehouse", in_list_view=1),
			{"fieldname": "suggested_qty", "fieldtype": "Float"},
			select("status", ("Draft", "Reviewed", "Ordered", "Cancelled"), default="Draft"),
			txt("reason"),
		],
	},
	"Pet Boarding Daily Log": {
		"autoname": "BDLOG-.YYYY.-.#####",
		"title_field": "boarding",
		"fields": [link("boarding", "Pet Boarding", reqd=1, in_list_view=1), link("pet", "Pet"), dt("log_datetime"), txt("summary", in_list_view=1), txt("internal_note")],
	},
	"Pet Boarding Feeding Schedule": {
		"autoname": "BFEED-.YYYY.-.#####",
		"title_field": "boarding",
		"fields": [link("boarding", "Pet Boarding", reqd=1), link("pet", "Pet"), {"fieldname": "feed_time", "fieldtype": "Time"}, txt("food"), txt("instructions"), check("guardian_visible", default=1)],
	},
	"Pet Boarding Medication Schedule": {
		"autoname": "BMED-.YYYY.-.#####",
		"title_field": "boarding",
		"fields": [link("boarding", "Pet Boarding", reqd=1), link("pet", "Pet"), link("medication", "Medication"), {"fieldname": "dose_time", "fieldtype": "Time"}, txt("dosage"), txt("instructions"), check("guardian_visible", default=1)],
	},
	"Pet Boarding Incident Report": {
		"autoname": "BINC-.YYYY.-.#####",
		"title_field": "incident_title",
		"fields": [link("boarding", "Pet Boarding", reqd=1), link("pet", "Pet"), {"fieldname": "incident_title", "fieldtype": "Data", "reqd": 1, "in_list_view": 1}, dt("incident_datetime"), txt("guardian_visible_summary"), txt("internal_note"), select("status", ("Draft", "Reported", "Reviewed", "Closed"), default="Draft")],
	},
	"Pet Boarding Media Update": {
		"autoname": "BMEDIA-.YYYY.-.#####",
		"title_field": "caption",
		"fields": [link("boarding", "Pet Boarding", reqd=1), link("pet", "Pet"), {"fieldname": "file", "fieldtype": "Attach"}, {"fieldname": "caption", "fieldtype": "Data", "in_list_view": 1}, check("guardian_visible", default=1), dt("posted_at")],
	},
	"Delivery Assignment": {
		"autoname": "DEL-ASG-.YYYY.-.#####",
		"title_field": "driver",
		"fields": [link("driver", "Driver", reqd=1, in_list_view=1), link("sales_order", "Sales Order"), link("sales_invoice", "Sales Invoice"), link("guardian", "Guardian"), link("customer", "Customer"), select("status", ("Assigned", "Picked Up", "Delivered", "Failed", "Cancelled"), default="Assigned", in_list_view=1), txt("delivery_address"), {"fieldname": "cash_to_collect", "fieldtype": "Currency"}, dt("assigned_at"), dt("delivered_at"), txt("failure_reason")],
	},
	"Delivery Proof": {
		"autoname": "DEL-PROOF-.YYYY.-.#####",
		"title_field": "assignment",
		"fields": [link("assignment", "Delivery Assignment", reqd=1, in_list_view=1), select("proof_type", ("Photo", "Signature", "OTP", "Note"), default="Photo"), {"fieldname": "file", "fieldtype": "Attach"}, {"fieldname": "received_by", "fieldtype": "Data"}, dt("captured_at"), txt("note")],
	},
	"Driver Shift": {
		"autoname": "DRV-SHIFT-.YYYY.-.#####",
		"title_field": "driver",
		"fields": [link("driver", "Driver", reqd=1, in_list_view=1), dt("started_at", reqd=1), dt("ended_at"), select("status", ("Open", "Closed", "Cancelled"), default="Open", in_list_view=1), txt("note")],
	},
	"Driver Cash Handover": {
		"autoname": "DRV-CASH-.YYYY.-.#####",
		"title_field": "driver",
		"fields": [link("driver", "Driver", reqd=1, in_list_view=1), link("shift", "Driver Shift"), {"fieldname": "amount", "fieldtype": "Currency", "reqd": 1}, select("status", ("Draft", "Submitted", "Cancelled"), default="Draft"), link("payment_entry", "Payment Entry"), txt("note")],
	},
	"Pet App Audit Log": {
		"autoname": "AUDIT-.YYYY.-.#####",
		"title_field": "event_type",
		"fields": [{"fieldname": "event_type", "fieldtype": "Data", "reqd": 1, "in_list_view": 1}, link("reference_doctype", "DocType"), dyn_link("reference_name", "reference_doctype"), link("user", "User", in_standard_filter=1), code("before_json"), code("after_json"), code("details_json"), dt("created_at", in_list_view=1)],
	},
	"Pet App Import Job": {
		"autoname": "IMPORT-.YYYY.-.#####",
		"title_field": "import_type",
		"fields": [select("import_type", ("Guardians", "Pets", "PetGuardian links", "Medical History", "Products", "Medications", "Services", "Opening Stock", "Outstanding Balances"), reqd=1, in_list_view=1), check("dry_run", default=1), select("status", ("Draft", "Validated", "Committed", "Failed"), default="Draft", in_list_view=1), {"fieldname": "source_file", "fieldtype": "Attach"}, code("payload_json"), code("summary_json")],
	},
	"Pet App Import Error": {
		"autoname": "IMPERR-.YYYY.-.#####",
		"title_field": "message",
		"fields": [link("import_job", "Pet App Import Job", reqd=1, in_list_view=1), {"fieldname": "row_no", "fieldtype": "Int"}, {"fieldname": "fieldname", "fieldtype": "Data"}, txt("message", reqd=1, in_list_view=1), code("raw_json")],
	},
	"Pet App Offline Request": {
		"autoname": "OFFREQ-.YYYY.-.#####",
		"title_field": "idempotency_key",
		"fields": [{"fieldname": "idempotency_key", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1}, {"fieldname": "client_request_id", "fieldtype": "Data", "in_list_view": 1}, link("user", "User"), {"fieldname": "endpoint", "fieldtype": "Data"}, {"fieldname": "request_hash", "fieldtype": "Data"}, select("status", ("Processing", "Completed", "Failed", "Conflict"), default="Processing", in_list_view=1), code("response_json"), dt("last_synced_at")],
	},
	"Pet Death Reason": {
		"autoname": "field:reason_name",
		"title_field": "reason_name",
		"roles": ("System Manager", "Healthcare Administrator", "Healthcare Practitioner"),
		"fields": [
			{"fieldname": "reason_name", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
			{"fieldname": "reason_code", "fieldtype": "Data", "unique": 1},
			select("category", DEATH_CATEGORIES, reqd=1, in_list_view=1, in_standard_filter=1),
			{"fieldname": "species", "fieldtype": "Data", "in_standard_filter": 1},
			txt("description"),
			check("active", default=1, in_list_view=1),
			check("requires_doctor_confirmation", label="Requires Practitioner Confirmation"),
			check("requires_manager_review"),
			check("requires_incident_report"),
			{"fieldname": "guardian_visible_label", "fieldtype": "Data"},
			{"fieldname": "sort_order", "fieldtype": "Int"},
		],
	},
	"Pet Death Record": {
		"autoname": "PDR-.YYYY.-.#####",
		"title_field": "pet",
		"roles": ("System Manager", "Healthcare Administrator", "Healthcare Practitioner"),
		"fields": [
			link("pet", "Pet", reqd=1, in_list_view=1, in_standard_filter=1),
			link("guardian", "Guardian", in_standard_filter=1),
			link("customer", "Customer"),
			dt("death_datetime", reqd=1, in_list_view=1),
			dt("reported_datetime"),
			link("reported_by", "User"),
			link("confirmed_by", "User"),
			dt("confirmed_at"),
			link("death_reason", "Pet Death Reason", in_standard_filter=1),
			select("death_reason_category", DEATH_CATEGORIES, in_list_view=1),
			txt("cause_of_death_text"),
			txt("clinical_summary"),
			txt("guardian_visible_summary"),
			txt("internal_note"),
			link("source_doctype", "DocType"),
			dyn_link("source_name", "source_doctype"),
			{"fieldname": "source_title", "fieldtype": "Data"},
			select("death_location", ("Clinic", "Boarding", "Home", "External Facility", "Unknown", "Other")),
			txt("death_location_detail"),
			check("was_under_clinic_care"),
			check("was_unexpected"),
			check("requires_manager_review"),
			select("manager_review_status", ("Not Required", "Pending", "Approved", "Rejected"), default="Not Required"),
			link("manager_reviewed_by", "User"),
			dt("manager_reviewed_at"),
			txt("manager_review_note"),
			select("body_handling_option", ("Released To Guardian", "Clinic Disposal", "Burial Arrangement", "Cremation", "Transferred To External Facility", "Pending Guardian Decision", "Other")),
			{"fieldname": "body_released_to", "fieldtype": "Data"},
			dt("body_released_at"),
			txt("body_release_note"),
			check("necropsy_requested"),
			select("necropsy_status", ("Not Requested", "Pending", "Completed", "Declined"), default="Not Requested"),
			txt("necropsy_result"),
			check("certificate_issued"),
			{"fieldname": "certificate_no", "fieldtype": "Data"},
			dt("certificate_issued_at"),
			{"fieldname": "certificate_file", "fieldtype": "Attach"},
			select("status", ("Draft", "Reported", "Pending Confirmation", "Confirmed", "Finalized", "Cancelled"), default="Draft", in_list_view=1, in_standard_filter=1),
			check("amended"),
			txt("amendment_reason"),
			txt("cancelled_reason"),
		],
	},
}


COMMON_OFFLINE_FIELDS = [
	{"fieldname": "client_request_id", "fieldtype": "Data", "insert_after": "modified"},
	{"fieldname": "idempotency_key", "fieldtype": "Data", "insert_after": "client_request_id"},
	{"fieldname": "last_synced_at", "fieldtype": "Datetime", "insert_after": "idempotency_key"},
]


CUSTOM_FIELDS = {
	"Pet": [
		{"fieldname": "is_deceased", "fieldtype": "Check", "label": "Is Deceased", "insert_after": "pet_status", "default": 0},
		{"fieldname": "death_date", "fieldtype": "Date", "label": "Death Date", "insert_after": "is_deceased"},
		{"fieldname": "death_record", "fieldtype": "Link", "options": "Pet Death Record", "label": "Death Record", "insert_after": "death_date", "read_only": 1},
	],
	"Vet Visit": [
		{"fieldname": "outcome", "fieldtype": "Select", "label": "Outcome", "options": "\nRecovered\nImproved\nUnchanged\nDeath\nEuthanasia\nTransferred\nOther", "insert_after": "status"},
		{"fieldname": "death_during_visit", "fieldtype": "Check", "label": "Death During Visit", "insert_after": "outcome"},
		{"fieldname": "death_record", "fieldtype": "Link", "options": "Pet Death Record", "label": "Death Record", "insert_after": "death_during_visit", "read_only": 1},
		*COMMON_OFFLINE_FIELDS,
	],
	"Pet Procedure": [
		{"fieldname": "death_during_procedure", "fieldtype": "Check", "label": "Death During Procedure", "insert_after": "outcome"},
		{"fieldname": "death_record", "fieldtype": "Link", "options": "Pet Death Record", "label": "Death Record", "insert_after": "death_during_procedure", "read_only": 1},
		*COMMON_OFFLINE_FIELDS,
	],
	"Pet Boarding": [
		{"fieldname": "boarding_outcome", "fieldtype": "Select", "label": "Boarding Outcome", "options": "\nCompleted\nDeath\nTransferred\nCancelled\nOther", "insert_after": "record_status"},
		{"fieldname": "death_during_boarding", "fieldtype": "Check", "label": "Death During Boarding", "insert_after": "boarding_outcome"},
		{"fieldname": "death_record", "fieldtype": "Link", "options": "Pet Death Record", "label": "Death Record", "insert_after": "death_during_boarding", "read_only": 1},
		*COMMON_OFFLINE_FIELDS,
	],
	"Appointment": [
		{"fieldname": "custom_cancelled_due_to_death", "fieldtype": "Check", "label": "Cancelled Due To Death", "insert_after": "custom_converted_at", "default": 0, "read_only": 1},
		{"fieldname": "custom_death_record", "fieldtype": "Link", "options": "Pet Death Record", "label": "Death Record", "insert_after": "custom_cancelled_due_to_death", "read_only": 1},
		{"fieldname": "custom_doctor", "fieldtype": "Link", "options": "Healthcare Practitioner", "label": "Healthcare Practitioner", "insert_after": "custom_customer"},
		{"fieldname": "custom_room", "fieldtype": "Link", "options": "Service Room", "label": "Room", "insert_after": "custom_doctor"},
		{"fieldname": "custom_duration_minutes", "fieldtype": "Int", "label": "Duration Minutes", "insert_after": "custom_room", "default": 30},
		{"fieldname": "custom_client_request_id", "fieldtype": "Data", "label": "Client Request ID", "insert_after": "custom_duration_minutes"},
		{"fieldname": "custom_idempotency_key", "fieldtype": "Data", "label": "Idempotency Key", "insert_after": "custom_client_request_id"},
		{"fieldname": "custom_last_synced_at", "fieldtype": "Datetime", "label": "Last Synced At", "insert_after": "custom_idempotency_key"},
	],
	"Vet Case Sheet": COMMON_OFFLINE_FIELDS,
	"PetCareService": [
		{"fieldname": "death_record", "fieldtype": "Link", "options": "Pet Death Record", "label": "Death Record", "insert_after": "status", "read_only": 1},
		*COMMON_OFFLINE_FIELDS,
	],
}
