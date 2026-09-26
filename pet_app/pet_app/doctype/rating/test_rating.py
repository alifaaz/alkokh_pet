import frappe
from frappe.tests.utils import FrappeTestCase


class TestRating(FrappeTestCase):
	def tearDown(self):
		rating_names = set(
			frappe.get_all("Rating", filters={"reference_name": ["like", "Test Rating%"]}, pluck="name")
		)
		rating_names.update(
			frappe.get_all("Rating", filters={"questionnaire": ["like", "Test Rating%"]}, pluck="name")
		)
		if rating_names:
			frappe.db.delete("Rating Answer", {"parent": ["in", list(rating_names)]})
			frappe.db.delete("Rating", {"name": ["in", list(rating_names)]})

		questionnaire_names = frappe.get_all(
			"Rating Questionnaire",
			filters={"questionnaire_name": ["like", "Test Rating%"]},
			pluck="name",
		)
		if questionnaire_names:
			frappe.db.delete("Rating Questionnaire Question", {"parent": ["in", questionnaire_names]})
			frappe.db.delete("Rating Questionnaire", {"name": ["in", questionnaire_names]})

	def test_accepts_rating_boundaries(self):
		for value in (1, 5):
			target = self._make_questionnaire("Test Rating Target Boundary")
			doc = self._make_rating(target, overall_rating=value)
			self.assertEqual(doc.overall_rating, value)
			self.assertEqual(doc.rated_by, frappe.session.user)
			self.assertTrue(doc.rated_at)

	def test_rejects_out_of_range_overall_rating(self):
		for value in (0, 6):
			target = self._make_questionnaire("Test Rating Target Out Of Range")
			doc = frappe.get_doc(
				{
					"doctype": "Rating",
					"reference_doctype": "Rating Questionnaire",
					"reference_name": target.name,
					"overall_rating": value,
				}
			)

			with self.assertRaises(frappe.ValidationError):
				doc.insert()

	def test_rejects_missing_dynamic_reference(self):
		doc = frappe.get_doc(
			{
				"doctype": "Rating",
				"reference_doctype": "Rating Questionnaire",
				"reference_name": "Test Rating Missing Target",
				"overall_rating": 5,
			}
		)

		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_questionnaire_scope(self):
		target = self._make_questionnaire("Test Rating Target Scope")
		global_questionnaire = self._make_questionnaire("Test Rating Global Questionnaire")
		scoped_questionnaire = self._make_questionnaire(
			"Test Rating Scoped Questionnaire",
			applies_to_doctype="Rating Questionnaire",
		)
		wrong_scope_questionnaire = self._make_questionnaire(
			"Test Rating Wrong Scope Questionnaire",
			applies_to_doctype="User",
		)

		self._make_rating(target, questionnaire=global_questionnaire.name)
		self._make_rating(target, questionnaire=scoped_questionnaire.name)

		doc = self._rating_doc(target, questionnaire=wrong_scope_questionnaire.name)
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_required_answers_and_answer_rating_range(self):
		target = self._make_questionnaire("Test Rating Target Required")
		questionnaire = self._make_questionnaire(
			"Test Rating Required Questionnaire",
			questions=[
				{
					"question_key": "staff",
					"question_text": "Staff",
					"required": 1,
				}
			],
		)

		missing_answer = self._rating_doc(target, questionnaire=questionnaire.name)
		with self.assertRaises(frappe.ValidationError):
			missing_answer.insert()

		invalid_answer = self._rating_doc(
			target,
			questionnaire=questionnaire.name,
			answers=[{"question_key": "staff", "rating": 6}],
		)
		with self.assertRaises(frappe.ValidationError):
			invalid_answer.insert()

		valid_answer = self._make_rating(
			target,
			questionnaire=questionnaire.name,
			answers=[{"question_key": "staff", "rating": 5}],
		)
		self.assertEqual(valid_answer.answers[0].question_text, "Staff")
		self.assertEqual(valid_answer.answers[0].required, 1)

	def test_prevents_duplicate_rating_per_user_target_and_questionnaire(self):
		target = self._make_questionnaire("Test Rating Target Duplicate")
		self._make_rating(target)

		duplicate = self._rating_doc(target)
		with self.assertRaises(frappe.ValidationError):
			duplicate.insert()

	def test_untyped_write_resolves_to_internal(self):
		"""The staff Desk/REST path types itself without a frontend change.

		A DocType field default would not do this: Frappe applies defaults in new_doc(),
		not in frappe.get_doc({...}).insert(), which is the path /api/resource/Rating
		takes and the path every existing staff rating on this site was written through.
		"""
		target = self._make_questionnaire("Test Rating Target Untyped")
		doc = self._make_rating(target)
		self.assertEqual(doc.rating_type, "Internal")

	def test_rejects_unknown_rating_type(self):
		target = self._make_questionnaire("Test Rating Target Bad Type")
		doc = self._rating_doc(target)
		doc.rating_type = "Staff"
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_rejects_guardian_reference_on_an_internal_rating(self):
		"""The guardian reference and the type are one fact; they may not disagree."""
		target = self._make_questionnaire("Test Rating Target Type Mismatch")
		guardian = frappe.get_all("Guardian", pluck="name", limit=1)
		if not guardian:
			self.skipTest("No Guardian on this site to reference.")
		doc = self._rating_doc(target)
		doc.rating_type = "Internal"
		doc.rated_by_guardian = guardian[0]
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_internal_and_customer_ratings_of_one_reference_do_not_collide(self):
		"""The type joins the uniqueness tuple, so the two sides are separate rows.

		Without this, adding the column would have been worse than leaving it out:
		readers would filter on a field whose value the last writer happened to win.
		"""
		target = self._make_questionnaire("Test Rating Target Both Types")
		guardian = frappe.get_all("Guardian", pluck="name", limit=1)
		if not guardian:
			self.skipTest("No Guardian on this site to reference.")
		internal = self._make_rating(target, overall_rating=4)
		self.assertEqual(internal.rating_type, "Internal")

		customer = self._rating_doc(target, overall_rating=1)
		customer.rating_type = "Customer"
		customer.rated_by_guardian = guardian[0]
		customer.insert()

		internal.reload()
		self.assertEqual(internal.overall_rating, 4)
		self.assertNotEqual(customer.name, internal.name)

	def _make_questionnaire(self, label, **kwargs):
		name = f"{label} {frappe.generate_hash(length=8)}"
		doc = frappe.get_doc(
			{
				"doctype": "Rating Questionnaire",
				"questionnaire_name": name,
				"active": kwargs.get("active", 1),
				"applies_to_doctype": kwargs.get("applies_to_doctype"),
				"questions": kwargs.get("questions", []),
			}
		)
		return doc.insert()

	def _rating_doc(self, target, **kwargs):
		return frappe.get_doc(
			{
				"doctype": "Rating",
				"reference_doctype": "Rating Questionnaire",
				"reference_name": target.name,
				"overall_rating": kwargs.get("overall_rating", 5),
				"questionnaire": kwargs.get("questionnaire"),
				"answers": kwargs.get("answers", []),
			}
		)

	def _make_rating(self, target, **kwargs):
		return self._rating_doc(target, **kwargs).insert()
