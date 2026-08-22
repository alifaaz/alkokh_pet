from __future__ import annotations

import functools

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from pet_app.api.mobile.response import error, ok
from pet_app.utils.guardian_customer import get_guardian_by_user, get_or_create_customer_from_guardian


ADDRESS_AUTH_ERROR = "auth.wrong_credentials"
ADDRESS_NOT_FOUND = "address.not_found"
ADDRESS_REQUEST_INVALID = "address.request_invalid"
DEFAULT_COUNTRY = "Iraq"
DEFAULT_ADDRESS_TYPE = "Shipping"


class MobileAddressError(Exception):
	def __init__(self, code: str, message: str, http_status: int = 400):
		super().__init__(message)
		self.code = code
		self.message = message
		self.http_status = http_status


def _mobile_address_endpoint(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		kwargs.pop("cmd", None)
		try:
			return ok(fn(*args, **kwargs))
		except MobileAddressError as exc:
			return error(exc.code, exc.message, exc.http_status)
		except frappe.PermissionError as exc:
			return error(ADDRESS_AUTH_ERROR, cstr(exc) or _("Not permitted"), 401)
		except Exception as exc:
			return error(getattr(exc, "code", None) or exc.__class__.__name__, cstr(exc), 400)

	return wrapper


def _current_guardian() -> dict:
	if frappe.session.user == "Guest":
		raise MobileAddressError(ADDRESS_AUTH_ERROR, _("Authentication required."), 401)
	guardian = get_guardian_by_user(frappe.session.user)
	if not guardian:
		raise MobileAddressError(ADDRESS_AUTH_ERROR, _("No Guardian is linked to the current user."), 401)
	return guardian


def _guardian_customer(guardian: dict) -> str:
	customer = guardian.get("customer_id")
	if customer and frappe.db.exists("Customer", customer):
		return customer
	return get_or_create_customer_from_guardian(guardian)


def _address_has_field(fieldname: str) -> bool:
	return bool(frappe.get_meta("Address").has_field(fieldname))


def _linked_address_names(customer: str, include_disabled=False) -> list[str]:
	if not customer:
		return []

	filters = ["dl.link_doctype = 'Customer'", "dl.link_name = %(customer)s"]
	if not include_disabled and _address_has_field("disabled"):
		filters.append("COALESCE(a.disabled, 0) = 0")

	return [
		row.name
		for row in frappe.db.sql(
			f"""
			SELECT a.name
			FROM `tabAddress` a
			INNER JOIN `tabDynamic Link` dl
			  ON dl.parent = a.name
			 AND dl.parenttype = 'Address'
			WHERE {" AND ".join(filters)}
			ORDER BY a.is_shipping_address DESC, a.is_primary_address DESC, a.modified DESC
			""",
			{"customer": customer},
			as_dict=True,
		)
	]


def _assert_address_access(address: str, customer: str, include_disabled=True):
	address = cstr(address).strip()
	if not address:
		raise MobileAddressError(ADDRESS_REQUEST_INVALID, _("Address is required."))
	if not frappe.db.exists("Address", address):
		raise MobileAddressError(ADDRESS_NOT_FOUND, _("Address was not found."), 404)
	if not frappe.db.exists(
		"Dynamic Link",
		{
			"parenttype": "Address",
			"parent": address,
			"link_doctype": "Customer",
			"link_name": customer,
		},
	):
		raise MobileAddressError(ADDRESS_NOT_FOUND, _("Address was not found."), 404)
	if not include_disabled and _address_has_field("disabled") and cint(frappe.db.get_value("Address", address, "disabled")):
		raise MobileAddressError(ADDRESS_NOT_FOUND, _("Address was not found."), 404)
	return frappe.get_doc("Address", address)


def assert_customer_address(address: str, customer: str):
	return _assert_address_access(address, customer, include_disabled=False)


def _address_payload(doc) -> dict:
	return {
		"id": doc.name,
		"address_id": doc.name,
		"title": doc.get("address_title"),
		"type": doc.get("address_type"),
		"address_line1": doc.get("address_line1"),
		"address_line2": doc.get("address_line2"),
		"city": doc.get("city"),
		"county": doc.get("county"),
		"state": doc.get("state"),
		"country": doc.get("country"),
		"pincode": doc.get("pincode"),
		"phone": doc.get("phone"),
		"email_id": doc.get("email_id"),
		"notes": doc.get("custom_notes"),
		"latitude": _coordinate_value(doc, "latitude"),
		"longitude": _coordinate_value(doc, "longitude"),
		"is_default": bool(cint(doc.get("is_shipping_address")) or cint(doc.get("is_primary_address"))),
		"is_shipping_address": bool(cint(doc.get("is_shipping_address"))),
		"is_primary_address": bool(cint(doc.get("is_primary_address"))),
		"is_disabled": bool(cint(doc.get("disabled"))) if _address_has_field("disabled") else False,
		"display": doc.get_display() if hasattr(doc, "get_display") else cstr(doc.get("address_line1")),
	}


def _validated_address_fields(kwargs, require_required=False, apply_defaults=False) -> dict:
	fields = {}
	for fieldname in (
		"address_title",
		"address_type",
		"address_line1",
		"address_line2",
		"city",
		"county",
		"state",
		"country",
		"pincode",
		"phone",
		"email_id",
		"custom_notes",
	):
		if fieldname in kwargs and kwargs.get(fieldname) is not None and _address_has_field(fieldname):
			fields[fieldname] = cstr(kwargs.get(fieldname)).strip() or None

	if "title" in kwargs and kwargs.get("title") is not None and _address_has_field("address_title"):
		fields["address_title"] = cstr(kwargs.get("title")).strip() or None

	if "notes" in kwargs and kwargs.get("notes") is not None and _address_has_field("custom_notes"):
		fields["custom_notes"] = cstr(kwargs.get("notes")).strip() or None

	fields.update(_validated_coordinates(kwargs))

	if apply_defaults:
		fields["address_title"] = fields.get("address_title") or _("Delivery Address")
		fields["address_type"] = fields.get("address_type") or DEFAULT_ADDRESS_TYPE
		fields["country"] = fields.get("country") or DEFAULT_COUNTRY

	if require_required:
		if not fields.get("address_line1"):
			raise MobileAddressError(ADDRESS_REQUEST_INVALID, _("Address line 1 is required."))
		if not fields.get("city"):
			raise MobileAddressError(ADDRESS_REQUEST_INVALID, _("City is required."))
	return fields


COORDINATE_FIELDS = {"latitude": "custom_latitude", "longitude": "custom_longitude"}
COORDINATE_BOUNDS = {"latitude": 90.0, "longitude": 180.0}


def _coordinate_value(doc, key: str):
	"""None rather than 0.0 when no pin is set.

	Frappe's Float column is NOT NULL DEFAULT 0, so an address that has never been
	pinned reads back as 0.0 - a valid-looking coordinate in the Gulf of Guinea. The
	pair is written together and cleared together, so (0, 0) can only mean "unset"
	here, and reporting it as null keeps the client from drawing a marker at sea.
	"""
	fieldname = COORDINATE_FIELDS[key]
	if not _address_has_field(fieldname):
		return None
	lat = flt(doc.get("custom_latitude"))
	lng = flt(doc.get("custom_longitude"))
	if not lat and not lng:
		return None
	return flt(doc.get(fieldname))


def _validated_coordinates(kwargs) -> dict:
	"""Latitude and longitude, validated as a pair.

	A lone coordinate is not a partial pin, it is a broken one, so sending one without
	the other is refused rather than half-applied. Sending neither leaves whatever is
	stored alone, which is what makes a partial update of some other field safe. Sending
	both empty clears the pin.
	"""
	present = {key: kwargs.get(key) for key in COORDINATE_FIELDS if key in kwargs}
	if not present:
		return {}
	if not all(_address_has_field(fieldname) for fieldname in COORDINATE_FIELDS.values()):
		return {}

	if len(present) != len(COORDINATE_FIELDS):
		raise MobileAddressError(
			ADDRESS_REQUEST_INVALID, _("Latitude and longitude must be sent together.")
		)

	blank = {key: cstr(value).strip() == "" for key, value in present.items()}
	if all(blank.values()):
		return {fieldname: None for fieldname in COORDINATE_FIELDS.values()}
	if any(blank.values()):
		raise MobileAddressError(
			ADDRESS_REQUEST_INVALID,
			_("Latitude and longitude must be cleared together."),
		)

	fields = {}
	for key, value in present.items():
		bound = COORDINATE_BOUNDS[key]
		try:
			number = float(cstr(value).strip())
		except (TypeError, ValueError):
			raise MobileAddressError(
				ADDRESS_REQUEST_INVALID,
				_("{0} must be a number between {1} and {2}.").format(key.title(), -bound, bound),
			) from None
		if number != number or number in (float("inf"), float("-inf")) or abs(number) > bound:
			raise MobileAddressError(
				ADDRESS_REQUEST_INVALID,
				_("{0} must be a number between {1} and {2}.").format(key.title(), -bound, bound),
			)
		fields[COORDINATE_FIELDS[key]] = number
	return fields


def _sync_guardian_default_address(guardian: dict, doc):
	updates = {}
	if doc.get("address_line1") is not None:
		updates["address_line1"] = doc.get("address_line1")
	if doc.get("city") is not None:
		updates["city"] = doc.get("city")
	if doc.get("country") is not None:
		updates["country"] = doc.get("country")
	if updates:
		frappe.db.set_value("Guardian", guardian.get("name"), updates, update_modified=False)


def _set_default_for_customer(address: str, customer: str, guardian: dict | None = None):
	names = _linked_address_names(customer, include_disabled=True)
	for name in names:
		updates = {}
		if _address_has_field("is_shipping_address"):
			updates["is_shipping_address"] = 1 if name == address else 0
		if _address_has_field("is_primary_address"):
			updates["is_primary_address"] = 1 if name == address else 0
		if updates:
			frappe.db.set_value("Address", name, updates, update_modified=False)

	if guardian:
		_sync_guardian_default_address(guardian, frappe.get_doc("Address", address))


def _promote_default_if_needed(customer: str, guardian: dict | None = None):
	active_names = _linked_address_names(customer)
	if not active_names:
		return None
	default_name = frappe.db.get_value(
		"Address",
		{
			"name": ["in", active_names],
			"is_shipping_address": 1,
		},
		"name",
	)
	if not default_name:
		default_name = active_names[0]
		_set_default_for_customer(default_name, customer, guardian=guardian)
	return default_name


@frappe.whitelist(methods=["GET"])
@_mobile_address_endpoint
def list_addresses(include_disabled=0, **kwargs):
	guardian = _current_guardian()
	customer = _guardian_customer(guardian)
	names = _linked_address_names(customer, include_disabled=cint(include_disabled))
	return {
		"items": [_address_payload(frappe.get_doc("Address", name)) for name in names],
		"nextCursor": None,
		"hasMore": False,
	}


@frappe.whitelist(methods=["GET"])
@_mobile_address_endpoint
def get_address(address=None, address_id=None, id=None, **kwargs):
	guardian = _current_guardian()
	customer = _guardian_customer(guardian)
	doc = _assert_address_access(address or address_id or id, customer)
	return _address_payload(doc)


@frappe.whitelist(methods=["POST"])
@_mobile_address_endpoint
def create_address(is_default=0, **kwargs):
	guardian = _current_guardian()
	customer = _guardian_customer(guardian)
	fields = _validated_address_fields(kwargs, require_required=True, apply_defaults=True)
	had_addresses = bool(_linked_address_names(customer))

	doc = frappe.get_doc(
		{
			"doctype": "Address",
			**fields,
			"is_shipping_address": 0,
			"is_primary_address": 0,
			"links": [{"link_doctype": "Customer", "link_name": customer}],
		}
	)
	doc.insert(ignore_permissions=True)

	if cint(is_default) or not had_addresses:
		_set_default_for_customer(doc.name, customer, guardian=guardian)
		doc.reload()

	return _address_payload(doc)


@frappe.whitelist(methods=["POST", "PUT", "PATCH"])
@_mobile_address_endpoint
def update_address(address=None, address_id=None, id=None, is_default=None, **kwargs):
	guardian = _current_guardian()
	customer = _guardian_customer(guardian)
	doc = _assert_address_access(address or address_id or id, customer)
	if _address_has_field("disabled") and cint(doc.get("disabled")):
		raise MobileAddressError(ADDRESS_NOT_FOUND, _("Address was not found."), 404)

	fields = _validated_address_fields(kwargs)
	if not fields and is_default is None:
		raise MobileAddressError(ADDRESS_REQUEST_INVALID, _("No address fields were supplied."))

	for fieldname, value in fields.items():
		doc.set(fieldname, value)
	doc.save(ignore_permissions=True)
	if is_default is not None and cint(is_default):
		_set_default_for_customer(doc.name, customer, guardian=guardian)
		doc.reload()
	return _address_payload(doc)


@frappe.whitelist(methods=["POST"])
@_mobile_address_endpoint
def set_default(address=None, address_id=None, id=None, **kwargs):
	guardian = _current_guardian()
	customer = _guardian_customer(guardian)
	doc = _assert_address_access(address or address_id or id, customer, include_disabled=False)
	_set_default_for_customer(doc.name, customer, guardian=guardian)
	doc.reload()
	return _address_payload(doc)


@frappe.whitelist(methods=["POST", "DELETE"])
@_mobile_address_endpoint
def delete_address(address=None, address_id=None, id=None, **kwargs):
	guardian = _current_guardian()
	customer = _guardian_customer(guardian)
	doc = _assert_address_access(address or address_id or id, customer)
	was_default = bool(cint(doc.get("is_shipping_address")) or cint(doc.get("is_primary_address")))
	if _address_has_field("disabled"):
		doc.disabled = 1
	if _address_has_field("is_shipping_address"):
		doc.is_shipping_address = 0
	if _address_has_field("is_primary_address"):
		doc.is_primary_address = 0
	doc.save(ignore_permissions=True)
	if was_default:
		_promote_default_if_needed(customer, guardian=guardian)
	return {"id": doc.name, "is_disabled": True}


@frappe.whitelist(methods=["GET"])
@_mobile_address_endpoint
def cities(**kwargs):
	return {
		"items": [
			{"id": "baghdad", "name": "Baghdad", "country": DEFAULT_COUNTRY},
			{"id": "basra", "name": "Basra", "country": DEFAULT_COUNTRY},
			{"id": "erbil", "name": "Erbil", "country": DEFAULT_COUNTRY},
			{"id": "najaf", "name": "Najaf", "country": DEFAULT_COUNTRY},
			{"id": "karbala", "name": "Karbala", "country": DEFAULT_COUNTRY},
			{"id": "mosul", "name": "Mosul", "country": DEFAULT_COUNTRY},
		]
	}


@frappe.whitelist(methods=["GET"])
@_mobile_address_endpoint
def reverse(lat=None, lng=None, **kwargs):
	if lat in (None, "") or lng in (None, ""):
		raise MobileAddressError(ADDRESS_REQUEST_INVALID, _("Latitude and longitude are required."))
	return {
		"latitude": lat,
		"longitude": lng,
		"country": DEFAULT_COUNTRY,
		"city": None,
		"address_line1": None,
		"provider": None,
	}
