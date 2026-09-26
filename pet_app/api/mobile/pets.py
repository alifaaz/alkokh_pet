from __future__ import annotations

import functools

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, now_datetime, nowdate

from pet_app.api import guardian_portal
from pet_app.api.mobile.files import MobileFileError, attach_public_image, get_first_uploaded_file
from pet_app.api.mobile.response import error, ok
from pet_app.pet_app.doctype.pet_medical_profile.pet_medical_profile import ensure_pet_medical_profile
from pet_app.pet_app.doctype.preventive_care_record.preventive_care_record import committed_by
from pet_app.utils.guardian_customer import get_guardian_by_user
from pet_app.utils.mortality import apply_pet_visibility_filters


PET_AUTH_ERROR = "auth.wrong_credentials"
PET_NOT_FOUND = "pet.not_found"
PET_REQUEST_INVALID = "pet.request_invalid"
PET_DISABLED = "pet.disabled"
PET_MEDICAL_NOT_FOUND = "pet.medical_record_not_found"
# A record that exists and belongs to the guardian's pet, but is the CLINIC's to change.
# Distinct from `not_found` on purpose: the app shows these rows in the pet's history and a
# client needs to tell "you cannot see this" apart from "you can see it but not edit it", so
# it can hide the edit control rather than the record.
PET_MEDICAL_CLINIC_OWNED = "pet.medical_record_clinic_owned"

PET_READ_FIELDS = (
	"name",
	"pet_name",
	"animal_species",
	"animal_type",
	"breed",
	"status",
	"pet_status",
	"birth_date",
	"registration_date",
	"color",
	"gender",
	"weight",
	"hight",
	"blood_type",
	"play",
	"activity_exercise",
	"food_brand",
	"food_type",
	"description",
	"note",
	"pet_image",
	"is_deceased",
	"death_date",
	"modified",
)

PET_FIELD_ALIASES = {
	"name": "pet_name",
	"pet_name": "pet_name",
	"species": "animal_species",
	"animal_species": "animal_species",
	"type": "animal_type",
	"animal_type": "animal_type",
	"breed": "breed",
	"birth_date": "birth_date",
	"registration_date": "registration_date",
	"color": "color",
	"gender": "gender",
	"weight": "weight",
	"height": "hight",
	"hight": "hight",
	"blood_type": "blood_type",
	"play": "play",
	"activity_exercise": "activity_exercise",
	"food_brand": "food_brand",
	"food_type": "food_type",
	"description": "description",
	"note": "note",
}

PET_REQUIRED_CREATE_FIELDS = (
	"pet_name",
	"animal_species",
	"animal_type",
	"birth_date",
	"weight",
	"gender",
)

# THE EXTERNAL CONTRACT IS UNCHANGED. Both record types still exist, still answer to
# "vaccination" and "deworming", and `vaccine_name` is still accepted on the way in. What
# changed is underneath: one doctype holds both, so `kind` is what separates them instead of
# which table the row came out of, and the one label field is `medication_name`.
#
# `vaccine_name` survives as an ALIAS rather than as a field, so an app that has not been
# rebuilt keeps working. It maps onto `medication_name`, which is the single label field the
# replacement doctype carries for both kinds.
PREVENTIVE_DOCTYPE = "Preventive Care Record"

MEDICAL_RECORD_TYPES = {
	"vaccination": {
		"doctype": PREVENTIVE_DOCTYPE,
		"kind": "Vaccination",
		"summary_field": "medication_name",
		"required_field": "medication_name",
		"fields": (
			"medication_name",
			"vaccine_type",
			"batch_no",
			"administered_on",
			"next_due_date",
			"reminder_enabled",
			"reminder_status",
			"notes",
		),
		"aliases": {"name": "medication_name", "title": "medication_name", "vaccine_name": "medication_name"},
	},
	"deworming": {
		"doctype": PREVENTIVE_DOCTYPE,
		"kind": "Deworming",
		"summary_field": "medication_name",
		"required_field": "medication_name",
		"fields": (
			"medication",
			"medication_name",
			"dose",
			"batch_no",
			"administered_on",
			"next_due_date",
			"reminder_enabled",
			"reminder_status",
			"notes",
		),
		"aliases": {"name": "medication_name", "title": "medication_name"},
	},
}


