# Warehouse & Stock Frontend API Guide

This guide explains how the frontend should build Warehouse and Stock screens using only standard Frappe / ERPNext REST APIs.

No custom backend APIs are required.

Base pattern:

```http
{{base_url}}/api/resource/{DocType}
```

Required headers:

```http
Content-Type: application/json
Accept: application/json
Authorization: token {{api_key}}:{{api_secret}}
```

If the app uses cookie/session authentication instead of token auth, keep the same endpoints and send the authenticated session cookie.

## Mandatory Pagination, Filters, and Search

Do not load all rows at once.

Every list request must use standard Frappe pagination:

```http
limit_page_length=20
limit_start=0
```

For the next page:

```http
limit_page_length=20
limit_start=20
```

For infinite scroll or "Load more":

```js
next_limit_start = current_limit_start + limit_page_length
```

When the user searches, reset pagination:

```js
limit_start = 0
```

Use Frappe filters directly in the endpoint:

```http
filters=[["fieldname","operator","value"]]
```

Search examples:

```http
filters=[["warehouse_name","like","%Stores%"]]
```

```http
filters=[["warehouse","=","Stores - H"],["item_code","like","%ITEM%"]]
```

Frontend rule:

```text
List UI = Frappe REST filters + limit_page_length + limit_start.
No frontend-only full-table loading.
```

## 1. Core Concepts

### Warehouse

`Warehouse` is the ERPNext location where stock is stored.

Warehouses are a tree:

- Group Warehouse: `is_group = 1`
- Leaf Warehouse: `is_group = 0`

Only leaf warehouses can be used for stock operations.

Example:

```text
All Warehouses - H
└── Main Warehouse - H        is_group = 1
    ├── Stores - H            is_group = 0
    └── Pharmacy - H          is_group = 0
```

Frontend rule:

```text
Users may view group warehouses, but must not select them for stock operations.
```

### Bin

`Bin` is the current stock balance per Item per Warehouse.

Use `Bin` only for reading stock.

Never update `Bin` directly.

### Stock Entry

`Stock Entry` is used to change stock.

Common stock entry types:

- `Material Receipt`: add stock into a warehouse
- `Material Issue`: remove stock from a warehouse
- `Material Transfer`: move stock from one warehouse to another

Stock is not updated when a Stock Entry is created as draft.

Stock is updated only after submit.

## 2. Document State

ERPNext uses `docstatus`:

| docstatus | Meaning | Stock Effect |
|---:|---|---|
| `0` | Draft | No stock effect |
| `1` | Submitted | Stock is applied |
| `2` | Cancelled | Stock effect is reversed |

Frontend behavior:

- After create, show Stock Entry as `Draft`.
- Do not refresh stock quantity expecting changes while `docstatus = 0`.
- Submit the Stock Entry using `POST /api/method/frappe.client.submit`.
- Refresh `Bin` only after successful submit.
- If cancelled later, refresh `Bin` again because stock is reversed.

## 3. Warehouse Management UI

### Frontend Flow

1. Load the first page of warehouses using `limit_page_length` and `limit_start`.
2. Render them as a tree using `parent_warehouse`.
3. Show `is_group = 1` warehouses as folders/groups.
4. Show `is_group = 0` warehouses as selectable warehouses.
5. Disable group warehouses in dropdowns used for stock operations.
6. Use "Load more" or infinite scroll by increasing `limit_start`.
7. Search by `warehouse_name` using Frappe `filters`.

### Get All Warehouses

```http
GET {{base_url}}/api/resource/Warehouse?fields=["name","warehouse_name","parent_warehouse","is_group","company"]&limit_page_length=20&limit_start=0
```

Next page:

```http
GET {{base_url}}/api/resource/Warehouse?fields=["name","warehouse_name","parent_warehouse","is_group","company"]&limit_page_length=20&limit_start=20
```

Search:

```http
GET {{base_url}}/api/resource/Warehouse?fields=["name","warehouse_name","parent_warehouse","is_group","company"]&filters=[["warehouse_name","like","%Stores%"]]&limit_page_length=20&limit_start=0
```

Example response:

