# Vue Admin Contract: Mobile Home Banners

This document is for the Vue.js admin panel team. It describes how to add an admin UI for managing banners used by the mobile Home v2 API.

## Source Of Truth

Use the Frappe DocType:

```text
Mobile Home Banner
```

The mobile API reads enabled records from this DocType and returns them in:

```text
pet_app.api.mobile.catalog.home_v2
```

Do not hardcode banners in Vue or in the mobile app.

## Admin Feature

Add an admin screen called:

```text
Mobile Home Banners
```

Required UI:

- List banners ordered by `display_order asc`
- Create banner
- Edit banner
- Enable / disable banner
- Delete banner, or hide delete if the admin panel avoids destructive actions
- Upload/select banner image
- Preview basic banner card if possible

## DocType Fields

| Field | Type | Required | Notes |
|---|---|---:|---|
| `enabled` | boolean/check | no | Only enabled banners appear in Home v2. Default `true`. |
| `display_order` | integer | no | Lower number appears earlier. Default `10`. |
| `block_type` | select | yes | `banner_carousel` or `single_banner`. |
| `block_id` | string | no | Groups carousel banners. Default for carousel: `hero-banners`. |
| `banner_id` | string | recommended | Stable id for analytics/clicks. Unique if supplied. |
| `image` | attach image URL | yes when enabled | Public image URL from Frappe `File`. |
| `title` | string | no | Banner title. |
| `subtitle` | string | no | Banner subtitle/body. |
| `button_title` | string | no | CTA label. Default `Shop Now`. |
| `gradient_start` | string | no | Hex color `#RRGGBB`. Default `#FF9A56`. |
| `gradient_end` | string | no | Hex color `#RRGGBB`. Default `#FF5E62`. |
| `action_type` | select | yes when enabled | `product`, `category`, `brand`, `list`, or `url`. |
| `action_value` | string | yes when enabled | Target id or URL depending on action type. |

## Select Options

`block_type`:

```json
["banner_carousel", "single_banner"]
```

`action_type`:

```json
["product", "category", "brand", "list", "url"]
```

Supported `list` action values:

```json
["best-sellers", "recently-added", "back-in-stock"]
```

## Create Payload Example

Example body for creating a carousel banner:

```json
{
  "doctype": "Mobile Home Banner",
  "enabled": 1,
  "display_order": 10,
  "block_type": "banner_carousel",
  "block_id": "hero-banners",
  "banner_id": "summer-sale",
  "image": "/files/summer-sale.png",
  "title": "Summer Sale",
  "subtitle": "Up to 30% off dog essentials",
  "button_title": "Shop Now",
  "gradient_start": "#FF9A56",
  "gradient_end": "#FF5E62",
  "action_type": "category",
  "action_value": "dog-food"
}
```

Example body for creating a single mid-feed banner:

```json
{
  "doctype": "Mobile Home Banner",
  "enabled": 1,
  "display_order": 40,
  "block_type": "single_banner",
  "block_id": "mid-feed-promo",
  "banner_id": "grooming-week",
  "image": "/files/grooming-week.png",
  "title": "Grooming Week",
  "subtitle": "Buy 2 get 1 free on shampoos",
  "button_title": "See Deals",
  "gradient_start": "#A18CD1",
  "gradient_end": "#FBC2EB",
  "action_type": "category",
  "action_value": "grooming"
}
```

## Read/List Example

List enabled and disabled banners for admin:

```text
GET /api/resource/Mobile Home Banner
```

Suggested query:

```json
{
  "fields": [
    "name",
    "enabled",
    "display_order",
    "block_type",
    "block_id",
    "banner_id",
    "image",
    "title",
    "subtitle",
    "button_title",
    "gradient_start",
    "gradient_end",
    "action_type",
    "action_value",
    "modified"
  ],
  "order_by": "display_order asc, modified desc"
}
```

## Image Upload

Use the existing Frappe file upload flow and store the returned public `file_url` in the banner `image` field.

Expected final banner image field:

```json
{
  "image": "/files/summer-sale.png"
}
```

The mobile API converts relative file URLs to absolute URLs in `home_v2`.

Image rules:

- Use public files, not private files.
- Recommended aspect ratio: wide banner, around `2:1` or `16:9`.
- Recommended minimum width: `1200px`.
- Avoid text baked into the image if the same text exists in `title`/`subtitle`.

## Validation Rules

Backend validates enabled banners:

- `block_type` must be `banner_carousel` or `single_banner`.
- `image` is required.
- `action_type` is required.
- `action_value` is required.
- `gradient_start` and `gradient_end` must be `#RRGGBB`; invalid values fall back to defaults.

Recommended Vue validation:

- Disable save if enabled banner has no image.
- Disable save if enabled banner has no action type/value.
- Validate color fields with `^#[0-9A-Fa-f]{6}$`.
- For `action_type = url`, validate absolute `http://` or `https://`.
- For `action_type = list`, offer a dropdown with supported list ids.
- For `action_type = product`, use a Product picker.
- For `action_type = category`, use a Product Category picker.
- For `action_type = brand`, use a Brand picker.

## How It Appears In Mobile Home v2

Multiple enabled records with:

```json
{
  "block_type": "banner_carousel",
  "block_id": "hero-banners"
}
```

become one carousel block:

```json
{
  "id": "hero-banners",
  "type": "banner_carousel",
  "data": {
    "banners": []
  }
}
```

An enabled record with:

```json
{
  "block_type": "single_banner",
  "block_id": "mid-feed-promo"
}
```

becomes:

```json
{
  "id": "mid-feed-promo",
  "type": "single_banner",
  "data": {
    "banner": {}
  }
}
```

## Notes For Admin UX

- `banner_id` should be stable. Do not auto-change it when title changes.
- `block_id` is important for analytics and layout targeting.
- Carousel banners with the same `block_id` are grouped together.
- `display_order` controls both block order and banner order.
- The mobile API does not return disabled banners.
- The mobile API skips banners with missing image/action data.