_KIND_TO_TYPE = {config["kind"]: key for key, config in MEDICAL_RECORD_TYPES.items()}


class MobilePetError(Exception):
	def __init__(self, code: str, message: str, http_status: int = 400):
		super().__init__(message)
		self.code = code
		self.message = message
		self.http_status = http_status


def _mobile_pet_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			return ok(fn(*args, **kwargs))
		except MobilePetError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except MobileFileError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except frappe.PermissionError as exc:
			return error(PET_AUTH_ERROR, cstr(exc) or _("Not permitted"), 401)
		except Exception as exc:
			return error(getattr(exc, "code", None) or exc.__class__.__name__, cstr(exc), 400)

	return wrapper


def _current_guardian() -> str:
	if frappe.session.user == "Guest":
		raise MobilePetError(PET_AUTH_ERROR, _("Authentication required."), 401)
	guardian = get_guardian_by_user(frappe.session.user)
	if not guardian:
		raise MobilePetError(PET_AUTH_ERROR, _("No Guardian is linked to the current user."), 401)
	return guardian.get("name")


def _assert_pet_access(guardian: str, pet: str):
	if not pet:
		raise MobilePetError(PET_REQUEST_INVALID, _("Pet is required."))
	if not frappe.db.exists("PetGuardian", {"guardian_id": guardian, "pet_id": pet}):
		raise MobilePetError(PET_NOT_FOUND, _("Pet was not found."), 404)


def _pet_has_field(fieldname: str) -> bool:
	return bool(frappe.get_meta("Pet").has_field(fieldname))


def _doctype_has_field(doctype: str, fieldname: str) -> bool:
	return bool(frappe.get_meta(doctype).has_field(fieldname))


def _read_fields() -> list[str]:
	return [fieldname for fieldname in PET_READ_FIELDS if fieldname == "name" or _pet_has_field(fieldname)]


def _breed_fields() -> list[str]:
	fields = ["name"]
	for fieldname in ("breed_name", "arabic_name", "animal_species", "animal_type"):
		if _doctype_has_field("Pet Breed", fieldname):
			fields.append(fieldname)
	return fields


def _is_disabled(row) -> bool:
	return cstr(row.get("pet_status")).strip() == "Archived"


def _pet_updates(kwargs, require_name=False) -> dict:
	updates = {}
	for source, target in PET_FIELD_ALIASES.items():
		if source not in kwargs:
			continue
		value = kwargs.get(source)
		if value is None:
			continue
		if target == "pet_name":
			value = cstr(value).strip()
			if not value:
				continue
			if len(value) > 140:
				raise MobilePetError(PET_REQUEST_INVALID, _("Pet name is too long."))
		if _pet_has_field(target):
			updates[target] = value

	if require_name:
		_missing = _missing_required_create_fields(updates)
		if _missing:
			raise MobilePetError(
				PET_REQUEST_INVALID,
				_("Missing required pet fields: {0}.").format(", ".join(_missing)),
			)
	return updates


def _missing_required_create_fields(updates: dict) -> list[str]:
	return [
		fieldname
		for fieldname in PET_REQUIRED_CREATE_FIELDS
		if _is_missing_pet_value(updates.get(fieldname))
	]


def _is_missing_pet_value(value) -> bool:
	if value is None:
		return True
	return cstr(value).strip() == ""


def _pet_from_name(name: str) -> dict:
	row = frappe.db.get_value("Pet", name, _read_fields(), as_dict=True)
	if not row:
		raise MobilePetError(PET_NOT_FOUND, _("Pet was not found."), 404)
	return _pet_payload(row)


def _medical_record_type(record_type=None, doctype=None) -> str:
	value = cstr(record_type or "").strip().lower().replace("-", "_")
	if value in MEDICAL_RECORD_TYPES:
		return value
	doctype = cstr(doctype or "").strip()
	for key, config in MEDICAL_RECORD_TYPES.items():
		if doctype == config["doctype"]:
			return key
	raise MobilePetError(PET_REQUEST_INVALID, _("Medical record type must be vaccination or deworming."))


def _medical_config(record_type: str) -> dict:
	return MEDICAL_RECORD_TYPES[_medical_record_type(record_type)]


