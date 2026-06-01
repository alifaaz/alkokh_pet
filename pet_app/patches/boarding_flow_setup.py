from __future__ import annotations

import frappe


def execute():
	_set_default_boarding_settings()
	_migrate_existing_pet_boarding_rows()


def _set_default_boarding_settings():
	if frappe.db.exists("DocType", "Pet Boarding Settings"):
		frappe.db.set_single_value("Pet Boarding Settings", "default_boarding_type", "Travel")


def _migrate_existing_pet_boarding_rows():
	if not frappe.db.table_exists("Pet Boarding"):
		return

	columns = set(frappe.db.get_table_columns("Pet Boarding"))
	updates = {}

	if "record_status" in columns and "status" in columns:
		frappe.db.sql(
			"""
			UPDATE `tabPet Boarding`
			SET record_status = status
			WHERE (record_status IS NULL OR record_status = '')
			  AND status IN ('Reserved', 'Checked In', 'Checked Out', 'Cancelled')
			"""
		)

	if "record_status" in columns:
		frappe.db.sql(
			"""
			UPDATE `tabPet Boarding`
			SET record_status = 'Reserved'
			WHERE record_status IS NULL OR record_status = ''
			"""
		)

	if "status" in columns and "record_status" in columns:
		frappe.db.sql(
			"""
			UPDATE `tabPet Boarding`
			SET status = CASE
				WHEN record_status = 'Checked Out' THEN 'Closed'
				WHEN record_status = 'Cancelled' THEN 'Cancelled'
				ELSE 'Open'
			END
			WHERE status IN ('Reserved', 'Checked In', 'Checked Out')
			   OR status IS NULL
			   OR status = ''
			"""
		)

	if "workflow_state" in columns and "record_status" in columns:
		frappe.db.sql(
			"""
			UPDATE `tabPet Boarding`
			SET workflow_state = CASE
				WHEN record_status = 'Checked Out' THEN 'Closed'
				WHEN record_status = 'Cancelled' THEN 'Cancelled'
				ELSE record_status
			END
			WHERE workflow_state IS NULL OR workflow_state = ''
			"""
		)

	if "deposit" in columns and "deposit_amount" in columns:
		updates["deposit"] = "deposit_amount"
	if "balance" in columns and "balance_due" in columns:
		updates["balance"] = "balance_due"
	if "stay_days" in columns and "number_of_days" in columns:
		updates["stay_days"] = "number_of_days"
	if "note" in columns and "special_instructions" in columns:
		updates["note"] = "special_instructions"

	for target, source in updates.items():
		if target == "note":
			frappe.db.sql(
				f"""
				UPDATE `tabPet Boarding`
				SET `{target}` = `{source}`
				WHERE (`{target}` IS NULL OR `{target}` = '')
				  AND `{source}` IS NOT NULL
				  AND `{source}` != ''
				"""
			)
		else:
			frappe.db.sql(
				f"""
				UPDATE `tabPet Boarding`
				SET `{target}` = `{source}`
				WHERE (`{target}` IS NULL OR `{target}` = 0)
				  AND `{source}` IS NOT NULL
				"""
			)

	if "billing_status" in columns:
		frappe.db.sql(
			"""
			UPDATE `tabPet Boarding`
			SET billing_status = CASE
				WHEN sales_invoice IS NOT NULL AND sales_invoice != '' THEN 'Invoiced'
				ELSE 'Unbilled'
			END
			WHERE billing_status IS NULL OR billing_status = ''
			"""
		)

	if "boarding_type" in columns:
		frappe.db.sql(
			"""
			UPDATE `tabPet Boarding`
			SET boarding_type = 'Travel'
			WHERE boarding_type IS NULL
			   OR boarding_type = ''
			   OR boarding_type NOT IN ('Travel', 'Treatment')
			"""
		)

	if {"check_in", "check_in_date"}.issubset(columns):
		time_expr = "`check_in_time`" if "check_in_time" in columns else "'00:00:00'"
		frappe.db.sql(
			f"""
			UPDATE `tabPet Boarding`
			SET check_in = TIMESTAMP(`check_in_date`, COALESCE({time_expr}, '00:00:00'))
			WHERE check_in IS NULL
			  AND check_in_date IS NOT NULL
			"""
		)

	if {"check_out", "check_out_date"}.issubset(columns):
		time_expr = "`check_out_time`" if "check_out_time" in columns else "'00:00:00'"
		frappe.db.sql(
			f"""
			UPDATE `tabPet Boarding`
			SET check_out = TIMESTAMP(`check_out_date`, COALESCE({time_expr}, '00:00:00'))
			WHERE check_out IS NULL
			  AND check_out_date IS NOT NULL
			"""
		)

	if "reserved_at" in columns:
		frappe.db.sql(
			"""
			UPDATE `tabPet Boarding`
			SET reserved_at = COALESCE(check_in, creation)
			WHERE reserved_at IS NULL
			"""
		)

	if {"customer", "owner_name"}.issubset(columns):
		frappe.db.sql(
			"""
			UPDATE `tabPet Boarding`
			SET customer = owner_name
			WHERE (customer IS NULL OR customer = '')
			  AND owner_name IS NOT NULL
			  AND owner_name != ''
			"""
		)

	if {"guardian", "customer"}.issubset(columns) and frappe.db.table_exists("Guardian"):
		frappe.db.sql(
			"""
			UPDATE `tabPet Boarding` pb
			INNER JOIN `tabGuardian` g ON g.customer_id = pb.customer
			SET pb.guardian = g.name
			WHERE (pb.guardian IS NULL OR pb.guardian = '')
			  AND pb.customer IS NOT NULL
			  AND pb.customer != ''
			"""
		)

	if {"guardian", "pet"}.issubset(columns) and frappe.db.table_exists("PetGuardian"):
		frappe.db.sql(
			"""
			UPDATE `tabPet Boarding` pb
			INNER JOIN `tabPetGuardian` pg ON pg.pet_id = pb.pet
			SET pb.guardian = pg.guardian_id
			WHERE (pb.guardian IS NULL OR pb.guardian = '')
			  AND pb.pet IS NOT NULL
			  AND pb.pet != ''
			"""
		)
