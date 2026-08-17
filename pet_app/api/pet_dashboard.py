"""Per-pet dashboard and doctor-suggestion insights.

Both entry points are read-only and return plain dicts (no envelope), because the
frontend unwraps `message`/`data` defensively and reads snake_case directly.

The hard part here is money. Billing in this system is guardian-scoped: a Sales
Invoice resolves to a Customer through Guardian.customer_id, and carries no pet
field at all. A guardian with nine pets has one customer ledger covering all of
them, so customer totals are emphatically not pet totals. The only pet-attributable
money lives on the invoice *line*, which is resolved two ways:

  1. line provenance - the [alkokh-source:...] / [alkokh-source-group:...] marker
     stamped into Sales Invoice Item.description, pointing at a source doc that
     carries a pet link;
  2. pet-scoped clinical billables - doctypes that already hold both a pet link
     and a sales_invoice back-link.

Neither covers everything on real data (markers sit on roughly half the lines, and
the back-links are sparse outside Vet Visit), so both run and `financial.basis`
reports which actually produced the figures. The caption the UI shows depends on it.

Permissions are the other load-bearing rule: `clinical` and `financial` are
independently permissioned, and a block the caller may not read is omitted rather
than zeroed. A fabricated 0 is indistinguishable from "never charged" to the
frontend, so it would render a permission boundary as a fact about the pet.
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict, defaultdict
from datetime import date, datetime

import frappe
from frappe import _
from frappe.utils import add_days, cint, cstr, flt, getdate, nowdate

from pet_app.api.permissions import get_user_roles, is_clinical_user, user_has_full_access

DEFAULT_CURRENCY = "IQD"

AVAILABILITY_OK = "ok"
AVAILABILITY_UNAVAILABLE = "unavailable"
AVAILABILITY_FORBIDDEN = "forbidden"

GUARDIAN_READ_ROLES = {"Guardian", "Guardians", "Pet"}

# Doctypes that carry both a pet link and (usually) a sales_invoice back-link.
# The pet field genuinely differs per doctype - Vet Case Sheet uses animal_patient,
# not `pet`, and Pet Boarding uses `pet`, not `pet_id`.
PET_LINK_FIELDS = {
	"Vet Visit": "animal_patient",
	"Vet Case Sheet": "animal_patient",
	"Pet Boarding": "pet",
	"PetCareService": "pet_id",
	"Lab": "pet",
	"Imaging": "pet",
	"Pet Procedure": "pet",
	"Pet Medical Profile": "pet",
	"Appointment": "custom_pet",
	"Pet Care Episode": "pet",
	"Pet Vaccination Record": "pet",
	"Pet Deworming Record": "pet",
}

# Billable doctypes carrying a sales_invoice link, mapped to the care category
# the frontend understands.
BILLABLE_SOURCES = {
	"Vet Visit": "consultation",
	"PetCareService": "grooming",
	"Lab": "lab",
	"Imaging": "imaging",
	"Pet Procedure": "procedure",
	"Pet Boarding": "boarding",
}

CATEGORY_LABELS = {
	"consultation": "Consultations",
	"procedure": "Procedures",
	"lab": "Laboratory",
	"imaging": "Imaging",
	"boarding": "Boarding",
	"grooming": "Care services",
	"medication": "Medication",
	"retail": "Retail",
	"other": "Other",
}

ACTIVE_EPISODE_STATUSES = {
	"Open",
	"Under Diagnosis",
	"Pending Diagnostics",
	"Under Treatment",
	"Monitoring",
	"Follow-up Scheduled",
	"Follow-up Due",
	"Referred",
}
RESOLVED_EPISODE_STATUSES = {"Resolved", "Closed"}

# Marker accepts both prefixes: line-level (alkokh-source) and grouped
# (alkokh-source-group). Both are live in production data.
_MARKER_RE = re.compile(r"\[alkokh-source(?:-group)?:([^:\]]+):([^\]]+)\]")


# ============================================================
# Access helpers
# ============================================================


def _guardian_for_user(user: str | None = None) -> str | None:
	user = user or frappe.session.user
	if not user or user in ("Guest",):
		return None
	return frappe.db.get_value("Guardian", {"user_id": user}, "name")


def _assert_pet_access(pet: str) -> str | None:
	"""Read access to the pet itself. Returns the guardian name when the caller is one."""
	if frappe.session.user == "Guest":
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not frappe.db.exists("Pet", pet):
		frappe.throw(_("Pet {0} was not found.").format(frappe.bold(pet)))

	if is_clinical_user("read"):
		return None

	guardian = _guardian_for_user()
	if guardian and frappe.db.exists("PetGuardian", {"guardian_id": guardian, "pet_id": pet}):
		return guardian

	if frappe.has_permission("Pet", doc=pet, ptype="read"):
		return None

	frappe.throw(_("Not permitted"), frappe.PermissionError)


def _can_read_clinical(pet: str, guardian: str | None) -> bool:
	"""Clinical block visibility. A guardian sees their own pet's clinical record."""
	if user_has_full_access() or is_clinical_user("read"):
		return True
	if guardian:
		return True
	return bool(frappe.has_permission("Vet Visit", ptype="read"))


def _can_read_financial(guardian: str | None) -> bool:
	"""Money block visibility - strictly Sales Invoice read permission.

	A guardian is deliberately not auto-granted here: the pet's invoices belong to
	the guardian's customer ledger, so this defers to the actual doctype permission.
	"""
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return False
	if user_has_full_access():
		return True
	if not frappe.has_permission("Sales Invoice", ptype="read"):
		return False
	if guardian and not (get_user_roles() - GUARDIAN_READ_ROLES):
		# Pure guardian account: only their own ledger, which is still their pet's.
		return True
	return True


# ============================================================
# Small shaping helpers
# ============================================================


def _kpi(key, label, value, helper=None, icon=None, color=None) -> dict:
	return {
		"key": key,
		"label": label,
		"value": value,
		"helper": helper,
		"icon": icon,
		"color": color,
	}


def _iso(value) -> str | None:
	"""Dates as YYYY-MM-DD strings; None stays None (the UI renders it as a dash)."""
	if not value:
		return None
	if isinstance(value, datetime):
		return value.date().isoformat()
	if isinstance(value, date):
		return value.isoformat()
	try:
		return getdate(value).isoformat()
	except Exception:
		return None


def _month_key(value) -> str | None:
	iso = _iso(value)
	return iso[:7] if iso else None


