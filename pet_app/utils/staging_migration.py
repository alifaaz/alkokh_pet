"""Per-document migration runner for the staging doctypes.

Both Pre Define Item and Pre Define Stock Entry expose a ``migrate()`` method. This
module runs it over many documents so that one failure is recorded on its own row
(status Error + message) and never blocks the others.

Each document runs inside a savepoint on the request's transaction: on failure the
document's partial writes are rolled back to the savepoint, the error is logged, and
the status is written. The request still commits as one unit, which keeps the whole
batch consistent and also lets the tests run inside their own transaction. Batches
above ``ENQUEUE_THRESHOLD`` are handed to a background worker instead, so a large
sheet does not hit the web request timeout.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, strip_html

ENQUEUE_THRESHOLD = 50
CHUNK_SIZE = 200
ERROR_MESSAGE_LIMIT = 2000


def migrate_each(doctype: str, names, *, allow_enqueue: bool = True) -> dict:
	"""Migrate ``names`` of ``doctype`` one by one.

	Returns ``{"migrated": [name, ...], "failed": {name: message}}``, or
	``{"queued": True, "count": n}`` when the batch was pushed to a background job.
	"""
	names = _as_name_list(names)
	if not names:
		return {"migrated": [], "failed": {}}

	if allow_enqueue and len(names) > ENQUEUE_THRESHOLD:
		frappe.enqueue(
			migrate_each,
			queue="long",
			timeout=3600,
			enqueue_after_commit=True,
			doctype=doctype,
			names=names,
			allow_enqueue=False,
		)
		return {"queued": True, "count": len(names)}

	result = {"migrated": [], "failed": {}}
	for name in names:
		savepoint = "staging_" + frappe.generate_hash(length=8)
		messages_before = len(frappe.local.message_log)
		frappe.db.savepoint(savepoint)
		try:
			doc = frappe.get_doc(doctype, name)
			doc.migrate()
			frappe.db.release_savepoint(savepoint)
			result["migrated"].append(name)
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			# frappe.throw already queued its message for the client; the row's own
			# status field is where this failure belongs, not a popup.
			del frappe.local.message_log[messages_before:]
			message = _record_failure(doctype, name, exc)
			result["failed"][name] = message
	return result


def migrate_in_chunks(doctype: str, names, *, chunk_size: int = CHUNK_SIZE, commit: bool = False) -> dict:
	"""Run ``migrate_each`` over slices and aggregate the reports.

	``migrate_each`` hands anything over ENQUEUE_THRESHOLD to a background worker and
	returns ``{"queued": True}`` with no per-row detail, and would otherwise hold a
	1700-row sheet open in a single transaction. Slicing bounds the transaction and keeps
	the per-row report.

	``commit=True`` is for ``bench execute`` only: it makes partial progress durable if a
	long run is interrupted. It MUST stay False inside a web request and inside tests,
	where the caller owns the transaction.
	"""
	names = _as_name_list(names)
	result = {"migrated": [], "failed": {}}
	for start in range(0, len(names), chunk_size):
		chunk = names[start : start + chunk_size]
		report = migrate_each(doctype, chunk, allow_enqueue=False)
		result["migrated"].extend(report.get("migrated", []))
		result["failed"].update(report.get("failed", {}))
		if commit:
			frappe.db.commit()
		frappe.publish_progress(
			min(100.0, (start + len(chunk)) * 100.0 / len(names)),
			title=_("Migrating {0}").format(_(doctype)),
		)
	return result


def _record_failure(doctype: str, name: str, exc: Exception) -> str:
	message = strip_html(cstr(exc)).strip() or exc.__class__.__name__
	# Logged after the rollback so the Error Log row itself survives.
	log = frappe.log_error(
		title=_("{0} migration failed: {1}").format(doctype, name),
		reference_doctype=doctype,
		reference_name=name,
	)
	if log:
		message = f"{message}\n({_('Error Log')} {log.name})"
	frappe.db.set_value(
		doctype,
		name,
		{"status": "Error", "migration_error": message[:ERROR_MESSAGE_LIMIT]},
		update_modified=False,
	)
	return message


def _as_name_list(names) -> list[str]:
	if isinstance(names, str):
		parsed = frappe.parse_json(names)
		names = parsed if isinstance(parsed, list) else [names]
	return [cstr(name) for name in (names or []) if cstr(name).strip()]
