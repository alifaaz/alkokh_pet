"""Which of the two recurring-care categories a care service belongs to, by NAME.

Extracted from utils/care_service_clinical_record.py so the clinical-record writer and the
biller answer this question with the same code rather than two drifting copies. That module
still re-exports the names it always exported, so nothing that imported them has to change.

Resolution is by `CategoryCareServices.category_name`, never by docname.
`CategoryCareServices-0018` is only this bench's id for Vaccination; it differs on any other
bench and changes the moment somebody deletes and recreates the category. A hardcoded
docname would silently stop matching and the feature would go quiet with no error.

Matched whole and normalised, not as a substring: a category named "Vaccination Supplies"
is a stock category, not a vaccination service, and must not be treated as one.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr

VACCINATION = "vaccination"
DEWORMING = "deworming"

CATEGORY_NAMES = {
	VACCINATION: {"vaccination", "vaccinations", "vaccine", "vaccines"},
	DEWORMING: {"deworming", "dewormings", "deworm"},
}


def normalise(value) -> str:
	return cstr(value).strip().lower()


def category_kind(category: str | None) -> str | None:
	"""``"vaccination"`` / ``"deworming"`` / None for every other category."""
	category = cstr(category).strip()
	if not category:
		return None

	category_name = normalise(frappe.db.get_value("CategoryCareServices", category, "category_name"))
	if not category_name:
		return None

	for kind, names in CATEGORY_NAMES.items():
		if category_name in names:
			return kind

	return None
