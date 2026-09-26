"""Which file a record will deliver, and why it is never guessed.

A template that says "press the button and we will send you the report" is a promise, and
there is no server-side PDF renderer here to keep it - api/printing.py returns JSON and
nothing imports get_pdf. The only bytes that can be sent are the ones a client generated
and a human reviewed, so something has to record which attachment that is.

The tests that matter most are the ones proving it is not inferred. On the live site Lab
and Imaging carry 1041 attachments: 1037 photos of paper results and three PDFs nobody
reviewed for sending. "Newest attachment" and "newest .pdf" each pick a wrong file
and send it to a customer, which is the failure this exists to prevent.

Nothing here names Lab, Imaging, or any template on purpose. The clinic will add a
radiology template and more after it, and a record type nobody has built yet has to work
with no change to reports.py - test_a_doctype_this_code_has_never_heard_of_works is that
claim, written down.
"""

from __future__ import annotations

import base64
from io import BytesIO

import frappe
from frappe.tests.utils import FrappeTestCase

from pet_app.notifications import reports


def _pdf(tag: str) -> bytes:
	"""A structurally valid one-page PDF whose bytes differ per tag.

	Valid rather than a b"%PDF-" stub because frappe's File.check_content parses every
	PDF it stores; a stub is rejected by the platform before this module's rules are
	reached, which would make these tests pass for the wrong reason.
	"""
	from pypdf import PdfWriter

	writer = PdfWriter()
	writer.add_blank_page(width=200, height=200)
	writer.add_metadata({"/Title": tag})
	buffer = BytesIO()
	writer.write(buffer)
	return buffer.getvalue()


def _attach(doctype, name, content, file_name, *, reviewed=False):
	"""An ordinary attachment, or a reviewed report through the one writer that marks one."""
	if reviewed:
		return reports.save_reviewed_report(doctype, name, content=content, file_name=file_name)
	return frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"content": content,
			"attached_to_doctype": doctype,
			"attached_to_name": name,
			"is_private": 1,
		}
	).insert(ignore_permissions=True)


