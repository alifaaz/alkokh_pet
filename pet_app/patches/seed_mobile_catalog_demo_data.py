from __future__ import annotations

from urllib.parse import quote

import frappe
from frappe.utils import flt, now_datetime

from pet_app.pet_app.doctype.product_category.product_category import STORE_ROOT_CATEGORY


WAREHOUSE = "Stores - K"
PRICE_LIST = "Standard Selling"
# The storefront root, spelled the same on both trees. This was "Mobile Shop", which
# stopped being the root's name when the two trees were unified - re-running the seed
# with the stale literal would have created a SECOND Product Category root and split the
# store tree in two.
ROOT_CATEGORY = STORE_ROOT_CATEGORY


def _img(label: str, size: str = "900x900", bg: str = "F7F1E8", fg: str = "1F2937") -> str:
	return f"https://placehold.co/{size}/{bg}/{fg}/png?text={quote(label)}"


BRANDS = (
	{"brand": "ACANA", "image": _img("ACANA", "500x240", "F4E7D3")},
	{"brand": "Applaws", "image": _img("Applaws", "500x240", "E8F4FD")},
	{"brand": "Trixie", "image": _img("Trixie", "500x240", "F3E8FD")},
	{"brand": "JBL", "image": _img("JBL", "500x240", "E8FDF0")},
	{"brand": "Versele-Laga", "image": _img("Versele Laga", "500x240", "FDF6E8")},
	{"brand": "Bioline", "image": _img("Bioline", "500x240", "FDE8F6")},
)

CATEGORIES = (
	{"category_name": "Dog Food", "display_order": 10, "image": _img("Dog Food", "700x420", "FDE8E8")},
	{"category_name": "Cat Food", "display_order": 20, "image": _img("Cat Food", "700x420", "E8F4FD")},
	{"category_name": "Bird Care", "display_order": 30, "image": _img("Bird Care", "700x420", "FDF6E8")},
	{"category_name": "Aquatics", "display_order": 40, "image": _img("Aquatics", "700x420", "E8FDF0")},
	{"category_name": "Toys", "display_order": 50, "image": _img("Pet Toys", "700x420", "F3E8FD")},
	{"category_name": "Grooming", "display_order": 60, "image": _img("Grooming", "700x420", "FDE8F6")},
)

