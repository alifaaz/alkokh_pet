# Delivery partners: "Bill the App Customer" (`is_inside`)

A delivery partner can now be set to **bill the real customer** instead of its own account.
The partner still collects the money at its end, keeps its commission and settles with us
later — exactly like Talabat — but the invoice sits on the account of the customer who
actually ordered.

> ⚠️ **The current build cannot sell for such a partner.** When a partner is attached, the
> till swaps the customer to `partner.customer` and locks the picker. For an inside partner
> the server now refuses exactly that customer (`PARTNER_INSIDE_NEEDS_REAL_CUSTOMER`).
> Ordinary partners (`is_inside = 0`) are completely unaffected. **Nobody should tick the box
> on a partner until this ships.**

Backend contract: `docs/backend/delivery-partners.md` §7a. Related, already handed off:
`docs/frontend-delivery-partner-commission-type.md` (percentage or flat fee).

---

## 1. What changed on the backend

| Where | Field | Meaning |
| --- | --- | --- |
| `Delivery Partner` | `is_inside` (Check) | Label "Bill the App Customer". Default `0`. |
| `Sales Invoice` | `custom_partner_is_inside` (Check, read-only) | Snapshot taken at sale time. `1` = this order is on a real customer's account but the partner holds the money. |
| `Delivery Partner Settlement` | `journal_entry` (Link, read-only) | Set **instead of** `payment_entry` when a settlement covers inside orders. |

**`create_pos_sale`** — request unchanged. For an inside partner:

- `customer` is the real customer the cashier picked. **Not** `partner.customer`.
- Still `payment_status: "Due"`, still no payment, still `partner_order_ref` required,
  commission still stamped. Everything about the partner group is the same.
- New refusal, rendered like every other `PARTNER_*` code (the block beside Process
  Transaction):

| Code | When |
| --- | --- |
| `PARTNER_INSIDE_NEEDS_REAL_CUSTOMER` | the customer is any delivery partner's own account, **or** the POS Profile's walk-in default |

**Customer payments** — until the partner settles, an inside order looks like an ordinary
debt on the customer's balance. It is not one: the partner already took the money. Any
attempt to collect it is refused:

| Code | Where |
| --- | --- |
| `PARTNER_INVOICE_COLLECTED_BY_PARTNER` | `accounting.cashier.create_payment_entry_with_cashier_context` and the driver-order payment, when a referenced invoice has `custom_partner_is_inside = 1` and no `custom_partner_settlement` |

**`get_partner_summary`** — gains `is_inside`.
**`create_settlement`** — result gains `journal_entry`; for an inside settlement
`payment_entry` is `null`. No request change.

---

## 2. `src/type/deliveryPartner/index.ts`

```ts
export interface DeliveryPartner {
  // …
  /**
   * "Bill the App Customer". The order goes on the REAL customer's account; the partner
   * still collects the money and settles with us. Absent on older records = false.
   */
  is_inside?: boolean | number | null;
}

export interface AddEditDeliveryPartner {
  // …
  is_inside: boolean;            // emptyAddEditDeliveryPartner(): false
}

/** The one place the till asks "do I bill the partner, or the customer?". */
export function billsAppCustomer(partner: Pick<DeliveryPartner, "is_inside"> | null | undefined): boolean {
  return Boolean(Number(partner?.is_inside ?? 0));
}
```

On `DeliveryPartnerSettlement`:

```ts
  /** Set INSTEAD of `paymentEntry` when the settlement covered orders billed to app customers. */
  journalEntry?: string | null;
```

---

## 3. `src/api/deliveryPartnerApi.ts`

- `PARTNER_FIELDS` → add `"is_inside"`.
- `mapPartnerRow` → `is_inside: toBool(row?.is_inside),`
- `buildPartnerDoc` → `is_inside: partner.is_inside ? 1 : 0,`
- `SETTLEMENT_FIELDS` → add `"journal_entry"`.
- Settlement row mapper (around the `paymentEntry: nullableText(row?.payment_entry)` line,
  and the same in the `create_settlement` result mapper):

```ts
    paymentEntry: nullableText(row?.payment_entry),
    journalEntry: nullableText(row?.journal_entry),
```

> ⚠️ **Deploy the backend first.** `is_inside` and `journal_entry` do not exist until the site
> has migrated, and Frappe fails the **entire** list query on one unknown field — the partner
> list and the settlements tab would both go blank. Same trap as before; same fix: backend
> first.

---

## 4. `src/pages/pos/sell.vue` — the actual change

