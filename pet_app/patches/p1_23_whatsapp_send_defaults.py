"""Seed the send-default table with today's values, so nothing changes on day one.

Phase 11a moved "which template does this surface send" out of code and into
Pet App Access Settings. This writes the answers that were in code immediately before
the move, so the first migrate is a no-op in behaviour and only a change in where the
answer lives.

The five boarding/death surfaces are seeded from the map this patch retires, resolved
through the mirror **by template name** - the same lookup the old registry did at send
time. Resolving here rather than hardcoding mirror docnames means the patch reproduces
whatever this site actually resolves, instead of asserting docnames from another one.

The frontend surfaces are seeded BLANK. They have no default today: the constants live
client-side and the two obvious candidates cannot send (invoice_due_reminder is
disabled and unbound, pet_service_staff_followup is unbound). A preselected refusing
template looks configured; blank prompts the operator to choose. Day-one behaviour for
those surfaces is already a refusal, so blank makes an existing problem visible rather
than creating a new one. manual.invoice_send is blank for a sharper reason: the constant
the frontend carries for it, invoice_send, matches no local template and no mirror row,
so there is nothing to seed it with. p1_24 corrects that key on sites seeded before it
was confirmed.

Idempotent, and never overwrites: a surface that already has a row is left exactly as
the operator set it.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr

SETTINGS_DOCTYPE = "Pet App Access Settings"
DEFAULTS_DOCTYPE = "Pet App WhatsApp Send Default"
DEFAULTS_FIELD = "send_defaults"
MIRROR_DOCTYPE = "Pet App WhatsApp Meta Template"

# The retired SEND_TARGETS map, verbatim, kept here as the seed's source and nowhere
# else. surface_key -> Meta template name, or None for "no default today".
SEED_TEMPLATE_NAMES = {
	"boarding.check_in": "boarding_checkinn",
	"boarding.check_out": "boarding_checkout",
	"boarding.pickup_ready": "boarding_pickup_ready",
	"boarding.sent_home": "boarding_sent_home",
	"pet.death": "pet_condolence",
	"manual_whatsapp_reply": None,
	"manual.guardian_message": None,
	"pet_service.staff_followup.manual": None,
	"appointment.response.manual": None,
	"manual.invoice_send": None,
}


def _mirror_name(template_name):
	"""The one mirror row for a template name, or None with a reason logged.

	Zero rows or several is not a crash: the surface is seeded blank, which is the
	no-default state, and an operator picks a template for it. Seeding a guess would be
	the one outcome that changes behaviour silently.
	"""
	rows = frappe.get_all(
		MIRROR_DOCTYPE, filters={"template_name": template_name}, pluck="name", ignore_permissions=True
	)
	if len(rows) == 1:
		return rows[0]
	print(
		f"  ! {template_name}: {len(rows)} mirror rows - seeding this surface blank"
	)
	return None


def execute():
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE) or not frappe.db.exists(
		"DocType", DEFAULTS_DOCTYPE
	):
		return
	# The field arrives with the doctype JSON at model sync. On a fresh install this
	# patch is re-run by after_install, where the JSON is already imported; if it is
	# somehow not, there is nothing to seed into and seeding is skipped rather than
	# guessed at.
	if not frappe.get_meta(SETTINGS_DOCTYPE).has_field(DEFAULTS_FIELD):
		return

	doc = frappe.get_single(SETTINGS_DOCTYPE)
	existing = {cstr(row.surface_key).strip() for row in doc.get(DEFAULTS_FIELD) or []}

	added = 0
	for surface, template_name in SEED_TEMPLATE_NAMES.items():
		if surface in existing:
			continue
		meta_template = _mirror_name(template_name) if template_name else None
		doc.append(
			DEFAULTS_FIELD,
			{
				"surface_key": surface,
				"enabled": 1,
				"template_source": "meta" if meta_template else "",
				"meta_template": meta_template,
				"template_key": None,
			},
		)
		added += 1
		print(f"  + {surface} -> {meta_template or '(blank)'}")

	if not added:
		return
	# validate() fills label from the registry and refuses an unregistered surface.
	doc.save(ignore_permissions=True)
	frappe.db.commit()
