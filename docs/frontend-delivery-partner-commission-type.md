# Delivery partner commission: percentage **or** flat amount per order

The backend now supports a partner that keeps a **flat fee per order** instead of a
percentage — Talabat keeps 25% of every order, another aggregator keeps 2,000 from each one
whatever it is worth. Until now only a percentage could be expressed, so a flat-fee partner
had to be entered as an approximate rate that was wrong on every order but one.

**Nothing in the app breaks before this lands.** Every existing partner is `Percentage`, the
POS sale request is unchanged, and a flat-fee partner created from the Frappe desk already
sells and settles correctly — the app just shows `0%` on its tile and previews a commission
of 0 until the changes below are made.

Backend contract: `docs/backend/delivery-partners.md` (§1, §2, §3, §5, §6).

---

## 1. The backend contract, in short

**`Delivery Partner`** gains two fields beside `commission_rate`:

| Field | Type | Meaning |
| --- | --- | --- |
| `commission_type` | `"Percentage"` \| `"Amount"` | Default `Percentage`. Which figure this partner is charged on. |
| `commission_amount` | Currency | The flat fee kept from **each** order. Must be `> 0` when the type is `Amount`. |

The server **zeroes the figure that is not in use**, so after a save of an `Amount` partner
`commission_rate` reads back as `0`, and vice versa. Do not treat a `0` rate as unset.

**`Sales Invoice`** gains `custom_partner_commission_type`, snapshotted at sale time beside
the rate. It is **empty on every order that predates this work** — those are all
`Percentage`. `custom_partner_commission_rate` is `0` on a flat-fee order, and
`custom_partner_commission_amount` carries the fee.

**`create_pos_sale`** takes one more optional argument, `partner_commission_amount`, and
two new refusals:

| Code | When |
| --- | --- |
| `PARTNER_INVALID_COMMISSION_AMOUNT` | `Amount` partner whose fee is not `> 0` |
| `PARTNER_COMMISSION_EXCEEDS_TOTAL` | the fee is not less than the order's total — a 2,000 fee on a 1,500 order. The flat-fee counterpart of refusing a rate of 100 or more. |

Both arrive the same way every other partner refusal does, so the existing block beside
Process Transaction renders them with no change. **`PARTNER_COMMISSION_EXCEEDS_TOTAL` is
raised late**, after the total is known — the cashier can only have hit it by ringing up a
cart that is too small, so the message says to sell it another way.

⚠️ **The type is the partner's, never the till's.** The server ignores whatever
`partner_commission_rate` a flat-fee sale sends, rather than refusing it — which is why the
current bundle keeps working. Do not add a type selector to the checkout.

**`get_partner_summary`** returns two more keys: `commission_type`, and
`partner_commission_amount` — the partner's fee per order. It is **not** `commission_amount`,
which is still this period's commission total.

---

## 2. `src/type/deliveryPartner/index.ts`

```ts
/** Which figure a partner's commission is worked out on. */
export const COMMISSION_TYPES = ["Percentage", "Amount"] as const;
export type CommissionType = (typeof COMMISSION_TYPES)[number];
```

Add to `DeliveryPartner`:

```ts
  /**
   * Which figure this partner is charged on. Older records answer nothing; they are
   * `Percentage`, which is what they were — so read it through `partnerCommissionType`
   * rather than trusting the raw value.
   */
  commission_type?: CommissionType | string | null;
  /** The flat fee kept from EACH order, when the type is `Amount`. */
  commission_amount?: number | null;
```

Add the same two to `AddEditDeliveryPartner` (non-optional: `commission_type: CommissionType`
and `commission_amount: number`) and to `emptyAddEditDeliveryPartner()`:

```ts
    commission_type: "Percentage",
    commission_rate: 0,
    commission_amount: 0,
```

Two helpers, so no component has to repeat the empty-means-percentage rule:

```ts
/**
 * ⚠️ EMPTY MEANS PERCENTAGE. Partners and invoices created before flat fees existed carry
 * no type, and reading that as "neither" would show a partner with a real 25% rate as
 * having no commission at all.
 */
export function partnerCommissionType(
  source: { commission_type?: string | null } | { commissionType?: string | null } | null | undefined,
): CommissionType {
  const raw = String(
    (source as any)?.commission_type ?? (source as any)?.commissionType ?? "",
  ).trim();

  return raw === "Amount" ? "Amount" : "Percentage";
}

/**
 * What the partner keeps on a cart of `total`.
 *
 * ⚠️ PREVIEW ONLY — never subtracted from what the customer is charged. The server computes
 * the authoritative figure from the snapshot it stamps on the invoice.
 */
export function partnerCommissionOn(partner: DeliveryPartner | null | undefined, total: number): number {
  if (!partner)
    return 0;

  if (partnerCommissionType(partner) === "Amount")
    return Number(partner.commission_amount ?? 0);

  return Math.round(Number(total) * Number(partner.commission_rate ?? 0)) / 100;
}
```