```json
{
  "data": [
    {
      "name": "Main Warehouse - H",
      "warehouse_name": "Main Warehouse",
      "parent_warehouse": "All Warehouses - H",
      "is_group": 1,
      "company": "HM"
    },
    {
      "name": "Stores - H",
      "warehouse_name": "Stores",
      "parent_warehouse": "Main Warehouse - H",
      "is_group": 0,
      "company": "HM"
    }
  ]
}
```

### Get Leaf Warehouses Only

```http
GET {{base_url}}/api/resource/Warehouse?fields=["name","warehouse_name","parent_warehouse","is_group","company"]&filters=[["is_group","=",0]]&limit_page_length=20&limit_start=0
```

Use this for stock operation dropdowns.

Search leaf warehouses:

```http
GET {{base_url}}/api/resource/Warehouse?fields=["name","warehouse_name","parent_warehouse","is_group","company"]&filters=[["is_group","=",0],["warehouse_name","like","%Stores%"]]&limit_page_length=20&limit_start=0
```

### Create Warehouse

```http
POST {{base_url}}/api/resource/Warehouse
```

Body:

```json
{
  "warehouse_name": "Pharmacy",
  "parent_warehouse": "Main Warehouse - H",
  "is_group": 0,
  "company": "HM"
}
```

Example response:

```json
{
  "data": {
    "name": "Pharmacy - H",
    "warehouse_name": "Pharmacy",
    "parent_warehouse": "Main Warehouse - H",
    "is_group": 0,
    "company": "HM"
  }
}
```

### Get Single Warehouse

```http
GET {{base_url}}/api/resource/Warehouse/{{warehouse_name}}
```

Example:

```http
GET {{base_url}}/api/resource/Warehouse/Stores%20-%20H
```

### Update Warehouse

```http
PUT {{base_url}}/api/resource/Warehouse/{{warehouse_name}}
```

Body:

```json
{
  "warehouse_name": "Updated Stores"
}
```

### Delete Warehouse

```http
DELETE {{base_url}}/api/resource/Warehouse/{{warehouse_name}}
```

Important:

- Cannot delete if warehouse has stock.
- Cannot delete if warehouse has child warehouses.
- Cannot delete if linked to submitted stock/accounting documents.

## 4. Stock Viewing UI

### Frontend Flow

1. User selects a leaf warehouse.
2. Frontend fetches the first page of `Bin` records for that warehouse.
3. Display item data and quantity.
4. If price is needed, fetch `Item Price` separately by `item_code`.
5. Use "Load more" or infinite scroll by increasing `limit_start`.
6. Search by `item_code` using Frappe `filters`.

### Get Items in Specific Warehouse

```http
GET {{base_url}}/api/resource/Bin?fields=["item_code","warehouse","actual_qty","valuation_rate"]&filters=[["warehouse","=","Stores - H"],["actual_qty",">",0]]&limit_page_length=20&limit_start=0
```

Next page:

```http
GET {{base_url}}/api/resource/Bin?fields=["item_code","warehouse","actual_qty","valuation_rate"]&filters=[["warehouse","=","Stores - H"],["actual_qty",">",0]]&limit_page_length=20&limit_start=20
```

Search stock by Item Code:

```http
GET {{base_url}}/api/resource/Bin?fields=["item_code","warehouse","actual_qty","valuation_rate"]&filters=[["warehouse","=","Stores - H"],["item_code","like","%ITEM%"]]&limit_page_length=20&limit_start=0
```

Example response:

```json
{
  "data": [
    {
      "item_code": "ITEM-001",
      "warehouse": "Stores - H",
      "actual_qty": 20,
      "valuation_rate": 12000
    }
  ]
}
```

### Get Item Details for Display

Use this after getting `item_code` from `Bin`.

```http
GET {{base_url}}/api/resource/Item/ITEM-001?fields=["name","item_name","image","item_group","brand"]
```

Example response:

```json
{
  "data": {
    "name": "ITEM-001",
    "item_name": "Dog Food 1kg",
    "image": "/files/dog-food.png",
    "item_group": "Pet Food",
    "brand": "Farmina"
  }
}
```

### Get Item Price

```http
GET {{base_url}}/api/resource/Item Price?fields=["name","item_code","price_list","price_list_rate","currency"]&filters=[["item_code","=","ITEM-001"],["price_list","=","Standard Selling"],["selling","=",1]]&limit_page_length=1&limit_start=0
```

