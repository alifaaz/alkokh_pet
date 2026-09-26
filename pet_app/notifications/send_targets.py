"""Which send surfaces exist, and which template each one currently sends.

Two facts, deliberately kept in two different places, because they change for
different reasons and by different people:

* **Which surfaces exist** is code. A surface is a screen or a job somebody built -
  a boarding check-in button, the inbox composer. It cannot be conjured from a
  settings row, so ``SEND_SURFACES`` below is the registry and a diff is the only
  way to add to it.
* **Which template a surface sends** is configuration, and lives in
  ``Pet App Access Settings.send_defaults``. Re-pointing a surface at a different
  template is a Tuesday decision an operator should be able to make, and - more to
  the point - there must be exactly one place to look up the answer.

Before Phase 11a the second fact was a dict of template names right here, and the
frontend also carried its own constants. Three places, none authoritative. Now there
is one, and this module reads it rather than restating it.

**There is no fallback.** A surface with no row, a blank row, and a row with
``enabled = 0`` all mean the same thing: no default is configured, the picker opens
with nothing selected, and ``resolve_send_target`` refuses. That equivalence is the
point - if code kept a default of its own, nobody could answer "what does boarding
check-in send?" without first knowing whether a settings row happened to exist, which
is the problem this replaced.

This is a **lookup, not a send path**. It resolves a surface to a target and stops.
The send itself is the existing one: a caller passing ``source_doctype`` +
``source_name`` + ``meta_template`` and omitting ``context["parameters"]`` already
resolves through ``build_document_context`` and then ``apply_slot_map_to_context``.

    target = resolve_send_target("boarding.check_in")
    send_manual_notification(data={
        "event_key": target.event,
        "template_source": target.template_source,
        "meta_template": target.meta_template,
        "source_doctype": target.source_doctype,
        "source_name": boarding_name,
        "recipient_type": "Guardian",
        "recipient_name": guardian_name,
        # no "parameters" key - the slot map fills them
    })
"""

from __future__ import annotations

from typing import NamedTuple, Optional

import frappe
from frappe import _
from frappe.utils import cint, cstr

from pet_app.notifications.meta_templates import (
	LOCAL_TEMPLATE_DOCTYPE,
	MIRROR_DOCTYPE,
	SENDABLE_STATUS,
	declared_slot_count,
	stored_slot_map,
	stored_source_doctype,
)

SETTINGS_DOCTYPE = "Pet App Access Settings"
DEFAULTS_DOCTYPE = "Pet App WhatsApp Send Default"
DEFAULTS_FIELD = "send_defaults"


# The surface registry: every screen or job that sends a WhatsApp message, and the
# name to show an operator on the settings screen. Keys are drawn from the event_key
# namespace already on the wire, so a surface is identified once and the same way
# everywhere - no second identifier scheme.
#
# It is a curated list rather than "whatever event_key arrives", because event_key is
# also used as a free-form per-send label: the queue carries
# whatsapp.action.<Rule Name>, reminder.<type> and one-off strings like
# whatsapp.real_test.hello_world.20260721T1519Z. Keying settings on arbitrary
# event_keys would make the table unbounded and fill it with rows describing a single
# historical send.
#
# Values are LABELS, not templates. What a surface sends is configuration - see the
# module docstring. Nothing in this file may state a template name.
SEND_SURFACES = {
	"boarding.check_in": "Boarding Check-In",
	"boarding.check_out": "Boarding Check-Out",
	"boarding.pickup_ready": "Boarding Pickup Ready",
	"boarding.sent_home": "Boarding Sent Home",
	"pet.death": "Pet Death Condolence",
	# Frontend-supplied surfaces. These mint their event_key client-side - no server
	# call site sets them - and were already on the wire before this registry existed
	# (row counts are the live queue at the time of writing).
	"manual_whatsapp_reply": "Inbox Reply",  # 36 rows
	"manual.guardian_message": "Guardian Message",  # 27 rows
	"pet_service.staff_followup.manual": "Service Staff Follow-up",  # 15 rows
	"appointment.response.manual": "Appointment Response Request",  # 7 rows
	# Confirmed against the frontend constant. It has never sent - no queue row carries
	# it - so unlike the four above, the key could not be read off live traffic and was
	# corrected from the client after the first seed. Its documents are Sales Invoice,
	# which is a registered source namespace, so a Meta default here can fill its slots
	# from the invoice. Seeded blank: the frontend's own defaultTemplateKey, invoice_send,
	# matches no local template and no mirror row, so there was nothing to seed it with.
	"manual.invoice_send": "Invoice Send",
}


class SendTargetError(frappe.ValidationError):
	"""A surface cannot be resolved to something sendable."""

	def __init__(self, message, code=None, details=None):
		super().__init__(message)
		self.exc_type = code or "SEND_TARGET_UNRESOLVED"
		self.details = details or {}


