app_name = "pet_app"
app_title = "Pet App"
app_publisher = "solvers"
app_description = "App for managing pet "
app_email = "mdrazor5@gmail.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "pet_app",
# 		"logo": "/assets/pet_app/logo.png",
# 		"title": "Pet App",
# 		"route": "/pet_app"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/pet_app/css/pet_app.css"
# app_include_js = "/assets/pet_app/js/pet_app.js"
# Whitelisted APIs
# include js, css files in header of web template
# web_include_css = "/assets/pet_app/css/pet_app.css"
# web_include_js = "/assets/pet_app/js/pet_app.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "pet_app/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
doctype_js = {
    "Pet App WhatsApp Template": "public/js/whatsapp_template.js",
    "Pet App WhatsApp Action Rule": "public/js/whatsapp_action_rule.js",
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "pet_app/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "pet_app.utils.jinja_methods",
# 	"filters": "pet_app.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "pet_app.install.before_install"
after_install = "pet_app.install.after_install"
after_migrate = "pet_app.patches.enforce_iqd_defaults.ensure_iqd_defaults"

# Uninstallation
# ------------

# before_uninstall = "pet_app.uninstall.before_uninstall"
# after_uninstall = "pet_app.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "pet_app.utils.before_app_install"
# after_app_install = "pet_app.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "pet_app.utils.before_app_uninstall"
# after_app_uninstall = "pet_app.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "pet_app.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
permission_query_conditions = {
    "PetCareService": "pet_app.permissions.petcareservice.get_permission_query_conditions",
}

# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events
# ═══════════════════════════════════════════════════════════════════
# Document Events - Auto Folder Creation
# ═══════════════════════════════════════════════════════════════════


# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"pet_app.tasks.all"
# 	],
# 	"daily": [
# 		"pet_app.tasks.daily"
# 	],
# 	"hourly": [
# 		"pet_app.tasks.hourly"
# 	],
# 	"weekly": [
# 		"pet_app.tasks.weekly"
# 	],
# 	"monthly": [
# 		"pet_app.tasks.monthly"
# 	],
# }

# Testing
# -------

before_tests = "pet_app.tests.bootstrap.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "pet_app.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "pet_app.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "pet_app.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
before_request = ["pet_app.api.auth_api.set_cors_for_oauth_token_endpoint"]
after_request = ["pet_app.api.session_guard.after_request"]

# Job Events
# ----------
# before_job = ["pet_app.utils.before_job"]
# after_job = ["pet_app.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

auth_hooks = [
    "pet_app.api.session_guard.enforce_authenticated_api_access",
]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }


