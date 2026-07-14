# Alkokh Mobile API

## Base URL

```text
http://178.105.22.175:8002
```

All mobile auth endpoints are backend method endpoints under:

```text
/api/method/pet_app.api.mobile.auth.<method_name>
```

All endpoints below use JSON request bodies.

## Auth

The mobile API uses opaque OAuth Bearer tokens.

Sign-up verification and sign-in return:

- `access_token`
- `refresh_token`
- `expires_in`
- `token_type`
- `user`
- `full_name`

The access token is not a JWT. Treat it as an opaque string.

Authenticated endpoints expect:

```text
Authorization: Bearer <access_token>
```

Access tokens currently expire after `3600` seconds. The `refresh` endpoint exchanges a refresh token for a new access token. The `sign_out` endpoint revokes the OAuth token row matching the supplied refresh token.

## Message Wrapper

Backend method responses are wrapped in a top-level `message` object.

Successful responses return `message.ok` and `message.data`.

Error responses return `message.error.code` and `message.error.message`.

## Input Rules

| Field | Format |
|---|---|
| `phone` | Iraqi mobile number as `07XXXXXXXXX` or `9647XXXXXXXXX` |
| `otp` | Six digits |
| `password` / `new_password` | 6 to 50 characters |
| `full_name` | 1 to 100 characters |
| `device_id` | Optional string; accepted by sign-in, not currently used |

## Endpoints

| # | Method | URL |
|---:|---|---|
| 1 | `POST` | `/api/method/pet_app.api.mobile.auth.sign_up_start` |
| 2 | `POST` | `/api/method/pet_app.api.mobile.auth.sign_up_verify` |
| 3 | `POST` | `/api/method/pet_app.api.mobile.auth.sign_in` |
| 4 | `POST` | `/api/method/pet_app.api.mobile.auth.refresh` |
| 5 | `POST` | `/api/method/pet_app.api.mobile.auth.sign_out` |
| 6 | `POST` | `/api/method/pet_app.api.mobile.auth.password_reset_request` |
| 7 | `POST` | `/api/method/pet_app.api.mobile.auth.password_reset_verify` |
| 8 | `POST` | `/api/method/pet_app.api.mobile.auth.password_reset_confirm` |

## 1. Sign Up Start

Starts sign-up for a phone number and sends an OTP.

```text
POST http://178.105.22.175:8002/api/method/pet_app.api.mobile.auth.sign_up_start
```

Request body:

```json
{
  "phone": "07799999999"
}
```

Success response:

```json
{
  "message": {
    "ok": true,
    "data": {
      "message": "OTP sent"
    }
  }
}
```

Possible errors:

| Code | HTTP |
|---|---:|
| `auth.phone_already_registered` | 400 |
| `auth.otp_expired` | 400 |
| `ValidationError` | 400 |

## 2. Sign Up Verify

Verifies the sign-up OTP, creates the linked account records, and returns OAuth tokens.

```text
POST http://178.105.22.175:8002/api/method/pet_app.api.mobile.auth.sign_up_verify
```

Request body:

```json
{
  "phone": "07799999999",
  "otp": "123456",
  "password": "Mobile@1234",
  "full_name": "Test Mobile User"
}
```

Success response:

```json
{
  "message": {
    "ok": true,
    "data": {
      "access_token": "opaque-access-token",
      "refresh_token": "opaque-refresh-token",
      "expires_in": 3600,
      "token_type": "Bearer",
      "user": "07799999999@petapp.local",
      "full_name": "Test Mobile User"
    }
  }
}
```

Possible errors:

| Code | HTTP |
|---|---:|
| `auth.phone_already_registered` | 400 |
| `auth.invalid_otp` | 400 |
| `auth.otp_expired` | 400 |
| `auth.token_invalid` | 401 or 500 |
| `ValidationError` | 400 |

## 3. Sign In

Signs in a verified Guardian by phone and password, then returns OAuth tokens.

```text
POST http://178.105.22.175:8002/api/method/pet_app.api.mobile.auth.sign_in
```

Request body:

```json
{
  "phone": "07700000001",
  "password": "Mobile@1234",
  "device_id": "optional-device-id"
}
```

Success response:

```json
{
  "message": {
    "ok": true,
    "data": {
      "access_token": "opaque-access-token",
      "refresh_token": "opaque-refresh-token",
      "expires_in": 3600,
      "token_type": "Bearer",
      "user": "testmobile@alkokh.com",
      "full_name": "Test Mobile User"
    }
  }
}
```

Possible errors:

| Code | HTTP |
|---|---:|
| `auth.wrong_credentials` | 401 |
| `auth.token_invalid` | 401 or 500 |
| `ValidationError` | 400 |

