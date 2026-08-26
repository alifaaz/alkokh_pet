"""Operational events, and the approved Meta template each one sends.

Why this exists: call sites should name what happened - a pet was checked in, a pet
died - not which WhatsApp template carries that news. A button on a boarding page
knows it is a check-in; it should not also have to know a template id, a slot map and
a source doctype.

Why a module-level dict and not a doctype: re-pointing an event at a different
template is a decision that should be reviewed in a diff, not changed in a dropdown on
a Tuesday. ``pet.death`` in particular. Everything else an entry would have held
already lives on the mirror row, and a second copy of it could only ever disagree.

This is a **lookup, not a send path**. It resolves an event to a target and stops. The
send itself is the existing one: a caller passing ``source_doctype`` + ``source_name``
+ ``meta_template`` and omitting ``context["parameters"]`` already resolves through
``build_document_context`` and then ``apply_slot_map_to_context``. Nothing here
duplicates that.

    target = resolve_send_target("boarding.check_in")
    send_manual_notification(data={
        "event_key": target.event,
        "template_source": "meta",
        "meta_template": target.meta_template,
        "source_doctype": target.source_doctype,
        "source_name": boarding_name,
        "recipient_type": "Guardian",
        "recipient_name": guardian_name,
        # no "parameters" key - the slot map fills them
    })
"""

from __future__ import annotations

from typing import NamedTuple

import frappe
from frappe import _
from frappe.utils import cint, cstr

from pet_app.notifications.meta_templates import (
	MIRROR_DOCTYPE,
	SENDABLE_STATUS,
	declared_slot_count,
	stored_slot_map,
	stored_source_doctype,
)


# Keyed by TEMPLATE NAME, never by Meta's template id. A template deleted and
# recreated on Meta comes back with a new id, so a registry pinned to an id would
# break at precisely the moment somebody fixed a template - and break silently, since
# the old id simply stops matching any row. The name survives that round trip.
#
# visit.follow_up is deliberately absent: visit_followup has no slot map, and no
# visit-shaped doctype is registered in SOURCE_NAMESPACES. Registering Pet or Guardian
# as a stand-in source would make the registry lie about where a follow-up comes from.
SEND_TARGETS = {
	"boarding.check_in": "boarding_checkinn",
	"boarding.check_out": "boarding_checkout",
	"boarding.pickup_ready": "boarding_pickup_ready",
	"boarding.sent_home": "boarding_sent_home",
	"pet.death": "pet_condolence",
}


class SendTargetError(frappe.ValidationError):
	"""An event cannot be resolved to something sendable."""

	def __init__(self, message, code=None, details=None):
		super().__init__(message)
		self.exc_type = code or "SEND_TARGET_UNRESOLVED"
		self.details = details or {}


class SendTarget(NamedTuple):
	"""Everything a call site needs to hand the existing send path."""

	event: str
	template_name: str
	meta_template: str
	source_doctype: str
	declared_count: int
	slot_count: int


def registered_events() -> list:
	"""Every event this registry knows, sorted. Nothing resolves them."""
	return sorted(SEND_TARGETS)


def resolve_send_target(event) -> SendTarget:
	"""The approved template and source behind an event, or a specific refusal.

	source_doctype is read from the mirror row rather than restated here. The slot map
	was validated against that value; a second copy in the registry could disagree,
	and disagreement would mean the map was checked against one source while the send
	resolved against another.

	Every failure names the event, the template and what was wrong. Nothing here ever
	falls back to a local template, and nothing returns a target whose slots cannot be
	filled.
	"""
	key = cstr(event).strip()
	template_name = SEND_TARGETS.get(key)
	if not template_name:
		raise SendTargetError(
			_("There is no send target for event {0}. Known events: {1}.").format(
				key or "(blank)", ", ".join(registered_events())
			),
			code="SEND_TARGET_UNKNOWN_EVENT",
			details={"event": key, "known_events": registered_events()},
		)

	rows = frappe.get_all(
		MIRROR_DOCTYPE,
		filters={"template_name": template_name},
		fields=["name", "status"],
		ignore_permissions=True,
	)
	if not rows:
		raise SendTargetError(
			_(
				"Event {0} sends template {1}, which is not in the local mirror. "
				"Run a sync from the Meta Templates tab."
			).format(key, template_name),
			code="SEND_TARGET_TEMPLATE_NOT_MIRRORED",
			details={"event": key, "template_name": template_name},
		)
	if len(rows) > 1:
		raise SendTargetError(
			_(
				"Event {0} sends template {1}, which matches {2} mirror rows ({3}). "
				"A template name must identify one row for an event to use it."
			).format(key, template_name, len(rows), ", ".join(sorted(r.name for r in rows))),
			code="SEND_TARGET_TEMPLATE_AMBIGUOUS",
			details={"event": key, "template_name": template_name,
					 "meta_templates": sorted(r.name for r in rows)},
		)

	row = rows[0]
	if cstr(row.status) != SENDABLE_STATUS:
		raise SendTargetError(
			_(
				"Event {0} sends template {1}, which is in status {2}. It can only be "
				"sent once Meta reports it as {3}."
			).format(key, template_name, cstr(row.status) or _("(none)"), SENDABLE_STATUS),
			code="SEND_TARGET_TEMPLATE_NOT_APPROVED",
			details={"event": key, "template_name": template_name,
					 "meta_template": row.name, "status": cstr(row.status)},
		)

	declared = cint(declared_slot_count(row.name))
	mapped = len(stored_slot_map(row.name))

	if declared and not mapped:
		raise SendTargetError(
			_(
				"Event {0} sends template {1}, which has {2} variable(s) and no saved "
				"variable map. Map its variables before this event can send."
			).format(key, template_name, declared),
			code="SEND_TARGET_SLOT_MAP_MISSING",
			details={"event": key, "template_name": template_name,
					 "meta_template": row.name, "declared_count": declared, "slot_count": 0},
		)
	if mapped != declared:
		raise SendTargetError(
			_(
				"Event {0} sends template {1}, which has {2} variable(s) but a saved "
				"variable map of {3}. The template changed on Meta; re-map it."
			).format(key, template_name, declared, mapped),
			code="SEND_TARGET_SLOT_MAP_STALE",
			details={"event": key, "template_name": template_name, "meta_template": row.name,
					 "declared_count": declared, "slot_count": mapped},
		)

	return SendTarget(
		event=key,
		template_name=template_name,
		meta_template=row.name,
		source_doctype=stored_source_doctype(row.name),
		declared_count=declared,
		slot_count=mapped,
	)
