# Push Settings Page - Frontend Handoff

Add this field to the Vue notification/settings page so push notification clicks open the Vue app instead of Frappe Desk.

## Field

```ts
type NotificationSettings = {
  push_frontend_base_url?: string | null;
};
```

Recommended label:

```text
Push Frontend Base URL
```

Example value:

```text
https://clinic.example.com
```

The backend also accepts `clinic.example.com` and normalizes generated push links to `https://clinic.example.com/...`.

## Read Settings

```http
GET /api/method/pet_app.api.notifications.get_notification_settings
```

The value is returned inside:

```json
{
  "message": {
    "ok": true,
    "data": {
      "settings": {
        "push_frontend_base_url": "https://clinic.example.com"
      }
    }
  }
}
```

## Save Settings

```http
POST /api/method/pet_app.api.notifications.update_notification_settings
Content-Type: application/json
```

```json
{
  "push_frontend_base_url": "https://clinic.example.com"
}
```

Send cookies/session auth with the request:

```ts
await fetch(`${API_BASE}/api/method/pet_app.api.notifications.update_notification_settings`, {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    push_frontend_base_url: form.push_frontend_base_url?.trim() || "",
  }),
});
```

## Frontend Validation

Accept either:

```text
https://clinic.example.com
clinic.example.com
```

Reject obvious invalid values with spaces. Keep the field optional; blank means backend falls back to the Frappe Desk link.

## Click URL Behavior

When `push_frontend_base_url` is set, backend push clicks use these Vue routes:

```ts
const pushRoutePrefixByDoctype = {
  "Vet Visit": "/healthcare/visits",
  "Vet Case Sheet": "/healthcare/case-sheets",
  "Appointment": "/healthcare/appointments",
  "PetCareService": "/healthcare/services",
  "Lab": "/healthcare/labs",
  "Imaging": "/healthcare/radiology",
  "Pet Procedure": "/healthcare/procedures",
  "Sales Invoice": "/accounting/sales-invoices",
};
```

Example:

```text
push_frontend_base_url = https://clinic.example.com
Notification document = PetCareService / PetCareService-02209
Push click URL = https://clinic.example.com/healthcare/services/PetCareService-02209
```

For unknown document types, backend keeps the original Frappe in-app link.

Manual test pushes do not have a document route. When `push_frontend_base_url` is set, their click URL is the Vue app origin itself:

```text
https://clinic.example.com
```
