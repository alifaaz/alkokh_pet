frappe.pages["customer-account-reset"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({parent: wrapper, title: __("Customer Account Reset"), single_column: true});
    if (frappe.session.user !== "Administrator" && !frappe.user_roles.includes("Administrator")) {
        $(page.body).text(__("Only the Administrator role can access this page."));
        return;
    }
    const area = $('<div class="p-4" style="max-width:850px"></div>').appendTo(page.body);
    $('<p class="text-muted"></p>').text(__("Reset a guardian/customer’s invoices, payments, accounting ledgers and medical billing. Medical history and pet records are kept. Submitted stock invoices are cancelled and their stock movement is reversed.")).appendTo(area);
    const fields = $('<div></div>').appendTo(area);
    const output = $('<div class="mt-4" aria-live="polite"></div>').appendTo(area);
    let preview = null;
    const invalidate = () => { preview = null; output.empty(); page.clear_primary_action(); };
    const controls = {};
    for (const df of [
        {fieldname: "guardian", label: __("Guardian"), fieldtype: "Link", options: "Guardian", change: invalidate},
        {fieldname: "customer", label: __("Customer"), fieldtype: "Link", options: "Customer", change: invalidate},
        {fieldname: "password", label: __("Reset Password"), fieldtype: "Password", reqd: 1, change: invalidate},
    ]) {
        controls[df.fieldname] = frappe.ui.form.make_control({parent: fields, df, render_input: true});
    }
    const args = () => Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.get_value()]));
    const call = async (method, data) => {
        const r = await frappe.call({method: `pet_app.api.accounting.account_reset.${method}`, args: data, freeze: true});
        if (!r.message?.ok) throw new Error(__("Account reset request failed."));
        return r.message.data;
    };
    const table = (title, counts) => {
        $('<h5 class="mt-4"></h5>').text(title).appendTo(output);
        const t = $('<table class="table table-bordered"><tbody></tbody></table>').appendTo(output).find('tbody');
        for (const [name, count] of Object.entries(counts)) {
            const row = $('<tr></tr>').appendTo(t);
            $('<td></td>').text(__(name)).appendTo(row);
            $('<td></td>').text(count).appendTo(row);
        }
    };
    page.set_secondary_action(__("Preview Reset"), async () => {
        invalidate();
        const data = args();
        if ((!data.guardian && !data.customer) || !data.password) {
            frappe.msgprint(__("Select a Guardian or Customer and enter the reset password."));
            return;
        }
        let result;
        try {
            result = await call("preview", data);
        } catch (error) {
            // Frappe displays the server validation message. Keep the page and
            // session available so the administrator can correct the password.
            return;
        }
        preview = {result, data};
        $('<h4></h4>').text(`${result.customer_name} (${result.customer})`).appendTo(output);
        $('<p></p>').text(__("Current net balance: {0}", [result.net_balance])).appendTo(output);
        table(__("Accounting records to remove"), result.counts);
        table(__("Ledger entries before cancellation"), result.ledger_counts);
        table(__("Medical records whose billing will be reset"), result.medical_counts);
        table(__("Notification history to retain"), {"Pet Notification Log": result.notification_log_count || 0});
        if (result.blockers.length) {
            const box = $('<div class="alert alert-danger"></div>').appendTo(output);
            for (const reason of result.blockers) $('<p></p>').text(reason).appendTo(box);
            return;
        }
        $('<div class="alert alert-warning"></div>').text(__("This permanently removes the listed accounting transactions. A private recovery snapshot is saved first. Type the exact Customer ID in the confirmation dialog to continue.")).appendTo(output);
        page.set_primary_action(__("Reset Account"), () => {
            if (!preview) return;
            const selected = preview;
            const dialog = new frappe.ui.Dialog({
                title: __("Confirm Account Reset"),
                fields: [{fieldname: "confirm_customer", fieldtype: "Data", reqd: 1, label: __("Type Customer ID: {0}", [selected.result.customer])}],
                primary_action_label: __("Permanently Reset Account"),
                primary_action: async (values) => {
                    if (values.confirm_customer !== selected.result.customer) {
                        frappe.msgprint(__("Customer ID does not match.")); return;
                    }
                    dialog.get_primary_btn().prop('disabled', true);
                    try {
                        const result = await call("execute", {...selected.data, ...values, fingerprint: selected.result.fingerprint});
                        dialog.hide();
                        controls.password.set_value("");
                        invalidate();
                        $('<div class="alert alert-success"></div>').text(__("Account {0} has been reset. Balance: 0. Backup/audit ID: {1}", [result.customer, result.reset_id])).appendTo(output);
                    } catch (error) {
                        dialog.hide(); invalidate();
                        frappe.msgprint(__("Reset did not complete. Preview again before retrying."));
                    }
                }
            });
            dialog.show();
        });
        page.btn_primary.addClass("btn-danger");
    });
};
