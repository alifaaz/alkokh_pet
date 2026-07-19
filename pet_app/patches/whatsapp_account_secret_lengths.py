from __future__ import annotations

from pet_app.patches.p1_5_notification_engine_schema import ensure_notification_schema


def execute():
	ensure_notification_schema()
