frappe.pages["whatsapp-inbox"].on_page_load = function (wrapper) {
	frappe.require("/assets/pet_app/css/whatsapp_inbox.css", () => new WhatsAppInbox(wrapper));
};

class WhatsAppInbox {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({ parent: wrapper, title: __("WhatsApp Inbox"), single_column: true });
		this.page.set_primary_action(__("Refresh"), () => this.loadConversations(), "refresh");
		this.page.add_menu_item(__("Action Rules"), () => frappe.set_route("List", "Pet App WhatsApp Action Rule", "List"));
		this.page.add_menu_item(__("Templates"), () => frappe.set_route("List", "Pet App WhatsApp Template", "List"));
		this.page.main.addClass("wa-inbox-page");
		this.renderShell();
		this.bindEvents();
		this.loadConversations();
		this.poller = window.setInterval(() => this.loadConversations(true), 30000);
		$(wrapper).on("remove", () => window.clearInterval(this.poller));
	}

	renderShell() {
		this.page.main.html(`
			<div class="wa-inbox">
				<aside class="wa-conversations">
					<div class="wa-search"><input type="search" class="form-control" placeholder="${__("Search conversations")}"></div>
					<div class="wa-conversation-list"></div>
				</aside>
				<section class="wa-thread">
					<header class="wa-thread-header"></header>
					<div class="wa-messages"><div class="wa-empty">${__("Select a conversation")}</div></div>
					<footer class="wa-composer">
						<button class="btn btn-default btn-sm wa-attach" title="${__("Attach file")}">${frappe.utils.icon("paperclip", "sm")}</button>
						<textarea class="form-control" rows="1" placeholder="${__("Message")}"></textarea>
						<button class="btn btn-primary btn-sm wa-send" title="${__("Send")}">${frappe.utils.icon("send", "sm")}</button>
					</footer>
			</section>
		</div>
		`);
		this.$list = this.page.main.find(".wa-conversation-list");
		this.$messages = this.page.main.find(".wa-messages");
		this.$header = this.page.main.find(".wa-thread-header");
		this.$composer = this.page.main.find(".wa-composer");
	}

	bindEvents() {
		this.page.main.on("input", ".wa-search input", (event) => this.renderConversationList(event.currentTarget.value));
		this.page.main.on("click", ".wa-conversation", (event) => this.openConversation(event.currentTarget.dataset.name));
		this.page.main.on("click", ".wa-send", () => this.sendReply());
		this.page.main.on("click", ".wa-attach", () => this.selectAttachment());
		this.page.main.on("keydown", ".wa-composer textarea", (event) => {
			if (event.key === "Enter" && !event.shiftKey) {
				event.preventDefault();
				this.sendReply();
			}
		});
		this.page.main.on("click", ".wa-action-approve", (event) => this.reviewAction(event.currentTarget.dataset.action, true));
		this.page.main.on("click", ".wa-action-reject", (event) => this.reviewAction(event.currentTarget.dataset.action, false));
		this.page.main.on("click", ".wa-file", (event) => this.openFile(event.currentTarget.dataset.message));
		this.page.main.on("click", ".wa-source-link", (event) => {
			frappe.set_route("Form", event.currentTarget.dataset.doctype, event.currentTarget.dataset.name);
		});
	}

	async loadConversations(quiet = false) {
		if (!quiet) this.$list.html(`<div class="wa-loading">${__("Loading")}</div>`);
		const response = await frappe.xcall("pet_app.api.notifications.list_whatsapp_conversations", { limit: 100 });
		this.conversations = this.unwrap(response).conversations || [];
		this.renderConversationList(this.page.main.find(".wa-search input").val());
		if (this.activeName && this.conversations.some((row) => row.name === this.activeName)) {
			await this.openConversation(this.activeName, true);
		}
	}

	renderConversationList(term = "") {
		term = (term || "").trim().toLowerCase();
		const rows = (this.conversations || []).filter((row) =>
			[row.display_name, row.normalized_phone, row.last_message_preview].join(" ").toLowerCase().includes(term)
		);
		this.$list.html(rows.map((row) => `
			<button class="wa-conversation ${row.name === this.activeName ? "is-active" : ""}" data-name="${this.escape(row.name)}">
				<span class="wa-avatar">${this.escape((row.display_name || row.normalized_phone || "W").slice(0, 1).toUpperCase())}</span>
				<span class="wa-conversation-copy">
					<span class="wa-conversation-title">${this.escape(row.display_name || row.normalized_phone)}</span>
					<span class="wa-preview">${this.escape(row.last_message_preview || row.normalized_phone)}</span>
				</span>
				<span class="wa-conversation-meta">
					<span class="wa-session ${row.session_open ? "is-open" : ""}">${row.session_open ? __("Open") : __("Closed")}</span>
					${row.unread_count ? `<span class="wa-unread">${row.unread_count}</span>` : ""}
				</span>
			</button>
		`).join("") || `<div class="wa-empty">${__("No conversations")}</div>`);
	}

	async openConversation(name, quiet = false) {
		this.activeName = name;
		if (!quiet) this.$messages.html(`<div class="wa-loading">${__("Loading")}</div>`);
		const response = await frappe.xcall("pet_app.api.notifications.get_whatsapp_conversation", { conversation: name, limit: 200 });
		const payload = this.unwrap(response);
		this.active = payload.conversation;
		this.actions = Object.fromEntries((payload.actions || []).map((row) => [row.name, row]));
		this.renderConversationList(this.page.main.find(".wa-search input").val());
		this.renderThread(payload.messages || []);
		if (this.active.unread_count) {
			await frappe.xcall("pet_app.api.notifications.mark_whatsapp_conversation_read", { conversation: name });
			this.active.unread_count = 0;
		}
	}

	renderThread(messages) {
		const open = Boolean(this.active.session_open);
		this.$header.html(`
			<div><strong>${this.escape(this.active.display_name || this.active.normalized_phone)}</strong><span>${this.escape(this.active.normalized_phone)}</span></div>
			<span class="wa-window ${open ? "is-open" : ""}">${open ? __("Service window open") : __("Service window closed")}</span>
		`);
		const shownActions = new Set();
		const messageHtml = messages.map((message) => {
			const action = message.action_request && !shownActions.has(message.action_request) ? this.actions[message.action_request] : null;
			if (action) shownActions.add(message.action_request);
			return this.renderMessage(message, action);
		}).join("");
		const actionHtml = Object.values(this.actions || {})
			.filter((action) => !shownActions.has(action.name))
			.map((action) => `<div class="wa-message-row is-outbound">${this.renderAction(action)}</div>`)
			.join("");
		this.$messages.html(messageHtml || actionHtml
			? `${messageHtml}${actionHtml}`
			: `<div class="wa-empty">${__("No messages yet")}</div>`);
		this.$messages.scrollTop(this.$messages[0].scrollHeight);
		this.$composer.toggleClass("is-disabled", !open);
		this.$composer.find("textarea, button").prop("disabled", !open);
		this.$composer.find("textarea").attr("placeholder", open ? __("Message") : __("Service window closed"));
	}

	renderMessage(message, action) {
		const body = message.body || message.caption || `[${message.message_type}]`;
		const attachment = message.file
			? `<button class="wa-file" data-message="${this.escape(message.name)}">${frappe.utils.icon("paperclip", "sm")} ${__("Attachment")}</button>`
			: "";
		const source = this.renderSourceLink(message.source_doctype, message.source_name);
		return `
			<div class="wa-message-row ${message.direction === "Outbound" ? "is-outbound" : "is-inbound"}">
				<div class="wa-bubble">
					<div class="wa-body">${this.escape(body).replace(/\n/g, "<br>")}</div>
					${attachment}
					${source}
					<div class="wa-time">${this.escape(frappe.datetime.str_to_user(message.message_at || ""))} · ${this.escape(message.status || "")}</div>
				</div>
				${action ? this.renderAction(action) : ""}
			</div>
		`;
	}

	renderAction(action) {
		const review = ["Pending Review", "Needs Review", "Matched"].includes(action.status);
		const chooser = action.status === "Needs Review"
			? `<select class="form-control input-xs wa-review-choice" data-action="${this.escape(action.name)}">
				<option value="">${__("Choose response")}</option>
				${(action.response_options || []).map((row) => `<option value="${this.escape(row.key)}">${this.escape(row.label)}</option>`).join("")}
			</select>`
			: "";
		const source = this.renderSourceLink(action.source_doctype, action.source_name);
		return `<div class="wa-action">
			<div><strong>${this.escape(action.rule)}</strong><span class="wa-action-status">${this.escape(action.status)}</span></div>
			${source}
			${action.response_value ? `<p>${this.escape(action.response_value)}</p>` : ""}
			${action.error_message ? `<p class="text-danger">${this.escape(action.error_message)}</p>` : ""}
			${chooser}
			${review ? `<div class="wa-action-buttons">
				<button class="btn btn-primary btn-xs wa-action-approve" data-action="${this.escape(action.name)}">${__("Approve")}</button>
				<button class="btn btn-default btn-xs wa-action-reject" data-action="${this.escape(action.name)}">${__("Reject")}</button>
			</div>` : ""}
		</div>`;
	}

	renderSourceLink(doctype, name) {
		if (!doctype || !name) return "";
		return `<button class="wa-source-link" data-doctype="${this.escape(doctype)}" data-name="${this.escape(name)}" title="${__("Open linked record")}">
			${frappe.utils.icon("link", "xs")}<span>${this.escape(doctype)} · ${this.escape(name)}</span>
		</button>`;
	}

	async sendReply() {
		if (!this.active || !this.active.session_open) return;
		const $input = this.$composer.find("textarea");
		const message = ($input.val() || "").trim();
		if (!message && !this.attachment) return;
		this.$composer.find("button, textarea").prop("disabled", true);
		try {
			await frappe.xcall("pet_app.api.notifications.reply_whatsapp_conversation", {
				conversation: this.active.name,
				message,
				file: this.attachment ? this.attachment.name : null,
			});
			$input.val("");
			this.attachment = null;
			await this.openConversation(this.active.name, true);
		} finally {
			this.$composer.find("button, textarea").prop("disabled", false);
		}
	}

	selectAttachment() {
		new frappe.ui.FileUploader({
			allow_multiple: false,
			restrictions: { allowed_file_types: ["image/*", "audio/*", "video/*", ".pdf", ".doc", ".docx"] },
			on_success: (file) => {
				this.attachment = file;
				this.$composer.find(".wa-attach").attr("title", file.file_name).addClass("has-file");
			},
		});
	}

	async reviewAction(name, approve) {
		const method = approve ? "approve_whatsapp_action" : "reject_whatsapp_action";
		const responseKey = this.page.main.find(`.wa-review-choice[data-action="${name}"]`).val();
		if (approve && this.actions[name].status === "Needs Review" && !responseKey) {
			frappe.msgprint(__("Choose the intended response first."));
			return;
		}
		await frappe.xcall(`pet_app.api.notifications.${method}`, { action_request: name, response_key: responseKey || null });
		frappe.show_alert({ message: approve ? __("Action approved") : __("Action rejected"), indicator: approve ? "green" : "orange" });
		await this.openConversation(this.active.name, true);
	}

	async openFile(message) {
		const response = await frappe.xcall("pet_app.api.notifications.download_whatsapp_media", { message });
		window.open(this.unwrap(response).file.file_url, "_blank", "noopener");
	}

	unwrap(response) {
		if (response && response.ok === false) {
			frappe.throw(response.errors?.[0]?.message || response.message || __("Request failed"));
		}
		return (response && response.data) || response || {};
	}

	escape(value) {
		return frappe.utils.escape_html(String(value == null ? "" : value));
	}
}
