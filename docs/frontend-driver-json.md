# Frontend Driver JSON

Backend authority: `pet_app.api.driver` (+ `pet_app.utils.driver_orders.guard_driver`).
This is the handoff for the **Drivers** screen: create, list, read, update, deactivate,
driver login, and driver cash balance.

Delivery/POS operations for an existing driver (dispatch, van load, settlement) are a
different contract: `docs/DRIVER_ORDERS_CONTRACT.md`.

---

## 1. Read this first — why "add driver" fails today

`Driver.custom_cash_account` is **required and never derived**. The backend used to
auto-create a cash ledger under a hardcoded parent; that path was removed. If the
frontend posts a driver without `custom_cash_account`, the request is rejected with
`Cash Account is required for a Driver.`

Second trap: the response envelope. Every method here is wrapped in
`@standardize_response`, which **catches the exception and still returns HTTP 200**.
A frontend that only checks `res.status === 200` sees success and renders nothing.
You must check `message.ok`.

```js
const r = await frappe.call({ method: "...", args: {...} });
if (!r.message?.ok) {
  showError(r.message.errors[0].message);   // real reason lives here
  return;
}
const driver = r.message.data;
```

---

## 2. Response envelope

Every `pet_app.api.driver` method returns this inside Frappe's `message` key:

```json
{
  "message": {
    "ok": true,
    "data": { "...": "method payload" },
    "meta": {},
    "errors": []
  }
}
```

Failure:

```json
{
  "message": {
    "ok": false,
    "data": {},
    "meta": { "code": "ValidationError" },
    "errors": [
      { "message": "Cash Account is required for a Driver.", "details": "<traceback>" }
    ]
  }
}
```

- `meta.code` is the Python exception class (`ValidationError`, `PermissionError`,
  `AuthenticationError`, `LinkValidationError`, …). Use it for branching, use
  `errors[0].message` for display.
- `errors[0].details` is a server traceback. Never show it to a user; log it.
- For backwards compatibility the success payload keys are **also copied to the top
  level** of `message` (`message.name`, `message.full_name`, …). Read them from
  `message.data`; the flattened copies are legacy.
- The same error text may also arrive in Frappe's `_server_messages`, so the Desk
  client may pop its own dialog. That is expected, not a second failure.

---

## 3. Create a driver

`POST /api/method/pet_app.api.driver.create_driver`

Caller needs `create` permission on `Driver` (`Pet App Admin`, `HR User`,
`HR Manager`, `Settings Create`, or System Manager). Otherwise: `Not authorized`,
`meta.code = "PermissionError"`.

### Request JSON

```json
{
  "full_name": "علي حسن",
  "phone": "07701234567",
  "custom_cash_account": "Driver Cash Ali - K",
  "custom_username": "ali",
  "custom_email": "ali@example.com",
  "status": "Active",
  "license_number": "IQ-882211",
  "license_expiry": "2028-04-30",
  "address_line1": "شارع الرشيد",
  "address_line2": "",
  "city": "بغداد",
  "country": "Iraq"
}
```

| Field | Required | Rules |
|---|---|---|
| `full_name` | **yes** | Non-empty after trim. Split on whitespace into User first/last name. |
| `phone` | **yes** | Iraqi mobile only: `07XXXXXXXXX` or `9647XXXXXXXXX` (stored as `07…`). Spaces and dashes are stripped. Anything else → `Invalid phone. Use: 07XXXXXXXXX or 9647XXXXXXXXX`. Stored in `cell_number`. |
| `custom_cash_account` | **yes** | See §4. Enabled, non-group, `root_type = Asset`, `account_type = Cash`, company `Kokh-vet`, **not already used by another driver**. |
| `custom_username` | no (send it) | Defaults to the first word of `full_name`. Lowercased; every character that is not alphanumeric / `.` / `_` / `-` becomes `_`. Must be unique across `Driver` **and** `User`. |
| `custom_email` | no | Contact email only, validated if present. Alias `email` accepted. **This is not the login.** |
| `status` | no | `Active` (default) or `Left`. Drives `User.enabled`. |
| `license_number` | no | Free text. |
| `license_expiry` | no | `YYYY-MM-DD` → stored in `expiry_date`. |
| `address_line1` / `address_line2` / `city` / `country` | no | An Address is created **only if** one of these is sent (or `country` differs from `Iraq`). Otherwise `data.address` is `null`. |

Send `custom_username` explicitly. The default takes the first word of an Arabic name
verbatim, which is legal but produces usernames staff cannot type.

### Success payload

```json
{
  "ok": true,
  "data": {
    "name": "HR-DRI-2026-00022",
    "full_name": "علي حسن",
    "status": "Active",
    "custom_cash_account": "Driver Cash Ali - K",
    "cell_number": "07701234567",
    "address": "علي حسن-Personal",
    "user": "hr-dri-2026-00022@petapp.com",
    "custom_username": "ali",
    "custom_email": "ali@example.com",
    "license_number": "IQ-882211",
    "expiry_date": "2028-04-30",
    "temporary_password": "f3Kq9xUa2LmZ0pRb"
  },
  "meta": {},
  "errors": []
}
```

