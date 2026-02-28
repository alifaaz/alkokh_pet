# Product API Documentation
> Al Kokh Clinic — Veterinary System (ERPNext/Frappe)
> Last updated: 2026-02-28

---

## Overview

The Product API manages the full lifecycle of products in the veterinary clinic store. It connects a custom `Product` Doctype with ERPNext core documents (Item, Item Price, Website Item, Stock Entry, Bin).

### Architecture

```
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
```

### Base URL
```
https://{domain}/api/method/pet_app.api.product.{endpoint}
```

---

## Doctypes

### Product
The main product document. Acts as an aggregator between the UI and ERPNext.

| Field | Type | Description |
|---|---|---|
| `name` | Auto | PROD-.#### |
| `product_name` | Data | Display name (required) |
| `sku` | Data | Unique SKU (required) |
| `barcode` | Data | Barcode |
| `description` | Text Editor | Rich text description |
| `image` | Attach Image | Product image |
| `price` | Currency | Base price |
| `discounted_price` | Currency | Sale price (overrides price) |
| `charge_tax` | Check | Apply tax |
| `in_stock` | Check | Stock status |
| `vendor` | Link → Supplier | Vendor |
| `category` | Link → Item Group | Category |
| `status` | Select | Draft / Active / Archived |
| `tags` | Data | Comma separated tags |
| `has_variants` | Check | Has variant options |
| `product_variant` | Table | Variant rows (child table) |
| `item` | Link → Item | ERP Item (readonly) |
| `item_price` | Link → Item Price | ERP Price (readonly) |
| `website_item` | Link → Website Item | Web listing (readonly) |

### Product Variant Row (Child Table)

| Field | Type | Description |
|---|---|---|
| `options` | Select | size / color / weight |
| `value` | Data | e.g. "1kg", "Red", "Large" |
| `price` | Currency | Variant-specific price (optional) |

---

## Allowed Roles

All endpoints require one of:
- `Administrator`
- `System Manager`
- `Item Manager`
- `Stock Manager`

---

## Endpoints

---

### 1. `publish_product`
**POST** `/api/method/pet_app.api.product.publish_product`

Creates a new product and publishes it to ERPNext in one request. Or publishes an existing draft product.

**What it does:**
1. Creates `Product` document
2. Creates `Item` in ERPNext
3. Creates `Item Price` (Standard Selling)
4. Creates `Website Item` (published)
5. Sets status → `Active`, in_stock → `1`

#### Create & Publish (new product)
```json
POST /api/method/pet_app.api.product.publish_product

{
  "product_name": "Royal Canin Adult",
  "sku": "RC-ADULT-001",
  "category": "All Item Groups",
  "price": 45000,
  "description": "Premium dry food for adult dogs",
  "barcode": "1234567890",
  "vendor": "Royal Canin Co",
  "tags": "dog,food,premium"
}
```

#### Publish Existing Draft
```json
POST /api/method/pet_app.api.product.publish_product

{
  "product_id": "PRODUCT-00001"
}
```

#### With Variants
```json
POST /api/method/pet_app.api.product.publish_product

{
  "product_name": "Royal Canin Size",
  "sku": "RC-SIZE-001",
  "category": "All Item Groups",
  "price": 45000,
  "has_variants": 1,
  "product_variant": [
    {"options": "weight", "value": "1kg"},
    {"options": "weight", "value": "3kg"},
    {"options": "weight", "value": "5kg"}
  ]
}
```

#### Response
```json
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
```

#### Response (with variants)
```json
{
  "message": {
    "data": {
      "product": "PRODUCT-00033",
      "item": "RC-SIZE-001",
      "website_item": "WEB-ITM-0014",
      "has_variants": 1,
      "variants": [],
      "options": [
        {"options": "weight", "value": "1kg", "price": 45000.0},
        {"options": "weight", "value": "3kg", "price": 45000.0},
        {"options": "weight", "value": "5kg", "price": 45000.0}
      ]
    }
  }
}
```

#### Validation Rules
- `product_name` required
- `category` required
- `sku` required + must be unique
- `price` required for simple products
- `product_variant` required if `has_variants = 1`