Example response:

```json
{
  "data": [
    {
      "name": "PRICE-001",
      "item_code": "ITEM-001",
      "price_list": "Standard Selling",
      "price_list_rate": 15000,
      "currency": "IQD"
    }
  ]
}
```

### Recommended Stock Card Data

For each row, display:

```json
{
  "item_code": "ITEM-001",
  "item_name": "Dog Food 1kg",
  "image": "/files/dog-food.png",
  "brand": "Farmina",
  "warehouse": "Stores - H",
  "actual_qty": 20,
  "price": 15000,
  "currency": "IQD"
}
```

## 5. Add Stock Flow: Material Receipt

Use Material Receipt when stock enters a warehouse.

Examples:

- Initial stock
- Supplier delivery
- Manual stock addition

### Frontend Flow

1. User selects a leaf target warehouse.
2. User selects one or more existing Items.
3. User enters quantity for each Item.
4. Frontend creates a draft Stock Entry.
5. Frontend submits the Stock Entry.
6. Frontend refreshes `Bin`.

Submit is required because draft Stock Entries do not affect stock.

### Create Material Receipt Draft

```http
POST {{base_url}}/api/resource/Stock Entry
```

Body:

```json
{
  "stock_entry_type": "Material Receipt",
  "company": "HM",
  "remarks": "Frontend stock receipt",
  "items": [
    {
      "item_code": "ITEM-001",
      "qty": 10,
      "t_warehouse": "Stores - H",
      "basic_rate": 15000
    }
  ]
}
```

Important:

- `t_warehouse` is required for Material Receipt.
- `basic_rate` should be sent to avoid valuation errors.
- `qty` must be greater than `0`.

Example response:

```json
{
  "data": {
    "name": "MAT-STE-2026-00001",
    "stock_entry_type": "Material Receipt",
    "docstatus": 0,
    "company": "HM",
    "items": [
      {
        "item_code": "ITEM-001",
        "qty": 10,
        "t_warehouse": "Stores - H",
        "basic_rate": 15000
      }
    ]
  }
}
```

At this point stock is not updated yet.

### Submit Material Receipt

```http
POST {{base_url}}/api/method/frappe.client.submit
```

Body:

```json
{
  "doctype": "Stock Entry",
  "name": "MAT-STE-2026-00001"
}
```

Example response:

```json
{
  "data": {
    "name": "MAT-STE-2026-00001",
    "stock_entry_type": "Material Receipt",
    "docstatus": 1
  }
}
```

After submit, refresh `Bin`.

## 6. Remove Stock Flow: Material Issue

Use Material Issue when stock leaves a warehouse without a transfer target.

Examples:

- Damaged item
- Internal consumption
- Manual adjustment out

### Frontend Flow

1. User selects a leaf source warehouse.
2. Frontend loads available stock from `Bin`.
3. User selects Item and quantity.
4. Frontend validates available quantity.
5. Frontend creates a draft Stock Entry.
6. Frontend submits the Stock Entry.
7. Frontend refreshes `Bin`.

### Create Material Issue Draft

```http
POST {{base_url}}/api/resource/Stock Entry
```

Body:

```json
{
  "stock_entry_type": "Material Issue",
  "company": "HM",
  "remarks": "Frontend stock issue",
  "items": [
    {
      "item_code": "ITEM-001",
      "qty": 2,
      "s_warehouse": "Stores - H",
      "basic_rate": 15000
    }
  ]
}
```

Important:

- `s_warehouse` is required for Material Issue.
- `basic_rate` should be sent to avoid valuation errors.
- Frontend should prevent issuing more than available `actual_qty`.

### Submit Material Issue

```http
POST {{base_url}}/api/method/frappe.client.submit
```

Body:

```json
{
  "doctype": "Stock Entry",
  "name": "MAT-STE-2026-00002"
}
```

After submit:

- Source warehouse quantity decreases.
- Refresh `Bin` for the selected warehouse.

## 7. Transfer Stock Flow: Material Transfer

Use Material Transfer to move stock between warehouses.

Examples:

- Move item from main store to pharmacy
- Move item between branches

### Frontend Flow