def _positive(value):
	"""A vital reading, or None. Never 0 - a 0 kg weight would plot as a real point."""
	number = flt(value)
	return number if number > 0 else None


def _month_range(from_date, to_date) -> list[str]:
	"""Every YYYY-MM between the bounds inclusive, so zero months are still charted."""
	if not from_date or not to_date:
		return []
	start, end = getdate(from_date), getdate(to_date)
	if start > end:
		return []
	months, year, month = [], start.year, start.month
	while (year, month) <= (end.year, end.month):
		months.append(f"{year:04d}-{month:02d}")
		month += 1
		if month > 12:
			month, year = 1, year + 1
	return months


def _series(counter: dict, months: list[str]) -> list[dict]:
	"""Dense series when a range is given, otherwise only the months with data."""
	keys = months or sorted(k for k in counter if k)
	return [{"period": key, "value": counter.get(key, 0)} for key in keys]


def _label_from_key(key: str) -> str:
	return _(CATEGORY_LABELS.get(key, key.replace("_", " ").title()))


def _split_lines(value) -> list[str]:
	"""Free-text clinical fields are newline/comma authored; render them as a list."""
	text = cstr(value or "").strip()
	if not text:
		return []
	text = re.sub(r"<[^>]+>", " ", text)
	parts = re.split(r"[\n\r;]+|,(?![^(]*\))", text)
	return [p.strip(" -•\t") for p in parts if p and p.strip(" -•\t")]


def _exists(doctype: str) -> bool:
	try:
		return bool(frappe.db.exists("DocType", doctype))
	except Exception:
		return False


def _table_has_column(doctype: str, fieldname: str) -> bool:
	try:
		return fieldname in frappe.db.get_table_columns(doctype)
	except Exception:
		return False


# ============================================================
# Clinical gathering
# ============================================================


def _visits(pet: str) -> list[dict]:
	return frappe.get_all(
		"Vet Visit",
		filters={"animal_patient": pet},
		fields=[
			"name",
			"visit_datetime",
			"status",
			"visit_type",
			"diagnosis",
			"follow_up_date",
			"follow_up_status",
			"follow_up_required",
			"weight",
			"temperature",
			"heart_rate",
			"respiratory_rate",
			"creation",
		],
		order_by="visit_datetime asc, creation asc",
		ignore_permissions=True,
	)


def _episodes(pet: str) -> list[dict]:
	if not _exists("Pet Care Episode"):
		return []
	return frappe.get_all(
		"Pet Care Episode",
		filters={"pet": pet},
		fields=[
			"name",
			"episode_title",
			"episode_type",
			"episode_status",
			"outcome",
			"primary_diagnosis",
			"started_on",
			"resolved_on",
			"closed_on",
			"next_follow_up_date",
			"creation",
		],
		order_by="started_on asc, creation asc",
		ignore_permissions=True,
	)


def _vitals_trend(pet: str, visits: list[dict]) -> list[dict]:
	"""Vital-sign child rows preferred; visit-level readings fill in where absent.

	Zero readings are dropped rather than sent - the frontend plots what it is given,
	and a 0 kg weight is a visible lie about the animal.
	"""
	points: list[dict] = []
	visit_names = [v["name"] for v in visits]

	if visit_names and _exists("Vet Visit Vital Sign"):
		rows = frappe.get_all(
			"Vet Visit Vital Sign",
			filters={"parent": ["in", visit_names], "parenttype": "Vet Visit"},
			fields=["parent", "recorded_at", "weight", "temperature", "heart_rate", "respiratory_rate"],
			order_by="recorded_at asc, idx asc",
			ignore_permissions=True,
		)
		seen_parents = set()
		for row in rows:
			recorded = _iso(row.get("recorded_at"))
			if not recorded:
				continue
			seen_parents.add(row.get("parent"))
			point = {
				"recorded_at": recorded,
				"weight": _positive(row.get("weight")),
				"temperature": _positive(row.get("temperature")),
				"heart_rate": _positive(row.get("heart_rate")),
				"respiratory_rate": _positive(row.get("respiratory_rate")),
			}
			if any(point[k] is not None for k in ("weight", "temperature", "heart_rate", "respiratory_rate")):
				points.append(point)
	else:
		seen_parents = set()

	for visit in visits:
		if visit["name"] in seen_parents:
			continue
		recorded = _iso(visit.get("visit_datetime") or visit.get("creation"))
		if not recorded:
			continue
		point = {
			"recorded_at": recorded,
			"weight": _positive(visit.get("weight")),
			"temperature": _positive(visit.get("temperature")),
			"heart_rate": _positive(visit.get("heart_rate")),
			"respiratory_rate": _positive(visit.get("respiratory_rate")),
		}
		if any(point[k] is not None for k in ("weight", "temperature", "heart_rate", "respiratory_rate")):
			points.append(point)

	points.sort(key=lambda p: p["recorded_at"])
	return points


def _disease_names(codes: set[str]) -> dict[str, str]:
	"""Map Disease docnames to their readable name - a DISEASE-00152 chip means nothing."""
	codes = {c for c in codes if c}
	if not codes or not _exists("Disease"):
		return {}
	try:
		rows = frappe.get_all(
			"Disease",
			filters={"name": ["in", sorted(codes)]},
			fields=["name", "disease_name"],
			ignore_permissions=True,
		)
	except Exception:
		return {}
	return {row["name"]: cstr(row.get("disease_name")).strip() for row in rows if row.get("disease_name")}


def _diagnoses(pet: str, visits: list[dict]) -> list[dict]:
	"""Structured Visit Diagnosis rows, falling back to the visit's free-text diagnosis."""
	counter: Counter = Counter()
	visit_names = [v["name"] for v in visits]

	if visit_names and _exists("Visit Diagnosis"):
		rows = frappe.get_all(
			"Visit Diagnosis",
			filters={"parent": ["in", visit_names], "parenttype": "Vet Visit"},
			fields=["disease", "diagnosis_text"],
			ignore_permissions=True,
		)
		names = _disease_names({cstr(r.get("disease")) for r in rows})
		for row in rows:
			code = cstr(row.get("disease")).strip()
			label = names.get(code) or code or cstr(row.get("diagnosis_text")).strip()
			if label:
				counter[label] += 1

	if not counter:
		for visit in visits:
			for label in _split_lines(visit.get("diagnosis"))[:1]:
				counter[label] += 1

	return [
		{"key": frappe.scrub(label)[:60] or "diagnosis", "label": label, "count": count}
		for label, count in counter.most_common(12)
	]