---

### 2. `restock_product`
**POST** `/api/method/pet_app.api.product.restock_product`

Adds stock to a product by creating a **Material Receipt** Stock Entry in ERPNext.

> ⚠️ Product must be published (has an `item`) before restocking.

```json
POST /api/method/pet_app.api.product.restock_product

{
  "product_id": "PRODUCT-00001",
  "qty": 50,
  "warehouse": "Stores - H"
}
```

| Parameter | Required | Description |
|---|---|---|
| `product_id` | ✅ | Product document name |
| `qty` | ✅ | Quantity to add (must be > 0) |
| `warehouse` | ❌ | Defaults to Stock Settings default warehouse |

#### Response
```json
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
```

#### Notes
- Stock is reduced automatically through Sales Orders / Invoices (not manually)
- All stock movements are tracked in ERPNext Stock Ledger
- Verify in ERPNext: `Stock → Stock Balance` or `Stock → Stock Ledger`

---

### 3. `get_stock_info`
**GET** `/api/method/pet_app.api.product.get_stock_info`

Returns live stock information from ERPNext Bin.

```
GET /api/method/pet_app.api.product.get_stock_info?product_id=PRODUCT-00001
GET /api/method/pet_app.api.product.get_stock_info?product_id=PRODUCT-00001&warehouse=Stores - H
```

| Parameter | Required | Description |
|---|---|---|
| `product_id` | ✅ | Product document name |
| `warehouse` | ❌ | Defaults to Stock Settings default warehouse |

#### Response (simple product)
```json
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
```

#### Response (variant product)
```json
{
  "message": {
    "data": {
      "in_stock": true,
      "qty": 200.0,
      "last_restocked": "2026-02-28",
      "total_lifetime": 200.0,
      "variants": [
        {"options": "weight", "value": "1kg", "price": 45000.0},
        {"options": "weight", "value": "3kg", "price": 45000.0}
      ]
    }
  }
}
```

#### Response (unpublished product)
```json
{
  "message": {
    "data": {
      "in_stock": false,
      "qty": 0,
      "last_restocked": null,
      "total_lifetime": 0,
      "variants": []
    }
  }
}
```

---

### 4. `get_products`
**GET** `/api/method/pet_app.api.product.get_products`

Returns a list of products with live qty from Bin. Follows Frappe standard parameter style.

#### Parameters

| Parameter | Description | Example |
|---|---|---|
| `limit_page_length` | Items per page (default: 20) | `10` |
| `limit_start` | Offset for pagination (default: 0) | `0`, `10`, `20` |
| `filters` | JSON array of filters | `[["status","=","Active"]]` |
| `fields` | JSON array of fields to return | `["name","product_name","price"]` |
| `order_by` | Sort field and direction | `creation desc`, `price asc` |
| `search_term` | Search by product name | `Royal` |

#### Examples

```
# All products
GET /api/method/pet_app.api.product.get_products

# Paginated
GET /api/method/pet_app.api.product.get_products?limit_page_length=10&limit_start=0

# Filter by status
GET /api/method/pet_app.api.product.get_products?filters=[["status","=","Active"]]

# Filter by in_stock
GET /api/method/pet_app.api.product.get_products?filters=[["in_stock","=",1]]

# Multiple filters
GET /api/method/pet_app.api.product.get_products?filters=[["status","=","Active"],["has_variants","=",0]]

# Search
GET /api/method/pet_app.api.product.get_products?search_term=Royal

# Sort by price
GET /api/method/pet_app.api.product.get_products?order_by=price asc

# Full query
GET /api/method/pet_app.api.product.get_products?limit_page_length=10&limit_start=0&filters=[["status","=","Active"]]&search_term=Royal&order_by=creation desc
```