PRODUCTS = (
	{
		"sku": "MOB-DOG-ACANA-2KG",
		"product_name": "ACANA Adult Dog Food 2kg",
		"description": "High-protein dry food for adult dogs.",
		"brand": "ACANA",
		"category": "Dog Food",
		"price": 30000,
		"discounted_price": 24000,
		"stock_uom": "Kg",
		"stock_qty": 60,
		"tags": "dog,dry-food,best-seller,recently-added",
		"image": _img("ACANA Dog Food", "900x900", "FDE8E8"),
		"rating": 5,
	},
	{
		"sku": "MOB-DOG-TRIXIE-CHEW",
		"product_name": "Trixie Durable Dog Chew Toy",
		"description": "Durable chew toy for active dogs.",
		"brand": "Trixie",
		"category": "Toys",
		"price": 12000,
		"discounted_price": 0,
		"stock_uom": "Nos",
		"stock_qty": 75,
		"tags": "dog,toy,best-seller",
		"image": _img("Dog Chew Toy", "900x900", "F3E8FD"),
		"rating": 4,
	},
	{
		"sku": "MOB-CAT-APPLAWS-TUNA",
		"product_name": "Applaws Tuna Cat Wet Food",
		"description": "Tuna wet food for cats.",
		"brand": "Applaws",
		"category": "Cat Food",
		"price": 9500,
		"discounted_price": 0,
		"stock_uom": "Nos",
		"stock_qty": 90,
		"tags": "cat,wet-food,recently-added",
		"image": _img("Tuna Cat Food", "900x900", "E8F4FD"),
		"rating": 4,
	},
	{
		"sku": "MOB-CAT-LITTER-5L",
		"product_name": "Clumping Cat Litter 5L",
		"description": "Fast-clumping litter with odor control.",
		"brand": "Bioline",
		"category": "Cat Food",
		"price": 8500,
		"discounted_price": 0,
		"stock_uom": "Litre",
		"stock_qty": 55,
		"tags": "cat,litter,best-seller",
		"image": _img("Cat Litter 5L", "900x900", "E8F4FD"),
		"rating": 4,
	},
	{
		"sku": "MOB-BIRD-SEED-900G",
		"product_name": "Versele-Laga Parrot Seed Mix 900g",
		"description": "Balanced seed mix for parrots and medium birds.",
		"brand": "Versele-Laga",
		"category": "Bird Care",
		"price": 7500,
		"discounted_price": 6000,
		"stock_uom": "Gram",
		"stock_qty": 40,
		"tags": "bird,food,back-in-stock",
		"image": _img("Parrot Seed Mix", "900x900", "FDF6E8"),
		"rating": 5,
	},
	{
		"sku": "MOB-FISH-JBL-FLAKES",
		"product_name": "JBL Goldfish Flakes 250ml",
		"description": "Daily nutrition flakes for goldfish.",
		"brand": "JBL",
		"category": "Aquatics",
		"price": 6500,
		"discounted_price": 0,
		"stock_uom": "Litre",
		"stock_qty": 45,
		"tags": "fish,aquatic,food,back-in-stock",
		"image": _img("Goldfish Flakes", "900x900", "E8FDF0"),
		"rating": 4,
	},
	{
		"sku": "MOB-GROOM-BIOLINE-SHAMPOO",
		"product_name": "Bioline Puppy Shampoo 250ml",
		"description": "Gentle puppy shampoo for sensitive coats.",
		"brand": "Bioline",
		"category": "Grooming",
		"price": 11000,
		"discounted_price": 9000,
		"stock_uom": "Nos",
		"stock_qty": 35,
		"tags": "dog,grooming,recently-added",
		"image": _img("Puppy Shampoo", "900x900", "FDE8F6"),
		"rating": 5,
	},
	{
		"sku": "MOB-CAT-TRIXIE-MOUSE",
		"product_name": "Trixie Catnip Mouse Toy",
		"description": "Soft catnip mouse toy for cats.",
		"brand": "Trixie",
		"category": "Toys",
		"price": 4500,
		"discounted_price": 0,
		"stock_uom": "Nos",
		"stock_qty": 80,
		"tags": "cat,toy,recently-added",
		"image": _img("Catnip Mouse Toy", "900x900", "F3E8FD"),
		"rating": 4,
	},
)

BANNERS = (
	{
		"banner_id": "banner-summer-dog-sale",
		"block_type": "banner_carousel",
		"block_id": "hero-banners",
		"display_order": 10,
		"image": _img("Dog Essentials Sale", "1200x560", "FFEDD5", "7C2D12"),
		"title": "Dog Essentials Sale",
		"subtitle": "Save on food, toys, and grooming picks",
		"button_title": "Shop Dog",
		"gradient_start": "#FF9A56",
		"gradient_end": "#FF5E62",
		"action_type": "category",
		"action_value": "Dog Food",
	},
	{
		"banner_id": "banner-new-cat-arrivals",
		"block_type": "banner_carousel",
		"block_id": "hero-banners",
		"display_order": 20,
		"image": _img("New Cat Arrivals", "1200x560", "E0F2FE", "075985"),
		"title": "New Cat Arrivals",
		"subtitle": "Fresh food and toys for feline friends",
		"button_title": "Explore",
		"gradient_start": "#43C6AC",
		"gradient_end": "#191654",
		"action_type": "list",
		"action_value": "recently-added",
	},
	{
		"banner_id": "banner-grooming-week",
		"block_type": "single_banner",
		"block_id": "mid-feed-promo",
		"display_order": 40,
		"image": _img("Grooming Week", "1200x560", "FCE7F3", "831843"),
		"title": "Grooming Week",
		"subtitle": "Gentle care products for clean, happy pets",
		"button_title": "See Deals",
		"gradient_start": "#A18CD1",
		"gradient_end": "#FBC2EB",
		"action_type": "category",
		"action_value": "Grooming",
	},
)


