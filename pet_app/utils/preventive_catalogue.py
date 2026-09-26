"""What the catalogue says about a vaccination or deworming, resolved in one place.

THE CATALOGUE IS NOT CHANGING. `CareService template`, `Care Service Billing Option` and
`CategoryCareServices` stay exactly as they are, and `Preventive Care Record` reads them
the way `Lab` reads `CareService template` today - item, price, drug and performing branch
come from the master row, not from the execution record. This module is the reader.

WHY A TEMPLATE **OR** A BAND, AND NOT A TEMPLATE ALONE. `Lab.care_service` is `reqd: 1` and
that is right for Lab, because every lab test on this site is a template. Preventive care is
not configured that way and cannot be made to be without reshaping the catalogue, which is
out of scope:

    Vaccination   1 `CareService template` (Rabies -> Rabisin),  0 weight bands
    Deworming     0 `CareService template`,                      2 weight bands

So requiring a template would make deworming unconfigurable, and requiring a band would make
vaccination unconfigurable. Exactly one of the two must be present, either is sufficient, and
`assert_selection` refuses the empty case by name rather than letting a record exist that
nothing can price.

THE BAND WINS WHERE BOTH SPEAK. The band is the specific answer - it is the row the operator
actually picked for this weight - and the template is the general one. That is the same
precedence `resolve_stock_medication` established for the drug, kept here for the item and
the rate too so one rule covers all three rather than three rules that can disagree.

WHAT REFRESHES AND WHAT IS FROZEN. `item_code`, `rate`, `medication` and the label refresh
from the catalogue on every validate, so a price correction reaches an order that has not
been billed yet - the same rule `Lab.set_values_from_care_service` follows. `kind`,
`category` and `performing_branch` are stamped once at insert and never rewritten, because
they answer "what was this and where did it happen", and rewriting them would retroactively
re-describe work that already took place. `snapshot_performing_branch` makes that argument
at length for the branch; it applies unchanged to the other two.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, flt

from pet_app.utils.care_service_category import DEWORMING, VACCINATION, category_kind

CARE_SERVICE_TEMPLATE = "CareService template"
BILLING_OPTION = "Care Service Billing Option"
CATEGORY = "CategoryCareServices"

# The stored `kind` value for each resolved category kind. `category_kind` answers in
# lowercase because it is comparing normalised names; the field stores the label an operator
# reads. Mapped explicitly rather than title-cased, so a future third kind has to be added
# here deliberately instead of appearing on its own.
KIND_LABELS = {
	VACCINATION: "Vaccination",
	DEWORMING: "Deworming",
}


def assert_selection(care_service: str | None, service_option: str | None, kind: str | None = None) -> None:
	"""Refuse a record that says neither WHAT was given nor WHICH KIND it was.

	THREE WAYS TO BE VALID, and the third is not a loophole.

	    care_service    a vaccination from the catalogue - prices and bills
	    service_option  a deworming weight band - prices and bills
	    kind            a dose with no catalogue row at all: history

	THE HISTORY CASE IS NOT NEW, it is the only case the doctypes this replaces ever had.
	`Pet Vaccination Record` and `Pet Deworming Record` had no `care_service`, no `item_code`
	and no `status` - they required pet, a name and a date, and nothing else. A guardian
	recording "the rabies shot she had at the other clinic last spring" has no template to
	point at, no price and no invoice, and refusing that row would break the mobile contract
	(`api.mobile.pets.add_medical_record`) outright rather than improve it.

	Such a record is INERT BY CONSTRUCTION rather than by a new guard:
	`plan_preventive_billing` already refuses a record with no `item_code`, and the mobile
	path creates it `Administered`, from which no action is allowed. It records; it never
	charges.

	Loud and specific either way: the operator is told all three field names, because which
	one applies depends on whether this is a clinic dose or a history entry and the message
	cannot know which.
	"""
	if cstr(care_service).strip() or cstr(service_option).strip() or cstr(kind).strip():
		return

	frappe.throw(
		_(
			"A Preventive Care Record must name a Care Service, a Service Option, or a Kind. "
			"Set {0} for a vaccination, {1} for a deworming weight band, or {2} alone to record "
			"a dose given elsewhere."
		).format(
			frappe.bold(_("Care Service")), frappe.bold(_("Service Option")), frappe.bold(_("Kind"))
		)
	)


def resolve_category(care_service: str | None, service_option: str | None) -> str:
	"""The one `CategoryCareServices` this record belongs to.

	When both a template and a band are given they must agree. A disagreement is a
	configuration error a human has to resolve - silently preferring one would file the
	record under a category its own price came from somewhere else.
	"""
	care_service = cstr(care_service).strip()
	service_option = cstr(service_option).strip()

	from_template = (
		cstr(frappe.db.get_value(CARE_SERVICE_TEMPLATE, care_service, "category_id")).strip()
		if care_service
		else ""
	)
	from_band = (
		cstr(frappe.db.get_value(BILLING_OPTION, service_option, "category_care_services")).strip()
		if service_option
		else ""
	)

	if from_template and from_band and from_template != from_band:
		frappe.throw(
			_(
				"Care Service {0} is in category {1} but Service Option {2} is in category {3}. "
				"They must be in the same category."
			).format(
				frappe.bold(care_service),
				frappe.bold(from_template),
				frappe.bold(service_option),
				frappe.bold(from_band),
			)
		)

	category = from_template or from_band
	if not category:
		frappe.throw(
			_("Neither {0} nor {1} names a category, so this record cannot be classified.").format(
				frappe.bold(care_service or _("Care Service")),
				frappe.bold(service_option or _("Service Option")),
			)
		)
	return category


def resolve_kind(category: str) -> str:
	"""``"Vaccination"`` or ``"Deworming"``, by category NAME.

	Delegated to `utils.care_service_category.category_kind` - the same resolver the rest of
	the app uses - so nothing here can disagree with it about what counts as a vaccination.
	Never a hardcoded docname: `CategoryCareServices-0018` is only this bench's id.

	Throws for every other category. A grooming service reaching this doctype is a routing
	mistake, and filing it as a vaccination would be worse than refusing it.
	"""
	kind = category_kind(category)
	if not kind:
		category_name = cstr(frappe.db.get_value(CATEGORY, category, "category_name")).strip()
		frappe.throw(
			_(
				"Category {0} is not a preventive care category, so it cannot be recorded here. "
				"Preventive Care Record covers Vaccination and Deworming only."
			).format(frappe.bold(category_name or category))
		)
	return KIND_LABELS[kind]


def resolve_medication(service_option: str | None, care_service: str | None) -> str:
	"""The drug this record gives: the band's own, else its template's.

	Moved here verbatim from `utils.care_service_billing.resolve_stock_medication`, which
	this replaces, and its reasoning is unchanged:

	THE BAND WINS, AND THE TEMPLATE IS NOT OPTIONAL. The band is the row the operator picked,
	and it is where a deworming drug is named because a band has no link to a template to
	read one through. The template stays the fallback because it is the only thing making
	vaccination work - CareService-00022 names Rabisin and has no band at all, so a band-only
	resolution would silently stop naming the vial.

	The band half is skipped until `care_service_billing_option_medication` has run, because
	querying a column that does not exist yet would raise on a record that has nothing wrong
	with it. Before that patch every record resolves through its template.
	"""
	service_option = cstr(service_option).strip()
	care_service = cstr(care_service).strip()

	medication = ""
	if service_option and frappe.db.has_column(BILLING_OPTION, "medication"):
		medication = cstr(frappe.db.get_value(BILLING_OPTION, service_option, "medication")).strip()
	if medication:
		return medication

	return cstr(frappe.db.get_value(CARE_SERVICE_TEMPLATE, care_service, "medication")).strip() if care_service else ""


def resolve_item_and_rate(care_service: str | None, service_option: str | None) -> tuple[str, float]:
	"""``(item_code, rate)`` from the band first, then the template.

	Rate is returned as the catalogue states it, INCLUDING zero. A zero price is refused at
	the billing moment, by the biller, naming the row to fix - the same division of labour
	`Lab` uses, where `set_values_from_care_service` requires the field to exist and
	`plan_order_billing` requires it to be positive. Refusing zero here as well would block
	ordering a service whose price has simply not been entered yet.
	"""
	care_service = cstr(care_service).strip()
	service_option = cstr(service_option).strip()

	if service_option:
		band = frappe.db.get_value(
			BILLING_OPTION, service_option, ["item_code", "default_rate"], as_dict=True
		)
		if not band:
			frappe.throw(_("Service Option {0} was not found.").format(frappe.bold(service_option)))
		item_code = cstr(band.item_code).strip()
		if item_code:
			return item_code, flt(band.default_rate)

	if care_service:
		template = frappe.db.get_value(
			CARE_SERVICE_TEMPLATE, care_service, ["item_code", "default_price"], as_dict=True
		)
		if not template:
			frappe.throw(_("Care Service {0} was not found.").format(frappe.bold(care_service)))
		item_code = cstr(template.item_code).strip()
		if item_code:
			return item_code, flt(template.default_price)

	frappe.throw(
		_(
			"Neither {0} nor {1} has an Item Code, so this record cannot be priced or billed. "
			"Set Item Code on the catalogue row before ordering it."
		).format(
			frappe.bold(care_service or _("Care Service")),
			frappe.bold(service_option or _("Service Option")),
		)
	)


def resolve_qty(service_option: str | None) -> float:
	"""How much of the drug this dose consumes, per the band.

	Zero means NOT CONFIGURED, not "deduct nothing" - the rule
	`care_service_billing_option_stock_qty` established and every consumer honours. Returned
	as zero here and interpreted by the stock leg, which refuses to deduct and names the band
	rather than silently issuing a dose that takes nothing off the shelf.

	VACCINATION HAS NO BAND AND SO RESOLVES 0 HERE, AND 0 IS NOT THIS FUNCTION'S FINAL ANSWER
	FOR IT. `CareService template` has no quantity field at all, so there is nothing for this
	to read; the whole-vial default is applied by `PreventiveCareRecord._default_qty`, which
	calls this first and falls back to `BILLED_QTY` when there is no band to answer.

	That split is stated because the previous version of this docstring asserted the default
	as though it already happened ("applied at completion") when nothing applied it - the
	completion path that once did was removed with the old preventive care, and a quantity of
	0 reads as an empty field rather than as a missing rule, so the loss was invisible until
	vaccinations turned up recorded as consuming nothing while their invoices relieved a vial.
	A docstring that promises behaviour is a place that behaviour can go missing from.
	"""
	service_option = cstr(service_option).strip()
	if not service_option or not frappe.db.has_column(BILLING_OPTION, "stock_deduction_qty"):
		return 0.0
	return flt(frappe.db.get_value(BILLING_OPTION, service_option, "stock_deduction_qty"))


def resolve_label(care_service: str | None, service_option: str | None, medication: str | None) -> str:
	"""What to call what was given, for the record and for the invoice line.

	Carries forward the fallback chain from `care_service_clinical_record._administered_label`,
	which this replaces, widened by the two sources that module could not reach: the band
	(it had no band to read) and the Medication row itself.

	Never blank by construction - the caller supplies a last-resort fallback - because both
	record doctypes this replaces required a name field and neither could be saved empty.
	"""
	service_option = cstr(service_option).strip()
	care_service = cstr(care_service).strip()
	medication = cstr(medication).strip()

	candidates = []
	if service_option:
		candidates.append(frappe.db.get_value(BILLING_OPTION, service_option, "service_title"))
	if care_service:
		candidates.append(frappe.db.get_value(CARE_SERVICE_TEMPLATE, care_service, "service_name"))
	if medication:
		candidates.append(frappe.db.get_value("Medication", medication, "medication_name") or medication)

	for value in candidates:
		value = cstr(value).strip()
		if value:
			return value
	return ""