What the backend did on top of inserting the row:

1. Created the login `User` `<driver-id>@petapp.com` with the `Driver` role,
   `enabled = (status == "Active")`, and a random password.
2. Created the `Address` when address fields were sent, linked to the Driver only.
3. Created the driver stock warehouse `Driver <driver-id>` under
   `مخزون في الطريق - K` (the driver-orders warehouse parent is configured on this site),
   and wrote it to `custom_warehouse`.

> **`temporary_password` is returned exactly once and is never stored or logged.**
> Show it in the success dialog with a copy button, and require the operator to
> acknowledge before closing. There is no "resend password" endpoint — if it is lost,
> an administrator must reset the User in Desk.

The driver signs in with **phone + this password**, not with the email.

### Errors you must render

| `errors[0].message` | Cause / fix |
|---|---|
| `Driver full name is required` | Empty `full_name`. |
| `Invalid phone. Use: 07XXXXXXXXX or 9647XXXXXXXXX` | Bad `phone`. |
| `Driver username is required` | `custom_username` normalized to empty (e.g. only symbols). |
| `Driver username X is already used by HR-DRI-…` | Pick another username. |
| `Username X is already used by User u@…` | Username collides with a non-driver User. |
| `Cash Account is required for a Driver.` | `custom_cash_account` missing — the #1 cause of "add driver doesn't work". |
| `Cash Account X does not exist.` | Bad link value. Send the account **`name`**, not its label. |
| `Cash Account X is disabled.` / `… must be a ledger account, not a group.` | Pick an enabled leaf account. |
| `A driver's Cash Account must be an Asset ledger of type Cash.` | Wrong account type. |
| `Each driver must have a distinct Cash Account.` | Already assigned to another driver. Filter these out in the picker (§4). |
| `Cash Account X belongs to Company Y, not Kokh-vet.` | Wrong company. |
| `The Driver role does not exist…` | Backend not migrated. Not a frontend bug — report it. |
| `Not authorized` (`meta.code = "PermissionError"`) | Caller lacks `Driver` create permission. |

---

## 4. The cash account picker (build this before the form)

Each driver needs their **own** cash ledger. On the current site the only free
Asset/Cash ledgers are the two POS tills and petty cash — **do not assign those to a
driver**; that would mix a driver's float with a cashier's till.

The normal flow is: an accountant creates `Driver Cash <name>` under
`531000 - الصندوق الرئيسي - K` (Company `Kokh-vet`, `account_type = Cash`,
`root_type = Asset`, currency IQD), then the driver form picks it.

Populate the dropdown with the accounts that are legal **and** unassigned:

```js
// 1. candidate ledgers
const accounts = await frappe.call({
  method: "frappe.client.get_list",
  args: {
    doctype: "Account",
    filters: {
      company: "Kokh-vet",
      account_type: "Cash",
      root_type: "Asset",
      is_group: 0,
      disabled: 0
    },
    fields: ["name", "account_name", "account_currency"],
    limit_page_length: 0
  }
});

// 2. already-taken ledgers
const taken = await frappe.call({
  method: "frappe.client.get_list",
  args: { doctype: "Driver", fields: ["custom_cash_account"], limit_page_length: 0 }
});

const takenSet = new Set(taken.message.map(d => d.custom_cash_account).filter(Boolean));
const options = accounts.message.filter(a => !takenSet.has(a.name));
```

Notes:

- `frappe.client.get_list` returns a **plain array** in `message` — it is not wrapped
  in the `ok/data` envelope. Only `pet_app.api.*` methods use the envelope.
- The caller needs `Account` read permission; a user without it gets an empty list, not
  an error. If the dropdown is empty, check permissions before assuming there is no data.
- Account currency must be the company currency (IQD). `create_driver` does not check
  currency, but every later save of the driver does and will then refuse the record.
- Send `a.name` as `custom_cash_account`; show `a.account_name` to the user.

---

## 5. List and read drivers

There is no custom list endpoint. Use the standard resource API.

```
GET /api/method/frappe.client.get_list
  ?doctype=Driver
  &fields=["name","full_name","cell_number","status","custom_username","custom_email","custom_cash_account","custom_warehouse","user","address","license_number","expiry_date"]
  &limit_page_length=0
  &order_by=modified desc
```

Row shape:

```json
{
  "name": "HR-DRI-2026-00021",
  "full_name": "اوراس",
  "cell_number": "07716940612",
  "status": "Active",
  "custom_username": "اوراس",
  "custom_email": null,
  "custom_cash_account": "Driver Cash HR-DRI-2026-00021 - K",
  "custom_warehouse": "Driver HR-DRI-2026-00021 - K",
  "user": "hr-dri-2026-00021@petapp.com",
  "address": null,
  "license_number": null,
  "expiry_date": null
}
```

Single driver: `GET /api/resource/Driver/HR-DRI-2026-00021`.