1. User selects source leaf warehouse.
2. User selects target leaf warehouse.
3. Frontend validates source and target are different.
4. Frontend loads source stock from `Bin`.
5. User selects Item and quantity.
6. Frontend creates draft Stock Entry.
7. Frontend submits Stock Entry.
8. Frontend refreshes `Bin` for both warehouses.

### Create Material Transfer Draft

```http
POST {{base_url}}/api/resource/Stock Entry
```

Body:

```json
{
  "stock_entry_type": "Material Transfer",
  "company": "HM",
  "remarks": "Frontend warehouse transfer",
  "items": [
    {
      "item_code": "ITEM-001",
      "qty": 5,
      "s_warehouse": "Stores - H",
      "t_warehouse": "Pharmacy - H",
      "basic_rate": 15000
    }
  ]
}
```

Important:

- `s_warehouse` is required.
- `t_warehouse` is required.
- Source and target warehouse must not be the same.
- Both warehouses must be leaf warehouses.

### Submit Material Transfer

```http
POST {{base_url}}/api/method/frappe.client.submit
```

Body:

```json
{
  "doctype": "Stock Entry",
  "name": "MAT-STE-2026-00003"
}
```

After submit:

- Source warehouse quantity decreases.
- Target warehouse quantity increases.
- Refresh `Bin` for both warehouses.

## 8. Validation Rules

Frontend must validate before creating Stock Entry:

| Field | Rule |
|---|---|
| `stock_entry_type` | Must be one of `Material Receipt`, `Material Issue`, `Material Transfer` |
| `company` | Required unless backend/site defaults are guaranteed |
| `item_code` | Required and must exist in `Item` |
| `qty` | Required and must be greater than `0` |
| `s_warehouse` | Required for `Material Issue` and `Material Transfer` |
| `t_warehouse` | Required for `Material Receipt` and `Material Transfer` |
| warehouse | Must be `is_group = 0` |
| transfer warehouses | Source and target must be different |
| `basic_rate` | Recommended for all stock entry rows |

For Material Issue and Material Transfer:

```text
qty must be <= Bin.actual_qty in the source warehouse
```

## 9. What Frontend Must Not Do

Do not:

- Do not edit `Bin` directly.
- Do not create stock movement by updating Item quantity fields.
- Do not assume stock changed after creating a draft Stock Entry.
- Do not use group warehouses in stock entry rows.
- Do not allow `qty <= 0`.
- Do not allow unsupported stock entry types.
- Do not submit with missing warehouse fields.
- Do not hide submit failures.
- Do not display draft Stock Entry as completed stock movement.
- Do not create Items from the Warehouse module.

## 10. Error Handling

### Stock Not Updating

Likely reason:

```text
Stock Entry is still Draft.
```

Check:

```json
{
  "docstatus": 0
}
```

Fix in frontend flow:

```text
Submit the Stock Entry, then refresh Bin.
```

### Invalid Warehouse

Possible reasons:

- Warehouse name does not exist.
- Warehouse is a group warehouse.
- Warehouse belongs to another company.
- Wrong warehouse name was sent.

Frontend handling:

- Fetch warehouses from `/api/resource/Warehouse`.
- Only allow `is_group = 0`.
- Send the `name` value, not only `warehouse_name`.

Correct:

```json
{
  "t_warehouse": "Stores - H"
}
```

Incorrect:

```json
{
  "t_warehouse": "Stores"
}
```

### Missing Fields

Common failures:

- Missing `item_code`
- Missing `qty`
- Missing `s_warehouse`
- Missing `t_warehouse`
- Missing `company`

Frontend should show the backend error message and keep the Stock Entry in failed/draft state.

### Valuation Error

Typical error:

```text
Valuation Rate for Item is required for Stock Entry
```

Cause:

- Item has no previous valuation rate.
- Item has no standard rate.
- Stock Entry row was submitted without `basic_rate`.

Frontend rule:

```text
Always send basic_rate when creating Stock Entry rows.
```

Recommended value:

- Use current purchase cost if available.
- Otherwise use standard selling price only if the business accepts it for this operation.
- For internal test/demo stock only, use a safe non-zero value approved by backend/accounting.

### Insufficient Stock

For Material Issue and Transfer:

Frontend should check `Bin.actual_qty` before submit.

If backend still rejects:

