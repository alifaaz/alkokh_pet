# Copyright (c) 2026, solvers and contributors
# For license information, please see license.txt

"""A vaccination or deworming that was ordered, and then given.

WHAT THIS REPLACES. `Pet Vaccination Record`, `Pet Deworming Record`, and the use of
`PetCareService` as the transaction row for preventive care. Those three between them
required a clinical-record writer to translate a completed service into a record, joined
the two halves with a LIKE over a provenance marker in free text, and shared a completion
path with grooming and bathing. This is one record with one lifecycle, and the join is a
Link.

MODELLED ON `Lab`, WITH THREE DELIBERATE DEVIATIONS. Everything else - the visit/source
routing, the catalogue read for item and rate, the set-once performing branch, the read-only
billing stamps, the status ladder - is Lab's shape, because Lab is the working template for
"an execution record that reads a catalogue and bills".

  1. `visit` IS OPTIONAL, AND SO IS `source_name`. Lab requires one or the other:
     `Lab.validate` branches to `validate_source()` whenever there is no visit, and that
     refuses a record with no source. A preventive dose is routinely given straight on a
     pet's record with no visit and nothing that ordered it - the operator IS the origin -
     and on this site that is the majority case: 28 of the 29 existing preventive rows have
     no visit. So a record with neither is legal here, and `pet` alone is the floor. Lab's
     `mandatory_depends_on` on `visit` is deliberately NOT copied for the same reason.

  2. `care_service` IS NOT `reqd`. Lab has it as `reqd: 1`, which is right for Lab. It
     cannot be right here: the Deworming category has zero `CareService template` rows and
     is configured entirely as weight bands, while Vaccination has one template and zero
     bands. Requiring a template would make deworming unconfigurable and requiring a band
     would make vaccination unconfigurable, and reshaping the catalogue to remove that
     asymmetry is out of scope. `utils.preventive_catalogue.assert_selection` enforces the
     real rule instead: at least one of the two, named by field, refused loudly when absent.

  3. `rate` STOPS REFRESHING ONCE BILLED. `Lab.set_values_from_care_service` rewrites
     `item_code` and `rate` from the catalogue on every validate, unconditionally. That is
     correct while an order is still unbilled - a price correction should reach it - and
     wrong afterwards, because the record would then disagree with the invoice line that
     was actually raised against it. Here the refresh is gated on `billed`.

WHAT THIS FILE DOES NOT DO. It does not bill, it does not move stock, and it does not
transition itself. Those are the completion path's job (`administer_preventive`), for the
reason `order_billing`'s module docstring gives: a record that cannot be billed must not
reach `Administered`, so the decision has to be made before the save, not inside it.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, flt, getdate, nowdate

from pet_app.utils.branch import snapshot_performing_branch
# The quantity the invoice bills for one catalogue-backed dose. Imported rather than
# restated as a literal `1`, because `plan_preventive_stock` refuses any dose whose `qty`
# disagrees with it - two copies of that number would be a check comparing a constant
# against a stale duplicate of itself.
from pet_app.utils.preventive_billing import BILLED_QTY
from pet_app.utils.care_plan_links import assert_no_active_plan_items_linked_to
from pet_app.utils.preventive_catalogue import (
	assert_selection,
	resolve_category,
	resolve_item_and_rate,
	resolve_kind,
	resolve_label,
	resolve_medication,
	resolve_qty,
)
from pet_app.utils.visit_billing import (
	BILLED_VISIT_LOCK_MESSAGE,
	STRICT_MODE,
	cancel_boarding_billable_item_by_link,
	cancel_visit_billable_item_by_link,
	sync_clinical_record_billable_item,
)
from pet_app.workflows import clinical_state


# What this order is called on a `Pet Billable Item` row and in `order_billing`. One value,
# defined once, because the biller and the canceller must agree or a cancellation silently
# fails to find the row it is meant to reverse.
ITEM_TYPE = "Preventive"

# THE MARKS A DOSE LEAVES ON THE WORLD OUTSIDE ITSELF.
#
# A record carrying any one of these did something that cannot be taken back by editing the
# row: it was priced against the catalogue, it raised a charge, a member of staff stamped it
# given or cancelled, or it moved goods. A record carrying none of them has only ever
# DESCRIBED something - a guardian typing in a rabies shot their previous vet gave - and
# describing is editable, because correcting the description is the whole point of it.
#
# That distinction, not `status`, is what `_refuse_edit_when_committed` gates on. Status alone
# would be the obvious rule and it is the wrong one: guardian history is created `Administered`
# (see `mobile.pets.add_medical_record`), so freezing on terminal status would freeze the one
# kind of record that exists to be corrected, and section 5.5's mobile API would need an
# exception flag carved through it to keep working. There is no flag. There is a predicate.
#
# Read from the SAVED row, never from the incoming one - otherwise clearing `item_code` in the
# same request would be the way past the gate.
COMMITMENT_MARKERS = (
	# Priced against the catalogue: this dose can be, or has been, billed.
	"item_code",
	"billed",
	"sales_invoice",
	# A member of staff stamped it. `administered_by` is written only by
	# `api.preventive_care._update_preventive_status_atomic`, never by an insert - which is
	# precisely why guardian history does not carry it.
	"administered_by",
	"administered_at",
	"cancelled_by",
	"cancelled_at",
	# Always empty on this path today (administration posts no Material Issue - see the
	# contract, section 1.2). Listed so that a future leg which DOES move goods freezes the
	# record it moved them for, without anyone having to remember to come back here.
	"stock_entry",
	"stock_issued_qty",
)


def committed_by(doc) -> list[str]:
	"""Which commitment markers this record carries. Empty means nothing was committed."""
	return [field for field in COMMITMENT_MARKERS if doc and doc.get(field)]


class PreventiveCareRecord(Document):
	def validate(self):
		# FIRST, before anything else reads or rewrites the row: a frozen record is refused
		# before the catalogue is re-applied, not after.
		self._refuse_edit_when_committed()
		self._validate_plan_item_links_before_detach()
		self._resolve_origin()
		self._require_pet()
		self._set_guardian_from_pet()
		# After the origin is settled, so `care_service`/`service_option` are final before
		# the catalogue is read through them.
		self._apply_catalogue()
		# After the catalogue, so `kind` is settled before the recurrence rule is applied.
		self._validate_next_due_date()
		# Set once: see snapshot_performing_branch.
		snapshot_performing_branch(self, care_service=self.care_service)
		clinical_state.validate_document_transition(self)

	def _refuse_edit_when_committed(self):
		"""A dose that committed something is closed once it reaches a terminal status.

		THE HOLE THIS CLOSES WAS LIVE. `read_only` on a DocType field governs the Desk form
		and nothing else - a REST write ignores it - and the only other guard on this doctype,
		`clinical_state.validate_document_transition`, returns early unless `status` itself
		changes. So `frappe.client.save` and `frappe.client.set_value` could rewrite an
		`Administered` dose freely, including clearing `billed` and `sales_invoice`, the two
		fields that record that a guardian was charged. 18 roles hold write on this doctype.
		The status ladder was never the protection anyone assumed it was; this is.

		KEYED ON THE STATUS BEFORE THE SAVE, which is what lets the lifecycle keep working.
		`administer_preventive` saves the record with `Administered` already set on it, and at
		that moment the SAVED row still says `Ordered` - a non-terminal status - so the
		transition is allowed through and every later edit is not. A freeze reading
		`self.status` instead would refuse the very save that legitimately ends the lifecycle.
		"""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before or not clinical_state.is_terminal_status(cstr(before.get("status")), self.doctype):
			return
		markers = committed_by(before)
		if not markers:
			# Nothing was committed - guardian history, which exists to be corrected. The
			# mobile API edits these and must keep working.
			return
		frappe.throw(
			_(
				"{0} is {1} and can no longer be edited: it already {2}. "
				"Record a correction as a new dose, or ask a System Manager."
			).format(
				frappe.bold(self.name),
				frappe.bold(_(cstr(before.get("status")))),
				_("carries") + " " + ", ".join(frappe.bold(field) for field in markers),
			),
			title=_("Dose is closed"),
		)

	def after_insert(self):
		# Visit-linked doses bill onto the Vet Visit, exactly as a visit-linked Lab does.
		# Source-linked ones (ordered from a Pet Boarding, say) are billed by their own flow,
		# and a dose with neither is billed on administration by `utils.preventive_billing`.
		if self.visit and self.status != "Cancelled":
			sync_clinical_record_billable_item(self, ITEM_TYPE)

	def on_update(self):
		# Re-entrancy guard: this saves the visit, whose own controller can save back.
		if self.flags.get("syncing_preventive_visit_side_effects"):
			return
		self.flags.syncing_preventive_visit_side_effects = True
		try:
			if self.status == "Cancelled":
				self._cancel_linked_billable_item()
				self._sync_visit_order_row("Cancelled")
				return
			if self.visit:
				visit = sync_clinical_record_billable_item(self, ITEM_TYPE, save=False)
				self._sync_visit_order_row(self._visit_order_status(), visit=visit, save=False)
				visit.flags.ignore_billing_lock = True
				visit.save(ignore_permissions=True)
		finally:
			self.flags.syncing_preventive_visit_side_effects = False

	def _visit_order_status(self) -> str:
		"""This record's status, expressed in the Visit Order row's vocabulary."""
		status = cstr(self.status).strip()
		if status == "Administered":
			return "Completed"
		if status == "Cancelled":
			return "Cancelled"
		if status == "Ordered":
			return "Ordered"
		return "In Progress" if status else "Draft"

	def _sync_visit_order_row(self, status: str, *, visit=None, save: bool = True):
		if not self.visit:
			return False
		from pet_app.api.diagnostics import _sync_order_status

		return _sync_order_status(self, status, visit=visit, save=save)

	def _cancel_linked_billable_item(self):
		if self.visit:
			cancel_visit_billable_item_by_link(
				self.visit,
				linked_service_id=f"{self.doctype}::{self.name}",
				linked_doctype=self.doctype,
				linked_name=self.name,
				order_id=self.get("order_id"),
				item_type=ITEM_TYPE,
			)
		elif self.source_doctype == "Pet Boarding":
			cancel_boarding_billable_item_by_link(
				self.source_name,
				linked_service_id=f"{self.doctype}::{self.name}",
				linked_doctype=self.doctype,
				linked_name=self.name,
				order_id=self.get("order_id"),
				item_type=ITEM_TYPE,
			)

	def on_trash(self):
		assert_no_active_plan_items_linked_to(self.doctype, self.name, action="delete")

	# ── origin ────────────────────────────────────────────────────────────────

	def _resolve_origin(self):
		"""Where this record came from: a visit, another document, or nobody.

		All three are legal. Only the first two carry values worth copying down, and only
		the first one can be locked by an invoice.
		"""
		if self.visit:
			self._set_values_from_visit()
		elif self.source_doctype or self.source_name:
			self._validate_source()

	def _set_values_from_visit(self):
		visit = frappe.db.get_value(
			"Vet Visit",
			self.visit,
			["animal_patient", "guardian", "doctor", "billed", "sales_invoice"],
			as_dict=True,
		)
		if not visit:
			frappe.throw(_("Vet Visit {0} was not found.").format(frappe.bold(self.visit)))

		# Same escape hatch Lab uses, and for the same reason: cancelling an order on an
		# already-billed visit is the one edit that must still be possible.
		allow_cancel = self.status == "Cancelled" and self.flags.get("allow_billed_visit_cancellation")
		if (visit.sales_invoice or visit.billed) and not allow_cancel:
			frappe.throw(_(BILLED_VISIT_LOCK_MESSAGE))

		if not self.pet:
			self.pet = visit.animal_patient
		elif visit.animal_patient and self.pet != visit.animal_patient:
			frappe.throw(_("Preventive Care Record pet must match the linked Vet Visit pet."))

		if not self.guardian and visit.guardian:
			self.guardian = visit.guardian

		# `doctor` is the ORDERING practitioner, which on a visit-sourced record is the
		# visit's own. `provider` - who gave the dose - is deliberately not touched here;
		# it is set when the record is started, by whoever started it.
		if not self.doctor and visit.doctor:
			self.doctor = visit.doctor
		elif STRICT_MODE and visit.doctor and self.doctor != visit.doctor:
			frappe.throw(_("Preventive Care Record practitioner must match the linked Vet Visit practitioner."))

	def _validate_source(self):
		if not (self.source_doctype and self.source_name):
			frappe.throw(
				_("A source record needs both {0} and {1}.").format(
					frappe.bold(_("Source Doctype")), frappe.bold(_("Source Name"))
				)
			)
		if not frappe.db.exists(self.source_doctype, self.source_name):
			frappe.throw(
				_("Source {0} {1} was not found.").format(
					frappe.bold(self.source_doctype), frappe.bold(self.source_name)
				)
			)

	def _require_pet(self):
		"""The floor, restated in code as well as in the schema.

		`reqd: 1` on the field is bypassed by `insert(ignore_mandatory=True)`, which several
		server-side paths use. A preventive record with no pet is unreachable from every
		screen that reads it and unfixable afterwards, so it is refused here too.
		"""
		if not cstr(self.pet).strip():
			frappe.throw(_("Pet is required on a Preventive Care Record."))

	def _set_guardian_from_pet(self):
		"""The primary owner, else any linked guardian. Same resolution PetCareService uses."""
		if self.guardian or not self.pet:
			return
		self.guardian = frappe.db.get_value(
			"PetGuardian", {"pet_id": self.pet, "role": "primary_owner"}, "guardian_id"
		) or frappe.db.get_value("PetGuardian", {"pet_id": self.pet}, "guardian_id")

	# ── catalogue ─────────────────────────────────────────────────────────────

	def _apply_catalogue(self):
		"""Read the catalogue. Refresh what should follow it; leave what was decided.

		The split matters and is the same one `snapshot_performing_branch` documents:

		  REFRESHED while unbilled - `item_code`, `rate`. These follow the catalogue so a
		  price correction reaches an order that has not been charged yet.

		  FROZEN at insert - `kind`, `category`. These say what this dose WAS. Rewriting them
		  after a category is renamed would retroactively re-describe work already done.

		  DEFAULTED ONCE, then left - `medication`, `qty`, `medication_name`. The catalogue
		  supplies the starting value and the operator may override it: a substituted vial or
		  a deliberate half dose is a clinical decision, and silently replacing it on the next
		  save would discard it without telling anybody.
		"""
		assert_selection(self.care_service, self.service_option, self.kind)

		if cstr(self.care_service).strip() or cstr(self.service_option).strip():
			self._apply_catalogue_row()
		else:
			# History: a dose given elsewhere, with no catalogue row to price it. `kind` came
			# from the caller (assert_selection guarantees one of the three) and there is
			# nothing to resolve - no category, no item, no rate. See assert_selection.
			pass

		if not self.medication:
			self.medication = resolve_medication(self.service_option, self.care_service) or None

		if not flt(self.qty):
			self.qty = self._default_qty()

		if not cstr(self.medication_name).strip():
			self.medication_name = (
				resolve_label(self.care_service, self.service_option, self.medication)
				or cstr(self.item_code).strip()
				or cstr(self.name).strip()
			)

	def _default_qty(self) -> float:
		"""How much of the consumable this dose claims, when nobody has said.

		THREE ANSWERS, BECAUSE THERE ARE THREE KINDS OF DOSE, and the difference is which
		thing is authoritative about the quantity.

		  A BAND says so itself. `stock_deduction_qty` is the configured consumption for that
		  weight band, and it is taken as given - INCLUDING when it is 0. A band at 0 has not
		  been configured, and `preventive_billing.plan_preventive_stock` refuses to administer
		  against it and names the band to fix. Defaulting it to a whole unit here would paper
		  over exactly the setup error that refusal exists to surface.

		  A TEMPLATE CANNOT SAY. `CareService template` has no quantity field at all - there is
		  nowhere to configure one - so a vaccination is one whole vial, which is what the
		  invoice bills for it either way. This is the default `preventive_catalogue.resolve_qty`
		  has always documented ("its one whole vial is the deliberate default applied at
		  completion") and which nothing actually applied: the completion path that once did was
		  removed with the rest of the old preventive care, and its loss was silent because a
		  quantity of 0 reads as an empty field rather than as a missing rule. The result was a
		  vaccination recorded as consuming nothing while its invoice relieved a vial.

		  HISTORY HAS NOTHING TO AGREE WITH. A dose given elsewhere carries no item and can
		  never be billed, so there is no invoice line for a quantity to match and 0 is the
		  honest answer rather than a gap.
		"""
		if cstr(self.service_option).strip():
			return resolve_qty(self.service_option)
		if cstr(self.item_code).strip():
			return flt(BILLED_QTY)
		return 0.0

	def _apply_catalogue_row(self):
		"""Resolve and stamp everything a catalogue-backed dose takes from its master row."""
		category = resolve_category(self.care_service, self.service_option)
		if not self.category:
			self.category = category
		elif self.category != category:
			frappe.throw(
				_(
					"This record was filed under category {0}, but its catalogue rows now resolve to {1}. "
					"A recorded dose cannot change category; create a new record instead."
				).format(frappe.bold(self.category), frappe.bold(category))
			)

		resolved_kind = resolve_kind(self.category)
		if not self.kind:
			self.kind = resolved_kind
		elif self.kind != resolved_kind:
			# A caller that states a kind AND names a catalogue row must agree with it. The
			# catalogue is the authority; a disagreement is a mistake worth surfacing rather
			# than silently overriding in either direction.
			frappe.throw(
				_("This record is marked {0}, but its catalogue rows resolve to {1}.").format(
					frappe.bold(self.kind), frappe.bold(resolved_kind)
				)
			)

		if not self.billed:
			self.item_code, self.rate = resolve_item_and_rate(self.care_service, self.service_option)

	# ── recurrence ────────────────────────────────────────────────────────────

	def _validate_next_due_date(self):
		"""Refuse a next dose date that is not after the day this dose is given.

		Carried forward from `PetCareService._validate_next_due_date`, which this replaces,
		with one simplification: that gate existed because the record was written at
		completion by a writer that swallowed the record's own exception, so a bad date
		produced a completed, billed service with no clinical record and no way to re-drive
		it. There is no writer any more and no second document to fall out of step with, so
		this is now simply the rule, checked at save time where the operator can still fix it.

		WHICH DATE IT COMPARES AGAINST.

		  - `administered_on` set (already given, being edited) -> that day, definitively.
		    Comparing against today instead would make every historical record un-saveable
		    the day after its next due date passed, which is a retroactive lock.
		  - otherwise -> the later of today and the day it is booked for
		    (`scheduled_datetime`, else `due_date`). Today alone is too weak for a dose booked
		    ahead: one booked for next month with a next-due two weeks out would clear a
		    today-check and still be wrong.

		STRICTLY AFTER. A dose due the day it is given is not a schedule, and its reminder
		would fire the same afternoon.
		"""
		if not self.next_due_date:
			# Empty is a real answer meaning no recurrence. Nothing to check.
			return

		if self.administered_on:
			given_on = getdate(self.administered_on)
		else:
			booked_on = self.scheduled_datetime or self.due_date
			given_on = getdate(nowdate())
			if booked_on:
				given_on = max(given_on, getdate(booked_on))

		if getdate(self.next_due_date) > given_on:
			return

		frappe.throw(
			_(
				"Next Due Date must be after the date this dose is given. {0} is on or before {1}. "
				"Set a later date, or leave it empty if there is no next dose."
			).format(frappe.bold(getdate(self.next_due_date)), frappe.bold(given_on))
		)

	# ── care plan links ───────────────────────────────────────────────────────

	def _validate_plan_item_links_before_detach(self):
		"""Same guard Lab applies, for the same reason: a care plan item pointing at this
		record must not be silently orphaned by moving or cancelling it."""
		previous = self.get_doc_before_save()
		if previous and previous.get("visit") != self.visit:
			assert_no_active_plan_items_linked_to(self.doctype, self.name, action="move")
		if self.status == "Cancelled" and (not previous or previous.get("status") != "Cancelled"):
			assert_no_active_plan_items_linked_to(self.doctype, self.name, action="cancel")