def _care_activity(pet: str, visits: list[dict]) -> list[dict]:
	counts: OrderedDict = OrderedDict()

	def add(key: str, value: int):
		if value:
			counts[key] = counts.get(key, 0) + value

	add("consultation", len(visits))
	for doctype, key in (
		("Lab", "lab"),
		("Imaging", "imaging"),
		("Pet Procedure", "procedure"),
		("Pet Boarding", "boarding"),
		("PetCareService", "grooming"),
	):
		if not _exists(doctype):
			continue
		field = PET_LINK_FIELDS.get(doctype)
		try:
			add(key, frappe.db.count(doctype, {field: pet}))
		except Exception:
			continue

	return [
		{"key": key, "label": _label_from_key(key), "value": value}
		for key, value in counts.items()
	]


def _preventive(pet: str) -> dict:
	"""Vaccination and deworming due/overdue state from the records' next_due_date."""
	today = getdate(nowdate())
	items: list[dict] = []
	due_count = overdue_count = 0
	next_due: date | None = None
	total = compliant = 0

	for doctype, icon_key in (("Pet Vaccination Record", "vaccine_name"), ("Pet Deworming Record", "medication_name")):
		if not _exists(doctype):
			continue
		rows = frappe.get_all(
			doctype,
			filters={"pet": pet},
			fields=["name", icon_key, "administered_on", "next_due_date"],
			order_by="next_due_date asc",
			ignore_permissions=True,
		)
		# Only the most recent record per protocol decides current standing.
		latest: dict[str, dict] = {}
		for row in rows:
			label = cstr(row.get(icon_key) or "").strip() or _("Protocol")
			current = latest.get(label)
			if not current or cstr(row.get("administered_on") or "") >= cstr(current.get("administered_on") or ""):
				latest[label] = row

		for label, row in latest.items():
			due = getdate(row["next_due_date"]) if row.get("next_due_date") else None
			total += 1
			if not due:
				compliant += 1
				continue
			if due < today:
				overdue_count += 1
				items.append(
					{
						"key": frappe.scrub(label)[:60] or "protocol",
						"label": label,
						"count": 1,
						"helper": _("Overdue since {0}").format(due.isoformat()),
					}
				)
			else:
				compliant += 1
				if due <= add_days(today, 30):
					due_count += 1
					items.append(
						{
							"key": frappe.scrub(label)[:60] or "protocol",
							"label": label,
							"count": 1,
							"helper": _("Due on {0}").format(due.isoformat()),
						}
					)
				if next_due is None or due < next_due:
					next_due = due

	return {
		"compliance_percent": int(round(compliant * 100.0 / total)) if total else None,
		"due_count": due_count,
		"overdue_count": overdue_count,
		"next_due_date": _iso(next_due),
		"items": items,
	}


def _pending_results(pet: str) -> list[dict]:
	"""Diagnostics ordered but not released - the clinician's open loop."""
	pending: list[dict] = []
	for doctype, unreleased in (
		("Lab", ("Ordered", "Sample Collected", "In Progress", "Result Entered")),
		("Imaging", ("Ordered", "Scheduled", "In Progress", "Reported")),
	):
		if not _exists(doctype):
			continue
		rows = frappe.get_all(
			doctype,
			filters={"pet": pet, "status": ["in", list(unreleased)]},
			fields=["name", "status", "care_service", "modified"],
			order_by="modified desc",
			limit_page_length=25,
			ignore_permissions=True,
		)
		for row in rows:
			pending.append(
				{
					"key": frappe.scrub(row["name"]),
					"label": cstr(row.get("care_service") or "").strip() or _(doctype),
					"count": 1,
					"helper": _("{0} - awaiting release").format(_(cstr(row.get("status")))),
				}
			)
	return pending


def _profile_lists(pet: str) -> dict:
	if not _exists("Pet Medical Profile"):
		return {"chronic_conditions": [], "allergies": [], "medications": []}
	row = frappe.db.get_value(
		"Pet Medical Profile",
		{"pet": pet},
		["chronic_conditions", "allergies"],
		as_dict=True,
	)
	medications: list[str] = []
	if _exists("Vet Visit Medication Item"):
		visit_names = frappe.get_all("Vet Visit", filters={"animal_patient": pet}, pluck="name", ignore_permissions=True)
		if visit_names:
			med_rows = frappe.get_all(
				"Vet Visit Medication Item",
				filters={"parent": ["in", visit_names], "parenttype": "Vet Visit"},
				fields=["*"],
				order_by="creation desc",
				limit_page_length=25,
				ignore_permissions=True,
			)
			seen = set()
			for med in med_rows:
				label = cstr(
					med.get("medication_name") or med.get("medication") or med.get("item_name") or ""
				).strip()
				dose = cstr(med.get("dosage") or med.get("dose") or "").strip()
				frequency = cstr(med.get("frequency") or "").strip()
				if not label:
					continue
				text = " ".join(part for part in (label, dose, frequency) if part)
				if text.lower() not in seen:
					seen.add(text.lower())
					medications.append(text)

	return {
		"chronic_conditions": _split_lines(row.get("chronic_conditions")) if row else [],
		"allergies": _split_lines(row.get("allergies")) if row else [],
		"medications": medications[:12],
	}