doc_events = {
	"*": {
		"after_insert": "pet_app.notifications.actions.after_insert",
		"on_update": "pet_app.notifications.actions.on_update",
		"on_submit": "pet_app.notifications.actions.on_submit",
	},
	"Notification Log": {
		"after_insert": "pet_app.notifications.push.mirror_notification_log",
	},
	"Pet App WhatsApp Action Rule": {
		"validate": "pet_app.notifications.actions.validate_action_rule",
	},
    "User": {
        "before_validate": "pet_app.utils.role_profiles.before_validate_user_role_profiles",
        "before_save": "pet_app.utils.role_profiles.before_save_user_role_profiles",
        "on_update": "pet_app.api.permissions.clear_access_snapshot_cache",
    },
    "User Role Profile": {
        "after_insert": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_update": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_trash": "pet_app.api.permissions.clear_access_snapshot_cache",
    },
    "Has Role": {
        "after_insert": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_update": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_trash": "pet_app.api.permissions.clear_access_snapshot_cache",
    },
    "Role Profile": {
        "after_insert": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_update": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_trash": "pet_app.api.permissions.clear_access_snapshot_cache",
    },
    "User Permission": {
        "after_insert": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_update": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_trash": "pet_app.api.permissions.clear_access_snapshot_cache",
    },
    "DocPerm": {
        "after_insert": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_update": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_trash": "pet_app.api.permissions.clear_access_snapshot_cache",
    },
    "Custom DocPerm": {
        "after_insert": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_update": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_trash": "pet_app.api.permissions.clear_access_snapshot_cache",
    },
    "Pet App Access Settings": {
        "on_update": "pet_app.api.permissions.clear_access_snapshot_cache",
    },
    "Pet App Page Access": {
        "after_insert": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_update": "pet_app.api.permissions.clear_access_snapshot_cache",
        "on_trash": "pet_app.api.permissions.clear_access_snapshot_cache",
    },
    "Vet Visit": {
        "before_insert": "pet_app.utils.mortality.validate_document_not_deceased",
        "on_update": "pet_app.pet_app.doctype.medication.medication.sync_medication_counters_for_visit",
        "on_trash": "pet_app.pet_app.doctype.medication.medication.sync_medication_counters_for_visit",
    },
    "Appointment": {
        "before_insert": [
            "pet_app.utils.appointment.link_appointment_identity",
            "pet_app.utils.mortality.validate_document_not_deceased",
        ],
        "validate": "pet_app.utils.appointment.link_appointment_identity",
    },
    "Vet Case Sheet": {
        "before_insert": "pet_app.utils.mortality.validate_document_not_deceased",
    },
    "Pet Boarding": {
        "before_insert": "pet_app.utils.mortality.validate_document_not_deceased",
    },
    "PetCareService": {
        "before_insert": "pet_app.utils.mortality.validate_document_not_deceased",
    },
    "Pet Procedure": {
        "before_insert": "pet_app.utils.mortality.validate_document_not_deceased",
    },
    "Sales Order": {
        "before_update_after_submit": "pet_app.api.order.before_sales_order_update",
        "on_update_after_submit": "pet_app.api.order.on_sales_order_update",
    },
    "Sales Invoice": {
        "before_insert": "pet_app.utils.sales_invoice_guard.before_insert",
        "on_submit": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_cancel": [
            "pet_app.utils.visit_billing.on_sales_invoice_cancel",
            "pet_app.api.mobile.home_builder.clear_home_cache",
        ],
        "validate": "pet_app.utils.sales_invoice_guard.fix_due_date",
    },
    "Stock Entry": {
        "on_submit": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_cancel": "pet_app.api.mobile.home_builder.clear_home_cache",
    },
    # Product -> Item projection removed: the storefront model is a THIN OVERLAY.
    # Item is the single source of truth for price/stock/warehouse/UOM/brand, and
    # Product must never write back to it. Cache invalidation stays.
    "Product": {
        "after_insert": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_update": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_trash": "pet_app.api.mobile.home_builder.clear_home_cache",
    },
    "Product Category": {
        "after_insert": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_update": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_trash": "pet_app.api.mobile.home_builder.clear_home_cache",
    },
    # "Item" doc_events entry removed entirely: its ONLY handler was the reverse
    # Item -> Product projection. Item saves still fire the wildcard "*" on_update
    # (notifications.actions.on_update) declared at the top of this dict, plus every
    # ERPNext/Frappe core Item hook - none of which are touched here.
    #
    # Item Price -> Product projection removed for the same reason: the overlay reads
    # prices from Item Price at request time instead of mirroring them onto Product.
    "Item Price": {
        "after_insert": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_update": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_trash": "pet_app.api.mobile.home_builder.clear_home_cache",
    },
    "Brand": {
        "before_save": "pet_app.api.product.before_save",
        "after_insert": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_update": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_trash": "pet_app.api.mobile.home_builder.clear_home_cache",
    },
    "Mobile Home Banner": {
        "after_insert": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_update": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_trash": "pet_app.api.mobile.home_builder.clear_home_cache",
    },
    "Mobile Home Product Collection": {
        "after_insert": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_update": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_trash": "pet_app.api.mobile.home_builder.clear_home_cache",
    },
    "Mobile Home Filter": {
        "after_insert": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_update": "pet_app.api.mobile.home_builder.clear_home_cache",
        "on_trash": "pet_app.api.mobile.home_builder.clear_home_cache",
    },
    # Item Group -> Product Category auto-mirror removed. It duplicated the Item Group
    # nested set into a second tree (67 auto-rows) whose only added value - display
    # order, image, description - was never populated, and it surfaced clinical groups
    # (Antibiotics, Anesthetics) as storefront categories. The store taxonomy will be
    # authored deliberately instead. `before_save` is UNRELATED and stays.
    "Item Group": {
        "before_save": "pet_app.api.product.before_save",
    },
    "Supplier": {
        "before_save": [
            "pet_app.utils.auto_update_links.before_save",
            "pet_app.api.product.before_save",
        ],
        "on_update":    "pet_app.utils.auto_update_links.on_update",
        "after_rename": "pet_app.utils.auto_update_links.after_rename",
    },
    "Driver": {
        "before_save": "pet_app.api.driver.before_driver_save",
        "after_insert": "pet_app.api.driver.after_driver_insert",
        "on_update": "pet_app.api.driver.on_driver_update",
    },
    "Customer": {
        "validate": "pet_app.utils.guardian_customer.validate_customer_identity_projection",
    },
    "Coupon Code": {                                        # ← add this
        "validate":     "pet_app.api.coupons.validate",
        "after_insert": "pet_app.api.coupons.after_insert",
        "on_update":    "pet_app.api.coupons.on_update",
        "on_trash":     "pet_app.api.coupons.on_delete",
    },
}


