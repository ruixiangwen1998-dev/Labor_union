"""
================================================================================
檔案名稱: services/webhook_event_service.py
功能說明: LINE Webhook 事件收件與去重服務，保存事件並阻擋同一事件重複處理
================================================================================
"""

import json
import pymysql

from services.line_order_group_service import redact_invite_event


def register_event(cursor, event: dict) -> bool:
    event_id = event.get("webhookEventId")
    if not event_id:
        return True
    source = event.get("source") or {}
    try:
        cursor.execute(
            """
            INSERT INTO line_webhook_events (
                webhook_event_id, event_type, source_type, source_user_id,
                source_group_id, event_timestamp, is_redelivery, payload_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                event_id, event.get("type", "unknown"), source.get("type"),
                source.get("userId"), source.get("groupId"), event.get("timestamp"),
                bool((event.get("deliveryContext") or {}).get("isRedelivery")),
                json.dumps(redact_invite_event(event), ensure_ascii=False),
            ),
        )
        return True
    except pymysql.err.IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            return False
        raise


def mark_event(cursor, event_id: str | None, status: str, error: str | None = None) -> None:
    if not event_id:
        return
    cursor.execute(
        """
        UPDATE line_webhook_events
        SET processing_status=%s, processed_at=NOW(), error_message=%s
        WHERE webhook_event_id=%s
        """,
        (status, error, event_id),
    )
