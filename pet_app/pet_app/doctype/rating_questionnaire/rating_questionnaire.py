from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr


class RatingQuestionnaire(Document):
	def validate(self):
		self.questionnaire_name = cstr(self.questionnaire_name).strip()
		self.applies_to_doctype = cstr(self.applies_to_doctype).strip()

		if not self.questionnaire_name:
			frappe.throw(_("Questionnaire Name is required."))
		if self.active in (None, ""):
			self.active = 1

		self._normalize_questions()

	def _normalize_questions(self):
		seen = set()

		for row in self.questions or []:
			row.question_text = cstr(row.question_text).strip()
			row.question_key = cstr(row.question_key).strip()
			row.help_text = cstr(row.help_text).strip()

			if not row.question_text:
				frappe.throw(_("Question row {0} needs Question Text.").format(row.idx))

			if not row.question_key:
				row.question_key = _normalize_question_key(row.question_text)
			else:
				row.question_key = _normalize_question_key(row.question_key)

			if not row.question_key:
				frappe.throw(_("Question row {0} needs a valid Question Key.").format(row.idx))
			if row.question_key in seen:
				frappe.throw(_("Duplicate question key {0}.").format(frappe.bold(row.question_key)))

			seen.add(row.question_key)
			if not cint(row.sort_order):
				row.sort_order = row.idx


def _normalize_question_key(value):
	key = frappe.scrub(cstr(value).strip()).lower()
	key = re.sub(r"[^a-z0-9_]+", "_", key)
	key = re.sub(r"_+", "_", key).strip("_")
	return key
