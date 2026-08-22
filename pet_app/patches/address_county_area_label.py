from __future__ import annotations

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

DOCTYPE = "Address"
FIELDNAME = "county"
LABEL = "Area"
PROPERTY_SETTER = "Address-county-label"


def execute():
	"""Relabels Address.county to "Area". Label only - the fieldname does not move.

	"County" is a UK/US administrative division and means nothing in Iraq. What the field
	actually holds here is the neighbourhood: Karrada, Mansour, Hay Aljihad. "Area" is the
	word for that, and it is the word the mobile app already puts on the input.

	The fieldname stays `county` on purpose. It is on the wire in both directions - the
	Dart SDK sends it in `_addressBody` and reads it back in `MobileAddress.fromJson`, and
	the mobile API accepts it in `_validated_address_fields` and returns it from
	`_address_payload`. Renaming the column would break a working path to change a word
	nobody sees in the payload. The label is the only part a human reads, so the label is
	the only part that changes.

	A Property Setter rather than an edit to the doctype JSON because Address belongs to
	Frappe, not to this app. This is the same reason `address_delivery_notes` reaches
	Address through create_custom_fields.

	Arabic needs no entry in pet_app/translations/ar.csv. Frappe core translates "County"
	to مقاطعة (a province - wrong for a neighbourhood); ERPNext core already translates
	"Area" to منطقة, which is right. Changing the source string moves the lookup onto the
	correct existing translation for free.

	Nothing printed changes. `county` appears in no print format, no report, and not in
	the Iraq address template (the site default), so `address_display` and every document
	that renders an address are untouched. The new label surfaces only in Desk: the Address
	form, Customize Form, list filters, and the Report Builder column picker.

	Idempotent: re-running updates the existing Property Setter's value in place rather
	than erroring on the duplicate name.
	"""
	if not frappe.db.exists("DocType", DOCTYPE):
		return
	if not frappe.get_meta(DOCTYPE).has_field(FIELDNAME):
		return

	if frappe.db.exists("Property Setter", PROPERTY_SETTER):
		frappe.db.set_value("Property Setter", PROPERTY_SETTER, "value", LABEL, update_modified=True)
	else:
		make_property_setter(DOCTYPE, FIELDNAME, "label", LABEL, "Data")

	frappe.clear_cache(doctype=DOCTYPE)
