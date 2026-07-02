"""Shared metadata + resolvers for Rating analytics.

Centralizes how a rated entity maps to its performer, human-readable name and
linked pet, plus a lightweight deterministic sentiment lexicon. Used by both the
``Rating`` controller (to denormalize fields at write time) and
``pet_app.api.ratings`` (to enrich/aggregate at read time).
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr, flt

PRACTITIONER_DOCTYPE = "Healthcare Practitioner"

# All doctypes that can be rated. Order is used for stable reporting output.
RATABLE_DOCTYPES = [
	"PetCareService",
	"Lab",
	"Imaging",
	"Pet Procedure",
	"Vet Visit",
]

# Per-doctype mapping:
#   performer_fields  -> ordered candidate fields holding the performer id
#   performer_doctype -> doctype the performer id belongs to
#   entity_name       -> {"field", optional "link_doctype", optional "link_field"}
#                        if link_doctype set, resolve that linked record's
#                        link_field (or its title) for a human-readable name
#   pet_field         -> field linking to the rated entity's Pet
ENTITY_CONFIG = {
	"PetCareService": {
		"performer_fields": ["provider", "doctor"],
		"performer_doctype": PRACTITIONER_DOCTYPE,
		"entity_name": {"field": "pet_service_name"},
		"pet_field": "pet_id",
	},
	"Lab": {
		"performer_fields": ["doctor"],
		"performer_doctype": PRACTITIONER_DOCTYPE,
		"entity_name": {
			"field": "care_service",
			"link_doctype": "CareService template",
			"link_field": "service_name",
		},
		"pet_field": "pet",
	},
	"Imaging": {
		"performer_fields": ["doctor"],
		"performer_doctype": PRACTITIONER_DOCTYPE,
		"entity_name": {
			"field": "care_service",
			"link_doctype": "CareService template",
			"link_field": "service_name",
		},
		"pet_field": "pet",
	},
	"Pet Procedure": {
		"performer_fields": ["doctor", "provider"],
		"performer_doctype": PRACTITIONER_DOCTYPE,
		"entity_name": {"field": "procedure_template"},
		"pet_field": "pet",
	},
	"Vet Visit": {
		"performer_fields": ["doctor"],
		"performer_doctype": PRACTITIONER_DOCTYPE,
		"entity_name": {"field": "visit_type"},
		"pet_field": "animal_patient",
	},
}


# ─────────────────────────────────────────
# Reference / entity resolution
# ─────────────────────────────────────────

def get_title(doctype, name):
	"""Human-readable title for a record, falling back to its name."""
	if not doctype or not name:
		return name
	try:
		meta = frappe.get_meta(doctype)
		title_field = meta.title_field or "name"
		return frappe.db.get_value(doctype, name, title_field) or name
	except Exception:
		return name


def resolve_performer(reference_doctype, reference_name):
	"""Return (performer_doctype, performer_id) for a rated entity, or (None, None).

	Reads the first non-empty candidate field for the doctype.
	"""
	config = ENTITY_CONFIG.get(reference_doctype)
	if not config or not reference_name:
		return None, None

	fields = config["performer_fields"]
	values = frappe.db.get_value(reference_doctype, reference_name, fields, as_dict=True) or {}
	for field in fields:
		value = values.get(field)
		if value:
			return config["performer_doctype"], value
	return None, None


def resolve_performer_name(performer_doctype, performer_id):
	if not performer_id:
		return None
	if not performer_doctype:
		return performer_id
	return get_title(performer_doctype, performer_id) or performer_id


def resolve_entity_name(reference_doctype, reference_name):
	"""Real human name of the rated thing (service/test/study name)."""
	config = ENTITY_CONFIG.get(reference_doctype)
	if not config or not reference_name:
		return get_title(reference_doctype, reference_name)

	spec = config["entity_name"]
	value = frappe.db.get_value(reference_doctype, reference_name, spec["field"])
	if not value:
		return None
	if spec.get("link_doctype"):
		link_field = spec.get("link_field")
		if link_field:
			return frappe.db.get_value(spec["link_doctype"], value, link_field) or value
		return get_title(spec["link_doctype"], value)
	return value


def resolve_pet_name(reference_doctype, reference_name):
	config = ENTITY_CONFIG.get(reference_doctype)
	if not config or not reference_name:
		return None
	pet = frappe.db.get_value(reference_doctype, reference_name, config["pet_field"])
	if not pet:
		return None
	return frappe.db.get_value("Pet", pet, "pet_name") or pet


# ─────────────────────────────────────────
# Sentiment (deterministic keyword lexicon, v1)
# ─────────────────────────────────────────

POSITIVE_WORDS = {
	"good", "great", "excellent", "amazing", "awesome", "wonderful", "fantastic",
	"happy", "satisfied", "love", "loved", "friendly", "kind", "caring", "clean",
	"helpful", "professional", "recommend", "recommended", "best", "perfect",
	"nice", "pleasant", "gentle", "fast", "quick", "thorough", "attentive",
	"smooth", "comfortable", "trust", "trusted", "reliable", "patient",
}

NEGATIVE_WORDS = {
	"bad", "terrible", "awful", "horrible", "poor", "worst", "disappointed",
	"disappointing", "unhappy", "rude", "unfriendly", "dirty", "slow", "late",
	"delay", "delayed", "wait", "waiting", "unprofessional", "rough", "painful",
	"expensive", "overpriced", "mistake", "error", "ignored", "careless",
	"unhelpful", "avoid", "never", "worse", "cold", "dismissive", "neglect",
}

_TOKEN_RE = None


def compute_sentiment(notes):
	"""Return (sentiment, score) for free-text notes.

	Returns (None, None) when notes is empty. Score is clamped to -1..1.
	"""
	text = cstr(notes).strip().lower()
	if not text:
		return None, None

	global _TOKEN_RE
	if _TOKEN_RE is None:
		import re

		_TOKEN_RE = re.compile(r"[a-z']+")

	tokens = _TOKEN_RE.findall(text)
	positive = sum(1 for token in tokens if token in POSITIVE_WORDS)
	negative = sum(1 for token in tokens if token in NEGATIVE_WORDS)
	total = positive + negative

	if not total:
		return "neutral", 0.0

	score = round((positive - negative) / total, 3)
	if score > 0.05:
		sentiment = "positive"
	elif score < -0.05:
		sentiment = "negative"
	else:
		sentiment = "neutral"
	return sentiment, flt(score)
