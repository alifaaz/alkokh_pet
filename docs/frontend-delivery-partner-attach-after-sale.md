# Delivery partners: attach an existing Due sale to a partner

Until now a delivery partner could only be picked **while** a sale was rung up
(`create_pos_sale` with the partner group). If the cashier had already saved the sale as
**Due** and only then learned it was an app order, for example a Hamody order, there was no
way to link it. The invoice never reached the partner page or a settlement, and the till
would still collect it, so the customer paid twice.

A new endpoint stamps an existing, fully unpaid invoice as a partner order, exactly as if the
partner had been picked at the till. Nothing is posted to the ledger. The invoice then
behaves like every other partner order:
- it shows on the partner's page and in `get_partner_summary`
- it is picked up by `create_settlement`
- for an inside partner, the till refuses to collect it (`PARTNER_INVOICE_COLLECTED_BY_PARTNER`)

Related handoffs: `docs/frontend-delivery-partner-bill-app-customer.md` (inside partners),
`docs/frontend-delivery-partner-commission-type.md` (percentage or flat fee).

> Desk already has this: a **"Attach to Delivery Partner"** button on the Sales Invoice form
> (`pet_app/public/js/sales_invoice.js`). This doc is for adding the same action to the Vue app.

---

## 1. Endpoint

`POST /api/method/pet_app.api.delivery_partners.attach_invoice_to_partner`

| Arg | Required | Notes |
| --- | --- | --- |
| `sales_invoice` | yes | The submitted Sales Invoice name. |
| `delivery_partner` | yes | `Delivery Partner.name`. Offer active partners only (`is_active = 1`). |
| `partner_order_ref` | yes | The app's order number. Same rule as at the till. |
| `partner_customer_name` | no | The customer's name as shown in the app. |
| `partner_commission_rate` | no | Percentage partners only. Empty = the partner's rate. Ignored for flat-fee partners. |
| `partner_commission_amount` | no | Flat-fee partners only. Empty = the partner's fee. |

The permission is the same as ringing up a sale (`POS_SETTLE_NOT_PERMITTED` otherwise), and the
user must be able to read the invoice, so branch permissions apply.

### Success

Standard envelope, `ok: true`. The `data` keys are also mirrored at the top level (legacy):

```json
{
  "ok": true,
  "data": {
    "invoice": "ACC-SINV-2026-02844",
    "customer": "ايمن رضوان وعد",
    "delivery_partner": "Hamody",
    "partner_label": "Hamody",
    "partner_order_ref": "H-1234",
    "is_inside": true,
    "commission_type": "Amount",
    "commission_amount": 10000.0,
    "outstanding_amount": 35000.0
  }
}
```

### Refusals

HTTP 200, `ok: false`. Read the code from `meta.code` and show `errors[0].message` (it is
cashier-readable). Ignore `errors[0].details`: it is a traceback.

| `meta.code` | When | Suggested UI |
| --- | --- | --- |
| `INVOICE_NOT_FOUND` | bad name | toast |
| `INVOICE_NOT_PERMITTED` | the user can't read that invoice (other branch) | toast |
| `INVOICE_NOT_SUBMITTED` | draft, cancelled or a return | hide the action for these (see §2) |
| `PARTNER_ALREADY_ATTACHED` | the invoice already has `custom_delivery_partner` | hide the action; refresh if it slipped through |
| `PARTNER_INVOICE_ALREADY_PAID` | anything was paid against it: `is_pos`, a payment, a credit note, or nothing outstanding | show the message; the sale can't be given to a partner |
| `PARTNER_NOT_FOUND` / `PARTNER_INACTIVE` | bad or switched-off partner | inline under the partner field |
| `PARTNER_INSIDE_NEEDS_REAL_CUSTOMER` | inside partner, and the invoice is on a walk-in or partner account | show the message; the invoice's customer must be changed first (not possible after submit, so it's cancel and re-sell) |
| `PARTNER_CUSTOMER_MISMATCH` | an **ordinary** partner (Talabat, Toters…), whose orders must be billed to the partner's own customer | inline under the partner field. In practice only **inside** partners (`is_inside = 1`) can take an existing customer's invoice, so filter the picker to them (see §2) |
| `PARTNER_ORDER_REF_REQUIRED` | empty order number | inline under the field |
| `PARTNER_INVALID_COMMISSION_RATE` / `PARTNER_INVALID_COMMISSION_AMOUNT` | bad override or bad partner record | inline under the commission field |
| `PARTNER_COMMISSION_EXCEEDS_TOTAL` | a flat fee ≥ the invoice's amount due (Hamody = 10,000, so any invoice ≤ 10,000 is refused) | show the message |