class SendTarget(NamedTuple):
	"""Everything a call site needs to hand the existing send path."""

	event: str
	template_source: str
	template_name: str
	meta_template: Optional[str] = None
	template_key: Optional[str] = None
	source_doctype: Optional[str] = None
	declared_count: int = 0
	slot_count: int = 0


class SurfaceDefault(NamedTuple):
	"""One configured row, already normalised. Blank and disabled arrive as configured=False."""

	surface: str
	configured: bool
	template_source: str = ""
	template_key: str = ""
	meta_template: str = ""


def registered_surfaces() -> list:
	"""Every surface this registry knows, sorted. Nothing resolves them."""
	return sorted(SEND_SURFACES)


def surface_label(surface) -> str:
	"""The operator-facing name for a surface, or the key itself if unregistered."""
	key = cstr(surface).strip()
	return SEND_SURFACES.get(key) or key


def _defaults_available() -> bool:
	"""Whether the settings table exists yet.

	get_meta is cached, so this costs nothing on a warm request. The window it guards
	is real: between deploying this code and running bench migrate, the field and its
	child table do not exist, and an unguarded read would raise on every send and on
	every open of the template picker.
	"""
	try:
		return bool(frappe.get_meta(SETTINGS_DOCTYPE).has_field(DEFAULTS_FIELD))
	except Exception:
		return False


def configured_defaults() -> dict:
	"""surface_key -> SurfaceDefault, for every registered surface. One query.

	Every registered surface is present in the result, so a caller never has to tell
	"no row" from "blank row" - both arrive as ``configured=False``, which is the whole
	rule. Rows for unregistered surfaces are ignored rather than returned: they cannot
	be reached by any surface, and returning them would let a stale key look live.
	"""
	stored = {}
	if _defaults_available():
		for row in frappe.get_all(
			DEFAULTS_DOCTYPE,
			filters={"parenttype": SETTINGS_DOCTYPE, "parentfield": DEFAULTS_FIELD},
			fields=["surface_key", "template_source", "template_key", "meta_template", "enabled"],
			ignore_permissions=True,
		):
			stored[cstr(row.get("surface_key")).strip()] = row

	defaults = {}
	for surface in SEND_SURFACES:
		row = stored.get(surface)
		source = cstr((row or {}).get("template_source")).strip()
		key = cstr((row or {}).get("template_key")).strip()
		meta = cstr((row or {}).get("meta_template")).strip()
		# Disabled is blank, deliberately: it lets an operator switch a default off
		# without losing which template it was.
		enabled = bool(cint((row or {}).get("enabled"))) if row else False
		configured = bool(enabled and ((source == "meta" and meta) or (source == "local" and key)))
		defaults[surface] = SurfaceDefault(
			surface=surface,
			configured=configured,
			template_source=source if configured else "",
			template_key=key if configured else "",
			meta_template=meta if configured else "",
		)
	return defaults


def defaults_by_template() -> dict:
	"""The configured defaults inverted, for a picker that already holds the templates.

	Returns ``{"meta": {mirror_docname: [surface, ...]}, "local": {template_key: [...]}}``
	- a list per template because one template may be the default for several surfaces,
	and because a caller must be able to tell "no surface defaults to this" from "the
	mapping is unknown".

	One query, shared with configured_defaults so the rule "a default addresses a
	template by source + identity" is stated once. Deliberately not resolve_send_target
	in a loop: that function validates and raises for exactly the states a picker still
	has to show a mapping for - a template with no slot map is unsendable but is still
	the template its surface is set to, and hiding it there would tell an operator no
	surface uses it.
	"""
	index = {"meta": {}, "local": {}}
	for surface, default in configured_defaults().items():
		if not default.configured:
			continue
		if default.template_source == "meta":
			index["meta"].setdefault(default.meta_template, []).append(surface)
		elif default.template_source == "local":
			index["local"].setdefault(default.template_key, []).append(surface)
	return {
		kind: {ident: sorted(surfaces) for ident, surfaces in bucket.items()}
		for kind, bucket in index.items()
	}


def _no_default(surface):
	return SendTargetError(
		_(
			"Surface {0} has no default template configured. Set one under "
			"Pet App Access Settings > WhatsApp Send Defaults, or send it with an "
			"explicit template."
		).format(surface),
		code="SEND_TARGET_NO_DEFAULT_CONFIGURED",
		details={"event": surface, "surface": surface, "label": surface_label(surface)},
	)