## 4. Refresh

Exchanges a refresh token for a new OAuth token response.

```text
POST http://178.105.22.175:8002/api/method/pet_app.api.mobile.auth.refresh
```

Request body:

```json
{
  "refresh_token": "opaque-refresh-token"
}
```

Success response:

```json
{
  "message": {
    "ok": true,
    "data": {
      "access_token": "new-opaque-access-token",
      "refresh_token": "new-or-null-refresh-token",
      "expires_in": 3600,
      "token_type": "Bearer",
      "user": null,
      "full_name": null
    }
  }
}
```

Possible errors:

| Code | HTTP |
|---|---:|
| `auth.token_invalid` | 401 or 500 |

## 5. Sign Out

Revokes the OAuth token row matching the supplied refresh token.

```text
POST http://178.105.22.175:8002/api/method/pet_app.api.mobile.auth.sign_out
```

Request body:

```json
{
  "refresh_token": "opaque-refresh-token"
}
```

Success response:

```json
{
  "message": {
    "ok": true,
    "data": {}
  }
}
```

Possible errors:

| Code | HTTP |
|---|---:|
| `auth.token_invalid` | 401 |

## 6. Password Reset Request

Sends a password reset OTP to a verified Guardian phone number.

```text
POST http://178.105.22.175:8002/api/method/pet_app.api.mobile.auth.password_reset_request
```

Request body:

```json
{
  "phone": "07700000001"
}
```

Success response:

```json
{
  "message": {
    "ok": true,
    "data": {
      "message": "OTP sent"
    }
  }
}
```

Possible errors:

| Code | HTTP |
|---|---:|
| `auth.wrong_credentials` | 401 |
| `auth.otp_expired` | 400 |
| `ValidationError` | 400 |

## 7. Password Reset Verify

Verifies the password reset OTP and returns a short-lived reset token.

The reset token is valid server-side for 15 minutes.

```text
POST http://178.105.22.175:8002/api/method/pet_app.api.mobile.auth.password_reset_verify
```

Request body:

```json
{
  "phone": "07700000001",
  "otp": "123456"
}
```

Success response:

```json
{
  "message": {
    "ok": true,
    "data": {
      "reset_token": "short-lived-reset-token"
    }
  }
}
```

Possible errors:

| Code | HTTP |
|---|---:|
| `auth.wrong_credentials` | 401 |
| `auth.invalid_otp` | 400 |
| `auth.otp_expired` | 400 |
| `ValidationError` | 400 |

## 8. Password Reset Confirm

Sets a new password using a valid reset token and revokes active OAuth tokens for that user.

```text
POST http://178.105.22.175:8002/api/method/pet_app.api.mobile.auth.password_reset_confirm
```

Request body:

```json
{
  "reset_token": "short-lived-reset-token",
  "new_password": "Mobile@1234"
}
```

Success response:

```json
{
  "message": {
    "ok": true,
    "data": {}
  }
}
```

Possible errors:

| Code | HTTP |
|---|---:|
| `auth.token_invalid` | 401 |
| `ValidationError` | 400 |

## Error Response Shape

```json
{
  "message": {
    "error": {
      "code": "auth.wrong_credentials",
      "message": "Invalid phone or password"
    }
  }
}
```

## Error Codes

| Code | Description |
|---|---|
| `auth.wrong_credentials` | Phone/password is wrong, phone is not found, phone is not verified where login is required, or password reset was requested for an invalid phone. |
| `auth.invalid_otp` | OTP is wrong, the OTP cannot be used for this action, or too many OTP attempts were made. |
| `auth.otp_expired` | OTP is missing, expired, or a new OTP is required. |
| `auth.phone_already_registered` | Phone number already belongs to a verified Guardian account. |
| `auth.token_invalid` | Refresh token, reset token, OAuth token request, or mobile OAuth configuration is invalid or expired. |
| `ValidationError` | Request body failed validation, such as invalid phone, OTP, password, or full name format. |

Unexpected backend exceptions may return the backend exception class name as `message.error.code`.

## HTTP Status Codes

| Status | Meaning |
|---:|---|
| `200` | Request succeeded. |
| `400` | Request validation failed, OTP failed, OTP expired, duplicate phone, or another request-level error occurred. |
| `401` | Credentials, refresh token, reset token, or OAuth token request is invalid or expired. |
| `500` | Server configuration error, including missing mobile OAuth client configuration. |

## Test Credentials

| Purpose | Value |
|---|---|
| Base URL | `http://178.105.22.175:8002` |
| Sign-in phone | `07700000001` |
| Sign-in password | `Mobile@1234` |
| Test sign-up phone | `07799999999` |
| Fixed OTP in debug/test mode | `123456` |
