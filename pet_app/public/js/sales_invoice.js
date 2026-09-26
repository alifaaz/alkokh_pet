// Give an already-submitted Due sale to a delivery partner (pet_app.api.delivery_partners).
frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		const doc = frm.doc;
		if (
			doc.docstatus !== 1 ||
			doc.is_return ||
			doc.is_pos ||
			doc.custom_delivery_partner ||
			!(flt(doc.outstanding_amount) > 0)
		) {
			return;
		}
		frm.add_custom_button(__("Attach to Delivery Partner"), () => attach_to_partner(frm));
	},
});

function attach_to_partner(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Attach to Delivery Partner"),
		fields: [
			{
				fieldname: "delivery_partner",
				fieldtype: "Link",
				options: "Delivery Partner",
				label: __("Delivery Partner"),
				reqd: 1,
				get_query: () => ({ filters: { is_active: 1 } }),
			},
			{
				fieldname: "partner_order_ref",
				fieldtype: "Data",
				label: __("Partner Order Number"),
				reqd: 1,
			},
			{
				fieldname: "partner_customer_name",
				fieldtype: "Data",
				label: __("Customer Name in the App"),
			},
			{
				fieldname: "partner_commission_rate",
				fieldtype: "Percent",
				label: __("Commission Rate (%)"),
				description: __("Leave empty to use the partner's rate."),
			},
			{
				fieldname: "note",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"The partner collects this money and pays it in their settlement. The till will refuse to take payment for it."
				)}</p>`,
			},
		],
		primary_action_label: __("Attach"),
		async primary_action(values) {
			const response = await frappe.xcall(
				"pet_app.api.delivery_partners.attach_invoice_to_partner",
				{ sales_invoice: frm.doc.name, ...values }
			);
			if (response && response.ok === false) {
				frappe.msgprint({
					title: __("Not attached"),
					indicator: "red",
					message:
						(response.errors || [])
							.map((e) => frappe.utils.escape_html((e && e.message) || String(e)))
							.join("<br>") || __("The invoice was not attached."),
				});
				return;
			}
			dialog.hide();
			frappe.show_alert({ message: __("Attached to {0}", [values.delivery_partner]), indicator: "green" });
			frm.reload_doc();
		},
	});
	dialog.show();
}
