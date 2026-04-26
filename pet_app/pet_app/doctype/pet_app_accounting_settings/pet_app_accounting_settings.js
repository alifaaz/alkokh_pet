// Copyright (c) 2026, solvers and contributors
// For license information, please see license.txt

frappe.ui.form.on("Pet App Accounting Settings", {
	refresh(frm) {
		frm.set_query("treasury_cash_account", () => ({
			filters: Object.assign(
				{
					is_group: 0,
					account_type: "Cash",
				},
				frm.doc.default_company ? { company: frm.doc.default_company } : {}
			),
		}));
	},
});