def _medical_fields(record_type: str) -> list[str]:
	config = _medical_config(record_type)
	doctype = config["doctype"]
	fields = ["name", "pet", "guardian", "creation", "modified"]
	fields.extend(field for field in ("administered_on", "next_due_date", "reminder_enabled", "reminder_status", "notes") if _doctype_has_field(doctype, field))
	for field in config["fields"]:
		if field not in fields and _doctype_has_field(doctype, field):
			fields.append(field)
	return fields


def _medical_updates(record_type: str, kwargs, *, require_required_field=False) -> dict:
	config = _medical_config(record_type)
	doctype = config["doctype"]
	source = dict(kwargs or {})
	for alias, target in config.get("aliases", {}).items():
		if alias in source and target not in source:
			source[target] = source[alias]

	updates = {}
	for field in config["fields"]:
		if field not in source or not _doctype_has_field(doctype, field):
			continue
		value = source.get(field)
		if value is None:
			continue
		if field in {config["required_field"], "notes", "batch_no", "vaccine_type", "dose", "medication"}:
			value = cstr(value).strip()
		if field == "reminder_enabled":
			value = cint(value)
		updates[field] = value

	if require_required_field and not cstr(updates.get(config["required_field"])).strip():
		raise MobilePetError(PET_REQUEST_INVALID, _("{0} is required.").format(config["required_field"].replace("_", " ").title()))
	if require_required_field and not updates.get("administered_on") and _doctype_has_field(doctype, "administered_on"):
		updates["administered_on"] = nowdate()
	return updates


def _medical_payload(record_type: str, row) -> dict:
	config = _medical_config(record_type)
	summary = row.get(config["summary_field"])
	payload = {
		"id": row.name,
		"name": row.name,
		"type": record_type,
		"source_doctype": config["doctype"],
		"pet": row.get("pet"),
		"guardian": row.get("guardian"),
		"summary": summary,
		"administered_on": cstr(row.get("administered_on") or ""),
		"next_due_date": cstr(row.get("next_due_date") or ""),
		"reminder_enabled": bool(cint(row.get("reminder_enabled"))),
		"reminder_status": row.get("reminder_status"),
		"notes": row.get("notes"),
		"created_at": cstr(row.get("creation") or ""),
		"modified": cstr(row.get("modified") or ""),
	}
	for field in config["fields"]:
		# `field not in payload`, not a blind overwrite. The keys above are deliberately
		# normalised - `administered_on` and `next_due_date` are stringified with cstr() - and
		# this loop used to replace them with the raw `datetime.date` off the row. The list
		# endpoint then sorted on a mix of dates and empty strings and died with
		# "'<' not supported between instances of 'datetime.date' and 'str'".
		#
		# It never showed before because `administered_on` was `reqd` on both doctypes this
		# replaces, so no row could have an empty one and every value compared was a date.
		# The replacement doctype is ordered before it is administered, so the empty case is
		# now real.
		if field in row and field not in payload:
			payload[field] = row.get(field)
	# Echoed back under its old name as well, for an app built against the previous shape.
	if record_type == "vaccination":
		payload.setdefault("vaccine_name", summary)
	return payload


def _find_medical_doc(record_name: str, record_type=None):
	record_name = cstr(record_name).strip()
	if not record_name:
		raise MobilePetError(PET_REQUEST_INVALID, _("Medical record is required."))

	# Both type keys share one table, so "does a row with this name exist in that doctype"
	# can no longer tell them apart - it would match whichever key came first and report a
	# deworming as a vaccination. The row's own `kind` is the answer.
	if not frappe.db.exists(PREVENTIVE_DOCTYPE, record_name):
		raise MobilePetError(PET_MEDICAL_NOT_FOUND, _("Medical record was not found."), 404)
	doc = frappe.get_doc(PREVENTIVE_DOCTYPE, record_name)
	resolved = _KIND_TO_TYPE.get(cstr(doc.get("kind")).strip())
	if not resolved:
		raise MobilePetError(PET_MEDICAL_NOT_FOUND, _("Medical record was not found."), 404)
	if record_type and _medical_record_type(record_type) != resolved:
		# Asked for as the wrong type. Refused rather than quietly returned as the type it
		# actually is: the caller's own state is wrong and silently correcting it hides that.
		raise MobilePetError(PET_MEDICAL_NOT_FOUND, _("Medical record was not found."), 404)
	return resolved, doc


