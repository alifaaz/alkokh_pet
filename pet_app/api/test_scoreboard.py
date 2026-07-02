"""Tests for the user Scoreboard API (pet_app.api.scoreboard).

Exercises the acceptance checks from the Scoreboard Backend spec against a
self-contained fixture set:

  1. records_created.total == sum(by_doctype[].count)
  2. provider.distribution sums to reviews_received; average_received matches a
     manual recompute over the same filtered ratings
  3. a user with no ratings given -> rater null + is_rater false; a non-provider
     -> provider null + is_service_provider false
  4. from_date / to_date narrows every section consistently
  5. a non-privileged caller passing someone else's user_id gets their own data

Like test_ratings.py, ratings target ``Rating Questionnaire`` records (a
self-referential ratable entity that is not in ENTITY_CONFIG), so the Rating
controller cannot resolve a performer from them and we supply performer_id
directly. Owned records are created *as* the subject user (owner = that user) so
they land in records_created; received ratings are created as Administrator with
a foreign rated_by so they only affect the provider section.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api import scoreboard

PREFIX = "Scoreboard Test"
OLD_DATE = "2020-01-01 12:00:00"  # before any plausible narrow window


def _data(envelope):
	assert isinstance(envelope, dict), f"expected dict envelope, got {type(envelope)}"
	assert envelope.get("ok") is True, f"endpoint failed: {envelope.get('errors')}"
	return envelope["data"]


class TestUserScoreboard(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._original_user = frappe.session.user
		frappe.set_user("Administrator")

		cls.subject = cls._make_user("subject")
		cls.outsider = cls._make_user("outsider")  # non-privileged caller / non-provider
		cls.practitioner = cls._make_practitioner(cls.subject)

		# 4 ratings GIVEN by (and owned by) the subject -> records_created + rater.
		# performer_id is a foreign practitioner so they are not "received".
		cls.given = [
			cls._rate(rated_by=cls.subject, owner=cls.subject, overall_rating=r,
					  performer_id="EXTERNAL-HCP")
			for r in (5, 4, 3, 2)
		]
		# Backdate 2 of them so a recent window excludes exactly those.
		for name in (cls.given[2], cls.given[3]):
			frappe.db.set_value("Rating", name, {"creation": OLD_DATE, "rated_at": OLD_DATE},
								update_modified=False)

		# 5 ratings RECEIVED by the subject's practitioner -> provider section.
		# Created as Administrator (foreign owner + rated_by) so they do not leak
		# into records_created or the subject's rater section.
		cls.received = [
			cls._rate(rated_by="Administrator", overall_rating=r,
					  performer_id=cls.practitioner)
			for r in (5, 5, 4, 4, 3)
		]
		for name in (cls.received[3], cls.received[4]):
			frappe.db.set_value("Rating", name, {"rated_at": OLD_DATE}, update_modified=False)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		ratings = frappe.get_all(
			"Rating", filters={"reference_name": ["like", f"{PREFIX}%"]}, pluck="name"
		)
		for name in ratings:
			frappe.delete_doc("Rating", name, force=True, ignore_permissions=True)
		for name in frappe.get_all(
			"Rating Questionnaire", filters={"questionnaire_name": ["like", f"{PREFIX}%"]}, pluck="name"
		):
			frappe.delete_doc("Rating Questionnaire", name, force=True, ignore_permissions=True)
		if frappe.db.exists("Healthcare Practitioner", cls.practitioner):
			frappe.delete_doc("Healthcare Practitioner", cls.practitioner, force=True, ignore_permissions=True)
		for user in (cls.subject, cls.outsider):
			if frappe.db.exists("User", user):
				frappe.delete_doc("User", user, force=True, ignore_permissions=True)
		frappe.db.commit()
		frappe.set_user(cls._original_user)
		super().tearDownClass()

	# ── fixture builders ─────────────────────────────────────

	@classmethod
	def _make_user(cls, label):
		email = f"scoreboard.{label}.{frappe.generate_hash(length=6)}@example.com"
		return frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": f"{PREFIX} {label}",
			"user_type": "System User",
			"send_welcome_email": 0,
		}).insert(ignore_permissions=True).name

	@classmethod
	def _make_practitioner(cls, user):
		return frappe.get_doc({
			"doctype": "Healthcare Practitioner",
			"practitioner_name": f"{PREFIX} Practitioner",
			"practitioner_type": "Doctor",
			"phone": "0700000000",
			"user_id": user,
			"disabled": 0,
		}).insert(ignore_permissions=True).name

	@classmethod
	def _make_questionnaire(cls):
		name = f"{PREFIX} {frappe.generate_hash(length=8)}"
		return frappe.get_doc({
			"doctype": "Rating Questionnaire",
			"questionnaire_name": name,
			"active": 1,
		}).insert(ignore_permissions=True).name

	@classmethod
	def _rate(cls, rated_by, overall_rating, performer_id, owner=None):
		"""Insert one Rating on a fresh questionnaire (sidesteps the dup guard)."""
		target = cls._make_questionnaire()
		doc = frappe.get_doc({
			"doctype": "Rating",
			"reference_doctype": "Rating Questionnaire",
			"reference_name": target,
			"overall_rating": overall_rating,
			"rated_by": rated_by,
			"performer_doctype": "Healthcare Practitioner",
			"performer_id": performer_id,
		}).insert(ignore_permissions=True)
		if owner:
			frappe.db.set_value("Rating", doc.name, "owner", owner, update_modified=False)
		return doc.name

	# ── criterion #1: records_created total parity ───────────

	def test_records_created_total_matches_breakdown(self):
		frappe.set_user("Administrator")
		data = _data(scoreboard.get_user_scoreboard(user_id=self.subject))
		rc = data["records_created"]
		self.assertEqual(rc["total"], sum(d["count"] for d in rc["by_doctype"]))
		# The subject owns exactly the 4 given ratings.
		self.assertEqual(rc["total"], 4)
		ratings_row = next(d for d in rc["by_doctype"] if d["doctype"] == "Rating")
		self.assertEqual(ratings_row["count"], 4)

	# ── criterion #2: provider distribution + average recompute

	def test_provider_distribution_and_average(self):
		frappe.set_user("Administrator")
		data = _data(scoreboard.get_user_scoreboard(user_id=self.subject))
		self.assertTrue(data["identity"]["is_service_provider"])
		prov = data["provider"]
		self.assertEqual(prov["reviews_received"], 5)
		self.assertEqual(sum(prov["distribution"].values()), prov["reviews_received"])
		self.assertEqual(prov["distribution"], {"1": 0, "2": 0, "3": 1, "4": 2, "5": 2})

		rows = frappe.get_all("Rating", filters={"performer_id": self.practitioner},
							  fields=["overall_rating"], ignore_permissions=True)
		manual = round(sum(int(r["overall_rating"]) for r in rows) / len(rows), 2)
		self.assertEqual(prov["average_received"], manual)
		self.assertEqual(prov["stars_received"], sum(int(r["overall_rating"]) for r in rows))

	# ── criterion #3: empty rater / non-provider ─────────────

	def test_rater_and_provider_null_for_outsider(self):
		frappe.set_user("Administrator")
		data = _data(scoreboard.get_user_scoreboard(user_id=self.outsider))
		self.assertIsNone(data["rater"])
		self.assertFalse(data["identity"]["is_rater"])
		self.assertIsNone(data["provider"])
		self.assertFalse(data["identity"]["is_service_provider"])

	def test_subject_is_rater(self):
		frappe.set_user("Administrator")
		data = _data(scoreboard.get_user_scoreboard(user_id=self.subject))
		self.assertTrue(data["identity"]["is_rater"])
		self.assertEqual(data["rater"]["ratings_given"], 4)

	# ── criterion #4: date range narrows every section ───────

	def test_date_range_narrows_sections(self):
		frappe.set_user("Administrator")
		full = _data(scoreboard.get_user_scoreboard(user_id=self.subject))
		narrow = _data(scoreboard.get_user_scoreboard(
			user_id=self.subject, from_date="2024-01-01", to_date="2099-01-01"))

		self.assertEqual(full["records_created"]["total"], 4)
		self.assertEqual(narrow["records_created"]["total"], 2)  # 2 backdated to 2020 excluded
		self.assertEqual(full["rater"]["ratings_given"], 4)
		self.assertEqual(narrow["rater"]["ratings_given"], 2)
		self.assertEqual(full["provider"]["reviews_received"], 5)
		self.assertEqual(narrow["provider"]["reviews_received"], 3)  # 2 received backdated

		# trend bucket sums stay consistent with each section's totals
		self.assertEqual(sum(b["records_created"] for b in narrow["trend"]),
						 narrow["records_created"]["total"])
		self.assertEqual(sum(b["ratings_given"] for b in narrow["trend"]),
						 narrow["rater"]["ratings_given"])

	# ── criterion #5: non-privileged caller cannot view others

	def test_non_privileged_caller_gets_own_scoreboard(self):
		frappe.set_user(self.outsider)
		try:
			data = _data(scoreboard.get_user_scoreboard(user_id=self.subject))
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(data["identity"]["user_id"], self.outsider)
		self.assertNotEqual(data["identity"]["user_id"], self.subject)
