# Pet Clinic Shop — API Endpoints Checklist

> Compact list of every endpoint the mobile app needs from the backend.

**Legend:** 🔓 public · 🔐 auth required · 🔄 optional auth (response personalised when authed) · 🪪 must accept `Idempotency-Key`

**Base URL:** `https://api.petclinicshop.com/v1`
**Auth:** `Authorization: Bearer <access_token>` (JWT, ~15 min) + rotating refresh tokens (~60 days)
**Localization:** `Accept-Language: en | ar`
**Money:** integer minor units (IQD)
**Pagination:** cursor-based — `?limit=20&cursor=…` → `{ items, nextCursor, hasMore }`

---

## 1. Auth & account

| # | Method | Path                                | Auth | Purpose                                            |
| - | ------ | ----------------------------------- | ---- | -------------------------------------------------- |
| 1 | POST   | `/auth/sign-up/start`               | 🔓   | Start sign-up, send OTP                            |
| 2 | POST   | `/auth/sign-up/verify`              | 🔓   | Verify OTP, create account, return session         |
| 3 | POST   | `/auth/sign-in`                     | 🔓   | Sign in with phone + password                      |
| 4 | POST   | `/auth/password-reset/request`      | 🔓   | Send password-reset OTP                            |
| 5 | POST   | `/auth/password-reset/verify`       | 🔓   | Verify reset OTP, return short-lived reset token   |
| 6 | POST   | `/auth/password-reset/confirm`      | 🔓   | Set new password using reset token, return session |
| 7 | POST   | `/auth/refresh`                     | 🔓   | Exchange refresh token for new access + refresh    |
| 8 | POST   | `/auth/sign-out`                    | 🔐   | Revoke refresh token                               |

---

## 2. Home (discovery)

| # | Method | Path     | Auth | Purpose                                                                    |
| - | ------ | -------- | ---- | -------------------------------------------------------------------------- |
| 9 | GET    | `/home`  | 🔄   | Composite home feed: promos, categories, shop-by-category, sections, brands |

---

## 3. Catalog

| #  | Method | Path                          | Auth | Purpose                                                |
| -- | ------ | ----------------------------- | ---- | ------------------------------------------------------ |
| 10 | GET    | `/products`                   | 🔄   | Paginated product list with filters (see query params) |
| 11 | GET    | `/products/{id}`              | 🔄   | Product detail + related + review summary              |
| 12 | GET    | `/products/{id}/reviews`      | 🔄   | Paginated reviews                                      |
| 13 | POST   | `/products/{id}/reviews`      | 🔐   | Submit / update own review                             |
| 14 | GET    | `/brands`                     | 🔓   | List brands                                            |
| 15 | GET    | `/categories`                 | 🔓   | List pet-type + shop-by categories                     |

**`/products` query params:** `category`, `shopCategory`, `brandId`, `tag` (`recentlyAdded|newToShop|backInStock|bestSellers`), `minPrice`, `maxPrice`, `inStock`, `sort` (`relevance|priceAsc|priceDesc|ratingDesc|newest`), `limit`, `cursor`.

---

## 4. Search

| #  | Method | Path                | Auth | Purpose                                |
| -- | ------ | ------------------- | ---- | -------------------------------------- |
| 16 | GET    | `/search?q=…`       | 🔄   | Search products (paginated)            |
| 17 | GET    | `/search/suggest?q=…` | 🔄 | Type-ahead suggestions + product hints |
| 18 | GET    | `/search/recent`    | 🔐   | User's recent searches (last 10)       |
| 19 | POST   | `/search/recent`    | 🔐   | Save a recent search                   |
| 20 | DELETE | `/search/recent`    | 🔐   | Clear recent searches                  |

---

## 5. Cart