def _build_clinical(pet: str, visits: list[dict], episodes: list[dict], from_date, to_date) -> dict:
	completed = [v for v in visits if cstr(v.get("status")) == "Completed"]
	last_visit = visits[-1] if visits else None

	today = getdate(nowdate())
	follow_ups = [
		getdate(v["follow_up_date"])
		for v in visits
		if v.get("follow_up_date") and getdate(v["follow_up_date"]) >= today
	]
	for episode in episodes:
		if episode.get("next_follow_up_date") and getdate(episode["next_follow_up_date"]) >= today:
			follow_ups.append(getdate(episode["next_follow_up_date"]))

	active = [e for e in episodes if cstr(e.get("episode_status")) in ACTIVE_EPISODE_STATUSES]
	resolved = [e for e in episodes if cstr(e.get("episode_status")) in RESOLVED_EPISODE_STATUSES]

	resolution_days = []
	for episode in episodes:
		start = episode.get("started_on")
		end = episode.get("resolved_on") or episode.get("closed_on")
		if start and end:
			delta = (getdate(end) - getdate(start)).days
			if delta >= 0:
				resolution_days.append(delta)

	outcome_counter = Counter(
		cstr(e.get("outcome")).strip() for e in episodes if cstr(e.get("outcome")).strip()
	)

	# A reopened episode: the same pet returning to an active state after a resolution.
	reopened = 0
	ordered = sorted(episodes, key=lambda e: cstr(e.get("started_on") or e.get("creation") or ""))
	had_resolution = False
	for episode in ordered:
		status = cstr(episode.get("episode_status"))
		if status in ACTIVE_EPISODE_STATUSES and had_resolution:
			reopened += 1
		if status in RESOLVED_EPISODE_STATUSES:
			had_resolution = True

	preventive = _preventive(pet)
	profile = _profile_lists(pet)
	vitals = _vitals_trend(pet, visits)

	metrics = [
		_kpi("visits", _("Visits"), len(visits), _("Lifetime clinical visits"), "tabler-stethoscope", "primary"),
		_kpi(
			"episodes-active",
			_("Active cases"),
			len(active),
			_("Care episodes still open"),
			"tabler-clipboard-heart",
			"info",
		),
		_kpi(
			"last-visit",
			_("Last visit"),
			_iso(last_visit.get("visit_datetime")) if last_visit else None,
			_("Most recent recorded visit"),
			"tabler-calendar-event",
			"secondary",
		),
	]

	return {
		"availability": AVAILABILITY_OK,
		"metrics": metrics,
		"visit_count": len(visits),
		"completed_visit_count": len(completed),
		"last_visit_date": _iso(last_visit.get("visit_datetime")) if last_visit else None,
		"next_follow_up_date": _iso(min(follow_ups)) if follow_ups else None,
		"episodes": {
			"total": len(episodes),
			"active": len(active),
			"resolved": len(resolved),
			"reopened": reopened,
			"average_resolution_days": (
				int(round(sum(resolution_days) / len(resolution_days))) if resolution_days else None
			),
			"outcomes": [
				{"key": frappe.scrub(name), "label": _(name), "value": count}
				for name, count in outcome_counter.most_common()
			],
		},
		"diagnoses": _diagnoses(pet, visits),
		"chronic_conditions": profile["chronic_conditions"],
		"allergies": profile["allergies"],
		"medications": profile["medications"],
		"care_activity": _care_activity(pet, visits),
		"preventive": preventive,
		"vitals_trend": vitals,
		"pending_results": _pending_results(pet),
	}


# ============================================================
# Financial gathering
# ============================================================


def _guardians_for_pet(pet: str) -> list[str]:
	return frappe.get_all("PetGuardian", filters={"pet_id": pet}, pluck="guardian_id", ignore_permissions=True) or []


def _customers_for_pet(pet: str) -> list[str]:
	guardians = _guardians_for_pet(pet)
	if not guardians:
		return []
	customers = frappe.get_all(
		"Guardian",
		filters={"name": ["in", guardians], "customer_id": ["is", "set"]},
		pluck="customer_id",
		ignore_permissions=True,
	)
	return sorted({c for c in customers if c})


def _marker_pet_map(doctype_names: dict[str, set[str]]) -> dict[tuple[str, str], str]:
	"""Resolve each referenced source doc to its pet, one query per doctype."""
	resolved: dict[tuple[str, str], str] = {}
	for doctype, names in doctype_names.items():
		field = PET_LINK_FIELDS.get(doctype)
		if not field or not names or not _exists(doctype):
			continue
		if not _table_has_column(doctype, field):
			continue
		try:
			rows = frappe.get_all(
				doctype,
				filters={"name": ["in", sorted(names)]},
				fields=["name", field],
				ignore_permissions=True,
			)
		except Exception:
			continue
		for row in rows:
			if row.get(field):
				resolved[(doctype, row["name"])] = row[field]
	return resolved


def _invoice_lines_for_pet(pet: str, customers: list[str]) -> tuple[list[dict], str]:
	"""Every invoice line attributable to this pet, plus the basis that produced them.

	Both strategies run over the same candidate set (the guardians' invoices), and a
	line is claimed by whichever resolves it. `basis` reports what actually carried
	the result so the UI can caption the number honestly.
	"""
	if not customers or not _exists("Sales Invoice"):
		return [], "none"

	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"customer": ["in", customers], "docstatus": ["<", 2]},
		fields=["name", "posting_date", "status", "currency", "grand_total", "outstanding_amount", "is_return"],
		order_by="posting_date desc, creation desc",
		ignore_permissions=True,
	)
	if not invoices:
		return [], "none"

	invoice_by_name = {inv["name"]: inv for inv in invoices}
	lines = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": ["in", list(invoice_by_name)], "parenttype": "Sales Invoice"},
		fields=["name", "parent", "item_code", "item_name", "item_group", "description", "amount", "net_amount", "qty"],
		ignore_permissions=True,
		limit_page_length=0,
	)
	if not lines:
		return [], "none"

	# Strategy 1 - line provenance.
	referenced: dict[str, set[str]] = defaultdict(set)
	line_markers: dict[str, list[tuple[str, str]]] = {}
	for line in lines:
		markers = _MARKER_RE.findall(cstr(line.get("description")))
		if not markers:
			continue
		cleaned = [(dt.strip(), nm.strip()) for dt, nm in markers if dt.strip() and nm.strip()]
		if cleaned:
			line_markers[line["name"]] = cleaned
			for doctype, name in cleaned:
				referenced[doctype].add(name)

	marker_pets = _marker_pet_map(referenced)

	# Strategy 2 - pet-scoped clinical billables pointing back at an invoice.
	billable_invoices: dict[str, str] = {}
	for doctype, category in BILLABLE_SOURCES.items():
		if not _exists(doctype):
			continue
		field = PET_LINK_FIELDS.get(doctype)
		if not field or not _table_has_column(doctype, "sales_invoice"):
			continue
		try:
			rows = frappe.get_all(
				doctype,
				filters={field: pet, "sales_invoice": ["in", list(invoice_by_name)]},
				fields=["name", "sales_invoice"],
				ignore_permissions=True,
			)
		except Exception:
			continue
		for row in rows:
			billable_invoices.setdefault(row["sales_invoice"], category)

	used_provenance = used_billable = False
	attributed: list[dict] = []

	for line in lines:
		invoice = invoice_by_name[line["parent"]]
		category = None
		matched = False

		markers = line_markers.get(line["name"])
		if markers:
			# The line names its origin. It belongs to this pet only if one of the
			# referenced docs does - an explicit marker for another pet is a real
			# exclusion, which is what keeps a multi-pet guardian's totals apart.
			marker_pet_values = [marker_pets.get(key) for key in markers if key in marker_pets]
			if marker_pet_values:
				if pet in marker_pet_values:
					matched = True
					used_provenance = True
					for doctype, _name in markers:
						if doctype in BILLABLE_SOURCES:
							category = BILLABLE_SOURCES[doctype]
							break
				else:
					continue  # resolved, and not this pet

		if not matched and line["parent"] in billable_invoices:
			# Fall back to the invoice-level back-link. Only safe when nothing on the
			# invoice is explicitly marked for a different pet.
			if not markers:
				matched = True
				used_billable = True
				category = billable_invoices[line["parent"]]

		if not matched:
			continue

		attributed.append(
			{
				"invoice": line["parent"],
				"posting_date": invoice.get("posting_date"),
				"status": invoice.get("status"),
				"currency": invoice.get("currency"),
				"is_return": cint(invoice.get("is_return")),
				"amount": flt(line.get("net_amount") or line.get("amount") or 0),
				"category": category or _category_from_item(line),
				"item_name": line.get("item_name") or line.get("item_code"),
			}
		)

	if used_provenance and used_billable:
		basis = "mixed"
	elif used_provenance:
		basis = "line_provenance"
	elif used_billable:
		basis = "pet_scoped_billables"
	else:
		basis = "none"

	return attributed, basis


