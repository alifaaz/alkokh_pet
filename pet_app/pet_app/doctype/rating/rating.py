from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, now_datetime


class Rating(Document):
	def validate(self):
		self._normalize_defaults()
		self._validate_reference()
		self._validate_overall_rating()
		questionnaire = self._validate_questionnaire()
		self._validate_answers(questionnaire)
		self._validate_duplicate_rating()

	def _normalize_defaults(self):
		self.reference_doctype = cstr(self.reference_doctype).strip()
		self.reference_name = cstr(self.reference_name).strip()
		self.questionnaire = cstr(self.questionnaire).strip()
		self.notes = cstr(self.notes).strip()

		if not self.rated_by:
			self.rated_by = frappe.session.user
		if not self.rated_at:
			self.rated_at = now_datetime()

	def _validate_reference(self):
		if not self.reference_doctype:
			frappe.throw(_("Reference DocType is required."))
		if not self.reference_name:
			frappe.throw(_("Reference Name is required."))
		if not frappe.db.exists("DocType", self.reference_doctype):
			frappe.throw(_("Reference DocType {0} does not exist.").format(frappe.bold(self.reference_doctype)))
		if not frappe.db.exists(self.reference_doctype, self.reference_name):
			frappe.throw(
				_("{0} {1} does not exist.").format(
					frappe.bold(self.reference_doctype),
					frappe.bold(self.reference_name),
				)
			)

	def _validate_overall_rating(self):
		if self.overall_rating in (None, ""):
			frappe.throw(_("Overall Rating is required."))

		self.overall_rating = cint(self.overall_rating)
		if self.overall_rating < 1 or self.overall_rating > 5:
			frappe.throw(_("Overall Rating must be between 1 and 5."))

	def _validate_questionnaire(self):
		if not self.questionnaire:
			if self.answers:
				frappe.throw(_("Answers require a Rating Questionnaire."))
			return None

		questionnaire = frappe.get_doc("Rating Questionnaire", self.questionnaire)
		if not cint(questionnaire.active):
			frappe.throw(_("Rating Questionnaire {0} is inactive.").format(frappe.bold(self.questionnaire)))
		if questionnaire.applies_to_doctype and questionnaire.applies_to_doctype != self.reference_doctype:
			frappe.throw(
				_("Rating Questionnaire {0} only applies to {1}.").format(
					frappe.bold(self.questionnaire),
					frappe.bold(questionnaire.applies_to_doctype),
				)
			)

		return questionnaire

	def _validate_answers(self, questionnaire):
		if not questionnaire:
			return

		questions = {question.question_key: question for question in questionnaire.questions or []}
		required_keys = {
			question.question_key for question in questionnaire.questions or [] if cint(question.required)
		}
		answered_required_keys = set()
		seen = set()

		for row in self.answers or []:
			row.question_key = _normalize_question_key(row.question_key)
			row.note = cstr(row.note).strip()

			if not row.question_key:
				frappe.throw(_("Answer row {0} needs Question Key.").format(row.idx))
			if row.question_key in seen:
				frappe.throw(_("Duplicate answer for question {0}.").format(frappe.bold(row.question_key)))
			if row.question_key not in questions:
				frappe.throw(
					_("Answer row {0} does not match the selected questionnaire.").format(row.idx)
				)

			seen.add(row.question_key)
			question = questions[row.question_key]
			row.question_text = question.question_text
			row.required = cint(question.required)
			if not cint(row.sort_order):
				row.sort_order = question.sort_order or question.idx

			if row.rating not in (None, ""):
				row.rating = cint(row.rating)
				if row.rating < 1 or row.rating > 5:
					frappe.throw(
						_("Rating for question {0} must be between 1 and 5.").format(
							frappe.bold(question.question_text)
						)
					)
				if row.question_key in required_keys:
					answered_required_keys.add(row.question_key)

		missing_required = required_keys - answered_required_keys
		if missing_required:
			labels = [questions[key].question_text for key in sorted(missing_required)]
			frappe.throw(_("Required rating answers are missing: {0}").format(", ".join(labels)))

	def _validate_duplicate_rating(self):
		existing = frappe.db.sql(
			"""
			select name
			from `tabRating`
			where rated_by = %s
				and reference_doctype = %s
				and reference_name = %s
				and ifnull(questionnaire, '') = %s
				and name != %s
			limit 1
			""",
			(
				self.rated_by,
				self.reference_doctype,
				self.reference_name,
				self.questionnaire or "",
				self.name or "",
			),
		)
		if existing:
			frappe.throw(_("This user has already rated the selected reference."))


def _normalize_question_key(value):
	key = frappe.scrub(cstr(value).strip()).lower()
	key = re.sub(r"[^a-z0-9_]+", "_", key)
	key = re.sub(r"_+", "_", key).strip("_")
	return key