`partnerCommissionLocked` does not change, but **what it locks does**: once a settlement
exists, lock the type and the amount as well as the rate (§6).

Add to `PartnerInvoiceRow`:

```ts
  /** The type this order was SOLD at, not the partner's current one. */
  commissionType?: CommissionType | null;
```

And to `PartnerSummary`:

```ts
  commissionType?: CommissionType | null;
  /** The partner's flat fee per order — NOT `commissionAmount`, which is the period total. */
  partnerCommissionAmount?: number | null;
```

---

## 3. `src/api/deliveryPartnerApi.ts`

**`PARTNER_FIELDS`** — add both columns:

```ts
  "commission_type",
  "commission_rate",
  "commission_amount",
```

**`PARTNER_INVOICE_CUSTOM_FIELDS`** — add `"custom_partner_commission_type"` after
`custom_partner_customer_name`.

> ⚠️ **Deploy the backend first.** This list is all-or-nothing: Frappe fails the entire
> query on one unknown column, and the existing retry then drops *all six* partner columns
> and caches "undeployed" for the session. Shipping this line against a site that has not
> migrated costs the orders tab its commission, order-ref and settled columns — not just the
> new one. The probe already handles it gracefully; it just degrades further than you want.

**`mapPartnerRow`** — carry both through:

```ts
    commission_type: partnerCommissionType(row),
    commission_rate: toNumber(row?.commission_rate),
    commission_amount: toNumber(row?.commission_amount),
```

**`buildPartnerDoc`** — send the type, and send **both** figures. Sending only the one in use
would leave the other holding whatever it held before, which the server zeroes anyway, so
sending both keeps the form and the record saying the same thing:

```ts
    commission_type: partner.commission_type || "Percentage",
    commission_rate: partner.commission_type === "Amount" ? 0 : toNumber(partner.commission_rate),
    commission_amount: partner.commission_type === "Amount" ? toNumber(partner.commission_amount) : 0,
```

**`mapInvoiceRow`** — a flat-fee order has a rate of `0` and a real amount, so the existing
"derive the amount from the rate" fallback must not be reached for it:

```ts
function mapInvoiceRow(row: any): PartnerInvoiceRow {
  const gross = toNumber(row?.grand_total);
  const type = partnerCommissionType({ commission_type: row?.custom_partner_commission_type });
  const rate = row?.custom_partner_commission_rate;
  const commission = row?.custom_partner_commission_amount;

  const commissionAmount = commission !== undefined && commission !== null
    ? toNumber(commission)
    // Only a percentage order can have its commission reconstructed from what is left:
    // a flat fee is not derivable from the gross, so it stays null and reads as unknown.
    : type === "Percentage" && rate !== undefined && rate !== null
      ? Math.round(gross * toNumber(rate)) / 100
      : null;

  return {
    // … unchanged …
    commissionType: type,
    commissionRate: type === "Amount" || rate === undefined || rate === null ? null : toNumber(rate),
    commissionAmount,
    // … unchanged …
  };
}
```

`commissionRate: null` on a flat-fee row is deliberate: the orders tab's rate column should
read "—" rather than "0%", which looks like a partner that takes nothing.

**`GetPartnerSummary`** — map the two new keys:

```ts
        commissionType: partnerCommissionType({ commission_type: data?.commission_type }),
        partnerCommissionAmount: toNumber(data?.partner_commission_amount),
```

The client-side fallback `summarisePartnerInvoices` needs no change — it sums
`commissionAmount`, which is already right for both types.

---

## 4. `src/type/pos.ts` and `src/api/posApi.ts`

`PosSalePayload`:

```ts
  partnerCommissionRate?: number | null;
  /**
   * The flat fee for THIS order, when the partner charges one. A snapshot, exactly like
   * the rate: it records what was agreed, it does not reduce the invoice.
   */
  partnerCommissionAmount?: number | null;
```

`buildPartnerFields`:

```ts
    partner_commission_rate: payload.partnerCommissionRate ?? undefined,
    partner_commission_amount: payload.partnerCommissionAmount ?? undefined,
```

Keep sending `partner_commission_rate` unconditionally. The server ignores it for a flat-fee
partner, and dropping it would be a behaviour change for percentage partners for no gain.

