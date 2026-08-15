from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

from pet_app.utils.practitioner import PRACTITIONER_DOCTYPE


BRANCH_DOCTYPE = "Branch"
RESTRICTION_TYPE = "branch"

# Only these doctypes are branch separated. An allow-list, not a deny-list: several
# ERPNext doctypes (Sales Invoice, Payment Entry, Stock Entry, POS Profile, Sales Order)
# already carry an unrelated `branch` custom field for reporting, and filtering on it
# would silently scope billing -- which must stay global, because one company means one
# receivable ledger and a customer's debt is collectable at any clinic.
SCOPED_DOCTYPES = frozenset(
	{
		"Vet Visit",
		"Vet Case Sheet",
		"Appointment",
		"Pet Queue Ticket",
		# NOT Pet Boarding. One boarding facility serves every clinic -- the 37 Service
		# Rooms are a single shared pool with no branch of their own -- so scoping the
		# boarding record would let one clinic book a room that another clinic then
		# could not see or check out. Accountability is tracked by
		# `Pet Boarding.practitioner` instead of by branch.
		# Both appear in the workspace worklists (_service_items / _procedure_items),
		# so they are separated for the same reason visits are. Branch is inherited
		# from the linked visit where there is one.
		"PetCareService",
		"Pet Procedure",
		# An invoice belongs to the clinic that raised it. This scopes the invoice
		# *list* only -- the customer, the receivable ledger and the outstanding
		# balance stay global, because one Company means one set of books and a debt
		# is owed to the business, not to a branch. Never filter Payment Entry,
		# Customer or GL Entry: doing so would make a customer look settled at one
		# clinic while owing money at another.
		"Sales Invoice",
	}
)


BOARDING_BRANCH_SETTING = ("Pet Boarding Settings", "boarding_branch")

# Set on a document by a caller that has already established the branch itself, rather
# than accepting one from the client. `stamp_branch_on_insert` then skips
# `assert_can_write_to_branch` for that document only.
#
# This exists because `ignore_permissions=True` does NOT reach here. That flag is read by
# `Document.check_permission`, which runs beside the doc_events, not around them - so an
# elevated insert still executed this hook and still threw. Stamping the boarding facility
# on a stay was reverted for exactly that reason: a clinic coordinator could no longer
# check out, because the invoice carried a branch they have no claim to. An elevation that
# covers only `insert()` fixes nothing.
#
# The flag is never set from request data. Every caller that sets it must have derived the
# branch from a record or a site setting and must state, at the call site, what it proved
# before doing so.
BRANCH_AUTHORISED_FLAG = "branch_write_authorised"


def get_boarding_branch(required: bool = True) -> str | None:
	"""The branch every boarding stay is attributed to.

	Boarding happens at the boarding facility, full stop. It must never be derived from
	the acting user: before this, Pet Boarding carried no branch, so billing fell through
	to the operator's branch and a hotel stay checked out by a clinic coordinator was
	posted to the clinic. `branch` is a live accounting dimension, so that reached the
	ledger and not just a report.

	Read from a setting rather than hardcoded - "hotel" is data, not a constant - and it
	throws when unset rather than guessing, because guessing is the bug being fixed.

	This is attribution only. Pet Boarding stays OUT of SCOPED_DOCTYPES: the 37 Service
	Rooms remain one shared pool and every clinic must still see and check out every
	stay.
	"""
	doctype, fieldname = BOARDING_BRANCH_SETTING
	branch = cstr(frappe.db.get_single_value(doctype, fieldname)).strip()
	if branch:
		return branch
	if not required:
		return None
	frappe.throw(
		_(
			"Boarding Branch is not set. Set it in {0} so boarding revenue is attributed "
			"to the boarding facility rather than to whoever processes the stay."
		).format(frappe.bold(_(doctype)))
	)


def stamp_boarding_branch(doc, method=None) -> None:
	"""``before_insert`` on Pet Boarding - the record carries its own attribution.

	Set here rather than resolved at billing time so the stay records where it happened
	at the moment it happened; a later change of setting cannot rewrite history.
	"""
	if not doc or not doc.meta.has_field("branch") or doc.get("branch"):
		return
	doc.branch = get_boarding_branch(required=False)