`name` (e.g. `HR-DRI-2026-00021`) is the driver id used by every other driver API.
Never key your state on `full_name` or `custom_username`.

---

## 6. Update a driver

There is no `update_driver` method. Save the `Driver` doctype directly; the backend
hooks (`before_save` / `on_update`) re-run the same validation and re-sync the User and
Address.

```js
await frappe.call({
  method: "frappe.client.set_value",
  args: {
    doctype: "Driver",
    name: "HR-DRI-2026-00021",
    fieldname: { full_name: "اوراس محمد", status: "Left", custom_username: "auras" }
  }
});
```

Rules on update:

- `user` is managed by the backend. Sending a different value fails with
  `Driver user is managed automatically and cannot be edited manually`.
- Setting `status` to `Left` disables the login User; back to `Active` re-enables it.
- Changing `custom_cash_account` or `custom_warehouse` fails with
  `Settle the driver's stock and cash before changing custody accounts` while the old
  warehouse holds stock or the old account has a non-zero balance.
- Editing address fields through `frappe.client.set_value` on `Driver` does **not**
  touch the Address; `on_update` only re-syncs title/phone/email onto the existing
  Address. Edit street/city on the `Address` document itself.
- These calls return the raw Frappe response, **not** the `ok/data` envelope. Failures
  arrive as a real HTTP 417 with `_server_messages`.

---

## 7. Delete / deactivate

`POST /api/method/pet_app.api.driver.delete_driver` — `{"driver_id": "HR-DRI-2026-00021"}`

Requires `delete` permission on `Driver`. The backend decides between soft and hard
delete; the frontend must render both outcomes.

Hard delete (no order history, no GL history) — Driver, User and Address are removed;
the cash account is deliberately kept:

```json
{ "ok": true, "data": { "driver": "HR-DRI-2026-00021", "action": "deleted" }, "meta": {}, "errors": [] }
```

Soft delete (any history) — status becomes `Left` and the login is disabled:

```json
{
  "ok": true,
  "data": {
    "driver": "HR-DRI-2026-00021",
    "action": "disabled",
    "status": "Left",
    "user": "hr-dri-2026-00021@petapp.com"
  },
  "meta": {}, "errors": []
}
```

Branch on `data.action` — say "Driver deactivated" for `disabled`, not "deleted".

Refusals:

- `Cannot delete driver with active orders` — an order is Preparing / Out for Delivery /
  Returned / Cash Collected.
- `Driver still holds stock or cash and cannot be deleted.`
- `Deactivate a driver with posted history instead of deleting it.`

---

## 8. Driver login (driver app)

`POST /api/method/pet_app.api.driver.driver_login` — guest-allowed, rate limited to
**20 requests / 60 seconds** per IP.

```json
{ "phone": "07716940612", "password": "f3Kq9xUa2LmZ0pRb" }
```

```json
{
  "ok": true,
  "data": {
    "status": "success",
    "token": "token 3a1f…:9c2b…",
    "driver_id": "HR-DRI-2026-00021",
    "full_name": "اوراس",
    "username": "اوراس",
    "cash_account": "Driver Cash HR-DRI-2026-00021 - K"
  },
  "meta": {}, "errors": []
}
```

Send `data.token` verbatim as the `Authorization` header on every later request:

```
Authorization: token 3a1f…:9c2b…
```

Every failure — unknown phone, wrong password, `Left` status, un-provisioned User —
returns the same message, `Invalid phone or password`, with
`meta.code = "AuthenticationError"`. That is deliberate; do not try to distinguish
them in the UI. Over the rate limit, the request is rejected by Frappe with HTTP 429
before it reaches this method, so handle 429 separately.

---

## 9. Driver cash balance

`GET /api/method/pet_app.api.driver.get_driver_balance?driver_id=HR-DRI-2026-00021`

```json
{
  "ok": true,
  "data": {
    "driver_id": "HR-DRI-2026-00021",
    "cash_account": "Driver Cash HR-DRI-2026-00021 - K",
    "balance": 125000
  },
  "meta": {}, "errors": []
}
```

- `balance` is `debit − credit` over non-cancelled GL entries on the driver's cash
  account, in IQD. Positive means the driver is holding the shop's cash.
- A driver may read **their own** balance; anyone else needs `Driver` read permission,
  otherwise `Not authorized` / `PermissionError`.
- A driver with no cash account returns `{"balance": 0}` only — no `driver_id` or
  `cash_account` keys. Read defensively.

---

## 10. Minimal add-driver flow

1. Load the cash-account options (§4). If empty, block the form and tell the operator
   an accountant must create a driver cash ledger first — do not let them submit.
2. Collect `full_name`, `phone`, `custom_username`, cash account; optional email,
   license, address.
3. Validate the phone client-side against `^(07\d{9}|9647\d{9})$` before posting.
4. POST `create_driver`.
5. Check `message.ok`. On `false`, show `message.errors[0].message` on the offending
   field.
6. On success, show `data.temporary_password` once, with a copy button and an explicit
   acknowledgement, then refresh the list.
