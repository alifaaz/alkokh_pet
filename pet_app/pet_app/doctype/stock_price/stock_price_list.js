frappe.listview_settings["Stock Price"] = {
	add_fields: ["status", "selling_changes", "buying_changes"],
	get_indicator(doc) {
		const colours = { Draft: "grey", Submitted: "blue", Cancelled: "red" };
		return [__(doc.status), colours[doc.status] || "grey", "status,=," + doc.status];
	},
};
