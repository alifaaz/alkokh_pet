"""Security and failure-atomicity tests; no database writes or live fixtures."""
from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from passlib.hash import pbkdf2_sha256

from pet_app.api.accounting import account_reset as reset


class AccountResetTests(TestCase):
    def setUp(self):
        self.fake = MagicMock()
        self.fake.PermissionError = frappe.PermissionError
        self.fake.ValidationError = frappe.ValidationError
        self.fake.session.user = "Administrator"
        self.fake.throw.side_effect = lambda message, exc=frappe.ValidationError: self._raise(exc(message))
        self.patcher = patch.object(reset, "frappe", self.fake)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    @staticmethod
    def _raise(exc):
        raise exc

    def test_system_manager_is_not_reset_admin(self):
        self.fake.session.user = "manager@example.test"
        self.fake.get_roles.return_value = ["System Manager", "Accounts Manager"]
        with self.assertRaises(frappe.PermissionError):
            reset.require_admin()

    def test_administrator_role_is_accepted(self):
        self.fake.session.user = "admin@example.test"
        self.fake.get_roles.return_value = ["Administrator"]
        reset.require_admin()

    def test_password_checked_even_for_administrator(self):
        self.fake.conf.get.return_value = pbkdf2_sha256.hash("test-only-secret")
        with self.assertRaises(frappe.ValidationError):
            reset._authenticate("wrong")
        reset._authenticate("test-only-secret")

    def test_reset_password_error_does_not_signal_session_expiration(self):
        self.fake.conf.get.return_value = pbkdf2_sha256.hash("test-only-secret")
        with self.assertRaises(frappe.ValidationError) as error:
            reset._authenticate("wrong")
        self.assertEqual(error.exception.http_status_code, 417)
        self.assertNotIsInstance(error.exception, frappe.AuthenticationError)

    def test_missing_password_configuration_denies_access(self):
        self.fake.conf.get.return_value = None
        with self.assertRaises(frappe.ValidationError):
            reset._authenticate("anything")

    def test_guardian_customer_mismatch(self):
        self.fake.db.get_value.return_value = "Customer A"
        with self.assertRaises(frappe.ValidationError):
            reset._resolve("Guardian A", "Customer B")

    def _run(self, plan, fingerprint="reviewed"):
        # Bypass only the HTTP rate-limit wrapper, never endpoint authorization.
        with patch.object(reset, "_authenticate"), patch.object(reset, "_resolve", return_value="Customer A"), \
             patch.object(reset, "_plan", return_value=plan), patch.object(reset, "_no_internal_commit", return_value=nullcontext()):
            return reset.execute.__wrapped__("password", fingerprint, "Customer A", customer="Customer A")

    def test_stale_preview_never_backs_up_or_deletes(self):
        with patch.object(reset, "_backup") as backup, patch.object(reset, "_cancel_and_delete") as delete:
            with self.assertRaises(frappe.ValidationError):
                self._run({"fingerprint": "changed"})
            backup.assert_not_called()
            delete.assert_not_called()
            self.fake.db.rollback.assert_called_once_with(save_point="account_reset")

    def test_shared_transaction_blocker_prevents_mutation(self):
        with patch.object(reset, "_backup") as backup:
            with self.assertRaises(frappe.ValidationError):
                self._run({"fingerprint": "reviewed", "blockers": ["Shared journal"]})
            backup.assert_not_called()

    def test_mid_reset_failure_rolls_back(self):
        plan = {"fingerprint": "reviewed", "blockers": [], "counts": {"Sales Invoice": 1}}
        with patch.object(reset, "_backup", return_value="backup"), \
             patch.object(reset, "_cancel_and_delete", side_effect=RuntimeError("cancel failed")):
            with self.assertRaisesRegex(RuntimeError, "cancel failed"):
                self._run(plan)
            self.fake.db.rollback.assert_called_once_with(save_point="account_reset")

    def test_internal_commit_refused_and_original_restored(self):
        original = self.fake.db.commit
        with reset._no_internal_commit():
            with self.assertRaisesRegex(RuntimeError, "intermediate"):
                self.fake.db.commit()
        self.assertIs(self.fake.db.commit, original)

    def test_notification_log_history_preserved_while_reference_detached(self):
        log = frappe._dict(name="LOG-1", reference_doctype="Sales Invoice", reference_name="INV-1",
                           message="Invoice reminder", status="Sent", recipient="recipient", reminder="REM-1")
        plan = {"customer": "Customer A", "snapshot": {"notification_logs": [log]}}
        reset._detach_notification_logs(plan)
        self.fake.get_doc.assert_called_once_with("Pet Notification Log", "LOG-1")
        comment = self.fake.get_doc.return_value.add_comment.call_args.kwargs["text"]
        self.assertIn("Sales Invoice INV-1", comment)
        self.fake.db.set_value.assert_called_once_with("Pet Notification Log", "LOG-1",
            {"reference_doctype": None, "reference_name": None})
        self.fake.delete_doc.assert_not_called()
        self.fake.db.delete.assert_not_called()

    def test_empty_notification_snapshot_does_not_touch_other_logs(self):
        reset._detach_notification_logs({"snapshot": {"notification_logs": []}})
        self.fake.db.set_value.assert_not_called()
        self.fake.get_doc.assert_not_called()

    def test_ledger_deletion_never_filtered_by_party(self):
        invoice = frappe._dict(name="INV-1", creation="2026-01-01")
        plan = {"customer": "Customer A", "snapshot": {"documents": {
            "Sales Invoice": [invoice], "Payment Entry": [], "Journal Entry": []},
            "attachments": [], "reminders": []}}
        doc = self.fake.get_doc.return_value
        doc.docstatus = 1
        self.fake.db.get_value.return_value = 2
        self.fake.db.exists.return_value = False
        with patch.object(reset, "_clear_medical"):
            reset._cancel_and_delete(plan)
        doc.cancel.assert_called_once()
        self.assertEqual(len(self.fake.db.delete.call_args_list), len(reset.LEDGERS))
        for call in self.fake.db.delete.call_args_list:
            self.assertEqual(call.args[1], {"voucher_type": "Sales Invoice", "voucher_no": "INV-1"})
        self.fake.delete_doc.assert_called_once_with("Sales Invoice", "INV-1", ignore_permissions=True, ignore_missing=False)
