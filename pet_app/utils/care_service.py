from __future__ import annotations

import frappe
from frappe import _


CARE_SERVICE_TEMPLATE_DOCTYPE = "CareService template"
LEGACY_CARE_SERVICE_DOCTYPE = "CareService"


def get_service_master_doctype() -> str:
	if frappe.db.exists("DocType", CARE_SERVICE_TEMPLATE_DOCTYPE):
		return CARE_SERVICE_TEMPLATE_DOCTYPE

	if frappe.db.exists("DocType", LEGACY_CARE_SERVICE_DOCTYPE) and _legacy_care_service_enabled():
		return LEGACY_CARE_SERVICE_DOCTYPE

	if frappe.db.exists("DocType", LEGACY_CARE_SERVICE_DOCTYPE):
		frappe.throw(
			_(
				"Care Service master is configured as legacy {0}, but this site expects {1}. "
				"Enable pet_app_allow_legacy_care_service only for intentional legacy support."
			).format(frappe.bold(LEGACY_CARE_SERVICE_DOCTYPE), frappe.bold(CARE_SERVICE_TEMPLATE_DOCTYPE))
		)

	frappe.throw(_("Care Service master DocType {0} is missing. Run migrations.").format(frappe.bold(CARE_SERVICE_TEMPLATE_DOCTYPE)))


def get_care_service_doc(care_service: str):
	if not care_service:
		frappe.throw(_("Care Service is required."))

	doctype = get_service_master_doctype()
	if frappe.db.exists(doctype, care_service):
		return frappe.get_cached_doc(doctype, care_service)

	frappe.throw(_("Care Service {0} was not found in {1}.").format(frappe.bold(care_service), frappe.bold(doctype)))


def _legacy_care_service_enabled() -> bool:
	return bool(getattr(frappe.conf, "pet_app_allow_legacy_care_service", False))
