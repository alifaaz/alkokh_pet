from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FIELDNAMES = ("custom_latitude", "custom_longitude")


def execute():
	"""The delivery map pin, stored on the Address instead of only on the phone.

	The mobile app has always had a pin - the user drops it on a map at checkout - but it
	lived in device storage, so a reinstall or a new handset lost it and the guardian had
	to re-drop it for every saved address. The coordinates that do reach the server today
	are `custom_delivery_latitude` / `custom_delivery_longitude` on Sales Order, captured
	per-order. That is the right place for "where this delivery went" and the wrong place
	for "where this address is": an order is a one-off, an address is reused.

	print_hide = 1 for the same reason as custom_notes. A lat/lng pair is for whoever is
	navigating to the door, not for the postal address printed on an invoice.

	precision = 6 is set explicitly and is the part worth reading twice. Frappe stores a
	Float as decimal(21,9), so the column is never the constraint - but the *field's*
	precision governs what a form round-trip keeps, and an empty precision inherits
	System Settings' float_precision, which is 2 on this site. Two decimals is 1.1 km in
	Baghdad, which is not a door. Six decimals is about 0.1 m and is the usual GPS
	convention. The Sales Order pair was created with precision "" and therefore has this
	defect; it is not fixed here because that is a different doctype and a separate
	decision, but it should be.

	Both fields are Float to match that Sales Order pair, so a later "copy the address
	pin onto the order" step is a straight assignment with no type conversion.

	Idempotent: create_custom_fields(update=True) is a no-op on a site that already has
	the fields, so this is safe to re-run.
	"""
	create_custom_fields(
		{
			"Address": [
				{
					"fieldname": "custom_latitude",
					"label": "Latitude",
					"fieldtype": "Float",
					"precision": "6",
					"insert_after": "custom_notes",
					"description": (
						"Delivery map pin, decimal degrees (-90 to 90). Set together with "
						"Longitude. Not part of the printed address."
					),
					"translatable": 0,
					"permlevel": 0,
					"reqd": 0,
					"hidden": 0,
					"print_hide": 1,
					"module": "Pet App",
				},
				{
					"fieldname": "custom_longitude",
					"label": "Longitude",
					"fieldtype": "Float",
					"precision": "6",
					"insert_after": "custom_latitude",
					"description": (
						"Delivery map pin, decimal degrees (-180 to 180). Set together with "
						"Latitude. Not part of the printed address."
					),
					"translatable": 0,
					"permlevel": 0,
					"reqd": 0,
					"hidden": 0,
					"print_hide": 1,
					"module": "Pet App",
				},
			]
		},
		update=True,
	)
	frappe.clear_cache(doctype="Address")
