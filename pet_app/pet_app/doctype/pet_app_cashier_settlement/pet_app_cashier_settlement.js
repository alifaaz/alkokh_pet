frappe.ui.form.on("Pet App Cashier Settlement", {
	refresh(frm) {
		frm.set_df_property("payment_entry", "read_only", frm.doc.docstatus === 1);
	},
});

