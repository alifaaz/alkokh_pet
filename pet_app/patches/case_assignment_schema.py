from __future__ import annotations

import frappe
from frappe.utils import now_datetime


TEAM_DOCTYPE = "Pet Care Episode Assigned Practitioner"
TEAM_FIELD = "assigned_practitioners"


def execute():
	backfill_visit_primary_practitioner()
	backfill_episode_care_teams()
	frappe.clear_cache()


def backfill_visit_primary_practitioner():
	if not _has_column("Vet Visit", "primary_practitioner") or not _has_column("Vet Visit", "doctor"):
		return
	frappe.db.sql(
		"""
		update `tabVet Visit`
		set primary_practitioner = doctor
		where ifnull(primary_practitioner, '') = ''
		  and ifnull(doctor, '') != ''
		"""
	)


def backfill_episode_care_teams():
	if not frappe.db.table_exists(TEAM_DOCTYPE) or not _has_column("Pet Care Episode", "primary_doctor"):
		return

	episode_names = frappe.get_all("Pet Care Episode", pluck="name", ignore_permissions=True)
	for episode_name in episode_names:
		for practitioner in _episode_practitioners(episode_name):
			_add_team_row_if_missing(episode_name, practitioner)


def _episode_practitioners(episode_name: str) -> list[str]:
	practitioners: list[str] = []
	seen: set[str] = set()

	primary_doctor = frappe.db.get_value("Pet Care Episode", episode_name, "primary_doctor")
	_add_practitioner(practitioners, seen, primary_doctor)

	if _has_column("Vet Visit", "care_episode"):
		fields = ["doctor"]
		if _has_column("Vet Visit", "primary_practitioner"):
			fields.append("primary_practitioner")
		for row in frappe.get_all(
			"Vet Visit",
			filters={"care_episode": episode_name},
			fields=fields,
			ignore_permissions=True,
		):
			_add_practitioner(practitioners, seen, row.get("primary_practitioner"))
			_add_practitioner(practitioners, seen, row.get("doctor"))

	return practitioners


def _add_team_row_if_missing(episode_name: str, practitioner: str):
	if not practitioner:
		return
	if frappe.db.exists(
		TEAM_DOCTYPE,
		{
			"parent": episode_name,
			"parenttype": "Pet Care Episode",
			"parentfield": TEAM_FIELD,
			"practitioner": practitioner,
		},
	):
		return

	idx = (frappe.db.count(TEAM_DOCTYPE, {"parent": episode_name, "parentfield": TEAM_FIELD}) or 0) + 1
	frappe.get_doc(
		{
			"doctype": TEAM_DOCTYPE,
			"parent": episode_name,
			"parenttype": "Pet Care Episode",
			"parentfield": TEAM_FIELD,
			"idx": idx,
			"practitioner": practitioner,
			"role": "Treating Doctor",
			"added_by": "Administrator",
			"added_at": now_datetime(),
		}
	).insert(ignore_permissions=True)


def _add_practitioner(values: list[str], seen: set[str], practitioner: str | None):
	if practitioner and practitioner not in seen:
		values.append(practitioner)
		seen.add(practitioner)


def _has_column(doctype: str, fieldname: str) -> bool:
	try:
		return frappe.db.has_column(doctype, fieldname)
	except Exception:
		return False