def _category_from_item(line: dict) -> str:
	group = cstr(line.get("item_group") or "").strip().lower()
	name = cstr(line.get("item_name") or line.get("item_code") or "").strip().lower()
	haystack = f"{group} {name}"
	for key, needles in (
		("lab", ("lab", "test", "panel", "blood")),
		("imaging", ("imaging", "x-ray", "xray", "ultrasound", "radiograph")),
		("boarding", ("boarding", "hostel", "stay")),
		("grooming", ("groom", "bath", "nail", "spa")),
		("medication", ("medic", "drug", "tablet", "injection", "syrup", "vaccine")),
		("procedure", ("procedure", "surgery", "operation", "dental")),
		("consultation", ("consult", "visit", "exam", "checkup")),
	):
		if any(needle in haystack for needle in needles):
			return key
	return "other"


def _build_financial(pet: str, from_date, to_date, visit_count: int) -> dict:
	customers = _customers_for_pet(pet)
	lines, basis = _invoice_lines_for_pet(pet, customers)
	currency = DEFAULT_CURRENCY
	for line in lines:
		if line.get("currency"):
			currency = line["currency"]
			break

	def signed(line: dict) -> float:
		# Credit notes carry negative amounts already in ERPNext; guard either way.
		amount = flt(line["amount"])
		return -abs(amount) if line["is_return"] and amount > 0 else amount

	lifetime = sum(signed(line) for line in lines)

	in_range = []
	for line in lines:
		posting = getdate(line["posting_date"]) if line.get("posting_date") else None
		if from_date and (not posting or posting < getdate(from_date)):
			continue
		if to_date and (not posting or posting > getdate(to_date)):
			continue
		in_range.append(line)
	period = sum(signed(line) for line in in_range)

	by_category: Counter = Counter()
	for line in lines:
		by_category[line["category"]] += signed(line)

	month_counter: Counter = Counter()
	for line in in_range if (from_date or to_date) else lines:
		key = _month_key(line.get("posting_date"))
		if key:
			month_counter[key] += signed(line)

	months = _month_range(from_date, to_date)
	by_month = [
		{"period": entry["period"], "value": round(flt(month_counter.get(entry["period"], 0)), 2)}
		for entry in _series(month_counter, months)
	]

	# Outstanding is an invoice-level figure on a guardian-scoped invoice, so the raw
	# number covers every pet on that invoice. Reporting it unmodified would let a
	# sibling pet's unpaid balance show up as this pet's debt (and could exceed this
	# pet's own spend). It is prorated by the pet's share of the invoice total.
	invoice_names = sorted({line["invoice"] for line in lines})
	invoices_payload = []
	outstanding_total = 0.0
	if invoice_names:
		rows = frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", invoice_names]},
			fields=["name", "posting_date", "status", "grand_total", "outstanding_amount"],
			order_by="posting_date desc, creation desc",
			ignore_permissions=True,
		)
		pet_totals: dict[str, float] = defaultdict(float)
		for line in lines:
			pet_totals[line["invoice"]] += signed(line)
		for row in rows:
			pet_total = flt(pet_totals.get(row["name"], 0))
			invoice_total = flt(row.get("grand_total"))
			raw_outstanding = flt(row.get("outstanding_amount"))
			if invoice_total > 0 and pet_total > 0:
				share = min(pet_total / invoice_total, 1.0)
			else:
				share = 0.0
			pet_outstanding = round(raw_outstanding * share, 2)
			outstanding_total += pet_outstanding
			invoices_payload.append(
				{
					"id": row["name"],
					"date": _iso(row.get("posting_date")),
					"status": cstr(row.get("status")),
					"total": round(pet_total, 2),
					"outstanding": pet_outstanding,
					"route_to": f"/accounting/sales-invoices/{row['name']}",
				}
			)

	average = round(lifetime / visit_count, 2) if visit_count and lifetime else None

	metrics = [
		_kpi("lifetime-spend", _("Lifetime spend"), round(lifetime, 2), _("All attributable charges"), "tabler-cash", "success"),
		_kpi("outstanding", _("Outstanding"), round(outstanding_total, 2), _("Unsettled invoice balance"), "tabler-receipt-2", "warning"),
	]

	return {
		"availability": AVAILABILITY_OK,
		"currency": currency,
		"basis": basis,
		"metrics": metrics,
		"lifetime_spend": round(lifetime, 2),
		"period_spend": round(period, 2),
		"outstanding": round(outstanding_total, 2),
		"average_per_visit": average,
		"by_category": [
			{"key": key, "label": _label_from_key(key), "value": round(flt(value), 2)}
			for key, value in by_category.most_common()
			if round(flt(value), 2)
		],
		"by_month": by_month,
		"invoices": invoices_payload[:50],
	}


# ============================================================
# Recurrence
# ============================================================