def execute():
	summary = {
		"brands": [],
		"categories": [],
		"items": [],
		"products": [],
		"banners": [],
		"stock_receipts": [],
		"ratings": [],
	}

	_ensure_price_list()
	_ensure_root_category(summary)
	for row in CATEGORIES:
		summary["categories"].append(_ensure_category(row))
	for row in BRANDS:
		summary["brands"].append(_ensure_brand(row))
	for row in PRODUCTS:
		item_code = _ensure_item(row)
		summary["items"].append(item_code)
		product_name = _ensure_product(row, item_code)
		summary["products"].append(product_name)
		_ensure_rating(product_name, row.get("rating"), summary)
		receipt = _ensure_stock(item_code, row["stock_qty"], row["price"])
		if receipt:
			summary["stock_receipts"].append(receipt)
	for row in BANNERS:
		banner = _ensure_banner(row)
		if banner:
			summary["banners"].append(banner)

	frappe.db.commit()
	return summary


def _ensure_price_list():
	if not frappe.db.exists("DocType", "Price List") or frappe.db.exists("Price List", PRICE_LIST):
		return
	doc = frappe.get_doc(
		{
			"doctype": "Price List",
			"price_list_name": PRICE_LIST,
			"enabled": 1,
			"selling": 1,
			"buying": 0,
			"currency": _default_currency(),
		}
	)
	doc.insert(ignore_permissions=True)


def _ensure_root_category(summary):
	if not frappe.db.exists("DocType", "Product Category"):
		return
	doc = _get_doc_by_name("Product Category", ROOT_CATEGORY)
	if not doc:
		doc = frappe.new_doc("Product Category")
		doc.category_name = ROOT_CATEGORY
	doc.enabled = 1
	doc.is_group = 1
	doc.display_order = 1
	doc.image = _img(ROOT_CATEGORY, "700x420", "ECFCCB", "365314")
	doc.description = "Mobile storefront demo category root."
	_save(doc)
	summary["categories"].append(doc.name)


def _ensure_category(row) -> str:
	doc = _get_doc_by_name("Product Category", row["category_name"])
	if not doc:
		doc = frappe.new_doc("Product Category")
		doc.category_name = row["category_name"]
	doc.parent_product_category = ROOT_CATEGORY
	doc.enabled = 1
	doc.is_group = 0
	doc.display_order = row["display_order"]
	doc.image = row["image"]
	doc.description = f"Mobile shop category for {row['category_name']}."
	_save(doc)
	return doc.name


def _ensure_brand(row) -> str:
	if not frappe.db.exists("DocType", "Brand"):
		return row["brand"]
	name = frappe.db.get_value("Brand", row["brand"], "name") or frappe.db.get_value("Brand", {"brand": row["brand"]}, "name")
	doc = frappe.get_doc("Brand", name) if name else frappe.new_doc("Brand")
	doc.brand = row["brand"]
	if doc.meta.has_field("image"):
		doc.image = row["image"]
	_save(doc)
	return doc.name


def _ensure_item(row) -> str:
	if not frappe.db.exists("DocType", "Item"):
		return row["sku"]
	item_group = frappe.db.get_value("Product Category", row["category"], "item_group") or row["category"]
	if not frappe.db.exists("Item Group", item_group):
		item_group = "All Item Groups"

	doc = _get_doc_by_name("Item", row["sku"])
	if not doc:
		doc = frappe.new_doc("Item")
		doc.item_code = row["sku"]
	doc.item_name = row["product_name"]
	doc.item_group = item_group
	doc.brand = _brand_name(row["brand"])
	doc.stock_uom = _uom(row["stock_uom"])
	doc.is_stock_item = 1
	doc.is_sales_item = 1
	doc.is_purchase_item = 1
	doc.disabled = 0
	doc.standard_rate = flt(row["price"])
	_save(doc)
	_ensure_item_price(doc.name, row["price"])
	return doc.name


def _ensure_item_price(item_code: str, rate) -> str | None:
	if not frappe.db.exists("DocType", "Item Price") or not frappe.db.exists("Price List", PRICE_LIST):
		return None
	name = frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": PRICE_LIST, "selling": 1}, "name")
	doc = frappe.get_doc("Item Price", name) if name else frappe.new_doc("Item Price")
	doc.item_code = item_code
	doc.price_list = PRICE_LIST
	doc.selling = 1
	doc.buying = 0
	doc.currency = _default_currency()
	doc.price_list_rate = flt(rate)
	_save(doc)
	return doc.name


