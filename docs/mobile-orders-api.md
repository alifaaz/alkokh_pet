# Alkokh Mobile Orders API

Mobile order endpoints are Frappe method endpoints under:

```text
/api/method/pet_app.api.mobile.orders.<method_name>
```

Orders are ERPNext `Sales Order` records. These endpoints do not create a new order DocType and do not bypass existing Sales Order status hooks, stock movement, driver, or COD accounting behavior.

## Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/method/pet_app.api.mobile.orders.list_orders` | List current Guardian Sales Orders |
| `GET` | `/api/method/pet_app.api.mobile.orders.get_order` | Get one Sales Order detail |
| `POST` | `/api/method/pet_app.api.mobile.orders.quote` | Validate the frontend cart and return current stock/price totals |
| `POST` | `/api/method/pet_app.api.mobile.orders.place_order` | Create a Sales Order through the guarded order API |
| `POST` | `/api/method/pet_app.api.mobile.orders.cancel_order` | Move a cancellable Sales Order to `Cancelled` |
| `POST` | `/api/method/pet_app.api.mobile.orders.reorder` | Return previous Sales Order items for cart reuse |

## Cart And Payment

The mobile cart is owned by the Flutter app. The backend receives the cart as an `items` payload:

```json
{
  "items": [
    { "item_code": "ITEM-001", "qty": 2 }
  ]
}
```

`quote` checks that payload against ERPNext Item, Item Price, and stock in `Stores - K`, then returns current totals and any cart issues. It does not create a Sales Order.

`place_order` turns the same cart payload into an ERPNext `Sales Order` by calling the existing guarded order API. Mobile checkout is currently cash-only. The mobile wrapper accepts `cash`, `cod`, or `Cash on Delivery`, and rejects other payment methods before reaching Sales Order creation.

## Statuses

The mobile API returns the existing backend statuses:

```text
Draft
Preparing
Out for Delivery
Returned
Cash Collected
Completed
Cancelled
```

`cancel_order` only allows transitions already allowed by `pet_app.api.order.ALLOWED_TRANSITIONS`.

## Delivery Coordinates

The order creation path now writes delivery coordinates to whichever Sales Order custom fields exist:

- preferred fixture names: `custom_delivery_latitude`, `custom_delivery_longitude`
- legacy names: `custom_delivery_lat`, `custom_delivery_lng`

The mobile order DTO reads both aliases.