def _assert_medical_record_access(guardian: str, pet: str, doc):
	"""The guardian's own history, and nothing else.

	Owning the pet is not the same as owning the record. This check used to test only that
	the row belonged to the pet and that the guardian could reach the pet - which let a
	guardian edit, or delete, a dose THE CLINIC gave: `medication_name`, `batch_no`,
	`administered_on` and `next_due_date` on a record that was priced, charged and stamped by
	a member of staff, with the clinic worklist then showing the result as fact.

	The line is the same one `PreventiveCareRecord._refuse_edit_when_committed` draws, drawn
	with the same function so the two can never disagree: a record that committed something -
	an item, a charge, a staff stamp, stock - belongs to the clinic. A record that committed
	nothing is what this API exists to maintain.

	Guarded on the SAVED row rather than on anything in the request, and reached before any
	field is applied, so neither an edit nor a delete can get past it.
	"""
	if doc.pet != pet:
		raise MobilePetError(PET_MEDICAL_NOT_FOUND, _("Medical record was not found."), 404)
	_assert_pet_access(guardian, pet)
	if doc.doctype == PREVENTIVE_DOCTYPE and committed_by(doc):
		raise MobilePetError(
			PET_MEDICAL_CLINIC_OWNED,
			_("This dose was recorded by the clinic and cannot be changed here. "
			  "Ask the clinic to correct it."),
			403,
		)


def _approve_mobile_pet(pet: str, guardian: str):
	request_name = frappe.db.get_value(
		"PetAddRequest",
		{"pet_id": pet, "guardian_id": guardian, "status": "Pending"},
		"name",
	)
	if request_name:
		request = frappe.get_doc("PetAddRequest", request_name)
		request.status = "Approved"
		if request.meta.has_field("approved_by"):
			request.approved_by = frappe.session.user
		if request.meta.has_field("approval_date"):
			request.approval_date = now_datetime()
		request.save(ignore_permissions=True)
		return

	if _pet_has_field("pet_status"):
		frappe.db.set_value("Pet", pet, "pet_status", "Approved", update_modified=False)
	if not frappe.db.exists("PetGuardian", {"pet_id": pet, "guardian_id": guardian}):
		frappe.get_doc(
			{
				"doctype": "PetGuardian",
				"pet_id": pet,
				"guardian_id": guardian,
				"role": "primary_owner",
			}
		).insert(ignore_permissions=True)
	ensure_pet_medical_profile(pet, guardian)


def _pet_images(pet: str) -> list[dict]:
	return frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Pet", "attached_to_name": pet, "is_private": 0},
		fields=["name", "file_url", "file_name", "custom_is_default"],
		order_by="custom_is_default desc, creation asc",
		ignore_permissions=True,
	)


def _pet_payload(row) -> dict:
	images = _pet_images(row.name)
	image = row.get("pet_image") or next((img.get("file_url") for img in images), None)
	return {
		"id": row.name,
		"pet_id": row.name,
		"name": row.get("pet_name") or row.name,
		"species": row.get("animal_species"),
		"type": row.get("animal_type"),
		"breed": row.get("breed"),
		"status": row.get("status"),
		"pet_status": row.get("pet_status"),
		"is_disabled": _is_disabled(row),
		"birth_date": cstr(row.get("birth_date") or ""),
		"registration_date": cstr(row.get("registration_date") or ""),
		"color": row.get("color"),
		"gender": row.get("gender"),
		"weight": flt(row.get("weight")),
		"height": flt(row.get("hight")),
		"blood_type": row.get("blood_type"),
		"play": row.get("play"),
		"activity_exercise": row.get("activity_exercise"),
		"food_brand": row.get("food_brand"),
		"food_type": row.get("food_type"),
		"description": row.get("description"),
		"note": row.get("note"),
		"image": image,
		"images": images,
		"is_deceased": bool(cint(row.get("is_deceased"))),
		"death_date": cstr(row.get("death_date") or ""),
	}


