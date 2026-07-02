from pathlib import Path

import frappe
from werkzeug.wrappers import Response


OPENAPI_FILENAME = "mobile-openapi.json"


def _openapi_path() -> Path:
    app_path = Path(frappe.get_app_path("pet_app"))
    candidates = [
        app_path.parents[2] / "docs" / OPENAPI_FILENAME,
        app_path.parent / "docs" / OPENAPI_FILENAME,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0]


@frappe.whitelist(allow_guest=True, methods=["GET"])
def openapi_json(**kwargs):
    kwargs.pop("cmd", None)
    spec_path = _openapi_path()
    return Response(
        spec_path.read_text(encoding="utf-8"),
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


@frappe.whitelist(allow_guest=True, methods=["GET"])
def swagger_ui(**kwargs):
    kwargs.pop("cmd", None)
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Alkokh Mobile API Docs</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <style>
    body {
      margin: 0;
      background: #f6f8fb;
      color: #172033;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    .docs-header {
      background: #ffffff;
      border-bottom: 1px solid #dfe5ef;
      padding: 20px 32px;
    }

    .docs-header h1 {
      margin: 0 0 6px;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0;
    }

    .docs-header p {
      margin: 0;
      color: #5f6f89;
      font-size: 14px;
    }

    #swagger-ui {
      max-width: 1240px;
      margin: 0 auto;
      padding: 24px 18px 48px;
    }

    .swagger-ui .topbar {
      display: none;
    }

    .swagger-ui .scheme-container,
    .swagger-ui .info {
      background: #ffffff;
      border: 1px solid #dfe5ef;
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(23, 32, 51, 0.06);
    }

    .swagger-ui .info {
      margin: 0 0 18px;
      padding: 22px;
    }

    .swagger-ui .scheme-container {
      padding: 16px 22px;
      margin: 0 0 18px;
    }

    .swagger-ui .opblock {
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(23, 32, 51, 0.06);
    }
  </style>
</head>
<body>
  <header class="docs-header">
    <h1>Alkokh Mobile API</h1>
    <p>Swagger UI for the implemented mobile authentication endpoints.</p>
  </header>
  <main id="swagger-ui"></main>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
  <script>
    window.addEventListener("load", function () {
      window.ui = SwaggerUIBundle({
        url: "/api/method/pet_app.api.mobile.docs.openapi_json",
        dom_id: "#swagger-ui",
        deepLinking: true,
        docExpansion: "list",
        defaultModelsExpandDepth: 1,
        defaultModelExpandDepth: 2,
        displayRequestDuration: true,
        filter: true,
        persistAuthorization: true,
        tryItOutEnabled: true,
        supportedSubmitMethods: ["get", "post"],
        presets: [
          SwaggerUIBundle.presets.apis,
          SwaggerUIStandalonePreset
        ],
        layout: "BaseLayout"
      });
    });
  </script>
</body>
</html>
"""
    return Response(
        html,
        mimetype="text/html",
        headers={"Cache-Control": "no-store"},
    )