---

## 5. `src/pages/pos/sell.vue`

The preview, which is the only place the cart's commission is worked out:

```ts
const partnerCommissionAmount = computed(() =>
  partnerCommissionOn(activePartner.value, totalAmount.value));
```

`partnerNetAmount` is unchanged — `Math.max(total − commission, 0)` is already right, and the
clamp now also covers the case the server refuses outright (a fee larger than the cart).

The checkout payload gains one line:

```ts
          partnerCommissionRate: activePartner.value
            ? Number(activePartner.value.commission_rate ?? 0)
            : null,
          partnerCommissionAmount: activePartner.value
            ? Number(activePartner.value.commission_amount ?? 0)
            : null,
```

**Optional, and worth it:** a cart worth no more than the fee is refused by the server with
`PARTNER_COMMISSION_EXCEEDS_TOTAL`. That refusal is correct and readable, but it arrives
after the cashier has hit Process Transaction. Blocking earlier — the same way
`checkoutBlockReason` blocks other partner problems — turns it into something the cashier
sees while the cart is still being built:

```ts
// A flat-fee order must leave something behind: the server refuses one that does not, and
// finding that out at Process Transaction is a slower way to learn it.
const partnerFeeExceedsCart = computed(() =>
  Boolean(activePartner.value)
  && partnerCommissionType(activePartner.value) === "Amount"
  && totalAmount.value > 0
  && partnerCommissionAmount.value >= totalAmount.value);
```

---

## 6. `src/components/delivery-partner/AddEditDeliveryPartnerDialog.vue`

A type toggle, with the rate field or the amount field beneath it. Both are locked once the
partner has been settled against, for the reason the rate already is: a form that disagrees
with the figures a settled month was computed at invites someone to "correct" a settlement
that was right.

```ts
const isFlatFee = computed(() => partner.value.commission_type === "Amount");

const commissionTypeItems = computed(() => COMMISSION_TYPES.map(value => ({
  value,
  title: t(`alkokh.deliveryPartners.commissionType.${value.toLowerCase()}`),
})));

/**
 * ⚠️ 100 IS REFUSED, AND SO IS 0. (unchanged — see the existing comment)
 */
const commissionRateRules: Rule[] = [ /* the current `commissionRules`, unchanged */ ];

/**
 * ⚠️ A FLAT FEE OF 0 IS REFUSED for the same reason a 0% rate is: it is an unfilled field
 * far more often than it is a partner that takes nothing, and it would quietly produce
 * settlements that look correct. The upper bound is the ORDER's, not the partner's, so it
 * cannot be checked here — the till refuses a cart worth no more than the fee.
 */
const commissionAmountRules: Rule[] = [
  required,
  (value) => {
    const amount = Number(value);

    if (!Number.isFinite(amount))
      return t("alkokh.deliveryPartners.validation.commissionNumber");

    if (amount <= 0)
      return t("alkokh.deliveryPartners.validation.commissionAmountRange");

    return true;
  },
];
```

Template — replace the single commission column with:

```vue
      <VCol cols="12" md="6">
        <AppSelect
          v-model="partner.commission_type"
          :items="commissionTypeItems"
          item-title="title"
          item-value="value"
          :disabled="props.commissionLocked"
          :label="t('alkokh.deliveryPartners.fields.commissionType')"
          :hint="t('alkokh.deliveryPartners.fields.commissionTypeHint')"
          persistent-hint
        />
      </VCol>

      <VCol cols="12" md="6">
        <!--
          One column, two fields: only the figure in use is shown, because a form offering
          both invites a partner saved with a rate AND a fee, where only one of them is
          ever charged.
        -->
        <AppTextField
          v-if="isFlatFee"
          v-model.number="partner.commission_amount"
          type="number"
          min="0"
          :rules="commissionAmountRules"
          :disabled="props.commissionLocked"
          :label="t('alkokh.deliveryPartners.fields.commissionAmount')"
          :hint="t('alkokh.deliveryPartners.fields.commissionAmountHint')"
          persistent-hint
        />
        <AppTextField
          v-else
          v-model.number="partner.commission_rate"
          type="number"
          min="0"
          max="100"
          suffix="%"
          :rules="commissionRateRules"
          :disabled="props.commissionLocked"
          :label="t('alkokh.deliveryPartners.fields.commissionRate')"
          :hint="t('alkokh.deliveryPartners.fields.commissionRateHint')"
          persistent-hint
        />
      </VCol>
```

