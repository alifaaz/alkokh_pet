"""Shared setup validation for API saves, Desk saves and runtime activation."""

import frappe

from pet_app.stock_transfer.rules import TransferError, fail, public_url

DOCTYPE = "Stock Transfer Settings"
FIELDS = (
	"enabled",
	"reservation_tests_passed",
	"transit_warehouse",
	"quarantine_warehouses",
	"loss_expense_account",
	"cost_center",
	"public_app_url",
)


def configuration_issues(doc, *, require_complete=True, scoped=False):
	issues = []

	def issue(field, code, message):
		issues.append(dict(field=field, code=code, message=message))

	def warehouse(name, field):
		if not name:
			return
		row = frappe.db.get_value("Warehouse", name, ["company", "disabled", "is_group"], as_dict=True)
		if not row or row.company != doc.company or row.disabled or row.is_group:
			issue(field, "WAREHOUSE_MISMATCH", "Choose an enabled leaf warehouse in this company.")
		elif scoped:
			from pet_app.stock_transfer.access import scoped_warehouse

			if not scoped_warehouse(name):
				issue(field, "PERMISSION_DENIED", "This warehouse is outside your access scope.")

	transit = doc.get("transit_warehouse")
	quarantine = [r.warehouse for r in doc.get("quarantine_warehouses") or []]
	if require_complete and not transit:
		issue("transit_warehouse", "TRANSIT_NOT_CONFIGURED", "Select a transit warehouse.")
	warehouse(transit, "transit_warehouse")
	if require_complete and not quarantine:
		issue(
			"quarantine_warehouses", "QUARANTINE_NOT_CONFIGURED", "Select at least one quarantine warehouse."
		)
	if len(quarantine) != len(set(quarantine)) or any(not q for q in quarantine):
		issue(
			"quarantine_warehouses", "VALIDATION_ERROR", "Quarantine warehouses must be nonempty and unique."
		)
	for name in quarantine:
		warehouse(name, "quarantine_warehouses")
		if name == transit:
			issue(
				"quarantine_warehouses",
				"WAREHOUSE_MISMATCH",
				"Transit and quarantine must be different warehouses.",
			)
	for field, doctype, code in (
		("loss_expense_account", "Account", "LOSS_ACCOUNT_NOT_CONFIGURED"),
		("cost_center", "Cost Center", "VALIDATION_ERROR"),
	):
		name = doc.get(field)
		if not name:
			if require_complete:
				issue(
					field,
					code,
					"Select a stock-loss expense account."
					if field == "loss_expense_account"
					else "Select a cost center.",
				)
			continue
		fields = ["company", "disabled", "is_group"] + (["root_type"] if doctype == "Account" else [])
		row = frappe.db.get_value(doctype, name, fields, as_dict=True)
		if (
			not row
			or row.company != doc.company
			or row.disabled
			or row.is_group
			or (doctype == "Account" and row.root_type != "Expense")
		):
			issue(
				field,
				code,
				"Choose an enabled company expense ledger."
				if doctype == "Account"
				else "Choose an enabled company cost-center leaf.",
			)
		elif scoped and not frappe.has_permission(doctype, doc=frappe.get_doc(doctype, name)):
			issue(field, "PERMISSION_DENIED", "The selected record is outside your access scope.")
	url = doc.get("public_app_url")
	if url or require_complete:
		try:
			normalized = public_url(url)
			if normalized.rstrip("/") == str(frappe.conf.host_name or "").rstrip("/"):
				fail("Enter the frontend application URL, not the backend host.")
		except TransferError as exc:
			issue("public_app_url", exc.code, str(exc))
	if require_complete and not doc.get("reservation_tests_passed"):
		issue(
			"reservation_tests_passed",
			"STOCK_TRANSFER_DISABLED",
			"Confirm that reservation and ledger acceptance tests passed before enabling.",
		)
	return issues


def validate_document(doc, *, scoped=False):
	previous = doc.get_doc_before_save()
	if previous and previous.company != doc.company:
		fail("The settings company cannot be changed.")
	issues = configuration_issues(doc, require_complete=bool(doc.enabled), scoped=scoped)
	if issues:
		fail(issues[0]["message"], issues[0]["code"])
	if doc.public_app_url:
		doc.public_app_url = public_url(doc.public_app_url)
	old_special = set()
	if previous:
		old_special = {previous.transit_warehouse} | {q.warehouse for q in previous.quarantine_warehouses}
	new_special = (
		({doc.transit_warehouse} | {q.warehouse for q in doc.quarantine_warehouses})
		- old_special
		- {None, ""}
	)
	if new_special:
		active = frappe.get_all(
			"Stock Transfer Order",
			filters={"company": doc.company, "status": ["in", ["Ordered", "Preparing", "Transferring"]]},
			fields=["source_warehouse", "target_warehouse"],
		)
		if any(r.source_warehouse in new_special or r.target_warehouse in new_special for r in active):
			fail(
				"A selected transit/quarantine warehouse is an endpoint of an open transfer. Complete or cancel that transfer first.",
				"WAREHOUSE_MISMATCH",
			)


def readiness(doc):
	issues = configuration_issues(doc)
	return dict(ready=not issues, enabled=bool(doc.enabled and not issues), issues=issues)


def values(doc):
	return dict(
		company=doc.company,
		enabled=bool(doc.enabled),
		reservation_tests_passed=bool(doc.reservation_tests_passed),
		transit_warehouse=doc.transit_warehouse or None,
		quarantine_warehouses=[q.warehouse for q in doc.quarantine_warehouses],
		loss_expense_account=doc.loss_expense_account or None,
		cost_center=doc.cost_center or None,
		public_app_url=doc.public_app_url or "",
	)
