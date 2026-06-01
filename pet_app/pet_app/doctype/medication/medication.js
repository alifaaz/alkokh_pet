// Copyright (c) 2026, solvers and contributors
// For license information, please see license.txt

frappe.ui.form.on("Medication", {
	setup(frm) {
		frm.set_query("item_group", () => ({
			filters: { is_group: 0 },
		}));
	},

	linked_item(frm) {
		if (!frm.doc.linked_item) {
			return;
		}

		frappe.db.get_value("Item", frm.doc.linked_item, ["stock_uom", "item_group"]).then((result) => {
			const item = result && result.message;
			if (!item) {
				return;
			}

			frm.set_value("dosage_form_or_unit", item.stock_uom || "");
			frm.set_value("item_group", item.item_group || "");
		});
	},
});
