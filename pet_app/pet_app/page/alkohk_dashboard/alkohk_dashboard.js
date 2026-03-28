frappe.pages["alkohk_dashboard"].on_page_load = function (wrapper) {
	frappe.require("/assets/pet_app/css/alkohk_dashboard.css", () => {
		new AlkohkDashboard(wrapper);
	});
};

class AlkohkDashboard {
	constructor(wrapper) {
		this.wrapper = $(wrapper);
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Alkohk"),
			single_column: true,
		});

		this.page.set_primary_action(__("Refresh"), () => this.load(), "refresh");
		frappe.breadcrumbs.add("Pet App");

		this.page.main.addClass("alkohk-dashboard-page");
		this.render_shell();
		this.bind_search();
		this.load();
	}

	render_shell() {
		this.page.main.html(`
			<div class="alkohk-dashboard">
				<section class="alkohk-hero">
					<div class="alkohk-hero-copy">
						<p class="alkohk-kicker">${__("Workspace Map")}</p>
						<h1 class="alkohk-title">${__("Alkohk")}</h1>
						<p class="alkohk-subtitle">
							${__("A warm control room for your pet records, veterinary operations, pricing, and settings.")}
						</p>
					</div>
					<div class="alkohk-search-wrap">
						<input class="alkohk-search" type="search" placeholder="${__("Filter cards, doctypes, or settings")}" />
					</div>
				</section>
				<section class="alkohk-metrics"></section>
				<section class="alkohk-sections"></section>
				<section class="alkohk-embedded"></section>
			</div>
		`);

		this.$metrics = this.page.main.find(".alkohk-metrics");
		this.$sections = this.page.main.find(".alkohk-sections");
		this.$embedded = this.page.main.find(".alkohk-embedded");
	}

	bind_search() {
		this.page.main.on("input", ".alkohk-search", (event) => {
			const term = (event.currentTarget.value || "").trim().toLowerCase();
			this.page.main.find(".alkohk-card").each((_, card) => {
				const $card = $(card);
				const haystack = ($card.data("search") || "").toLowerCase();
				$card.toggleClass("is-hidden", Boolean(term) && !haystack.includes(term));
			});

			this.page.main.find(".alkohk-section").each((_, section) => {
				const visibleCards = $(section).find(".alkohk-card:not(.is-hidden)").length;
				$(section).toggleClass("is-empty", !visibleCards);
			});
		});
	}

	async load() {
		this.show_loading();

		const payload = await frappe.xcall("pet_app.pet_app.page.alkohk_dashboard.alkohk_dashboard.get_dashboard_payload");
		this.payload = payload || {};

		this.render_metrics(this.payload.hero_metrics || []);
		this.render_sections(this.payload.sections || []);
		this.render_embedded(this.payload.embedded_models || []);
		this.bind_card_actions();
	}

	show_loading() {
		this.$metrics.html(this.make_skeleton(4));
		this.$sections.html(this.make_skeleton(6));
		this.$embedded.empty();
	}

	make_skeleton(count) {
		return Array.from({ length: count })
			.map(
				(_, index) => `
					<div class="alkohk-card alkohk-card-skeleton" style="animation-delay:${index * 60}ms">
						<div class="alkohk-skeleton-line short"></div>
						<div class="alkohk-skeleton-line"></div>
						<div class="alkohk-skeleton-line wide"></div>
					</div>
				`
			)
			.join("");
	}

	render_metrics(metrics) {
		this.$metrics.html(
			(metrics || [])
				.map(
					(metric, index) => `
						<button class="alkohk-card alkohk-metric-card accent-${metric.accent}" data-route='${JSON.stringify(metric.route || [])}'
							style="animation-delay:${index * 70}ms" data-search="${frappe.utils.escape_html(metric.label)}">
							<span class="alkohk-metric-label">${frappe.utils.escape_html(metric.label)}</span>
							<span class="alkohk-metric-value">${frappe.format(metric.value || 0, { fieldtype: "Int" })}</span>
						</button>
					`
				)
				.join("")
		);
	}

	render_sections(sections) {
		this.$sections.html(
			(sections || [])
				.map((section, sectionIndex) => {
					const cards = (section.items || [])
						.map((item, itemIndex) => this.render_card(item, sectionIndex, itemIndex))
						.join("");

					return `
						<section class="alkohk-section tone-${section.tone}">
							<div class="alkohk-section-head">
								<div>
									<p class="alkohk-section-kicker">${frappe.utils.escape_html(section.title)}</p>
									<h2>${frappe.utils.escape_html(section.subtitle)}</h2>
								</div>
							</div>
							<div class="alkohk-card-grid">
								${cards}
							</div>
						</section>
					`;
				})
				.join("")
		);
	}

	render_card(item, sectionIndex, itemIndex) {
		const searchBlob = [item.label, item.description, item.kind, item.target].filter(Boolean).join(" ");
		const countHtml = item.count !== null && item.count !== undefined
			? `<span class="alkohk-card-count">${frappe.format(item.count, { fieldtype: "Int" })}</span>`
			: `<span class="alkohk-card-pill">${frappe.utils.escape_html(item.kind)}</span>`;

		return `
			<button class="alkohk-card accent-${item.accent}" data-route='${JSON.stringify(item.route || [])}'
				data-search="${frappe.utils.escape_html(searchBlob)}"
				style="animation-delay:${(sectionIndex * 120) + (itemIndex * 70)}ms">
				<div class="alkohk-card-top">
					<div class="alkohk-icon-shell">
						<span class="${frappe.utils.escape_html(item.icon)}"></span>
					</div>
					${countHtml}
				</div>
				<div class="alkohk-card-body">
					<h3>${frappe.utils.escape_html(item.label)}</h3>
					<p>${frappe.utils.escape_html(item.description)}</p>
				</div>
				<div class="alkohk-card-foot">
					<span>${__("Open")}</span>
					<span class="alkohk-arrow">›</span>
				</div>
			</button>
		`;
	}

	render_embedded(models) {
		if (!models.length) {
			this.$embedded.empty();
			return;
		}

		this.$embedded.html(`
			<div class="alkohk-embedded-box">
				<div>
					<p class="alkohk-section-kicker">${__("Embedded Models")}</p>
					<h3>${__("Managed inside parent forms")}</h3>
				</div>
				<div class="alkohk-chip-row">
					${models
						.map(
							(model) => `
								<button class="alkohk-chip" data-route='${JSON.stringify(model.route || [])}'>
									${frappe.utils.escape_html(model.label)}
								</button>
							`
						)
						.join("")}
				</div>
			</div>
		`);
	}

	bind_card_actions() {
		this.page.main.find("[data-route]").off("click").on("click", (event) => {
			const element = event.currentTarget;
			const route = JSON.parse(element.dataset.route || "[]");
			if (!route.length) {
				return;
			}

			this.spawn_ripple(element, event);
			element.classList.add("is-pressed");

			window.setTimeout(() => {
				element.classList.remove("is-pressed");
				frappe.set_route(...route);
			}, 140);
		});
	}

	spawn_ripple(element, event) {
		const ripple = document.createElement("span");
		const rect = element.getBoundingClientRect();
		const size = Math.max(rect.width, rect.height) * 0.6;
		ripple.className = "alkohk-ripple";
		ripple.style.width = `${size}px`;
		ripple.style.height = `${size}px`;
		ripple.style.left = `${event.clientX - rect.left - size / 2}px`;
		ripple.style.top = `${event.clientY - rect.top - size / 2}px`;
		element.appendChild(ripple);

		window.setTimeout(() => ripple.remove(), 420);
	}
}
