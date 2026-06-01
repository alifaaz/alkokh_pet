from __future__ import annotations

import frappe


def execute():
	if not frappe.db.table_exists("Vet Visit"):
		return

	meta = frappe.get_meta("Vet Visit")
	if meta.has_field("follow_up_preferred_date") and meta.has_field("follow_up_date"):
		frappe.db.sql(
			"""
			UPDATE `tabVet Visit`
			SET follow_up_preferred_date = follow_up_date
			WHERE follow_up_preferred_date IS NULL
			  AND follow_up_date IS NOT NULL
			"""
		)

	if meta.has_field("follow_up_status"):
		frappe.db.sql(
			"""
			UPDATE `tabVet Visit`
			SET follow_up_status = CASE
				WHEN follow_up_visit_id IS NOT NULL AND follow_up_visit_id != '' THEN 'Seen'
				WHEN follow_up_appointment_id IS NOT NULL AND follow_up_appointment_id != '' THEN 'Scheduled'
				WHEN follow_up_required = 1 THEN 'Requested'
				ELSE 'Not Needed'
			END
			WHERE follow_up_status IS NULL OR follow_up_status = ''
			"""
		)
