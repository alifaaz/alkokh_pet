"""When a billable item becomes a Sales Invoice line, for the whole clinic.

`Pet App Access Settings.billing_trigger` decides the moment for the four ORDER types -
Lab, Imaging, Procedure and Service. Medication has its own, `medication_billing_trigger`,
because it is not an order: it has no document, no start and no release of its own, and
its quantity stays editable in a way an order's never is. Those are different enough
questions that one answer was making the wrong trade for one of them.

There is still no per-branch override, and no third trigger: a clinic that billed
radiology at order time and labs at release would have two answers to "is this order paid
for" with nothing to distinguish them, and every screen showing billing state would have
to ask which kind it was looking at first. The medication split is the one exception, and
it exists because medication genuinely behaves differently, not because a clinic wanted
finer control.

The four moments, and what each means for an order and for a prescription:

    on_request      the order is created            | the medication is prescribed
    on_start        it moves to In Progress         | first dispense
    on_release      result released / service done  | fully dispensed
    on_visit_close  complete_case runs              | complete_case runs

Defaults differ, and only one of them preserves prior behaviour:

    billing_trigger             on_release   - exactly as the clinic billed before
    medication_billing_trigger  on_request   - a CHANGE; medication used to reach an
                                               invoice only when the visit closed

Read through `get_billing_trigger()` / `get_medication_billing_trigger()` and gate through
`should_bill_now()`. Nothing reads a field directly: the has_field guard below is not
optional, and duplicating it at each call site is how one of them ends up missing it.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, cstr

SETTINGS_DOCTYPE = "Pet App Access Settings"
BILLING_TRIGGER_FIELD = "billing_trigger"
MEDICATION_BILLING_TRIGGER_FIELD = "medication_billing_trigger"
INCLUDE_VISIT_MEDICATION_FIELD = "include_visit_medication_in_boarding"

# On by default: a pet on Treatment terms is being medicated by the facility under a rate
# that already covers it, so billing the prescription again is a double charge. Absent
# means on, for the same reason the field defaults to 1 - but note this differs from the
# billing_trigger default, which preserves prior behaviour. This one CHANGES it.
DEFAULT_INCLUDE_VISIT_MEDICATION = True

ON_REQUEST = "on_request"
ON_START = "on_start"
ON_RELEASE = "on_release"
ON_VISIT_CLOSE = "on_visit_close"

BILLING_TRIGGERS = (ON_REQUEST, ON_START, ON_RELEASE, ON_VISIT_CLOSE)

# The behaviour that predates the setting. Also what a blank field means: the Select
# ships with an empty first option, and an existing site has NULL there until the patch
# runs, so "unset" must resolve to the old behaviour rather than to nothing at all.
DEFAULT_BILLING_TRIGGER = ON_RELEASE

# Medication's own default, and NOT the behaviour that predates it: before these settings
# a prescription reached an invoice only when the visit closed. `on_request` raises the
# charge at prescribe time instead. That is a deliberate change, not a preservation.
DEFAULT_MEDICATION_BILLING_TRIGGER = ON_REQUEST

# Triggers whose charge is raised on one of the per-order paths. `on_visit_close` is
# absent: under it every per-order call site stands down and the charge rides the visit
# to `_create_sales_invoice_for_visit`, exactly as it did before per-order billing.
PER_ORDER_TRIGGERS = frozenset({ON_REQUEST, ON_START, ON_RELEASE})


def get_billing_trigger() -> str:
	"""The configured moment, or `on_release` when unset.

	The has_field guard is checked before the read, not after, for the reason spelled out
	at api/workspace.py:3518 - `get_single_value` *throws* when the field is absent rather
	than returning None. Between deploying this code and running `bench migrate` the field
	does not exist, and without the guard every billing call in the clinic would raise
	instead of falling through to the old behaviour.
	"""
	if not frappe.get_meta(SETTINGS_DOCTYPE).has_field(BILLING_TRIGGER_FIELD):
		return DEFAULT_BILLING_TRIGGER

	value = cstr(frappe.db.get_single_value(SETTINGS_DOCTYPE, BILLING_TRIGGER_FIELD)).strip()
	if value not in BILLING_TRIGGERS:
		# Covers blank (never set, or the patch has not run) and anything a direct database
		# edit put there. An unrecognised value must not silently disable billing.
		return DEFAULT_BILLING_TRIGGER
	return value


def include_visit_medication_in_boarding() -> bool:
	"""Whether the Treatment boarding rate absorbs medication prescribed on a visit.

	Same has_field guard and same reason as `get_billing_trigger`: between deploy and
	migrate the field does not exist and `get_single_value` throws rather than returning
	None.

	Unlike the trigger, "unset" here does NOT mean "as it behaved before". Before this
	setting, visit-prescribed medication was always billed - the split documented at
	api/healthcare/boarding.py:3227 said the rate covers what the facility gives, not what
	a consultation prescribes. Turning this on reverses that for Treatment stays, which is
	a deliberate pricing decision; the field ships on because that is the decision taken.
	"""
	if not frappe.get_meta(SETTINGS_DOCTYPE).has_field(INCLUDE_VISIT_MEDICATION_FIELD):
		return DEFAULT_INCLUDE_VISIT_MEDICATION
	value = frappe.db.get_single_value(SETTINGS_DOCTYPE, INCLUDE_VISIT_MEDICATION_FIELD)
	if value is None:
		return DEFAULT_INCLUDE_VISIT_MEDICATION
	return bool(cint(value))


def get_medication_billing_trigger() -> str:
	"""The configured moment for a prescription, or `on_request` when unset.

	Same has_field guard and same reason as `get_billing_trigger`: `get_single_value`
	throws on a missing field, and between deploying this code and running migrate the
	field does not exist.

	Deliberately does NOT fall back to `get_billing_trigger()` when unset. A site that has
	chosen a trigger for its orders has said nothing about medication, and inheriting would
	make the medication answer change silently the next time somebody edited the order
	trigger. Unset means the medication default, always.
	"""
	if not frappe.get_meta(SETTINGS_DOCTYPE).has_field(MEDICATION_BILLING_TRIGGER_FIELD):
		return DEFAULT_MEDICATION_BILLING_TRIGGER

	value = cstr(
		frappe.db.get_single_value(SETTINGS_DOCTYPE, MEDICATION_BILLING_TRIGGER_FIELD)
	).strip()
	if value not in BILLING_TRIGGERS:
		return DEFAULT_MEDICATION_BILLING_TRIGGER
	return value


def should_bill_now(moment: str, *, trigger: str | None = None) -> bool:
	"""Whether `moment` is when the configured trigger raises the charge.

	`on_release` is ALSO a catch-up point for the two earlier triggers. clinical_state
	allows a release straight from Pending (workflows/clinical_state.py:84), so an order
	billing `on_start` that was released without ever being started would otherwise never
	be charged at all - the charge would silently vanish rather than arrive late. The same
	applies to `on_request` for an order created by a path that does not bill.

	Double-charging is prevented by `plan_order_billing`, which returns None once `billed`
	is set (utils/order_billing.py:119-120), so an order already charged at its real
	trigger plans nothing here. The catch-up is therefore idempotent by construction, not
	by the caller remembering to check.

	`trigger` is the caller's own configured trigger, for a kind of charge that does not
	follow `billing_trigger`. Medication passes `get_medication_billing_trigger()`. Passed
	rather than resolved from the moment, because "which setting governs this charge" is
	the call site's knowledge and this function has no way to infer it.
	"""
	trigger = trigger or get_billing_trigger()
	if trigger == ON_VISIT_CLOSE:
		# Every per-order path stands down. The visit's own close path bills everything.
		return False
	if moment == ON_RELEASE:
		return trigger in PER_ORDER_TRIGGERS
	return trigger == moment


def bills_at_visit_close() -> bool:
	"""Whether the visit close path owns every charge.

	Distinct from "there is nothing left to bill at close": under the per-order triggers a
	visit still closes with medication or manual rows on it that never had an order.
	"""
	return get_billing_trigger() == ON_VISIT_CLOSE
