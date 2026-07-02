"""Tests for the Ratings analytics API (pet_app.api.ratings).

These exercise the whitelisted endpoints end-to-end against the acceptance
criteria in the Advanced Ratings Backend spec:

  1. analytics.stats.total parity with get_ratings.stats.total (same filters)
  2. each get_ratings record self-describes (entity_name / performer_name / ...)
  3. providers[] averages match a manual recompute grouped by performer_id
  4. tags persist and surface in top_tags; sentiment set when notes present
  5. date-range + performer_id + order_by filter/sort/paginate, with a
     stats.total that reflects the *filtered* set (not just the page)

Ratings target ``Rating Questionnaire`` records as a self-referential ratable
entity (mirrors test_rating.py) so no Pet/Practitioner fixtures are needed.
``Rating Questionnaire`` is not in ENTITY_CONFIG, so the controller cannot
resolve a performer from it -- performer_* are supplied directly on insert.

The Rating controller forbids the same user rating the same (reference_name,
questionnaire) twice. Tests run as a single manager user (Administrator, for
full _apply_scope visibility), so to create N ratings we either use N distinct
target records or N distinct ``questionnaire`` values on one target.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.api import ratings
from pet_app.utils.rating_entities import RATABLE_DOCTYPES, compute_sentiment

TAG_PREFIX = "Test Rating"


def _data(envelope):
	"""Unwrap the @standardize_response envelope, asserting success."""
	assert isinstance(envelope, dict), f"expected dict envelope, got {type(envelope)}"
	assert envelope.get("ok") is True, f"endpoint failed: {envelope.get('errors')}"
	return envelope["data"]


class TestRatingsAnalytics(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._original_user = frappe.session.user
		# Manager scope -> _apply_scope returns every row, so analytics covers all.
		frappe.set_user("Administrator")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user(cls._original_user)
		super().tearDownClass()

	def tearDown(self):
		rating_names = set(
			frappe.get_all(
				"Rating", filters={"reference_name": ["like", f"{TAG_PREFIX}%"]}, pluck="name"
			)
		)
		rating_names.update(
			frappe.get_all(
				"Rating", filters={"questionnaire": ["like", f"{TAG_PREFIX}%"]}, pluck="name"
			)
		)
		if rating_names:
			names = list(rating_names)
			frappe.db.delete("Rating Tag Selection", {"parent": ["in", names]})
			frappe.db.delete("Rating Answer", {"parent": ["in", names]})
			frappe.db.delete("Rating", {"name": ["in", names]})

		questionnaire_names = frappe.get_all(
			"Rating Questionnaire",
			filters={"questionnaire_name": ["like", f"{TAG_PREFIX}%"]},
			pluck="name",
		)
		if questionnaire_names:
			frappe.db.delete("Rating Questionnaire Tag", {"parent": ["in", questionnaire_names]})
			frappe.db.delete(
				"Rating Questionnaire Question", {"parent": ["in", questionnaire_names]}
			)
			frappe.db.delete("Rating Questionnaire", {"name": ["in", questionnaire_names]})
		frappe.db.commit()

	# ── fixtures ─────────────────────────────────────────────

	def _make_questionnaire(self, label="Target", **kwargs):
		name = f"{TAG_PREFIX} {label} {frappe.generate_hash(length=8)}"
		return frappe.get_doc(
			{
				"doctype": "Rating Questionnaire",
				"questionnaire_name": name,
				"active": 1,
				"applies_to_doctype": kwargs.get("applies_to_doctype"),
				"questions": kwargs.get("questions", []),
				"tags": kwargs.get("tags", []),
			}
		).insert()

	def _rate(self, target=None, **kwargs):
		"""Create one Rating. With no ``target`` a fresh one is made so each
		rating has a unique reference_name (sidesteps the duplicate guard)."""
		if target is None:
			target = self._make_questionnaire(kwargs.pop("label", "Target"))
		doc = frappe.get_doc(
			{
				"doctype": "Rating",
				"reference_doctype": "Rating Questionnaire",
				"reference_name": target.name,
				"overall_rating": kwargs.get("overall_rating", 5),
				"questionnaire": kwargs.get("questionnaire"),
				"notes": kwargs.get("notes"),
				"answers": kwargs.get("answers", []),
				"tags": kwargs.get("tags", []),
				"performer_doctype": kwargs.get("performer_doctype"),
				"performer_id": kwargs.get("performer_id"),
				"performer_name": kwargs.get("performer_name"),
			}
		)
		if "rated_at" in kwargs:
			doc.rated_at = kwargs["rated_at"]
		return doc.insert()

	# ── criterion #1: analytics/get_ratings total parity ─────

	def test_analytics_total_matches_get_ratings(self):
		for value in (5, 4, 3):
			self._rate(overall_rating=value)

		analytics = _data(ratings.get_ratings_analytics(reference_doctype="Rating Questionnaire"))
		listing = _data(
			ratings.get_ratings(reference_doctype="Rating Questionnaire", limit_page_length=1)
		)

		self.assertEqual(analytics["stats"]["total"], listing["stats"]["total"])
		# stats.total reflects the full filtered set even though we paged to 1.
		self.assertGreaterEqual(analytics["stats"]["total"], 3)
		self.assertEqual(len(listing["ratings"]), 1)

	# ── criterion #2: records self-describe (no follow-up fetch) ──

	def test_get_ratings_records_self_describe(self):
		target = self._rate(
			notes="Friendly and clean, great service",
			performer_doctype="User",
			performer_id="Administrator",
		).reference_name

		listing = _data(ratings.get_ratings(reference_doctype="Rating Questionnaire"))
		record = next(r for r in listing["ratings"] if r["reference_name"] == target)

		for field in (
			"performer_id",
			"performer_name",
			"entity_name",
			"pet_name",
			"tags",
			"sentiment",
			"sentiment_score",
			"reference_title",
			"rated_by_name",
		):
			self.assertIn(field, record, f"missing self-describe field: {field}")

		# entity_name resolves the human title (here the questionnaire name).
		self.assertEqual(record["entity_name"], target)
		# performer_name is denormalized from performer_id by the controller.
		self.assertEqual(record["performer_name"], "Administrator")
		self.assertIsInstance(record["tags"], list)
		self.assertEqual(record["sentiment"], "positive")

	# ── criterion #3: providers averages match manual recompute ──

	def test_providers_average_recompute(self):
		spec = [
			("PERF-A", "Dr A", [5, 5, 3]),
			("PERF-B", "Dr B", [4, 2]),
		]
		for pid, pname, values in spec:
			for value in values:
				# Distinct targets -> distinct reference_names -> N reviews allowed.
				self._rate(
					overall_rating=value,
					performer_doctype="User",
					performer_id=pid,
					performer_name=pname,
				)

		analytics = _data(ratings.get_ratings_analytics(reference_doctype="Rating Questionnaire"))
		by_id = {p["performer_id"]: p for p in analytics["providers"]}

		for pid, _pname, values in spec:
			self.assertIn(pid, by_id)
			provider = by_id[pid]
			self.assertEqual(provider["reviews"], len(values))
			self.assertEqual(provider["entities"], len(values))  # one target per rating
			self.assertEqual(provider["average"], round(sum(values) / len(values), 2))

		# Sorted by average desc.
		averages = [p["average"] for p in analytics["providers"]]
		self.assertEqual(averages, sorted(averages, reverse=True))

	def test_providers_skip_ratings_without_performer(self):
		self._rate(overall_rating=5)  # no performer_id supplied/resolvable
		analytics = _data(ratings.get_ratings_analytics(reference_doctype="Rating Questionnaire"))
		self.assertTrue(all(p["performer_id"] for p in analytics["providers"]))

	# ── criterion #4: tags persist + surface; sentiment from notes ──

	def test_tags_persist_and_surface_in_top_tags(self):
		target = self._make_questionnaire(
			"Tags",
			tags=[
				{"tag_key": "friendly", "tag_label": "Friendly", "sort_order": 1},
				{"tag_key": "clean", "tag_label": "Clean", "sort_order": 2},
			],
		)
		rating = self._rate(
			target,
			overall_rating=5,
			notes="Staff were friendly and the clinic was clean",
			questionnaire=target.name,
			tags=[{"tag_key": "friendly"}, {"tag_key": "clean"}],
		)

		# Persisted on the doc, with labels denormalized from the questionnaire.
		saved = frappe.get_doc("Rating", rating.name)
		labels = {row.tag_key: row.tag_label for row in saved.tags}
		self.assertEqual(labels, {"friendly": "Friendly", "clean": "Clean"})
		self.assertIsNotNone(saved.sentiment)

		# Surfaced in get_ratings record.
		listing = _data(ratings.get_ratings(reference_doctype="Rating Questionnaire"))
		record = next(r for r in listing["ratings"] if r["name"] == rating.name)
		self.assertEqual({t["tag_key"] for t in record["tags"]}, {"friendly", "clean"})

		# Surfaced + aggregated in analytics.top_tags.
		analytics = _data(ratings.get_ratings_analytics(reference_doctype="Rating Questionnaire"))
		top = {t["tag_key"]: t for t in analytics["top_tags"]}
		self.assertIn("friendly", top)
		self.assertEqual(top["friendly"]["tag_label"], "Friendly")
		self.assertEqual(top["friendly"]["count"], 1)
		self.assertEqual(top["friendly"]["avg_rating"], 5.0)

	def test_sentiment_null_when_no_notes(self):
		rating = self._rate(overall_rating=4)  # notes empty
		saved = frappe.get_doc("Rating", rating.name)
		self.assertIsNone(saved.sentiment)
		# Sanity: the lexicon agrees empty notes -> (None, None).
		self.assertEqual(compute_sentiment(""), (None, None))

		# API contract: sentiment_score is null (not 0.0) when no sentiment.
		listing = _data(ratings.get_ratings(reference_name=rating.reference_name))
		record = listing["ratings"][0]
		self.assertIsNone(record["sentiment"])
		self.assertIsNone(record["sentiment_score"])

	# ── criterion #5: filters + order_by + pagination ────────

	def test_filter_by_performer_id(self):
		self._rate(
			overall_rating=5, performer_doctype="User",
			performer_id="WANTED", performer_name="Wanted",
		)
		self._rate(
			overall_rating=2, performer_doctype="User",
			performer_id="OTHER", performer_name="Other",
		)

		listing = _data(
			ratings.get_ratings(
				reference_doctype="Rating Questionnaire", performer_id="WANTED"
			)
		)
		self.assertTrue(listing["ratings"])
		self.assertTrue(all(r["performer_id"] == "WANTED" for r in listing["ratings"]))
		self.assertEqual(listing["stats"]["total"], 1)

	def test_order_by_variants(self):
		# Same target, distinct questionnaire values -> 3 ratings allowed.
		target = self._make_questionnaire("OrderByTarget")
		for value in (1, 5, 3):
			q = self._make_questionnaire(f"OrderByQ{value}")
			self._rate(target, overall_rating=value, questionnaire=q.name)

		asc = _data(
			ratings.get_ratings(reference_name=target.name, order_by="overall_rating asc")
		)["ratings"]
		desc = _data(
			ratings.get_ratings(reference_name=target.name, order_by="overall_rating desc")
		)["ratings"]
		asc_values = [r["overall_rating"] for r in asc]
		desc_values = [r["overall_rating"] for r in desc]
		self.assertEqual(asc_values, sorted(asc_values))
		self.assertEqual(desc_values, sorted(desc_values, reverse=True))

	def test_pagination_with_filtered_total(self):
		target = self._make_questionnaire("PaginateTarget")
		for i in range(5):
			q = self._make_questionnaire(f"PaginateQ{i}")
			self._rate(target, overall_rating=4, questionnaire=q.name)

		page1 = _data(
			ratings.get_ratings(reference_name=target.name, limit_start=0, limit_page_length=2)
		)
		page2 = _data(
			ratings.get_ratings(reference_name=target.name, limit_start=2, limit_page_length=2)
		)
		self.assertEqual(len(page1["ratings"]), 2)
		self.assertEqual(len(page2["ratings"]), 2)
		# stats.total counts the whole filtered set, not the page.
		self.assertEqual(page1["stats"]["total"], 5)
		self.assertEqual(page2["stats"]["total"], 5)
		# Pages don't overlap.
		self.assertFalse(
			{r["name"] for r in page1["ratings"]} & {r["name"] for r in page2["ratings"]}
		)

	def test_date_range_filter(self):
		target = self._make_questionnaire("DateRangeTarget")
		q_jan = self._make_questionnaire("DateRangeJan")
		q_mar = self._make_questionnaire("DateRangeMar")
		self._rate(target, overall_rating=5, questionnaire=q_jan.name, rated_at="2026-01-15 10:00:00")
		self._rate(target, overall_rating=3, questionnaire=q_mar.name, rated_at="2026-03-20 10:00:00")

		listing = _data(
			ratings.get_ratings(
				reference_name=target.name,
				from_date="2026-01-01",
				to_date="2026-02-01",
			)
		)
		self.assertEqual(listing["stats"]["total"], 1)
		self.assertEqual(listing["ratings"][0]["overall_rating"], 5)

	# ── analytics shape guarantees ───────────────────────────

	def test_by_type_includes_all_ratable_doctypes(self):
		analytics = _data(ratings.get_ratings_analytics())
		by_type_keys = {row["reference_doctype"] for row in analytics["by_type"]}
		for doctype in RATABLE_DOCTYPES:
			self.assertIn(doctype, by_type_keys)
			self.assertIn(doctype, analytics["top_entities"])

	def test_sentiment_buckets_present(self):
		analytics = _data(ratings.get_ratings_analytics())
		self.assertEqual(set(analytics["sentiment"].keys()), {"positive", "neutral", "negative"})

	# ── B3: trend endpoint ───────────────────────────────────

	def test_trend_buckets_month(self):
		self._rate(overall_rating=4, rated_at="2026-04-10 09:00:00")
		self._rate(overall_rating=2, rated_at="2026-04-12 09:00:00")
		self._rate(overall_rating=5, rated_at="2026-05-03 09:00:00")

		trend = _data(
			ratings.get_ratings_trend(
				reference_doctype="Rating Questionnaire",
				bucket="month",
				from_date="2026-04-01",
				to_date="2026-05-31",
			)
		)
		periods = [b["period"] for b in trend["buckets"]]
		self.assertEqual(periods, sorted(periods))  # chronological
		by_period = {b["period"]: b for b in trend["buckets"]}
		self.assertIn("2026-04", by_period)
		self.assertEqual(by_period["2026-04"]["count"], 2)
		self.assertEqual(by_period["2026-04"]["average"], 3.0)
		self.assertEqual(by_period["2026-05"]["count"], 1)

	def test_trend_bucket_label_formats(self):
		self._rate(overall_rating=5, rated_at="2026-06-14 09:00:00")
		for bucket, expected in (
			("day", "2026-06-14"),
			("week", "2026-W24"),
			("month", "2026-06"),
		):
			trend = _data(
				ratings.get_ratings_trend(
					reference_doctype="Rating Questionnaire",
					bucket=bucket,
					from_date="2026-06-01",
					to_date="2026-06-30",
				)
			)
			periods = [b["period"] for b in trend["buckets"]]
			self.assertIn(expected, periods, f"{bucket} label mismatch: {periods}")