| #  | Method | Path                       | Auth | Purpose                                                  |
| -- | ------ | -------------------------- | ---- | -------------------------------------------------------- |
| 21 | GET    | `/cart`                    | 🔐   | Get current cart + totals + issues (e.g. stock changes)  |
| 22 | POST   | `/cart/items`              | 🔐   | Add item                                                 |
| 23 | PATCH  | `/cart/items/{itemId}`     | 🔐   | Update quantity (0 = remove; over-stock clamps + warns)  |
| 24 | DELETE | `/cart/items/{itemId}`     | 🔐   | Remove a single item                                     |
| 25 | DELETE | `/cart`                    | 🔐   | Clear cart                                               |
| 26 | POST   | `/cart/merge`              | 🔐   | Merge device-local cart into server cart on sign-in      |

---

## 6. Favorites / wishlist

| #  | Method | Path                     | Auth | Purpose                                |
| -- | ------ | ------------------------ | ---- | -------------------------------------- |
| 27 | GET    | `/favorites`             | 🔐   | Paginated favorite products            |
| 28 | POST   | `/favorites`             | 🔐   | Toggle favorite (returns new state)    |
| 29 | DELETE | `/favorites/{productId}` | 🔐   | Remove (for swipe-to-delete on list)   |

---

## 7. Addresses

| #  | Method | Path                              | Auth | Purpose                                  |
| -- | ------ | --------------------------------- | ---- | ---------------------------------------- |
| 30 | GET    | `/addresses`                      | 🔐   | List addresses                           |
| 31 | POST   | `/addresses`                      | 🔐   | Create                                   |
| 32 | PUT    | `/addresses/{id}`                 | 🔐   | Update                                   |
| 33 | DELETE | `/addresses/{id}`                 | 🔐   | Delete (auto-promotes new default)       |
| 34 | POST   | `/addresses/{id}/default`         | 🔐   | Set as default                           |
| 35 | GET    | `/geo/cities`                     | 🔓   | Supported cities for the city picker     |
| 36 | GET    | `/geo/reverse?lat=&lng=`          | 🔓   | Reverse geocode for the map picker       |

---

## 8. Payment methods

| #  | Method | Path                                  | Auth | Purpose                                              |
| -- | ------ | ------------------------------------- | ---- | ---------------------------------------------------- |
| 37 | GET    | `/payment-methods`                    | 🔐   | List (COD is always present, server-seeded)          |
| 38 | POST   | `/payment-methods`                    | 🔐   | Add card via gateway token (PAN never sent here)     |
| 39 | PATCH  | `/payment-methods/{id}`               | 🔐   | Rename card nickname                                 |
| 40 | DELETE | `/payment-methods/{id}`               | 🔐   | Delete card (COD cannot be deleted)                  |
| 41 | POST   | `/payment-methods/{id}/default`       | 🔐   | Set as default                                       |

---

## 9. Checkout & orders

| #  | Method | Path                              | Auth     | Purpose                                                   |
| -- | ------ | --------------------------------- | -------- | --------------------------------------------------------- |
| 42 | POST   | `/checkout/quote`                 | 🔐       | Preview total with selected address + payment + promo     |
| 43 | POST   | `/orders`                         | 🔐 🪪    | Place order (snapshots address + payment; clears cart)    |
| 44 | GET    | `/orders`                         | 🔐       | Paginated order list (most-recent first, summary shape)   |
| 45 | GET    | `/orders/{id}`                    | 🔐       | Full order + status timeline                              |
| 46 | POST   | `/orders/{id}/cancel`             | 🔐       | Cancel (only while `placed` or `preparing`)               |
| 47 | POST   | `/orders/{id}/reorder`            | 🔐       | Re-add past order's items to current cart                 |
| 48 | POST   | `/promos/validate`                | 🔐       | Validate a promo code, return discount                    |

**Order status enum:** `placed → preparing → outForDelivery → delivered` + terminal `cancelled`, `failed`.

---

## 10. User profile