def _breed_payload(row) -> dict:
	name = cstr(row.get("name")).strip()
	return {
		"id": name,
		"name": cstr(row.get("breed_name") or name),
		"arabic_name": row.get("arabic_name"),
		"species": row.get("animal_species"),
		"type": row.get("animal_type"),
	}


def _unwrap_guardian_portal(result, key):
	if isinstance(result, dict) and result.get("ok") is True:
		return (result.get("data") or {}).get(key) or []
	if isinstance(result, dict) and result.get("ok") is False:
		errors = result.get("errors") or []
		message = errors[0].get("message") if errors else _("Request failed.")
		raise MobilePetError((result.get("meta") or {}).get("code") or PET_REQUEST_INVALID, message)
	return []


@frappe.whitelist(methods=["GET"])
@_mobile_pet_endpoint
def list_breeds(
	animal_type=None,
	animal_species=None,
	search=None,
	limit=100,
	page_size=None,
	**kwargs,
):
	_current_guardian()
	if not frappe.db.exists("DocType", "Pet Breed"):
		return {"items": [], "total": 0}

	filters = {}
	if _doctype_has_field("Pet Breed", "enabled"):
		filters["enabled"] = 1
	animal_type = cstr(animal_type).strip()
	animal_species = cstr(animal_species).strip()
	search = cstr(search).strip()
	if animal_type and _doctype_has_field("Pet Breed", "animal_type"):
		filters["animal_type"] = animal_type
	if animal_species and _doctype_has_field("Pet Breed", "animal_species"):
		filters["animal_species"] = animal_species
	if search:
		search_field = (
			"breed_name"
			if _doctype_has_field("Pet Breed", "breed_name")
			else "name"
		)
		filters[search_field] = ["like", f"%{search}%"]

	page_length = max(1, min(cint(page_size or limit or 100), 500))
	order_by = (
		"breed_name asc"
		if _doctype_has_field("Pet Breed", "breed_name")
		else "name asc"
	)
	rows = frappe.get_all(
		"Pet Breed",
		filters=filters,
		fields=_breed_fields(),
		order_by=order_by,
		limit_page_length=page_length,
		ignore_permissions=True,
	)
	return {"items": [_breed_payload(row) for row in rows], "total": len(rows)}


@frappe.whitelist(methods=["GET"])
@_mobile_pet_endpoint
def list_pets(limit=20, cursor=0, include_disabled=0, exclude_deceased=0, **kwargs):
	"""The guardian's own pets. Deceased ones are KEPT by default, and that is deliberate.

	This is a guardian looking at their own animals, not a clinician picking one to bill.
	Hiding a pet that died would delete it from its owner's view of their own family, and
	the payload already carries `is_deceased` and `death_date` so the app can render a
	memorial state instead of a bookable row.

	`exclude_deceased=1` is for the flows inside the app that DO create records - booking
	or requesting an appointment - where offering a dead animal is the bug this closes.
	"""
	guardian = _current_guardian()
	limit = max(1, min(cint(limit or 20), 100))
	offset = max(0, cint(cursor or 0))
	linked_pets = frappe.get_all(
		"PetGuardian",
		filters={"guardian_id": guardian},
		pluck="pet_id",
		ignore_permissions=True,
	)
	if not linked_pets:
		return {"items": [], "nextCursor": None, "hasMore": False}

	filters = {"name": ["in", linked_pets]}
	if not cint(include_disabled) and _pet_has_field("pet_status"):
		filters["pet_status"] = ["!=", "Archived"]
	filters = apply_pet_visibility_filters(filters, exclude_deceased=bool(cint(exclude_deceased)))

	rows = frappe.get_all(
		"Pet",
		filters=filters,
		fields=_read_fields(),
		order_by="pet_name asc",
		limit_start=offset,
		limit_page_length=limit + 1,
		ignore_permissions=True,
	)
	has_more = len(rows) > limit
	rows = rows[:limit]
	return {
		"items": [_pet_payload(row) for row in rows],
		"nextCursor": str(offset + limit) if has_more else None,
		"hasMore": has_more,
	}


