# Product API Documentation
> Al Kokh Clinic — Veterinary System (ERPNext/Frappe)
> Last updated: 2026-02-28

---

## Overview

The Product API manages the full lifecycle of products in the veterinary clinic store. It connects a custom `Product` Doctype with ERPNext core documents (Item, Item Price, Website Item, Stock Entry, Bin).

### Architecture

\`\`\`
Frontend
   ↓
Product API (pet_app/api/product.py)
   ↓
Product Doctype  ←→  ERPNext Core
                      ├── Item
                      ├── Item Price
                      ├── Website Item
                      ├── Stock Entry
                      └── Bin (live qty)
\`\`\`

### Base URL
\`\`\`
https://{domain}/api/method/pet_app.api.product.{endpoint}
\`\`\`

---

## Doctypes

### Product

| Field | Type | Description |
|---|---|---|
| \`name\` | Auto | PROD-.#### |
| \`product_name\` | Data | Display name (required) |
| \`sku\` | Data | Unique SKU (required) |
| \`barcode\` | Data | Barcode |
| \`description\` | Text Editor | Rich text description |
| \`image\` | Attach Image | Product image |
| \`price\` | Currency | Base price |
| \`discounted_price\` | Currency | Sale price (overrides price) |
| \`charge_tax\` | Check | Apply tax |
| \`in_stock\` | Check | Stock status |
| \`vendor\` | Link → Supplier | Vendor |
| \`category\` | Link → Item Group | Category |
| \`status\` | Select | Draft / Active / Archived |
| \`tags\` | Data | Comma separated tags |
| \`has_variants\` | Check | Has variant options |
| \`product_variant\` | Table | Variant rows (child table) |
| \`item\` | Link → Item | ERP Item (readonly) |
| \`item_price\` | Link → Item Price | ERP Price (readonly) |
| \`website_item\` | Link → Website Item | Web listing (readonly) |

### Product Variant Row (Child Table)

| Field | Type | Description |
|---|---|---|
| \`options\` | Select | size / color / weight |
| \`value\` | Data | e.g. "1kg", "Red", "Large" |
| \`price\` | Currency | Variant-specific price (optional) |

---

## Allowed Roles

All endpoints require one of:
- \`Administrator\`
- \`System Manager\`
- \`Item Manager\`
- \`Stock Manager\`

---

## Endpoints

### 1. publish_product
**POST** \`/api/method/pet_app.api.product.publish_product\`

Creates and publishes a product to ERPNext in one request.

**What it does:**
1. Creates Product document
2. Creates Item in ERPNext
3. Creates Item Price (Standard Selling)
4. Creates Website Item (published)
5. Sets status → Active

#### Create & Publish
\`\`\`json
{
  "product_name": "Royal Canin Adult",
  "sku": "RC-ADULT-001",
  "category": "All Item Groups",
  "price": 45000,
  "description": "Premium dry food",
  "vendor": "Royal Canin Co",
  "tags": "dog,food,premium"
}
\`\`\`

#### Publish Existing Draft
\`\`\`json
{
  "product_id": "PRODUCT-00001"
}
\`\`\`

#### With Variants
\`\`\`json
{
  "product_name": "Royal Canin Size",
  "sku": "RC-SIZE-001",
  "category": "All Item Groups",
  "price": 45000,
  "has_variants": 1,
  "product_variant": [
    {"options": "weight", "value": "1kg"},
    {"options": "weight", "value": "3kg"}
  ]
}
\`\`\`

#### Response
\`\`\`json
{
  "message": {
    "data": {
      "product": "PRODUCT-00035",
      "item": "RC-ADULT-001",
      "website_item": "WEB-ITM-0017",
      "has_variants": 0,
      "variants": [],
      "options": []
    }
  }
}
\`\`\`

---

### 2. restock_product
**POST** \`/api/method/pet_app.api.product.restock_product\`

Adds stock by creating a Material Receipt Stock Entry in ERPNext.

\`\`\`json
{
  "product_id": "PRODUCT-00001",
  "qty": 50,
  "warehouse": "Stores - H"
}
\`\`\`

| Parameter | Required | Description |
|---|---|---|
| \`product_id\` | ✅ | Product document name |
| \`qty\` | ✅ | Quantity to add (must be > 0) |
| \`warehouse\` | ❌ | Defaults to Stock Settings default |

#### Response
\`\`\`json
{
  "message": {
    "data": {
      "stock_entry": "MAT-STE-2026-00005",
      "item_code": "RC-ADULT-001",
      "warehouse": "Stores - H",
      "qty_added": 50.0,
      "current_qty": 150.0
    }
  }
}
\`\`\`

---

### 3. get_stock_info
**GET** \`/api/method/pet_app.api.product.get_stock_info\`

Returns live stock info from ERPNext Bin.

\`\`\`
GET ?product_id=PRODUCT-00001
GET ?product_id=PRODUCT-00001&warehouse=Stores - H
\`\`\`

#### Response
\`\`\`json
{
  "message": {
    "data": {
      "in_stock": true,
      "qty": 150.0,
      "last_restocked": "2026-02-28",
      "total_lifetime": 200.0,
      "variants": []
    }
  }
}
\`\`\`

---

### 4. get_products
**GET** \`/api/method/pet_app.api.product.get_products\`

Returns paginated product list with live qty. Follows Frappe standard style.

| Parameter | Description | Example |
|---|---|---|
| \`limit_page_length\` | Items per page (default: 20) | \`10\` |
| \`limit_start\` | Pagination offset | \`0\`, \`10\` |
| \`filters\` | JSON filter array | \`[["status","=","Active"]]\` |
| \`fields\` | Fields to return | \`["name","sku","price"]\` |
| \`order_by\` | Sort | \`creation desc\` |
| \`search_term\` | Search by name | \`Royal\` |

\`\`\`
GET ?limit_page_length=10&limit_start=0
GET ?filters=[["status","=","Active"]]
GET ?search_term=Royal&order_by=price asc
GET ?limit_page_length=10&filters=[["status","=","Active"]]&search_term=Royal
\`\`\`

---

## Frappe Standard Endpoints

### Product CRUD
\`\`\`
GET    /api/resource/Product/{id}
POST   /api/resource/Product       ← Save Draft
PUT    /api/resource/Product/{id}  ← Update
DELETE /api/resource/Product/{id}  ← Delete
\`\`\`

### Vendor (Supplier)
\`\`\`
GET    /api/resource/Supplier?fields=["name","supplier_name"]
POST   /api/resource/Supplier
PUT    /api/resource/Supplier/{name}
DELETE /api/resource/Supplier/{name}
GET    /api/resource/Supplier?filters=[["supplier_name","like","%Royal%"]]
\`\`\`

### Brand
\`\`\`
GET    /api/resource/Brand?fields=["name","brand","description"]
POST   /api/resource/Brand
PUT    /api/resource/Brand/{name}
DELETE /api/resource/Brand/{name}
\`\`\`

### Category
\`\`\`
GET /api/resource/Item Group?fields=["name","parent_item_group"]
\`\`\`

### Upload Image
\`\`\`
POST /api/method/upload_file
Content-Type: multipart/form-data
file: <binary>
is_private: 0
\`\`\`

---

## Product Flow

\`\`\`
publish_product → Item + Item Price + Website Item → status: Active
restock_product → Stock Entry (Material Receipt) → Bin qty updated
get_stock_info  → reads Bin → returns live qty
get_products    → Product list + Bin qty per product
\`\`\`

## Stock Reduction
Stock only reduces through:
- Sales Invoice / Delivery Note (customer orders)
- Stock Entry Material Issue (damaged/lost items)

---

## Notes
- All prices in **IQD**
- Default warehouse: **Stores - H**
- Default price list: **Standard Selling**
- Frappe response wrapper: \`response.message.data\`