| #  | Method | Path                  | Auth | Purpose                                              |
| -- | ------ | --------------------- | ---- | ---------------------------------------------------- |
| 49 | GET    | `/me`                 | 🔐   | Current user                                         |
| 50 | PATCH  | `/me`                 | 🔐   | Update full name                                     |
| 51 | POST   | `/me/phone/start`     | 🔐   | Start phone-change flow (sends OTP to new number)    |
| 52 | POST   | `/me/phone/verify`    | 🔐   | Verify OTP, switch phone, rotate all refresh tokens  |
| 53 | POST   | `/me/password`        | 🔐   | Change password (revokes other refresh tokens)       |
| 54 | POST   | `/me/avatar`          | 🔐   | Upload avatar (multipart)                            |
| 55 | DELETE | `/me`                 | 🔐   | Delete account (anonymises past orders)              |

---

## 11. Pets

| #  | Method | Path                                                   | Auth     | Purpose                            |
| -- | ------ | ------------------------------------------------------ | -------- | ---------------------------------- |
| 56 | GET    | `/pets`                                                | 🔐       | List pets                          |
| 57 | POST   | `/pets`                                                | 🔐 🪪    | Create pet                         |
| 58 | PUT    | `/pets/{id}`                                           | 🔐       | Update pet                         |
| 59 | DELETE | `/pets/{id}`                                           | 🔐       | Delete pet                         |
| 60 | POST   | `/pets/{id}/primary`                                   | 🔐       | Set as primary pet                 |
| 61 | POST   | `/pets/{id}/photo`                                     | 🔐       | Upload pet photo (multipart)       |
| 62 | GET    | `/pets/{petId}/medical-records`                        | 🔐       | List medical records               |
| 63 | POST   | `/pets/{petId}/medical-records`                        | 🔐       | Add record                         |
| 64 | PUT    | `/pets/{petId}/medical-records/{id}`                   | 🔐       | Update record                      |
| 65 | DELETE | `/pets/{petId}/medical-records/{id}`                   | 🔐       | Delete record                      |

**Enums:** `species: dog|cat|rabbit|bird|other` · `gender: male|female|unknown` · `medical record type: vaccination|surgery|checkup|other`.

---

## 12. App config & devices

| #  | Method | Path                        | Auth | Purpose                                                              |
| -- | ------ | --------------------------- | ---- | -------------------------------------------------------------------- |
| 74 | GET    | `/config`                   | 🔓   | Currency, delivery fee, supported locales, feature flags, min versions |
| 75 | POST   | `/devices`                  | 🔐   | Register / refresh FCM push token                                    |
| 76 | DELETE | `/devices/{fcmToken}`       | 🔐   | Deregister token on sign-out                                         |
| 77 | GET    | `/support/contact`          | 🔓   | Help & support contact info                                          |
| 78 | GET    | `/content/{key}`            | 🔓   | Static content (privacy, terms, FAQ)                                 |

---

## Cross-cutting requirements (apply to all endpoints)

- **Error envelope:** `{ "error": { "code": "<stable.code>", "message": "…", "details": {…} } }`
- **Stable error codes the app already maps:** `auth.wrong_credentials`, `auth.invalid_otp`, `auth.otp_expired`, `auth.phone_already_registered`, `cart.out_of_stock`, `cart.variant_unavailable`, `cart.stock_limit_reached`, `cart.price_changed`, `order.cart_empty`, `order.address_required`, `order.payment_required`, `order.not_cancellable`, `payment.card_declined`, `payment.card_already_exists`, `payment.cod_undeletable`, `appointment.slot_taken`, `appointment.too_late_to_cancel`, `promo.invalid`, `promo.expired`, `promo.minimum_not_met`, `rate_limit.exceeded`.
- **Idempotency-Key header (UUID v4)** required on the endpoints marked 🪪. Replays within 24 h must return the original response.
- **Tracing:** echo back the client-supplied `X-Request-Id` header in responses and logs.
- **Rate limits:** OTP 3/hr/phone · sign-in 10/min/phone+IP · search 60/min/user · generic authed 600/min/user · generic unauth 120/min/IP.
- **No PII in logs:** never log passwords, OTPs, gateway tokens, full PANs, or `Authorization` headers.

---

## Total endpoint count

**70 endpoints** across 12 sections.