def _ensure_product(row, item_code: str) -> str:
	doc = _get_doc_by_field("Product", "sku", row["sku"])
	if not doc:
		doc = frappe.new_doc("Product")
	doc.product_name = row["product_name"]
	doc.sku = row["sku"]
	doc.description = row["description"]
	doc.image = row["image"]
	doc.price = flt(row["price"])
	doc.discounted_price = flt(row["discounted_price"])
	doc.in_stock = 1
	doc.charge_tax = 0
	doc.brand = _brand_name(row["brand"])
	doc.category = row["category"]
	doc.status = "Active"
	doc.tags = row["tags"]
	doc.item = item_code
	doc.item_price = _item_price_name(item_code)
	doc.has_variants = 0
	_save(doc)
	return doc.name


def _ensure_banner(row) -> str | None:
	if not frappe.db.exists("DocType", "Mobile Home Banner"):
		return None
	name = frappe.db.get_value("Mobile Home Banner", {"banner_id": row["banner_id"]}, "name")
	doc = frappe.get_doc("Mobile Home Banner", name) if name else frappe.new_doc("Mobile Home Banner")
	doc.enabled = 1
	doc.display_order = row["display_order"]
	doc.block_type = row["block_type"]
	doc.block_id = row["block_id"]
	doc.banner_id = row["banner_id"]
	doc.image = row["image"]
	doc.title = row["title"]
	doc.subtitle = row["subtitle"]
	doc.button_title = row["button_title"]
	doc.gradient_start = row["gradient_start"]
	doc.gradient_end = row["gradient_end"]
	doc.action_type = row["action_type"]
	doc.action_value = row["action_value"]
	_save(doc)
	return doc.name


def _ensure_rating(product_name: str, rating, summary):
	if not rating or not frappe.db.exists("DocType", "Rating"):
		return
	name = frappe.db.get_value(
		"Rating",
		{
			"reference_doctype": "Product",
			"reference_name": product_name,
			"rated_by": "Administrator",
		},
		"name",
	)
	doc = frappe.get_doc("Rating", name) if name else frappe.new_doc("Rating")
	doc.reference_doctype = "Product"
	doc.reference_name = product_name
	doc.overall_rating = int(rating)
	doc.notes = "Seeded mobile catalog rating."
	doc.rated_by = "Administrator"
	doc.rated_at = now_datetime()
	_save(doc)
	summary["ratings"].append(doc.name)


def _ensure_stock(item_code: str, target_qty, rate) -> str | None:
	if not frappe.db.exists("DocType", "Stock Entry") or not frappe.db.exists("Warehouse", WAREHOUSE):
		return None
	current_qty = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": WAREHOUSE}, "actual_qty"))
	target_qty = flt(target_qty)
	if current_qty >= target_qty:
		return None

	doc = frappe.new_doc("Stock Entry")
	doc.stock_entry_type = "Material Receipt"
	doc.company = _default_company()
	doc.to_warehouse = WAREHOUSE
	doc.append(
		"items",
		{
			"item_code": item_code,
			"t_warehouse": WAREHOUSE,
			"qty": target_qty - current_qty,
			"basic_rate": flt(rate),
		},
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def _get_doc_by_name(doctype: str, name: str):
	if frappe.db.exists(doctype, name):
		return frappe.get_doc(doctype, name)
	return None


def _get_doc_by_field(doctype: str, fieldname: str, value: str):
	name = frappe.db.get_value(doctype, {fieldname: value}, "name")
	if name:
		return frappe.get_doc(doctype, name)
	return None


def _save(doc):
	doc.flags.ignore_permissions = True
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return doc


def _brand_name(brand: str) -> str | None:
	if not brand or not frappe.db.exists("DocType", "Brand"):
		return None
	return frappe.db.get_value("Brand", brand, "name") or frappe.db.get_value("Brand", {"brand": brand}, "name")


def _item_price_name(item_code: str) -> str | None:
	return frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": PRICE_LIST, "selling": 1}, "name")


def _uom(preferred: str) -> str:
	if preferred and frappe.db.exists("UOM", preferred):
		return preferred
	return "Nos" if frappe.db.exists("UOM", "Nos") else preferred


def _default_company() -> str | None:
	company = frappe.defaults.get_global_default("company")
	if company:
		return company
	rows = frappe.get_all("Company", fields=["name"], limit_page_length=1)
	return rows[0].name if rows else None


def _default_currency() -> str:
	company = _default_company()
	if company:
		currency = frappe.db.get_value("Company", company, "default_currency")
		if currency:
			return currency
	return frappe.defaults.get_global_default("currency") or "IQD"