Three places. All keyed on `billsAppCustomer(activePartner.value)`.

### 4.1 `attachPartner` — do not swap the customer

Today it replaces the customer with the partner's account and returns early if the partner
has none. For an inside partner, **keep whoever the cashier already picked**:

```ts
async function attachPartner(partner: DeliveryPartner) {
  // ⚠️ AN INSIDE PARTNER BILLS THE CUSTOMER THE CASHIER PICKED. Swapping to the partner's
  // own account here is exactly what the server refuses for one
  // (PARTNER_INSIDE_NEEDS_REAL_CUSTOMER) - so the customer is left alone, and the picker
  // stays open for the cashier to choose the person who ordered.
  if (!billsAppCustomer(partner)) {
    const customerId = String(partner.customer ?? "").trim();

    if (!customerId) {
      checkoutBlockReason.value = t("alkokh.pos.partner.noCustomerLink");
      sidePanel.value = null;

      return;
    }

    customerOption.value = {
      id: customerId,
      label: partner.partner_name || customerId,
      subtitle: customerId,
      image: null,
      raw: {},
    };

    await nextTick();
  }
  else if (isWalkInCustomer.value) {
    // The till reseeds the walk-in default on every cleared sale, so it is almost always
    // what is selected when the partner is tapped. Clear it rather than let the cashier
    // sell onto it and meet the refusal at Process Transaction.
    customerOption.value = null;
    await nextTick();
  }

  activePartner.value = partner;

  // … the rest is unchanged: Due, no payment mode, paid 0, clear the block reason …
}
```

`isWalkInCustomer` is a new computed:

```ts
const isWalkInCustomer = computed(() => {
  const walkIn = String(cashierStore.defaultCustomer ?? "").trim();

  return Boolean(walkIn) && customerOption.value?.id === walkIn;
});
```

> ⚠️ **Keep the `await nextTick()` ordering** for the ordinary branch — the existing
> comment on `attachPartner` explains why: the customer watcher's reset would otherwise wipe
> the partner state written after it.

### 4.2 `detachPartner` — do not clear a real customer