@frappe.whitelist(methods=["GET"])
@_mobile_pet_endpoint
def get_pet(pet=None, pet_id=None, id=None, **kwargs):
	guardian = _current_guardian()
	name = cstr(pet or pet_id or id).strip()
	_assert_pet_access(guardian, name)
	return _pet_from_name(name)


@frappe.whitelist(methods=["POST"])
@_mobile_pet_endpoint
def create_pet(**kwargs):
	guardian = _current_guardian()
	updates = _pet_updates(kwargs, require_name=True)

	doc = frappe.new_doc("Pet")
	for fieldname, value in updates.items():
		doc.set(fieldname, value)
	if _pet_has_field("requested_by"):
		doc.requested_by = guardian
	if _pet_has_field("pet_status"):
		doc.pet_status = "Pending"
	doc.insert(ignore_permissions=True)
	_approve_mobile_pet(doc.name, guardian)
	return _pet_from_name(doc.name)


@frappe.whitelist(methods=["POST"])
@_mobile_pet_endpoint
def update_pet(pet=None, pet_id=None, id=None, **kwargs):
	guardian = _current_guardian()
	name = cstr(pet or pet_id or id).strip()
	_assert_pet_access(guardian, name)

	doc = frappe.get_doc("Pet", name)
	if _is_disabled(doc):
		raise MobilePetError(PET_DISABLED, _("Disabled pets cannot be updated."))
	updates = _pet_updates(kwargs)
	if not updates:
		raise MobilePetError(PET_REQUEST_INVALID, _("No pet fields were supplied."))

	for fieldname, value in updates.items():
		doc.set(fieldname, value)
	doc.save(ignore_permissions=True)
	return _pet_from_name(doc.name)


@frappe.whitelist(methods=["POST"])
@_mobile_pet_endpoint
def disable_pet(pet=None, pet_id=None, id=None, **kwargs):
	guardian = _current_guardian()
	name = cstr(pet or pet_id or id).strip()
	_assert_pet_access(guardian, name)

	doc = frappe.get_doc("Pet", name)
	if _pet_has_field("pet_status"):
		doc.pet_status = "Archived"
	doc.save(ignore_permissions=True)
	return _pet_from_name(doc.name)


@frappe.whitelist(methods=["POST"])
@_mobile_pet_endpoint
def upload_photo(pet=None, pet_id=None, id=None, **kwargs):
	guardian = _current_guardian()
	name = cstr(pet or pet_id or id).strip()
	_assert_pet_access(guardian, name)
	file_payload = attach_public_image(
		"Pet",
		name,
		"pet_image",
		get_first_uploaded_file(),
		folder="Home/Pet",
	)
	return {"file": file_payload, "pet": _pet_from_name(name)}


@frappe.whitelist(methods=["GET"])
@_mobile_pet_endpoint
def medical_timeline(pet=None, pet_id=None, limit=50, **kwargs):
	guardian = _current_guardian()
	name = cstr(pet or pet_id).strip()
	_assert_pet_access(guardian, name)
	return {
		"items": _unwrap_guardian_portal(
			guardian_portal.get_pet_medical_timeline(pet=name, limit=limit),
			"events",
		)
	}


@frappe.whitelist(methods=["GET"])
@_mobile_pet_endpoint
def documents(pet=None, pet_id=None, **kwargs):
	guardian = _current_guardian()
	name = cstr(pet or pet_id).strip()
	_assert_pet_access(guardian, name)
	return {
		"items": _unwrap_guardian_portal(
			guardian_portal.get_pet_documents(pet=name),
			"documents",
		)
	}