The call is all-or-nothing: on any refusal nothing is written.

---

## 2. When to show the action

Show **"Attach to Delivery Partner"** only on an invoice where **all** of these hold:

```ts
invoice.docstatus === 1 &&
!invoice.is_return &&
!invoice.is_pos &&
!invoice.custom_delivery_partner &&
invoice.outstanding_amount > 0
```

The server also refuses partly paid invoices, but that can't be told from these fields alone,
so let the server answer (`PARTNER_INVOICE_ALREADY_PAID`).

Good places to put the action:
- the customer's **open / Due invoices** list (row action)
- the invoice detail panel
- the POS open-invoices panel in `pages/pos/sell.vue`

**Partner picker:** list `Delivery Partner` with `is_active = 1` **and** `is_inside = 1`. An
ordinary partner can never pass `PARTNER_CUSTOMER_MISMATCH` here, because an existing sale is
billed to a real customer, never to the partner's own account.

---

## 3. Dialog

Fields:
1. **Delivery Partner** (required): the picker described in §2.
2. **Partner order number** (required).
3. **Customer name in the app** (optional).
4. **Commission** (optional, collapsed by default): a rate for percentage partners, an amount for flat-fee ones.
   Read `commission_type` off the picked partner to decide which to show.

Show this note in the dialog:

> The partner collects this money and pays it in their settlement. Do not take payment for
> this invoice at the till.

No idempotency key is needed: a second attach is refused with `PARTNER_ALREADY_ATTACHED`,
so a double click is harmless. Still disable the button while the call is in flight. On success:
- toast "Attached to {partner_label}"
- refresh the invoice / list, so the row now shows the partner badge and loses its **Collect / Pay** actions
- invalidate the partner summary cache for that partner, if you keep one

---

## 4. After attaching

Nothing new to build. Existing screens pick it up automatically:
- **Partner detail page** (`pages/delivery-partners/[id].vue`): the invoice appears in the
  orders table as unsettled, and `RecordSettlementDialog` can settle it.
- **Receipt / invoice view:** `partner_label` and `partner_order_ref` are read off the invoice,
  so reprints show the partner.
- **Customer payment:** `create_payment_entry_with_cashier_context` now refuses it with
  `PARTNER_INVOICE_COLLECTED_BY_PARTNER`. It is worth hiding the Pay button for any invoice where
  `custom_partner_is_inside = 1` and `custom_partner_settlement` is empty.

---

## 5. Type additions

```ts
// src/api/deliveryPartnerApi.ts
export interface AttachInvoiceToPartnerArgs {
  sales_invoice: string
  delivery_partner: string
  partner_order_ref: string
  partner_customer_name?: string
  partner_commission_rate?: number
  partner_commission_amount?: number
}

export interface AttachInvoiceToPartnerResult {
  invoice: string
  customer: string
  delivery_partner: string
  partner_label: string
  partner_order_ref: string
  is_inside: boolean
  commission_type: 'Percentage' | 'Amount'
  commission_amount: number
  outstanding_amount: number
}

export const AttachInvoiceToPartner = (args: AttachInvoiceToPartnerArgs) =>
  call<AttachInvoiceToPartnerResult>(
    'pet_app.api.delivery_partners.attach_invoice_to_partner',
    args,
  )
```

(Use whatever `call` / envelope helper `deliveryPartnerApi.ts` already uses for `create_settlement`.)

---

## 6. Test checklist

- [ ] Due invoice over 10,000 → attach to Hamody → ok, row shows the partner, Pay is hidden.
- [ ] Same invoice again → `PARTNER_ALREADY_ATTACHED`.
- [ ] Invoice of 10,000 or less → `PARTNER_COMMISSION_EXCEEDS_TOTAL`.
- [ ] Partly paid invoice → `PARTNER_INVOICE_ALREADY_PAID`.
- [ ] Empty order number → inline `PARTNER_ORDER_REF_REQUIRED`.
- [ ] Invoice from another branch → `INVOICE_NOT_PERMITTED`.
- [ ] Hamody's partner page lists the invoice as unsettled, and a settlement clears it.