- Another user may have consumed stock after the frontend loaded the Bin.
- Refresh Bin and ask user to retry with available quantity.

## 11. End-to-End Example: Add 10 Items and Show Stock Update

Scenario:

```text
Add 10 units of ITEM-001 to Stores - H.
```

### Step 1: Load Leaf Warehouses

```http
GET {{base_url}}/api/resource/Warehouse?fields=["name","warehouse_name","is_group"]&filters=[["is_group","=",0]]&limit_page_length=20&limit_start=0
```

User selects:

```text
Stores - H
```

### Step 2: Check Current Stock

```http
GET {{base_url}}/api/resource/Bin?fields=["item_code","warehouse","actual_qty"]&filters=[["item_code","=","ITEM-001"],["warehouse","=","Stores - H"]]
```

Example response before:

```json
{
  "data": [
    {
      "item_code": "ITEM-001",
      "warehouse": "Stores - H",
      "actual_qty": 20
    }
  ]
}
```

### Step 3: Create Draft Stock Entry

```http
POST {{base_url}}/api/resource/Stock Entry
```

Body:

```json
{
  "stock_entry_type": "Material Receipt",
  "company": "HM",
  "remarks": "Add 10 units from frontend",
  "items": [
    {
      "item_code": "ITEM-001",
      "qty": 10,
      "t_warehouse": "Stores - H",
      "basic_rate": 15000
    }
  ]
}
```

Response:

```json
{
  "data": {
    "name": "MAT-STE-2026-00010",
    "docstatus": 0,
    "stock_entry_type": "Material Receipt"
  }
}
```

Stock is still `20`.

### Step 4: Submit Stock Entry

```http
POST {{base_url}}/api/method/frappe.client.submit
```

Body:

```json
{
  "doctype": "Stock Entry",
  "name": "MAT-STE-2026-00010"
}
```

Response:

```json
{
  "data": {
    "name": "MAT-STE-2026-00010",
    "docstatus": 1,
    "stock_entry_type": "Material Receipt"
  }
}
```

### Step 5: Refresh Stock

```http
GET {{base_url}}/api/resource/Bin?fields=["item_code","warehouse","actual_qty"]&filters=[["item_code","=","ITEM-001"],["warehouse","=","Stores - H"]]
```

Expected response after:

```json
{
  "data": [
    {
      "item_code": "ITEM-001",
      "warehouse": "Stores - H",
      "actual_qty": 30
    }
  ]
}
```

## 12. Frontend Checklist

Warehouse management:

- Load warehouses with paginated Frappe REST requests.
- Render tree using `parent_warehouse`.
- Display group and leaf warehouses differently.
- Prevent selecting group warehouses for stock operations.
- Create, update, and delete Warehouse using `/api/resource/Warehouse`.

Stock viewing:

- Load stock using `/api/resource/Bin`.
- Fetch Item details for `item_name`, `image`, `brand`, and `item_group`.
- Fetch Item Price if price is displayed.
- Show empty state when selected warehouse has no stock.

Add stock:

- Create `Material Receipt`.
- Include `t_warehouse`.
- Include `basic_rate`.
- Submit using `POST /api/method/frappe.client.submit`.
- Refresh Bin after submit.

Remove stock:

- Create `Material Issue`.
- Include `s_warehouse`.
- Validate available quantity.
- Include `basic_rate`.
- Submit using `POST /api/method/frappe.client.submit`.
- Refresh Bin after submit.

Transfer stock:

- Create `Material Transfer`.
- Include both `s_warehouse` and `t_warehouse`.
- Validate warehouses are different.
- Validate source quantity.
- Submit using `POST /api/method/frappe.client.submit`.
- Refresh both source and target warehouses.

Error handling:

- Show backend validation errors.
- Do not mark draft Stock Entries as completed.
- Handle valuation errors by requiring `basic_rate`.
- Handle insufficient stock by refreshing Bin.

Production testing:

- Test warehouse tree loading.
- Test group warehouse disabled state.
- Test Material Receipt create and submit.
- Test Material Issue create and submit.
- Test Material Transfer create and submit.
- Test Bin refresh after submit.
- Test validation for `qty <= 0`.
- Test validation for missing warehouse.
- Test validation for group warehouse selection.
- Test valuation error prevention with `basic_rate`.