#### Response
```json
{
  "message": {
    "data": [
      {
        "name": "PRODUCT-00001",
        "product_name": "Royal Canin Puppy",
        "sku": "RC-PUPPY-001",
        "image": null,
        "description": "Premium puppy food",
        "price": 45000.0,
        "discounted_price": 0.0,
        "status": "Active",
        "in_stock": 1,
        "category": "All Item Groups",
        "vendor": null,
        "has_variants": 0,
        "item": "RC-PUPPY-001",
        "qty": 20.0
      }
    ]
  }
}
```

---

## Frappe Standard Endpoints

These work out of the box — no custom code needed.

### Product CRUD

```
# Get one product (with child tables)
GET /api/resource/Product/PRODUCT-00001

# Save as Draft
POST /api/resource/Product
{
  "product_name": "New Product",
  "sku": "NEW-001",
  "category": "All Item Groups",
  "price": 25000,
  "status": "Draft"
}

# Update product
PUT /api/resource/Product/PRODUCT-00001
{
  "price": 50000,
  "description": "Updated description"
}

# Delete product
DELETE /api/resource/Product/PRODUCT-00001
```

### Vendor (Supplier) CRUD

```
# List vendors
GET /api/resource/Supplier?fields=["name","supplier_name","supplier_group"]&limit_page_length=50

# Get one
GET /api/resource/Supplier/SUP-00001

# Create
POST /api/resource/Supplier
{"supplier_name": "Royal Canin Co", "supplier_group": "All Supplier Groups"}

# Update
PUT /api/resource/Supplier/SUP-00001
{"supplier_name": "Updated Name"}

# Delete
DELETE /api/resource/Supplier/SUP-00001

# Search
GET /api/resource/Supplier?filters=[["supplier_name","like","%Royal%"]]
```

### Brand CRUD

```
# List brands
GET /api/resource/Brand?fields=["name","brand","description"]&limit_page_length=50

# Get one
GET /api/resource/Brand/Royal Canin

# Create
POST /api/resource/Brand
{"brand": "Royal Canin", "description": "Premium pet food brand"}

# Update
PUT /api/resource/Brand/Royal Canin
{"description": "Updated description"}

# Delete
DELETE /api/resource/Brand/Royal Canin

# Search
GET /api/resource/Brand?filters=[["brand","like","%Royal%"]]
```

### Category (Item Group)

```
# List categories
GET /api/resource/Item Group?fields=["name","parent_item_group"]&limit_page_length=50

# Search
GET /api/resource/Item Group?filters=[["name","like","%Food%"]]
```

### Upload Image

```
POST /api/method/upload_file
Content-Type: multipart/form-data

file: <binary>
is_private: 0
folder: Home/Attachments
```

---

## Product Flow

### New Product Flow
```
1. POST publish_product (with all fields)
      ↓
2. Creates Product (PRODUCT-XXXXX)
      ↓
3. Creates Item (item_code = SKU)
      ↓
4. Creates Item Price (Standard Selling)
      ↓
5. Creates Website Item (published = 1)
      ↓
6. Product status → Active
```

### Restock Flow
```
1. POST restock_product (product_id, qty, warehouse)
      ↓
2. Creates Stock Entry (Material Receipt)
      ↓
3. ERPNext updates Bin (actual_qty)
      ↓
4. GET get_stock_info → returns new qty
```

### Stock Reduction Flow
```
Stock only reduces through:
  ├── Sales Invoice
  ├── Delivery Note
  └── Stock Entry (Material Issue) — for damaged/lost items
```

---

## Error Responses

```json
{
  "exception": "frappe.exceptions.ValidationError: SKU 'RC-001' موجود مسبقاً",
  "exc_type": "ValidationError"
}
```

| Error | Cause |
|---|---|
| `SKU already exists` | Duplicate SKU on publish |
| `Product not published` | Restock before publish |
| `Warehouse not found` | Invalid warehouse name |
| `qty must be > 0` | Zero or negative qty on restock |
| `price required` | Simple product without price |
| `category required` | Missing category on publish |

---

## Notes

- All prices are in **IQD** (Iraqi Dinar)
- Default warehouse: **Stores - H**
- Default price list: **Standard Selling**
- Product naming: **PROD-.####** (auto)
- Frappe response wrapper: all custom endpoints return `response.message.data`