def performing_branch_for(care_service: str | None = None, procedure_template: str | None = None) -> str | None:
	"""Where the catalogue says this service is physically performed.

	The location belongs to the service, not to the person ordering it: a main doctor may
	order radiology, read the result and manage the case, but the machine is at the
	boarding facility, so the revenue is the facility's. Recording it on the catalogue
	rather than on each order means one row answers it for every future order of that
	service, and there is exactly one place to correct if a machine moves.

	``care_service`` wins when both are given - a Pet Procedure that names a CareService
	template is billed from that template, and only falls back to its Procedure Template
	when it has none, which is the same precedence its ``item_code``/``rate`` already use.

	Returns ``None`` when the catalogue says nothing, which is the common case and means
	"performed wherever it was ordered". Callers must treat that as "leave the existing
	behaviour alone", never as a reason to guess.
	"""
	if care_service:
		branch = cstr(frappe.db.get_value("CareService template", care_service, "performing_branch")).strip()
		if branch:
			return branch
	if procedure_template:
		branch = cstr(frappe.db.get_value("Procedure Template", procedure_template, "performing_branch")).strip()
		if branch:
			return branch
	return None


def snapshot_performing_branch(doc, *, care_service=None, procedure_template=None) -> None:
	"""Stamp ``performing_branch`` onto an order once, at insert.

	Set-once, deliberately - and this is where it diverges from ``item_code`` and ``rate``,
	which the same controllers refresh from the catalogue on every validate. A price
	correction should reach an order that has not been billed yet; a change to where a
	service is performed must not, because it would silently re-attribute revenue for work
	that already happened somewhere else. The stay-level equivalent of this rule is
	:func:`stamp_boarding_branch`, which makes the same argument for the same reason.

	No-op when the field is absent, so this is safe to call before the doctype is
	reloaded on a site mid-migration.
	"""
	if not doc.meta.has_field("performing_branch") or doc.get("performing_branch"):
		return
	doc.performing_branch = performing_branch_for(
		care_service=care_service, procedure_template=procedure_template
	)


def get_current_branch(user: str | None = None) -> str | None:
	"""Resolve the clinic branch a user belongs to.

	Order:
	1. ``User Permission`` on ``Branch`` -- the single source of truth. Works for any
	   user, clinical or not, and is what Frappe itself filters Desk queries with.
	2. ``Healthcare Practitioner.clinic_branch`` -- a read-only mirror of (1), kept in
	   sync by ``sync_practitioner_branch``. Only reached when the mirror is stale or
	   the permission row was removed by hand.

	Returns ``None`` when nothing resolves. Callers that write records must use
	:func:`require_current_branch` instead -- a blank branch is visible to *every*
	clinic (see ``frappe/model/db_query.py`` ``add_user_permissions``), so writing one
	is a leak, not a safe default.
	"""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return None

	for branch in get_user_branches(user):
		return branch

	if frappe.db.exists("DocType", PRACTITIONER_DOCTYPE):
		mirrored = frappe.db.get_value(PRACTITIONER_DOCTYPE, {"user_id": user}, "clinic_branch")
		if mirrored and frappe.db.exists(BRANCH_DOCTYPE, mirrored):
			return mirrored

	return None


def get_user_branches(user: str | None = None) -> list[str]:
	"""Every branch the user is permitted to see, via ``User Permission``.

	Reuses ``get_restriction_values`` so nested-set descendants are expanded the same
	way the rest of the app already does it.
	"""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return []

	from pet_app.api.permissions import get_restriction_values

	return [value for value in get_restriction_values(RESTRICTION_TYPE, user) if value]


def require_current_branch(user: str | None = None) -> str:
	"""Resolve the user's branch or throw.

	Used on write paths. Failing closed is deliberate: an unassigned user writing a
	blank branch would produce a record visible to every clinic.
	"""
	branch = get_current_branch(user)
	if branch:
		return branch

	frappe.throw(
		_("No clinic branch is assigned to {0}. Ask an administrator to assign one before creating records.").format(
			frappe.bold(user or frappe.session.user)
		),
		frappe.ValidationError,
	)


def user_sees_all_branches(user: str | None = None) -> bool:
	"""True when the user is unrestricted and should see every branch.

	Administrator always qualifies. So does any user with no Branch User Permission --
	that matches how Frappe itself behaves (no restriction rows means no filtering),
	and keeps back-office/report users working until they are explicitly assigned.
	"""
	user = user or frappe.session.user

	from pet_app.api.permissions import user_has_full_access

	if user_has_full_access(user):
		return True
	return not get_user_branches(user)


def apply_branch_filter(filters: dict, doctype: str, user: str | None = None) -> dict:
	"""Add a branch condition to a ``get_all`` filters dict, in place.

	No-op when the user is unrestricted, when the doctype has no ``branch`` field, or
	when the caller already set one. Returns ``filters`` for chaining.
	"""
	if filters is None or "branch" in filters:
		return filters
	if doctype not in SCOPED_DOCTYPES:
		return filters
	if user_sees_all_branches(user):
		return filters
	if not _has_branch_field(doctype):
		return filters

	branches = get_user_branches(user)
	filters["branch"] = branches[0] if len(branches) == 1 else ["in", branches]
	return filters


