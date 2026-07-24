from __future__ import annotations

from frappe.model.document import Document

from pet_app.utils.clinical_options import validate_selection_row


class VetVisitClinicalSelection(Document):
	def validate(self):
		validate_selection_row(self)