scheduler_events = {
    "all": [
        "pet_app.notifications.scheduler.enqueue_due_reminders",
        "pet_app.notifications.scheduler.process_due_notifications",
        "pet_app.notifications.retry.retry_failed_notifications",
    ],
    # The two product-projection repair jobs are gone (hourly repair_active_*, daily
    # repair_all_*). They called sync_product_item in a loop and committed mid-loop, so
    # they rewrote Items from Products unattended - the same Product -> Item direction
    # the doc_events above no longer allow, only worse because no user action triggered
    # it. With these removed there is no remaining path by which Product writes to Item.
    "hourly": [
        "pet_app.tasks.reminders.enqueue_due_reminders",
        "pet_app.notifications.actions.expire_due_actions",
    ],
    "daily": [
        "pet_app.tasks.reminders.send_due_reminders",
        "pet_app.notifications.scheduler.create_daily_reminders",
        "pet_app.notifications.scheduler.cleanup_old_webhook_events",
    ],
    
}

# ملاحظة مهمة:
# الـ before_save للـ sync (auto_update_links.before_save) لازم يشتغل.
# إذا عندك before_save وحدة بس في hooks — حوّله لـ list:
#
# "Supplier": {
#     "before_save": [
#         "pet_app.api.product.before_save",           # rename
#         "pet_app.utils.auto_update_links.before_save", # sync
#     ],
#     "on_update":    "pet_app.utils.auto_update_links.on_update",
#     "after_rename": "pet_app.utils.auto_update_links.after_rename",
# },
fixtures = [
    # Custom Fields
    {
        "dt": "Custom Field",
        "filters": [["dt", "in", [
            "Address", "Appointment", "Communication", "Contact", "Coupon Code",
            "Driver", "Email Account", "File", "GoCardless Mandate", "Item",
            "Item Group", "Payment Entry",
            "POS Profile", "Print Settings", "Sales Invoice", "Sales Order",
            "Stock Entry", "UTM Campaign", "Web Form",
        ]]],
    },

    # Property Setters
    {
        "dt": "Property Setter",
        "filters": [["doc_type", "in", [
            "Address", "Appointment", "Clinical Procedure Item", "Coupon Code",
            "Customer", "Delivery Note", "Delivery Note Item", "Driver", "Item",
            "Item Barcode", "Item Group", "Job Card", "Material Request",
            "Packed Item", "Pick List",
            "POS Invoice", "POS Invoice Item", "Purchase Invoice",
            "Purchase Invoice Item", "Purchase Order", "Purchase Receipt",
            "Purchase Receipt Item", "Quotation", "Sales Invoice",
            "Sales Invoice Item", "Sales Order", "Specimen", "Stock Entry",
            "Stock Entry Detail", "Stock Reconciliation",
            "Stock Reconciliation Item", "Supplier", "Supplier Quotation",
        ]]],
    },


    # Workflow
    {
        "dt": "Workflow",
        "filters": [["name", "in", ["orders"]]],
    },

    # Workflow States (all 3 — Approved was missing from first scan)
    {
        "dt": "Workflow State",
        "filters": [["name", "in", ["Approved", "Pending", "Rejected"]]],
    },

    # Workflow Action Masters (Approve and Review were missing from first scan)
    {
        "dt": "Workflow Action Master",
        "filters": [["name", "in", ["Approve", "Reject", "Review"]]],
    },

    # Email Template
    {
        "dt": "Email Template",
        "filters": [["name", "in", ["Dispatch Notification"]]],
    },

    # Letter Heads
    {
        "dt": "Letter Head",
        "filters": [["name", "in", ["Company Letterhead", "Company Letterhead - Grey"]]],
    },

    # Print Formats (all 14 — safe to export all since they were modified on this site)
    {
        "dt": "Print Format",
        "filters": [["name", "in", [
            "IRS 1099 Form",
            "Delivery Note Standard", "Delivery Note with Item Image",
            "Cheque Printing Format",
            "POS Invoice", "POS Invoice Standard", "POS Invoice with Item Image",
            "Return POS Invoice",
            "Drop Shipping Format",
            "Purchase Order Standard", "Purchase Order with Item Image",
            "Point of Sale",
            "Sales Invoice Standard", "Sales Invoice with Item Image",
        ]]],
    },

    # Custom Roles (17 roles — completely missed in first scan)
    {
        "dt": "Role",
        "filters": [["is_custom", "=", 1]],
    },

    # Custom DocPerms — 274 permission rules across 56 doctypes
    # WARNING: without this, all custom roles will have zero access on fresh install
    {
        "dt": "Custom DocPerm",
    },
]