def _build_recurrence(pet: str, visits: list[dict], from_date, to_date) -> dict:
	dates = [getdate(v["visit_datetime"]) for v in visits if v.get("visit_datetime")]
	dates.sort()

	gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
	average_gap = int(round(sum(gaps) / len(gaps))) if gaps else None
	longest_gap = max(gaps) if gaps else None

	no_show = len([v for v in visits if cstr(v.get("follow_up_status")) == "Missed"])
	if _exists("Appointment"):
		try:
			no_show += frappe.db.count("Appointment", {"custom_pet": pet, "status": ["in", ["No Show", "Missed"]]})
		except Exception:
			pass

	registration = frappe.db.get_value("Pet", pet, "registration_date")
	anchor = getdate(registration) if registration else (dates[0] if dates else None)
	tenure = (getdate(nowdate()) - anchor).days if anchor else None

	month_counter: Counter = Counter()
	for value in dates:
		if from_date and value < getdate(from_date):
			continue
		if to_date and value > getdate(to_date):
			continue
		month_counter[value.strftime("%Y-%m")] += 1
	by_month = _series(month_counter, _month_range(from_date, to_date))

	# Engagement: recency against this pet's own rhythm, damped by missed follow-ups.
	score = None
	if dates:
		days_since = (getdate(nowdate()) - dates[-1]).days
		if average_gap and average_gap > 0:
			ratio = days_since / float(average_gap)
			score = 100.0 if ratio <= 1 else max(0.0, 100.0 - (ratio - 1) * 40.0)
		else:
			score = 70.0 if days_since <= 365 else 30.0
		if len(dates) == 1:
			score = min(score, 60.0)
		if no_show:
			score = max(0.0, score - min(no_show * 8.0, 30.0))
		score = int(round(score))

	return {
		"visit_count": len(visits),
		"average_days_between_visits": average_gap,
		"longest_gap_days": longest_gap,
		"no_show_count": no_show,
		"tenure_days": tenure if tenure is not None and tenure >= 0 else None,
		"score": score,
		"by_month": by_month,
	}


# ============================================================
# Public: dashboard
# ============================================================


@frappe.whitelist()
def get_pet_dashboard(pet=None, pet_id=None, from_date=None, to_date=None):
	pet_name = cstr(pet or pet_id).strip()
	if not pet_name:
		frappe.throw(_("Pet is required."))

	guardian = _assert_pet_access(pet_name)

	from_date = getdate(from_date) if from_date else None
	to_date = getdate(to_date) if to_date else None
	if from_date and to_date and from_date > to_date:
		from_date, to_date = to_date, from_date

	clinical_allowed = _can_read_clinical(pet_name, guardian)
	financial_allowed = _can_read_financial(guardian)

	visits: list[dict] = []
	episodes: list[dict] = []
	if clinical_allowed:
		visits = _visits(pet_name)
		episodes = _episodes(pet_name)
		clinical = _build_clinical(pet_name, visits, episodes, from_date, to_date)
	else:
		clinical = {"availability": AVAILABILITY_FORBIDDEN}

	if financial_allowed:
		financial = _build_financial(pet_name, from_date, to_date, len(visits))
	else:
		# No zeros, no currency, no metrics - nothing the UI could render as a fact.
		financial = {"availability": AVAILABILITY_FORBIDDEN}

	recurrence = _build_recurrence(pet_name, visits, from_date, to_date) if clinical_allowed else {
		"visit_count": 0,
		"average_days_between_visits": None,
		"longest_gap_days": None,
		"no_show_count": 0,
		"tenure_days": None,
		"score": None,
		"by_month": [],
	}

	return {
		"summary": _build_summary(pet_name, clinical, financial, clinical_allowed, financial_allowed),
		"clinical": clinical,
		"financial": financial,
		"recurrence": recurrence,
	}


def _build_summary(pet: str, clinical: dict, financial: dict, clinical_allowed: bool, financial_allowed: bool) -> dict:
	kpis: list[dict] = []
	warnings: list[dict] = []

	if clinical_allowed:
		kpis.append(
			_kpi(
				"visits",
				_("Visits"),
				cstr(clinical.get("visit_count") or 0),
				_("Lifetime clinical visits"),
				"tabler-stethoscope",
				"primary",
			)
		)
		last_visit = clinical.get("last_visit_date")
		kpis.append(
			_kpi(
				"last-visit",
				_("Last visit"),
				last_visit,
				_("Most recent recorded visit"),
				"tabler-calendar-event",
				"info",
			)
		)
		episodes = clinical.get("episodes") or {}
		if episodes.get("active"):
			kpis.append(
				_kpi(
					"active-cases",
					_("Active cases"),
					cstr(episodes.get("active")),
					_("Care episodes still open"),
					"tabler-clipboard-heart",
					"warning",
				)
			)
		preventive = clinical.get("preventive") or {}
		if preventive.get("overdue_count"):
			warnings.append(
				{
					"key": "vaccination",
					"label": _("Preventive care overdue"),
					"value": preventive["overdue_count"],
					"icon": "tabler-vaccine",
					"color": "error",
				}
			)
		if clinical.get("pending_results"):
			warnings.append(
				{
					"key": "pending-results",
					"label": _("Diagnostics awaiting release"),
					"value": len(clinical["pending_results"]),
					"icon": "tabler-flask",
					"color": "warning",
				}
			)

	if financial_allowed:
		currency = financial.get("currency") or DEFAULT_CURRENCY
		kpis.append(
			_kpi(
				"lifetime-spend",
				_("Lifetime spend"),
				f"{financial.get('lifetime_spend', 0):,.0f} {currency}",
				_("All attributable charges"),
				"tabler-cash",
				"success",
			)
		)
		if flt(financial.get("outstanding")) > 0:
			warnings.append(
				{
					"key": "outstanding",
					"label": _("Outstanding balance"),
					"value": financial["outstanding"],
					"icon": "tabler-receipt-2",
					"color": "warning",
				}
			)

	return {
		"headline": _("Care and spend overview"),
		"caption": _("Clinical trajectory and billing for this pet."),
		"kpis": kpis,
		"warnings": warnings,
	}


# ============================================================
# Public: insights
# ============================================================


def _insight(key, title, detail, confidence, category, icon, color, action, evidence) -> dict:
	return {
		"key": key,
		"title": title,
		"detail": detail,
		"confidence": confidence,
		"category": category,
		"icon": icon,
		"color": color,
		"recommended_action": action,
		"evidence": evidence,
	}


def _visit_route(name: str) -> str:
	return f"/healthcare/visits/{name}"


