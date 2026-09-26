from frappe.model.document import Document


class StockTransferSettings(Document):
	def validate(self):
		from pet_app.stock_transfer.configuration import validate_document
		from pet_app.stock_transfer.guards import lock

		lock()
		validate_document(self, scoped=True)