def sync_practitioner_branch(user: str) -> None:
	"""Push a user's resolved branch onto their Healthcare Practitioner record.

	The practitioner field is a read-only mirror so the two can never disagree.
	Writes directly with ``db.set_value`` to avoid triggering a full practitioner
	save (and its validations) from a User Permission hook.
	"""
	if not user or not frappe.db.exists("DocType", PRACTITIONER_DOCTYPE):
		return

	practitioner = frappe.db.get_value(PRACTITIONER_DOCTYPE, {"user_id": user}, "name")
	if not practitioner:
		return

	branches = get_user_branches(user)
	value = branches[0] if len(branches) == 1 else None
	if frappe.db.get_value(PRACTITIONER_DOCTYPE, practitioner, "clinic_branch") != value:
		frappe.db.set_value(PRACTITIONER_DOCTYPE, practitioner, "clinic_branch", value, update_modified=False)


def stamp_branch_on_insert(doc, method=None) -> None:
	"""``before_insert`` hook -- resolve the branch a scoped record belongs to.

	Wired via ``doc_events`` rather than at each call site: ``Vet Visit`` alone has five
	non-test insert sites, and a missed one would write a NULL branch, which is visible
	to *every* clinic (``frappe/model/db_query.py``, ``add_user_permissions``).

	Rules, in order:

	1. A branch supplied by the caller is honoured only if they are allowed to write to
	   it. A restricted user cannot file a record into somebody else's clinic - unless
	   the caller carries :data:`BRANCH_AUTHORISED_FLAG`, which says the branch came
	   from a record or a setting rather than from the request, and that the endpoint
	   has already authorised the operation. See that constant for why the flag has to
	   be read here and not left to ``ignore_permissions``.
	2. Otherwise it is taken from the user's own branch.
	3. Unrestricted users (Administrator, back-office staff with no Branch User
	   Permission) legitimately have no single branch, so they **must** choose one.
	   Only when the site has exactly one Branch is that choice made for them.
	"""
	if not doc or doc.doctype not in SCOPED_DOCTYPES or not doc.meta.has_field("branch"):
		return

	# Background work has no session user to resolve a branch from. Stamp the default so
	# the row is never NULL, rather than blocking installs, patches and scheduled jobs.
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch or frappe.flags.in_test:
		if not doc.get("branch"):
			doc.branch = get_default_branch()
		return

	supplied = cstr(doc.get("branch")).strip()
	if supplied:
		# The service owns the branch, so the acting user's claim to it is not the
		# question being asked - but only when the caller has said so explicitly and
		# proved something first. Anything else is still a client-supplied branch.
		if not doc.flags.get(BRANCH_AUTHORISED_FLAG):
			assert_can_write_to_branch(supplied)
		doc.branch = supplied
		return

	branch = get_current_branch()
	if branch:
		doc.branch = branch
		return

	if user_sees_all_branches():
		# One branch on site means there is nothing to choose between.
		only = _sole_branch()
		if only:
			doc.branch = only
			return
		frappe.throw(
			_("Select a branch for this {0}. Your user is not restricted to one clinic, so the branch cannot be inferred.").format(
				_(doc.doctype)
			),
			frappe.MandatoryError,
		)

	# Restricted, yet nothing resolved -- a real misconfiguration. Fail closed rather
	# than write a row that every clinic can see.
	doc.branch = require_current_branch()


def assert_can_write_to_branch(branch: str, user: str | None = None) -> None:
	"""Reject an attempt to file a record into a branch the user has no claim to."""
	if not branch:
		return
	if not frappe.db.exists(BRANCH_DOCTYPE, branch):
		frappe.throw(_("Branch {0} was not found.").format(frappe.bold(branch)))
	if user_sees_all_branches(user):
		return
	if branch not in get_user_branches(user):
		frappe.throw(
			_("You are not permitted to create records for branch {0}.").format(frappe.bold(branch)),
			frappe.PermissionError,
		)


def get_default_branch() -> str | None:
	"""Fallback branch for background work: the oldest Branch on site."""
	rows = frappe.get_all(BRANCH_DOCTYPE, pluck="name", order_by="creation asc", limit_page_length=1)
	return rows[0] if rows else None


def _sole_branch() -> str | None:
	rows = frappe.get_all(BRANCH_DOCTYPE, pluck="name", limit_page_length=2)
	return rows[0] if len(rows) == 1 else None


def on_user_permission_change(doc, method=None) -> None:
	"""``doc_events`` hook on User Permission -- keeps the practitioner mirror current."""
	if getattr(doc, "allow", None) != BRANCH_DOCTYPE:
		return
	user = getattr(doc, "user", None)
	if user:
		sync_practitioner_branch(user)


def _has_branch_field(doctype: str) -> bool:
	try:
		return bool(frappe.get_meta(doctype).has_field("branch"))
	except Exception:
		return False