def _resolve_meta_default(surface, meta_template) -> SendTarget:
	"""A meta default, validated the same way it was before the settings store.

	Addressed by mirror docname rather than by template name. The old registry keyed by
	name to survive a template being deleted and recreated on Meta with a new id; the
	settings row is a Link instead, so that round trip now breaks loudly - the mirror
	row is gone, this refuses, and an operator is told which surface to re-point -
	rather than quietly matching nothing.
	"""
	row = frappe.db.get_value(
		MIRROR_DOCTYPE, meta_template, ["name", "template_name", "status"], as_dict=True
	)
	if not row:
		raise SendTargetError(
			_(
				"Surface {0} is set to a Meta template ({1}) that is no longer in the "
				"local mirror. Run a sync from the Meta Templates tab, or pick another "
				"template for this surface."
			).format(surface, meta_template),
			code="SEND_TARGET_TEMPLATE_NOT_MIRRORED",
			details={"event": surface, "surface": surface, "meta_template": meta_template},
		)

	template_name = cstr(row.template_name)
	if cstr(row.status) != SENDABLE_STATUS:
		raise SendTargetError(
			_(
				"Surface {0} sends template {1}, which is in status {2}. It can only be "
				"sent once Meta reports it as {3}."
			).format(surface, template_name, cstr(row.status) or _("(none)"), SENDABLE_STATUS),
			code="SEND_TARGET_TEMPLATE_NOT_APPROVED",
			details={"event": surface, "surface": surface, "template_name": template_name,
					 "meta_template": row.name, "status": cstr(row.status)},
		)

	declared = cint(declared_slot_count(row.name))
	mapped = len(stored_slot_map(row.name))

	if declared and not mapped:
		raise SendTargetError(
			_(
				"Surface {0} sends template {1}, which has {2} variable(s) and no saved "
				"variable map. Map its variables before this surface can send."
			).format(surface, template_name, declared),
			code="SEND_TARGET_SLOT_MAP_MISSING",
			details={"event": surface, "surface": surface, "template_name": template_name,
					 "meta_template": row.name, "declared_count": declared, "slot_count": 0},
		)
	if mapped != declared:
		raise SendTargetError(
			_(
				"Surface {0} sends template {1}, which has {2} variable(s) but a saved "
				"variable map of {3}. The template changed on Meta; re-map it."
			).format(surface, template_name, declared, mapped),
			code="SEND_TARGET_SLOT_MAP_STALE",
			details={"event": surface, "surface": surface, "template_name": template_name,
					 "meta_template": row.name, "declared_count": declared,
					 "slot_count": mapped},
		)

	return SendTarget(
		event=surface,
		template_source="meta",
		template_name=template_name,
		meta_template=row.name,
		source_doctype=stored_source_doctype(row.name),
		declared_count=declared,
		slot_count=mapped,
	)


def _resolve_local_default(surface, template_key) -> SendTarget:
	"""A local default. Existence and enabled only.

	Everything past that - body, parameters, delivery mode - is resolve_template's job
	on the existing local path, and checking it twice in two places is how the two
	answers start to disagree. No slot map: a local template is rendered, not slotted.
	"""
	row = frappe.db.get_value(
		LOCAL_TEMPLATE_DOCTYPE,
		{"template_key": template_key},
		["name", "template_name", "enabled", "source_doctype"],
		as_dict=True,
	)
	if not row:
		raise SendTargetError(
			_(
				"Surface {0} is set to local template {1}, which no longer exists. "
				"Pick another template for this surface."
			).format(surface, template_key),
			code="SEND_TARGET_LOCAL_TEMPLATE_MISSING",
			details={"event": surface, "surface": surface, "template_key": template_key},
		)
	if not cint(row.enabled):
		raise SendTargetError(
			_("Surface {0} is set to local template {1}, which is disabled.").format(
				surface, template_key
			),
			code="SEND_TARGET_LOCAL_TEMPLATE_DISABLED",
			details={"event": surface, "surface": surface, "template_key": template_key},
		)
	return SendTarget(
		event=surface,
		template_source="local",
		template_name=cstr(row.template_name),
		template_key=template_key,
		source_doctype=cstr(row.source_doctype) or None,
	)


def resolve_send_target(surface, default=None) -> SendTarget:
	"""The configured template and source behind a surface, or a specific refusal.

	source_doctype is read from the template rather than restated in settings. The slot
	map was validated against that value; a second copy in the settings row could
	disagree, and disagreement would mean the map was checked against one source while
	the send resolved against another.

	Every failure names the surface, the template and what was wrong. Nothing here ever
	falls back, and nothing returns a target whose slots cannot be filled.
	"""
	key = cstr(surface).strip()
	if key not in SEND_SURFACES:
		raise SendTargetError(
			_("There is no send surface {0}. Known surfaces: {1}.").format(
				key or "(blank)", ", ".join(registered_surfaces())
			),
			code="SEND_TARGET_UNKNOWN_EVENT",
			details={"event": key, "surface": key, "known_events": registered_surfaces(),
					 "known_surfaces": registered_surfaces()},
		)

	# A caller that already read configured_defaults() passes the row back rather than
	# paying for the same query twice on one send.
	if default is None:
		default = configured_defaults()[key]
	if not default.configured:
		raise _no_default(key)
	if default.template_source == "meta":
		return _resolve_meta_default(key, default.meta_template)
	return _resolve_local_default(key, default.template_key)
