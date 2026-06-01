frappe.ui.form.on("Healthcare Practitioner", {
	refresh(frm) {
		frm.set_query("user_id", () => ({
			filters: {
				enabled: 1,
				user_type: "System User",
			},
		}));
	},
});