class TestReviewedReport(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		# A ToDo is a record with no clinical meaning and no attachments of its own, so
		# each test starts from a record whose only files are the ones it created.
		self.record = frappe.get_doc(
			{"doctype": "ToDo", "description": f"reviewed report test {frappe.generate_hash(length=8)}"}
		).insert(ignore_permissions=True)
		self.target = ("ToDo", self.record.name)

	# -------------------------------------------------- it is marked, never inferred

	def test_an_unmarked_pdf_is_not_a_reviewed_report(self):
		"""The three real PDFs on Lab records were uploaded by staff and reviewed by nobody."""
		_attach(*self.target, _pdf("staff upload"), "scan.pdf")

		self.assertIsNone(reports.newest_reviewed_report(*self.target))
		self.assertFalse(reports.has_reviewed_report(*self.target))

	def test_a_newer_unmarked_attachment_does_not_displace_the_reviewed_report(self):
		"""Newest-attachment would send the photo. It is 1037 of 1041 files on this site."""
		report = _attach(*self.target, _pdf("reviewed"), "report.pdf", reviewed=True)
		_attach(*self.target, b"not a pdf at all", "result-photo.webp")

		self.assertEqual(reports.newest_reviewed_report(*self.target)["name"], report.name)

	def test_a_newer_unmarked_pdf_does_not_displace_the_reviewed_report(self):
		"""Newest-.pdf would send the staff scan."""
		report = _attach(*self.target, _pdf("reviewed"), "report.pdf", reviewed=True)
		_attach(*self.target, _pdf("staff upload"), "scan.pdf")

		self.assertEqual(reports.newest_reviewed_report(*self.target)["name"], report.name)

	# ------------------------------------------------------------ newest marked wins

	def test_a_re_reviewed_report_replaces_the_one_before_it(self):
		first = _attach(*self.target, _pdf("first"), "report.pdf", reviewed=True)
		second = _attach(*self.target, _pdf("corrected"), "report.pdf", reviewed=True)

		self.assertNotEqual(first.name, second.name)
		self.assertEqual(reports.newest_reviewed_report(*self.target)["name"], second.name)

	def test_the_same_bytes_attached_twice_are_one_report(self):
		"""A retried upload is not a second document, and must not accumulate copies."""
		content = _pdf("same")
		first = reports.save_reviewed_report(*self.target, content=content, file_name="report.pdf")
		again = reports.save_reviewed_report(*self.target, content=content, file_name="report.pdf")

		self.assertEqual(first.name, again.name)
		self.assertEqual(
			frappe.db.count(
				"File",
				{
					"attached_to_doctype": self.target[0],
					"attached_to_name": self.target[1],
					reports.REVIEWED_REPORT_FIELD: 1,
				},
			),
			1,
		)

	def test_one_record_s_report_is_not_another_s(self):
		other = frappe.get_doc({"doctype": "ToDo", "description": "other"}).insert(ignore_permissions=True)
		mine = _attach(*self.target, _pdf("mine"), "report.pdf", reviewed=True)

		self.assertEqual(reports.newest_reviewed_report(*self.target)["name"], mine.name)
		self.assertIsNone(reports.newest_reviewed_report("ToDo", other.name))

	# --------------------------------------------------------------- what is refused

	def test_a_non_pdf_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			reports.save_reviewed_report(*self.target, content=b"\x89PNG\r\n\x1a\n", file_name="report.pdf")
		self.assertFalse(reports.has_reviewed_report(*self.target))

	def test_an_empty_upload_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			reports.save_reviewed_report(*self.target, content=b"", file_name="report.pdf")

	def test_a_truncated_pdf_is_refused_with_a_sentence(self):
		"""frappe rejects it too, as a raw pypdf 'startxref not found'. That is not an answer."""
		with self.assertRaises(frappe.ValidationError) as caught:
			reports.save_reviewed_report(*self.target, content=_pdf("t")[:40], file_name="report.pdf")
		self.assertIn("could not be read", str(caught.exception))

	# ------------------------------------------------------------------ what is kept

	def test_a_reviewed_report_is_stored_private(self):
		"""Every attachment on this site's Lab and Imaging records today is public."""
		report = reports.save_reviewed_report(*self.target, content=_pdf("x"), file_name="report.pdf")

		self.assertTrue(report.is_private)

	def test_the_stored_name_always_ends_in_pdf(self):
		"""The customer sees this string, and inbox._media_type reads its extension."""
		from pet_app.notifications.inbox import _media_type

		report = reports.save_reviewed_report(*self.target, content=_pdf("x"), file_name="Lab Result")

		self.assertTrue(report.file_name.endswith(".pdf"))
		self.assertEqual(_media_type(report.file_name), "document")

	def test_a_path_in_the_client_s_filename_does_not_escape(self):
		report = reports.save_reviewed_report(
			*self.target, content=_pdf("x"), file_name="../../etc/passwd.pdf"
		)

		self.assertEqual(report.file_name, "passwd.pdf")

	def test_base64_and_data_urls_are_both_accepted(self):
		"""A JSON request cannot carry bytes; both shapes reach this from the client."""
		encoded = base64.b64encode(_pdf("plain")).decode()
		plain = reports.save_reviewed_report(*self.target, content=encoded, file_name="a.pdf")
		data_url = reports.save_reviewed_report(
			*self.target,
			content="data:application/pdf;base64," + base64.b64encode(_pdf("url")).decode(),
			file_name="b.pdf",
		)

		self.assertTrue(plain.name)
		self.assertNotEqual(plain.name, data_url.name)

	# ------------------------------------------------------------------- generality

	def test_a_doctype_this_code_has_never_heard_of_works(self):
		"""The whole point. A radiology template, and whatever comes after it, need no code.

		ToDo is not clinical and reports.py has never been told it exists, which is what
		makes it the right witness: the resolver addresses records as (doctype, name) and
		knows nothing else about them.
		"""
		report = reports.save_reviewed_report(*self.target, content=_pdf("any"), file_name="report.pdf")

		self.assertEqual(reports.newest_reviewed_report(*self.target)["name"], report.name)
		self.assertTrue(reports.has_reviewed_report(*self.target))

	def test_a_record_with_no_attachments_at_all_answers_cleanly(self):
		self.assertIsNone(reports.newest_reviewed_report(*self.target))
		self.assertFalse(reports.has_reviewed_report(*self.target))
		self.assertFalse(reports.has_reviewed_report("ToDo", None))
		self.assertFalse(reports.has_reviewed_report(None, None))