It currently sets `customerOption.value = null` and reseeds the walk-in. That is right for an
ordinary partner (the selected "customer" was the partner's account) and **wrong** for an
inside one — the selected customer is a real person the cashier chose, and the cart may well
be continuing as a normal sale for them:

```ts
function detachPartner() {
  const wasInside = billsAppCustomer(activePartner.value);

  activePartner.value = null;
  if (!wasInside)
    customerOption.value = null;
  // … rest unchanged; applyDefaultCustomer() already no-ops when a customer is selected …
}
```

Apply the same rule wherever else a partner is dropped along with the customer (the
`activePartner.value = null` block near the clear-sale path around line 1320 clears the whole
sale, so it can stay as it is).

### 4.3 The customer picker lock

```vue
<PosCustomerSection
  …
  :locked="isPartnerOrder && !billsAppCustomer(activePartner)"
```

The payment section's `:locked="isPartnerOrder"` **stays exactly as it is** — an inside
order is still `Due` and still takes no money at the till.

### 4.4 Block before checkout, not after

The existing watcher already blocks a sale with no customer
(`customerBlockedCheckout`). Add the walk-in case for an inside partner beside it, so the
cashier sees the reason while building the cart rather than as a refusal after Process
Transaction:

```ts
watch(
  [activePartner, () => customerOption.value?.id ?? null],
  ([partner]) => {
    if (slotSwapping.value)
      return;

    const blocked = billsAppCustomer(partner as DeliveryPartner | null) && isWalkInCustomer.value;
    const reason = t("alkokh.pos.partner.insideNeedsCustomer");

    if (blocked)
      checkoutBlockReason.value = reason;
    else if (checkoutBlockReason.value === reason)
      checkoutBlockReason.value = null;
  },
);
```

Declare it **after** the blanket "clear on any edit" watcher, for the reason the existing
customer watcher documents.

### 4.5 The payload

No change. `customer` is already `customerOption.value.id`; for an inside partner that is now
the real customer, which is exactly what the server expects.

---

## 5. `src/components/pos/PosPartnerOrderBar.vue`

Hide the "end customer name" field for an inside partner — the customer is the real one on
the invoice, so a free-text name beside it is a second answer to the same question:

```vue
<AppTextField
  v-if="!billsAppCustomer(props.partner)"
  :model-value="props.endCustomerName"
  …
/>
```

And add a one-line hint under the partner name so the cashier knows why the customer picker
is open:

```vue
<div v-if="billsAppCustomer(props.partner)" class="pos-partner-bar__hint">
  {{ t("alkokh.pos.partner.billsCustomerHint") }}
</div>
```

In `sell.vue`, clear `form.partnerCustomerName` when attaching an inside partner so a name
typed for a previous partner is not sent along.

---

## 6. `src/components/delivery-partner/AddEditDeliveryPartnerDialog.vue`

One switch, beside Active:

```vue
<VCol cols="12" md="6">
  <VSwitch
    v-model="partner.is_inside"
    color="primary"
    :label="t('alkokh.deliveryPartners.fields.isInside')"
    :hint="t('alkokh.deliveryPartners.fields.isInsideHint')"
    persistent-hint
  />
</VCol>
```

It does **not** need the settlement lock the commission has: the server snapshots the flag
onto every invoice and chooses the settlement document from who each invoice is actually
billed to, so flipping it only affects orders sold afterwards.

---

## 7. Settlements tab / settlement result

Show the Journal Entry when there is no Payment Entry:

```ts
const voucher = settlement.paymentEntry ?? settlement.journalEntry ?? null;
const voucherDoctype = settlement.paymentEntry ? "Payment Entry" : "Journal Entry";
```

Everything else about a settlement — the invoices, the tie-out check, the three money
fields — is identical for inside partners.

---

## 8. Customer balance and payment screens

An inside order shows as **debt on the customer's balance** until the partner settles. That
is correct accounting, but a cashier reading it will naturally try to collect it.

- The server refuses (`PARTNER_INVOICE_COLLECTED_BY_PARTNER`); render its message verbatim —
  it names the partner and says the customer would pay twice.
- **Better:** wherever outstanding invoices are listed for payment (the customer section's
  "Receive money" dialog, the customer statement), mark rows with
  `custom_partner_is_inside = 1` and an empty `custom_partner_settlement` as
  **"Collected by {partner}"** and make them unselectable. Request both fields from the
  module's own query with its existing retry, never from the shared
  `SALES_INVOICE_FIELDS` — the same rule as the other partner columns.
- An **unallocated** "on account" payment from the customer is not caught by the guard
  (there is no invoice reference to check). If that dialog can take one, a short warning
  when the customer's only debt is partner-collected is worth adding.

---

## 9. Strings (en / ar)

```
alkokh.deliveryPartners.fields.isInside
  en: "Bill the app customer"
  ar: "الفوترة على زبون التطبيق"

alkokh.deliveryPartners.fields.isInsideHint
  en: "The order goes on the customer's account; the partner still collects the money and settles with us."
  ar: "يُسجَّل الطلب على حساب الزبون، والشركة تستلم المبلغ وتسدده لنا في التسوية."

alkokh.pos.partner.billsCustomerHint
  en: "Billed to the customer — select who ordered."
  ar: "يُفوتر على الزبون — اختر صاحب الطلب."

alkokh.pos.partner.insideNeedsCustomer
  en: "Select the customer who ordered. This partner's orders cannot go on the walk-in customer."
  ar: "اختر الزبون صاحب الطلب. لا يمكن تسجيل طلبات هذه الشركة على الزبون النقدي."

alkokh.pos.partner.collectedByPartner
  en: "Collected by {partner}"
  ar: "تستلمه {partner}"
```

---

## 10. How to check it

1. **An ordinary partner is untouched.** Attach Talabat: the customer switches to Talabat
   and locks, the sale goes through, the settlement shows a Payment Entry.
2. **Inside partner, real customer.** Tick "Bill the app customer" on a test partner. Pick a
   real customer, attach the partner: the customer stays, the picker stays open, payment is
   locked to Due, the end-customer-name field is gone. The sale goes through and the invoice
   is on that customer.
3. **Walk-in is blocked.** With the walk-in default selected, attach the inside partner:
   the picker is cleared (or the block reason shows). It must not reach the server.
4. **Detach keeps the customer.** Detach the inside partner: the real customer is still
   selected and the cart is intact.
5. **No collecting twice.** Open that customer's balance: the order shows as debt, marked
   "Collected by {partner}". Trying to take payment for it is refused with the server's
   message.
6. **Settlement.** Settle two inside orders for two different customers: one settlement,
   `journal_entry` set and linkable, `payment_entry` empty, both invoices closed, both
   customers' balances back to what they were.
7. **Tabs.** Park a sale with an inside partner and a real customer, switch tabs, come back:
   partner, customer and order ref all restored.
