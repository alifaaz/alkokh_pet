import frappe
from frappe.tests.utils import FrappeTestCase


class TestRatingQuestionnaire(FrappeTestCase):
	def tearDown(self):
		names = frappe.get_all(
			"Rating Questionnaire",
			filters={"questionnaire_name": ["like", "Test Rating Questionnaire%"]},
			pluck="name",
		)
		if names:
			frappe.db.delete("Rating Questionnaire Question", {"parent": ["in", names]})
			frappe.db.delete("Rating Questionnaire", {"name": ["in", names]})

	def test_generates_question_key(self):
		doc = frappe.get_doc(
			{
				"doctype": "Rating Questionnaire",
				"questionnaire_name": "Test Rating Questionnaire Key",
				"questions": [
					{
						"question_text": "How was the visit?",
						"required": 1,
					}
				],
			}
		).insert()

		self.assertEqual(doc.active, 1)
		self.assertEqual(doc.questions[0].question_key, "how_was_the_visit")

	def test_rejects_duplicate_question_keys(self):
		doc = frappe.get_doc(
			{
				"doctype": "Rating Questionnaire",
				"questionnaire_name": "Test Rating Questionnaire Duplicate",
				"questions": [
					{"question_key": "service", "question_text": "Service"},
					{"question_key": "service", "question_text": "Service Again"},
				],
			}
		)

		with self.assertRaises(frappe.ValidationError):
			doc.insert()