`v-if` / `v-else` rather than `:disabled`, so the hidden field's rules stop blocking the
form — a Vuetify rule on a rendered-but-unused field still fails validation, and a partner
switched to a flat fee would refuse to save because the rate is 0.

**The edit path must seed the new fields.** Wherever the list page builds the
`AddEditDeliveryPartner` from a row, carry `commission_type` (through
`partnerCommissionType`) and `commission_amount`, or opening an existing flat-fee partner
shows the rate form and saves it back as a percentage.

---

## 7. POS tiles: `PosPartnerPickerPanel.vue` and `PosPartnerOrderBar.vue`

Both render `t("alkokh.pos.partner.commissionRate", { rate: … })`. A flat-fee partner needs
the money instead:

```ts
function commissionLabel(partner: DeliveryPartner): string {
  if (partnerCommissionType(partner) === "Amount") {
    return t("alkokh.pos.partner.commissionFlat", {
      amount: props.formatAmount(Number(partner.commission_amount ?? 0)),
    });
  }

  return t("alkokh.pos.partner.commissionRate", {
    rate: percentFormatter.value.format(Number(partner.commission_rate ?? 0)),
  });
}
```

`PosPartnerPickerPanel` has no `formatAmount` prop today — either pass the one `sell.vue`
already owns, or format with the picker's own currency formatter. Do not print a bare
number: "2000" beside "25%" reads as a percentage.

`PosCheckoutPanel` needs no change; it passes `partnerCommissionAmount` straight through and
that figure is now correct for both types.

---

## 8. Strings

English:

```
alkokh.deliveryPartners.fields.commissionType        = "Commission type"
alkokh.deliveryPartners.fields.commissionTypeHint    = "A share of each order, or a fixed amount per order"
alkokh.deliveryPartners.fields.commissionAmount      = "Commission per order"
alkokh.deliveryPartners.fields.commissionAmountHint  = "The fixed amount the partner keeps from every order, whatever it is worth"
alkokh.deliveryPartners.commissionType.percentage    = "Percentage"
alkokh.deliveryPartners.commissionType.amount        = "Fixed amount"
alkokh.deliveryPartners.validation.commissionAmountRange = "The commission must be above 0"
alkokh.pos.partner.commissionFlat                    = "{amount} commission"
```

Arabic (matching the existing entries' register):

```
alkokh.deliveryPartners.fields.commissionType        = "نوع العمولة"
alkokh.deliveryPartners.fields.commissionTypeHint    = "نسبة من كل طلب، أو مبلغ ثابت لكل طلب"
alkokh.deliveryPartners.fields.commissionAmount      = "العمولة لكل طلب"
alkokh.deliveryPartners.fields.commissionAmountHint  = "المبلغ الثابت الذي تحتفظ به الشركة من كل طلب مهما كانت قيمته"
alkokh.deliveryPartners.commissionType.percentage    = "نسبة مئوية"
alkokh.deliveryPartners.commissionType.amount        = "مبلغ ثابت"
alkokh.deliveryPartners.validation.commissionAmountRange = "يجب أن تكون العمولة أكبر من 0"
alkokh.pos.partner.commissionFlat                    = "عمولة {amount}"
```

The existing `form.commissionLocked` says "the commission rate is locked". Reword both
languages to "the commission is locked", since it now locks the type and the fee too.

---

## 8a. "Bill the App Customer" partners (`is_inside`)

A separate change on the same screens, with its own handoff:
**`docs/frontend-delivery-partner-bill-app-customer.md`**. Read it before ticking that box on
any partner — the current build cannot sell for one.

## 9. What to check once it is in

1. **A percentage partner is untouched.** Create at 25%, sell 100,000, the tile reads 25%,
   the preview reads 25,000, the invoice stores rate 25 and amount 25,000.
2. **A flat-fee partner.** Create at 2,000 per order, sell 100,000 and then 10,000 — the
   preview reads 2,000 both times, and the tile reads the money, not `0%`.
3. **A cart worth no more than the fee** is blocked (or refused with
   `PARTNER_COMMISSION_EXCEEDS_TOTAL`, if you skip the §5 early block).
4. **Editing an existing flat-fee partner** opens on the amount field with the amount in it,
   and saving does not turn it back into a percentage.
5. **A settled partner** has its type, rate and amount all locked, with the reason on screen.
6. **The orders tab against an unmigrated site** still lists orders — the probe should drop
   the custom columns and say the detail is unavailable, exactly as it does today.
7. **Old orders** (sold before this work) still show their rate and commission: their
   `custom_partner_commission_type` is empty and must read as `Percentage`.
