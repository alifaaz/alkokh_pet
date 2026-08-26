"""Let an action rule address a Meta template directly.

Rules could only name a local ``Pet App WhatsApp Template``, so every rule took the
local route and refused at META_TEMPLATE_NOT_LINKED unless that local row happened to
be bound to an approved mirror row. None of the three existing rules is bound.

These two fields mirror the composer's contract exactly - same names, same meanings -
so a rule and a manual send are indistinguishable by the time they reach
queue_notification. There is one addressing vocabulary, not two.

``template_key`` stops being unconditionally mandatory and becomes mandatory only when
the rule is *not* addressing a Meta template. A rule still cannot name nothing: local
rules keep the guarantee they had, and mirror rules are checked by the designer's own
validation instead.
"""

from __future__ import annotations

import frappe

from pet_app.patches.p1_5_notification_engine_schema import ensure_doctype, link


RULE_DOCTYPE = "Pet App WhatsApp Action Rule"
TEMPLATE_KEY_MANDATORY_IF = 'eval:doc.template_source!="meta"'


def execute():
	for doctype, spec in DOCTYPES.items():
		ensure_doctype(doctype, spec)
	relax_template_key()
	frappe.clear_cache()


def relax_template_key():
	"""Make template_key conditionally rather than unconditionally mandatory.

	ensure_doctype only appends missing fields; changing an existing one is done here
	explicitly. reqd must be cleared as well - Frappe treats reqd=1 as always
	mandatory regardless of mandatory_depends_on.
	"""
	if not frappe.db.exists("DocType", RULE_DOCTYPE):
		return
	doc = frappe.get_doc("DocType", RULE_DOCTYPE)
	changed = False
	for field in doc.fields:
		if field.fieldname != "template_key":
			continue
		if field.reqd:
			field.reqd = 0
			changed = True
		if field.mandatory_depends_on != TEMPLATE_KEY_MANDATORY_IF:
			field.mandatory_depends_on = TEMPLATE_KEY_MANDATORY_IF
			changed = True
	if changed:
		doc.save(ignore_permissions=True)


DOCTYPES = {
	RULE_DOCTYPE: {
		"fields": [
			# "local" (or blank) and "meta" - the same two values the queue row stores.
			{"fieldname": "template_source", "fieldtype": "Data", "in_standard_filter": 1},
			link("meta_template", "Pet App WhatsApp Meta Template"),
		],
	},
}