def _recurring_diagnosis_insights(pet: str, visits: list[dict]) -> list[dict]:
	"""Same diagnosis three or more times - an observed pattern, not a prediction."""
	insights: list[dict] = []
	visit_names = [v["name"] for v in visits]
	if not visit_names or not _exists("Visit Diagnosis"):
		return insights

	rows = frappe.get_all(
		"Visit Diagnosis",
		filters={"parent": ["in", visit_names], "parenttype": "Vet Visit"},
		fields=["parent", "disease", "diagnosis_text"],
		ignore_permissions=True,
	)
	visit_dates = {v["name"]: v.get("visit_datetime") for v in visits}
	names = _disease_names({cstr(r.get("disease")) for r in rows})
	grouped: dict[str, list[dict]] = defaultdict(list)
	for row in rows:
		code = cstr(row.get("disease")).strip()
		label = names.get(code) or code or cstr(row.get("diagnosis_text")).strip()
		if label:
			grouped[label].append(row)

	for label, occurrences in grouped.items():
		if len(occurrences) < 3:
			continue
		dated = sorted(
			(
				{"visit": o["parent"], "date": visit_dates.get(o["parent"])}
				for o in occurrences
				if visit_dates.get(o["parent"])
			),
			key=lambda item: cstr(item["date"]),
		)
		if not dated:
			continue
		first, last = getdate(dated[0]["date"]), getdate(dated[-1]["date"])
		span_days = (last - first).days
		span_months = max(1, int(round(span_days / 30.0)))
		months = {getdate(d["date"]).month for d in dated}
		# Seasonality is only observable once the history spans enough of the year to
		# show a return. Calling a one-month cluster "seasonal" would be a claim the
		# data cannot support.
		seasonal = span_days >= 300 and len(months) <= 3 and len(dated) >= 3

		if seasonal:
			detail = _("{0} recorded {1} times over {2} months, clustered in the same part of the year.").format(
				label, len(dated), span_months
			)
		elif span_days >= 30:
			detail = _("{0} has been recorded {1} times over {2} months in this pet's history.").format(
				label, len(dated), span_months
			)
		else:
			detail = _("{0} has been recorded {1} times within {2} days in this pet's history.").format(
				label, len(dated), max(span_days, 1)
			)

		insights.append(
			_insight(
				key=f"recurrent-{frappe.scrub(label)[:40]}",
				title=_("Recurring {0}").format(label),
				detail=detail,
				confidence="high" if len(dated) >= 4 else "medium",
				category=_("Recurring diagnosis"),
				icon="tabler-repeat",
				color="warning",
				action=_("Consider reviewing whether an underlying cause has been investigated."),
				evidence=[
					{
						"label": _("{0} recorded").format(label),
						"reference": item["visit"],
						"date": _iso(item["date"]),
						"route_to": _visit_route(item["visit"]),
					}
					for item in dated[-5:]
				],
			)
		)
	return insights


def _preventive_lapse_insight(pet: str) -> list[dict]:
	insights: list[dict] = []
	today = getdate(nowdate())
	evidence: list[dict] = []
	labels: list[str] = []

	for doctype, name_field, route in (
		("Pet Vaccination Record", "vaccine_name", "/healthcare/vaccinations"),
		("Pet Deworming Record", "medication_name", "/healthcare/deworming"),
	):
		if not _exists(doctype):
			continue
		rows = frappe.get_all(
			doctype,
			filters={"pet": pet, "next_due_date": ["<", today]},
			fields=["name", name_field, "next_due_date", "administered_on"],
			order_by="next_due_date asc",
			ignore_permissions=True,
		)
		for row in rows:
			label = cstr(row.get(name_field) or "").strip() or _("Protocol")
			labels.append(label)
			evidence.append(
				{
					"label": _("{0} due {1}").format(label, _iso(row.get("next_due_date"))),
					"reference": row["name"],
					"date": _iso(row.get("next_due_date")),
					"route_to": f"{route}/{row['name']}",
				}
			)

	if evidence:
		insights.append(
			_insight(
				key="preventive-lapse",
				title=_("Preventive care past its due date"),
				detail=_("{0} preventive record(s) show a due date that has already passed: {1}.").format(
					len(evidence), ", ".join(sorted(set(labels))[:5])
				),
				confidence="high",
				category=_("Preventive care"),
				icon="tabler-vaccine",
				color="error",
				action=_("Consider confirming with the guardian whether these were given elsewhere."),
				evidence=evidence[:6],
			)
		)
	return insights


def _weight_trend_insight(pet: str, vitals: list[dict]) -> list[dict]:
	"""A sustained directional change across at least three weight readings."""
	points = [p for p in vitals if p.get("weight")]
	if len(points) < 3:
		return []

	recent = points[-5:]
	first, last = flt(recent[0]["weight"]), flt(recent[-1]["weight"])
	if first <= 0:
		return []
	change = (last - first) / first * 100.0
	if abs(change) < 10:
		return []

	monotonic = all(
		flt(recent[i]["weight"]) <= flt(recent[i + 1]["weight"]) for i in range(len(recent) - 1)
	) or all(flt(recent[i]["weight"]) >= flt(recent[i + 1]["weight"]) for i in range(len(recent) - 1))

	rising = change > 0
	return [
		_insight(
			key="weight-trajectory",
			title=_("Weight trending {0}").format(_("up") if rising else _("down")),
			detail=_("Recorded weight moved from {0} to {1} kg across {2} readings ({3}{4}%).").format(
				round(first, 2), round(last, 2), len(recent), "+" if rising else "", round(change, 1)
			),
			confidence="high" if monotonic and abs(change) >= 15 else "medium",
			category=_("Weight trajectory"),
			icon="tabler-scale",
			color="warning" if not rising else "info",
			action=_("Consider whether diet, dosing by weight, or a body-condition check should be revisited."),
			evidence=[
				{
					"label": _("Weight {0} kg").format(round(flt(p["weight"]), 2)),
					"reference": pet,
					"date": p["recorded_at"],
					"route_to": f"/pet-profile/{pet}/dashboard",
				}
				for p in recent
			],
		)
	]


def _unresolved_diagnostics_insight(pet: str) -> list[dict]:
	evidence: list[dict] = []
	for doctype, unreleased, route in (
		("Lab", ("Ordered", "Sample Collected", "In Progress", "Result Entered"), "/healthcare/labs"),
		("Imaging", ("Ordered", "Scheduled", "In Progress", "Reported"), "/healthcare/imaging"),
	):
		if not _exists(doctype):
			continue
		rows = frappe.get_all(
			doctype,
			filters={"pet": pet, "status": ["in", list(unreleased)]},
			fields=["name", "status", "care_service", "creation"],
			order_by="creation asc",
			limit_page_length=10,
			ignore_permissions=True,
		)
		for row in rows:
			evidence.append(
				{
					"label": _("{0} - {1}").format(
						cstr(row.get("care_service") or "").strip() or _(doctype), _(cstr(row.get("status")))
					),
					"reference": row["name"],
					"date": _iso(row.get("creation")),
					"route_to": f"{route}/{row['name']}",
				}
			)

	if not evidence:
		return []
	return [
		_insight(
			key="unresolved-diagnostics",
			title=_("Diagnostics ordered but not released"),
			detail=_("{0} diagnostic order(s) on this pet have not reached a released result.").format(len(evidence)),
			confidence="high",
			category=_("Diagnostics"),
			icon="tabler-flask",
			color="info",
			action=_("Consider following up so the result reaches the record."),
			evidence=evidence[:6],
		)
	]


