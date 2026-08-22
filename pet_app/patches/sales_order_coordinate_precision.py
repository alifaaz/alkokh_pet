from __future__ import annotations

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

DOCTYPE = "Sales Order"
FIELDNAMES = ("custom_delivery_latitude", "custom_delivery_longitude")
PRECISION = "6"


def execute():
	"""Gives the delivery coordinates six decimals instead of two.

	Both fields were created with precision "", which does not mean "full precision" - it
	means "inherit System Settings.float_precision", and that is 2 on this site. Two
	decimals of latitude is about 1.1 km in Baghdad, which is a district, not a door.

	What this does and does not protect is worth being exact about, because the obvious
	version of the story is wrong. A server-side save does NOT round these fields: the
	column is decimal(21,9) and `doc.save()` writes what it is given, verified by setting
	33.315241 on the existing order at precision 2 and reading 33.315241 straight back.
	`place_order` is therefore safe as it stands, and it does not use db.set_value - it
	sets the field on the document and inserts.

	The loss happens in Desk. The form renders a Float control at the field's precision,
	so the pin displays as 33.32, and a human who opens the order and saves it - for any
	reason, changing anything - posts that rounded value back. One incurious save turns a
	doorstep into a kilometre. That is also why this matters more now than it did last
	week: `_copy_address_coordinates_to_order` starts populating these fields from the
	guardian's saved address, so there will be many more orders carrying a pin worth
	keeping.

	Property Setter rather than editing the two Custom Field records directly: both are
	exported as fixtures on this app, so either would round-trip, but a Property Setter
	states the change as a deliberate override and leaves the original field definition
	untouched.

	No data migration. The one order that carries a real pin stores 33.3152 / 44.3661 -
	four decimals, already inside six, never rounded. Widening the precision cannot
	change a stored value; it only stops a future Desk save from narrowing it.

	Idempotent: re-running updates each Property Setter's value in place.
	"""
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	meta = frappe.get_meta(DOCTYPE)
	for fieldname in FIELDNAMES:
		if not meta.has_field(fieldname):
			continue
		name = f"{DOCTYPE}-{fieldname}-precision"
		if frappe.db.exists("Property Setter", name):
			frappe.db.set_value("Property Setter", name, "value", PRECISION, update_modified=True)
		else:
			make_property_setter(DOCTYPE, fieldname, "precision", PRECISION, "Select")

	frappe.clear_cache(doctype=DOCTYPE)