@frappe.whitelist(methods=["GET"])
@_mobile_pet_endpoint
def list_medical_records(pet=None, pet_id=None, record_type=None, type=None, limit=50, cursor=0, **kwargs):
	guardian = _current_guardian()
	name = cstr(pet or pet_id).strip()
	_assert_pet_access(guardian, name)
	limit = max(1, min(cint(limit or 50), 100))
	offset = max(0, cint(cursor or 0))
	types = [_medical_record_type(record_type or type)] if (record_type or type) else list(MEDICAL_RECORD_TYPES)
	items = []
	for type_key in types:
		config = MEDICAL_RECORD_TYPES[type_key]
		rows = frappe.get_all(
			config["doctype"],
			# `kind` is what separates the two types now. Without it every listing would
			# return both and label them all as whichever type was asked for.
			#
			# `Administered` only, because that is what a medical record IS. The doctypes this
			# replaces had no status and could hold nothing else; the replacement is Ordered
			# first, and an ordered-but-not-given dose has not happened yet. It belongs to the
			# clinic's worklist and to the reminder queue, not to the pet's history.
			filters={"pet": name, "kind": config["kind"], "status": "Administered"},
			fields=_medical_fields(type_key),
			order_by="administered_on desc, creation desc",
			ignore_permissions=True,
		)
		items.extend(_medical_payload(type_key, row) for row in rows)

	items.sort(key=lambda item: (item.get("administered_on") or "", item.get("created_at") or ""), reverse=True)
	paged = items[offset : offset + limit + 1]
	has_more = len(paged) > limit
	paged = paged[:limit]
	return {
		"items": paged,
		"nextCursor": str(offset + limit) if has_more else None,
		"hasMore": has_more,
	}


@frappe.whitelist(methods=["POST"])
@_mobile_pet_endpoint
def add_medical_record(pet=None, pet_id=None, record_type=None, type=None, **kwargs):
	guardian = _current_guardian()
	name = cstr(pet or pet_id).strip()
	_assert_pet_access(guardian, name)
	type_key = _medical_record_type(record_type or type or kwargs.get("medical_record_type"))
	config = MEDICAL_RECORD_TYPES[type_key]
	updates = _medical_updates(type_key, kwargs, require_required_field=True)

	from pet_app.utils.branch import get_default_branch

	doc = frappe.get_doc({"doctype": config["doctype"], "pet": name, "guardian": guardian})
	# WHAT was given, stated rather than derived: a guardian recording a dose given elsewhere
	# has no catalogue row to resolve a kind from. See preventive_catalogue.assert_selection.
	doc.kind = config["kind"]
	# ALREADY GIVEN. This records history, it does not order anything: the doctypes this
	# replaces had no status at all and every row they held was a dose that had happened.
	# Created `Administered`, it is also inert - no action is allowed from a terminal status,
	# and with no item_code it can never be billed.
	doc.status = "Administered"
	# A self-reported dose belongs to no clinic, but a NULL branch is visible to every clinic
	# rather than to none, so the site default is stamped instead. Without this the insert is
	# refused outright: a guardian holds no Branch User Permission, which makes them
	# "unrestricted", and an unrestricted user must choose a branch.
	doc.branch = get_default_branch()
	for fieldname, value in updates.items():
		doc.set(fieldname, value)
	doc.insert(ignore_permissions=True)
	row = frappe.db.get_value(config["doctype"], doc.name, _medical_fields(type_key), as_dict=True)
	return _medical_payload(type_key, row)


@frappe.whitelist(methods=["POST", "PUT"])
@_mobile_pet_endpoint
def update_medical_record(pet=None, pet_id=None, record=None, record_id=None, id=None, record_type=None, type=None, **kwargs):
	guardian = _current_guardian()
	name = cstr(pet or pet_id).strip()
	type_key, doc = _find_medical_doc(record or record_id or id, record_type or type)
	_assert_medical_record_access(guardian, name, doc)
	updates = _medical_updates(type_key, kwargs)
	if not updates:
		raise MobilePetError(PET_REQUEST_INVALID, _("No medical record fields were supplied."))

	for fieldname, value in updates.items():
		doc.set(fieldname, value)
	doc.save(ignore_permissions=True)
	row = frappe.db.get_value(doc.doctype, doc.name, _medical_fields(type_key), as_dict=True)
	return _medical_payload(type_key, row)


@frappe.whitelist(methods=["POST", "DELETE"])
@_mobile_pet_endpoint
def delete_medical_record(pet=None, pet_id=None, record=None, record_id=None, id=None, record_type=None, type=None, **kwargs):
	guardian = _current_guardian()
	name = cstr(pet or pet_id).strip()
	type_key, doc = _find_medical_doc(record or record_id or id, record_type or type)
	_assert_medical_record_access(guardian, name, doc)
	frappe.delete_doc(doc.doctype, doc.name, ignore_permissions=True)
	return {"id": doc.name, "type": type_key, "deleted": True}
