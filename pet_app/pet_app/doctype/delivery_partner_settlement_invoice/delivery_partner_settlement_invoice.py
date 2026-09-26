"""One covered invoice on a Delivery Partner Settlement. Rows are written by
`pet_app.api.delivery_partners.create_settlement`, never by hand."""

from __future__ import annotations

from frappe.model.document import Document


class DeliveryPartnerSettlementInvoice(Document):
	pass