def _care_gap_insight(pet: str, visits: list[dict]) -> list[dict]:
	dates = sorted(getdate(v["visit_datetime"]) for v in visits if v.get("visit_datetime"))
	if len(dates) < 3:
		return []
	gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
	average = sum(gaps) / len(gaps)
	if average <= 0:
		return []
	days_since = (getdate(nowdate()) - dates[-1]).days
	if days_since < max(average * 2, average + 30):
		return []

	last_visit = next(v for v in reversed(visits) if v.get("visit_datetime"))
	return [
		_insight(
			key="care-gap",
			title=_("Longer gap than this pet's usual pattern"),
			detail=_("The last visit was {0} days ago, against an average interval of {1} days.").format(
				days_since, int(round(average))
			),
			confidence="medium",
			category=_("Care gap"),
			icon="tabler-calendar-off",
			color="secondary",
			action=_("Consider whether a wellness check is worth offering."),
			evidence=[
				{
					"label": _("Most recent visit"),
					"reference": last_visit["name"],
					"date": _iso(last_visit.get("visit_datetime")),
					"route_to": _visit_route(last_visit["name"]),
				}
			],
		)
	]


def _chronic_escalation_insight(pet: str, visits: list[dict], episodes: list[dict]) -> list[dict]:
	"""Visit frequency rising for a pet carrying an open chronic episode."""
	chronic = [
		e
		for e in episodes
		if cstr(e.get("episode_type")) == "Chronic Disease"
		and cstr(e.get("episode_status")) in ACTIVE_EPISODE_STATUSES
	]
	if not chronic:
		return []

	dates = sorted(getdate(v["visit_datetime"]) for v in visits if v.get("visit_datetime"))
	if len(dates) < 4:
		return []

	midpoint = len(dates) // 2
	early, late = dates[:midpoint], dates[midpoint:]
	early_gaps = [(early[i] - early[i - 1]).days for i in range(1, len(early))]
	late_gaps = [(late[i] - late[i - 1]).days for i in range(1, len(late))]
	if not early_gaps or not late_gaps:
		return []
	early_avg = sum(early_gaps) / len(early_gaps)
	late_avg = sum(late_gaps) / len(late_gaps)
	if late_avg >= early_avg * 0.7 or late_avg <= 0:
		return []

	episode = chronic[0]
	return [
		_insight(
			key="chronic-escalation",
			title=_("Visits are becoming more frequent"),
			detail=_("Average interval narrowed from {0} to {1} days while a chronic case is open.").format(
				int(round(early_avg)), int(round(late_avg))
			),
			confidence="medium",
			category=_("Chronic case"),
			icon="tabler-activity-heartbeat",
			color="warning",
			action=_("Consider reviewing whether the current plan is holding the condition."),
			evidence=[
				{
					"label": cstr(episode.get("episode_title") or "").strip() or _("Chronic care episode"),
					"reference": episode["name"],
					"date": _iso(episode.get("started_on")),
					"route_to": f"/healthcare/care-episodes/{episode['name']}",
				}
			],
		)
	]


def _repeat_presentation_insight(pet: str, episodes: list[dict]) -> list[dict]:
	"""Several episodes of the same type - repeat presentations for one body system."""
	counter: Counter = Counter()
	by_type: dict[str, list[dict]] = defaultdict(list)
	for episode in episodes:
		etype = cstr(episode.get("episode_type")).strip()
		if etype and etype not in ("General Wellness", "Other"):
			counter[etype] += 1
			by_type[etype].append(episode)

	insights: list[dict] = []
	for etype, count in counter.most_common(2):
		if count < 3:
			continue
		rows = sorted(by_type[etype], key=lambda e: cstr(e.get("started_on") or e.get("creation") or ""))
		insights.append(
			_insight(
				key=f"repeat-{frappe.scrub(etype)[:40]}",
				title=_("Repeat presentations: {0}").format(_(etype)),
				detail=_("{0} separate care episodes of this type are recorded for this pet.").format(count),
				confidence="medium",
				category=_("Repeat presentations"),
				icon="tabler-stethoscope",
				color="info",
				action=_("Consider whether these episodes share a common underlying driver."),
				evidence=[
					{
						"label": cstr(e.get("episode_title") or "").strip() or _(etype),
						"reference": e["name"],
						"date": _iso(e.get("started_on")),
						"route_to": f"/healthcare/care-episodes/{e['name']}",
					}
					for e in rows[-5:]
				],
			)
		)
	return insights


CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


@frappe.whitelist()
def get_pet_insights(pet=None, pet_id=None):
	pet_name = cstr(pet or pet_id).strip()
	if not pet_name:
		frappe.throw(_("Pet is required."))

	guardian = _assert_pet_access(pet_name)

	if not _can_read_clinical(pet_name, guardian):
		return {"availability": AVAILABILITY_FORBIDDEN, "insights": []}

	visits = _visits(pet_name)
	episodes = _episodes(pet_name)
	vitals = _vitals_trend(pet_name, visits)

	insights: list[dict] = []
	insights.extend(_recurring_diagnosis_insights(pet_name, visits))
	insights.extend(_preventive_lapse_insight(pet_name))
	insights.extend(_weight_trend_insight(pet_name, vitals))
	insights.extend(_repeat_presentation_insight(pet_name, episodes))
	insights.extend(_chronic_escalation_insight(pet_name, visits, episodes))
	insights.extend(_unresolved_diagnostics_insight(pet_name))
	insights.extend(_care_gap_insight(pet_name, visits))

	# An insight a clinician cannot verify is not shippable.
	insights = [i for i in insights if i.get("evidence")]
	insights.sort(key=lambda i: CONFIDENCE_ORDER.get(i.get("confidence"), 2))

	return {
		"availability": AVAILABILITY_OK,
		"disclaimer": _(
			"Observed patterns from this pet's own recorded history. Decision support only, not a diagnosis."
		),
		"insights": insights,
	}
