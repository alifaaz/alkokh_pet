from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

from frappe.model.document import Document


class PetAppAccessSettings(Document):
	def validate(self):
		self._validate_send_defaults()

	def _validate_send_defaults(self):
		"""Keep the send-default table addressable: known surfaces, one row each.

		A row naming a surface no code reads is a default an operator can set and then
		watch have no effect, which is worse than no row at all - so it is refused here
		rather than stored. Two rows for one surface is refused for the same reason:
		nothing could say which of them wins.

		Imported inside the method. send_targets imports meta_templates, and a
		module-level import in a doctype controller pulls that whole chain in at boot.
		"""
		from pet_app.notifications.send_targets import SEND_SURFACES, registered_surfaces, surface_label

		seen = set()
		for row in self.get("send_defaults") or []:
			key = cstr(row.surface_key).strip()
			if not key:
				frappe.throw(
					_("Row {0}: Surface Key is required.").format(row.idx),
					title=_("Send Defaults"),
				)
			if key not in SEND_SURFACES:
				frappe.throw(
					_(
						"Row {0}: {1} is not a known send surface. Known surfaces: {2}."
					).format(row.idx, key, ", ".join(registered_surfaces())),
					title=_("Send Defaults"),
				)
			if key in seen:
				frappe.throw(
					_("Row {0}: surface {1} already has a default. One row per surface.").format(
						row.idx, key
					),
					title=_("Send Defaults"),
				)
			seen.add(key)

			row.surface_key = key
			row.label = surface_label(key)

			source = cstr(row.template_source).strip()
			row.template_source = source
			# The unused half is cleared rather than left behind, so a row can never carry
			# two answers and leave a reader to pick. A blank source is a blank default -
			# the picker opens with nothing selected - so it clears both.
			if source == "meta":
				row.template_key = None
				if not cstr(row.meta_template).strip():
					frappe.throw(
						_("Row {0}: surface {1} is set to a Meta template but names none.").format(
							row.idx, key
						),
						title=_("Send Defaults"),
					)
			elif source == "local":
				row.meta_template = None
				if not cstr(row.template_key).strip():
					frappe.throw(
						_("Row {0}: surface {1} is set to a local template but names none.").format(
							row.idx, key
						),
						title=_("Send Defaults"),
					)
			else:
				row.template_key = None
				row.meta_template = None
