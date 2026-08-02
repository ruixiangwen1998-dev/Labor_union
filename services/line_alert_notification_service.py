"""
================================================================================
檔案名稱: services/line_alert_notification_service.py
功能說明: LINE 服務異常通知對象、事件去重、獨立派送、重試與本機目標快取服務
================================================================================
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymysql
import requests

from api.schemas.line_alert_notifications import LineAlertNotificationConfig
from services.db_service import get_connection
from services.json_config_service import read_config


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / ".monitor_state"
TARGET_CACHE_PATH = STATE_DIR / "line_alert_targets.json"
FALLBACK_STATE_PATH = STATE_DIR / "line_alert_fallback_state.json"
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
SEVERITY_RANK = {"warning": 1, "critical": 2}


class AlertNotificationError(RuntimeError):
    pass


class AlertNotificationPermissionError(AlertNotificationError):
    pass


class AlertNotificationNotFoundError(AlertNotificationError):
    pass


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_alert_notification_config() -> LineAlertNotificationConfig:
    return read_config("line_alert_notifications", LineAlertNotificationConfig)


def _parse_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else _parse_json(value)
        if key.endswith("_json") and value
        else value
        for key, value in row.items()
    }


def _target_select_sql() -> str:
    return """
        SELECT t.*,u.username AS admin_username,u.role AS admin_role,
               u.enabled AS admin_enabled,u.linked_line_user_id,
               CASE WHEN t.target_type='group' THEN t.line_target_id
                    ELSE u.linked_line_user_id END AS resolved_line_target_id
        FROM line_alert_notification_targets t
        LEFT JOIN admin_users u ON u.id=t.admin_user_id
    """


def list_notification_targets(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            sql = _target_select_sql()
            if enabled_only:
                sql += " WHERE t.enabled=TRUE"
            sql += " ORDER BY t.target_type,t.display_name,t.id"
            cursor.execute(sql)
            rows = cursor.fetchall()
        return [_serialize_row(row) for row in rows]
    finally:
        conn.close()


def list_available_admin_targets() -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT id,username,display_name,role,linked_line_user_id
                FROM admin_users
                WHERE enabled=TRUE AND linked_line_user_id IS NOT NULL
                ORDER BY display_name,id
                """
            )
            return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def create_notification_target(
    payload: dict[str, Any], *, created_by_admin_user_id: int | None
) -> dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            if payload["target_type"] == "user":
                cursor.execute(
                    """
                    SELECT id FROM admin_users
                    WHERE id=%s AND enabled=TRUE AND linked_line_user_id IS NOT NULL
                    """,
                    (payload.get("admin_user_id"),),
                )
                if not cursor.fetchone():
                    raise AlertNotificationError("工會人員尚未綁定可用的 LINE 帳號")
            cursor.execute(
                """
                INSERT INTO line_alert_notification_targets (
                    target_type,admin_user_id,line_target_id,display_name,
                    minimum_severity,notify_recovery,enabled,verified_at,
                    created_by_admin_user_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(),%s)
                """,
                (
                    payload["target_type"],
                    payload.get("admin_user_id"),
                    payload.get("line_target_id"),
                    payload["display_name"].strip(),
                    payload.get("minimum_severity", "critical"),
                    bool(payload.get("notify_recovery", True)),
                    bool(payload.get("enabled", True)),
                    created_by_admin_user_id,
                ),
            )
            target_id = int(cursor.lastrowid)
        conn.commit()
    except pymysql.err.IntegrityError as exc:
        conn.rollback()
        raise AlertNotificationError("此通知對象已經存在") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    refresh_notification_target_cache()
    return get_notification_target(target_id)


def get_notification_target(target_id: int) -> dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(_target_select_sql() + " WHERE t.id=%s", (target_id,))
            row = cursor.fetchone()
        if not row:
            raise AlertNotificationNotFoundError("找不到異常通知對象")
        return _serialize_row(row)
    finally:
        conn.close()


def update_notification_target(target_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE line_alert_notification_targets
                SET display_name=%s,minimum_severity=%s,notify_recovery=%s,enabled=%s
                WHERE id=%s
                """,
                (
                    payload["display_name"].strip(),
                    payload.get("minimum_severity", "critical"),
                    bool(payload.get("notify_recovery", True)),
                    bool(payload.get("enabled", True)),
                    target_id,
                ),
            )
            if cursor.rowcount == 0:
                raise AlertNotificationNotFoundError("找不到異常通知對象")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    refresh_notification_target_cache()
    return get_notification_target(target_id)


def delete_notification_target(target_id: int) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM line_alert_notification_targets WHERE id=%s", (target_id,)
            )
            if cursor.rowcount == 0:
                raise AlertNotificationNotFoundError("找不到異常通知對象")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    refresh_notification_target_cache()


def bind_notification_group(cursor, *, group_id: str, actor_line_user_id: str) -> dict[str, Any]:
    """Bind a group only when the message sender is a linked LINE manager."""
    cursor.execute(
        """
        SELECT id,display_name,role
        FROM admin_users
        WHERE linked_line_user_id=%s AND enabled=TRUE
        LIMIT 1
        """,
        (actor_line_user_id,),
    )
    admin = cursor.fetchone()
    if not admin or admin.get("role") not in {"line_manager", "system_admin"}:
        raise AlertNotificationPermissionError("只有 LINE 主管或系統管理員可以綁定異常通知群組")
    cursor.execute(
        """
        INSERT INTO line_alert_notification_targets (
            target_type,line_target_id,display_name,minimum_severity,
            notify_recovery,enabled,verified_at,created_by_admin_user_id
        ) VALUES ('group',%s,%s,'critical',TRUE,TRUE,UTC_TIMESTAMP(),%s)
        ON DUPLICATE KEY UPDATE display_name=VALUES(display_name),enabled=TRUE,
            verified_at=UTC_TIMESTAMP(),created_by_admin_user_id=VALUES(created_by_admin_user_id)
        """,
        (group_id, "系統異常通知群組", admin["id"]),
    )
    cursor.execute(
        _target_select_sql() + " WHERE t.target_type='group' AND t.line_target_id=%s",
        (group_id,),
    )
    return _serialize_row(cursor.fetchone())


def unbind_notification_group(cursor, *, group_id: str, actor_line_user_id: str) -> bool:
    cursor.execute(
        """
        SELECT role FROM admin_users
        WHERE linked_line_user_id=%s AND enabled=TRUE LIMIT 1
        """,
        (actor_line_user_id,),
    )
    admin = cursor.fetchone()
    if not admin or admin.get("role") not in {"line_manager", "system_admin"}:
        raise AlertNotificationPermissionError("只有 LINE 主管或系統管理員可以解除通知群組")
    cursor.execute(
        """
        UPDATE line_alert_notification_targets SET enabled=FALSE
        WHERE target_type='group' AND line_target_id=%s
        """,
        (group_id,),
    )
    return cursor.rowcount > 0


def disable_notification_group(cursor, group_id: str) -> None:
    cursor.execute(
        """
        UPDATE line_alert_notification_targets SET enabled=FALSE
        WHERE target_type='group' AND line_target_id=%s
        """,
        (group_id,),
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def refresh_notification_target_cache() -> list[dict[str, Any]]:
    targets = [
        {
            "id": int(item["id"]),
            "target_type": item["target_type"],
            "display_name": item["display_name"],
            "minimum_severity": item["minimum_severity"],
            "notify_recovery": bool(item["notify_recovery"]),
            "resolved_line_target_id": item.get("resolved_line_target_id"),
        }
        for item in list_notification_targets(enabled_only=True)
        if item.get("resolved_line_target_id")
        and (item.get("target_type") == "group" or item.get("admin_enabled"))
    ]
    _atomic_write_json(
        TARGET_CACHE_PATH,
        {"refreshed_at": _utc_now_naive().isoformat(), "targets": targets},
    )
    return targets


def _severity_allowed(actual: str, minimum: str) -> bool:
    return SEVERITY_RANK.get(actual, 0) >= SEVERITY_RANK.get(minimum, 2)


def _component_enabled(config: LineAlertNotificationConfig, alert: dict[str, Any]) -> bool:
    key = (
        "development_supervisor"
        if alert.get("event_type") == "service_supervisor"
        else str(alert.get("component") or "")
    )
    return config.components.get(key, True)


def _delivery_payload(alert: dict[str, Any], transition: str) -> dict[str, Any]:
    return {
        "event_type": alert.get("event_type"),
        "component": alert.get("component") or "系統服務",
        "description": str(alert.get("description") or "系統偵測到服務異常")[:500],
        "severity": alert.get("severity") or "warning",
        "transition": transition,
        "first_detected_at": alert.get("first_detected_at").isoformat()
        if isinstance(alert.get("first_detected_at"), datetime)
        else str(alert.get("first_detected_at") or ""),
        "last_detected_at": alert.get("last_detected_at").isoformat()
        if isinstance(alert.get("last_detected_at"), datetime)
        else str(alert.get("last_detected_at") or ""),
        "resolved_at": alert.get("resolved_at").isoformat()
        if isinstance(alert.get("resolved_at"), datetime)
        else str(alert.get("resolved_at") or ""),
        "occurrence_count": int(alert.get("occurrence_count") or 1),
    }


def _insert_delivery(
    cursor,
    *,
    alert: dict[str, Any] | None,
    target: dict[str, Any],
    transition: str,
    severity: str,
    idempotency_key: str,
    max_attempts: int,
    payload: dict[str, Any],
) -> bool:
    cursor.execute(
        """
        INSERT IGNORE INTO line_alert_deliveries (
            monitor_alert_id,target_id,transition,severity,idempotency_key,
            line_retry_key,payload_json,max_attempts
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            alert.get("id") if alert else None,
            target["id"],
            transition,
            severity,
            idempotency_key,
            str(uuid.uuid4()),
            json.dumps(payload, ensure_ascii=False),
            max_attempts,
        ),
    )
    return cursor.rowcount > 0


def stage_monitor_alert_deliveries() -> int:
    config = load_alert_notification_config()
    if not config.enabled:
        return 0
    conn = get_connection()
    created = 0
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                _target_select_sql()
                + """
                  WHERE t.enabled=TRUE
                    AND (t.target_type='group' OR u.enabled=TRUE)
                """
            )
            targets = [row for row in cursor.fetchall() if row.get("resolved_line_target_id")]
            cursor.execute(
                """
                SELECT id,event_type,description,status,component,severity,fingerprint,
                       first_detected_at,last_detected_at,occurrence_count,resolved_at
                FROM service_monitor_alerts
                WHERE event_type IN ('line_health','service_supervisor')
                  AND (status='pending' OR resolved_at >= UTC_TIMESTAMP() - INTERVAL 7 DAY)
                ORDER BY id
                """
            )
            alerts = cursor.fetchall()
            now = _utc_now_naive()
            for alert in alerts:
                if not _component_enabled(config, alert):
                    continue
                severity = str(alert.get("severity") or "warning")
                for target in targets:
                    target_minimum = str(target.get("minimum_severity") or "critical")
                    minimum = (
                        target_minimum
                        if SEVERITY_RANK[target_minimum] >= SEVERITY_RANK[config.minimum_severity]
                        else config.minimum_severity
                    )
                    cursor.execute(
                        """
                        SELECT transition,severity,status,sent_at,created_at
                        FROM line_alert_deliveries
                        WHERE monitor_alert_id=%s AND target_id=%s AND transition<>'test'
                        ORDER BY id
                        """,
                        (alert["id"], target["id"]),
                    )
                    existing = cursor.fetchall()
                    if alert["status"] == "pending" and _severity_allowed(severity, minimum):
                        notified_severities = {
                            item["severity"] for item in existing
                            if item["transition"] in {"opened", "escalated"}
                            and item["status"] != "cancelled"
                        }
                        transition = None
                        if not notified_severities:
                            transition = "opened"
                        elif severity == "critical" and "critical" not in notified_severities:
                            transition = "escalated"
                        if transition:
                            key = f"monitor-alert:{alert['id']}:{target['id']}:{transition}:{severity}"
                            created += int(
                                _insert_delivery(
                                    cursor,
                                    alert=alert,
                                    target=target,
                                    transition=transition,
                                    severity=severity,
                                    idempotency_key=key,
                                    max_attempts=config.max_retries,
                                    payload=_delivery_payload(alert, transition),
                                )
                            )
                        elif config.repeat_after_minutes > 0:
                            sent_times = [
                                item.get("sent_at") for item in existing if item.get("sent_at")
                            ]
                            latest_sent = max(sent_times) if sent_times else None
                            if latest_sent and (now - latest_sent).total_seconds() >= config.repeat_after_minutes * 60:
                                bucket = int(now.timestamp() // (config.repeat_after_minutes * 60))
                                key = f"monitor-alert:{alert['id']}:{target['id']}:reminder:{bucket}"
                                created += int(
                                    _insert_delivery(
                                        cursor,
                                        alert=alert,
                                        target=target,
                                        transition="reminder",
                                        severity=severity,
                                        idempotency_key=key,
                                        max_attempts=config.max_retries,
                                        payload=_delivery_payload(alert, "reminder"),
                                    )
                                )
                    elif (
                        alert["status"] == "resolved"
                        and config.notify_recovery
                        and bool(target.get("notify_recovery"))
                        and any(
                            item["transition"] in {"opened", "escalated", "reminder"}
                            and item["status"] == "sent"
                            for item in existing
                        )
                        and not any(item["transition"] == "recovered" for item in existing)
                    ):
                        key = f"monitor-alert:{alert['id']}:{target['id']}:recovered"
                        created += int(
                            _insert_delivery(
                                cursor,
                                alert=alert,
                                target=target,
                                transition="recovered",
                                severity=severity,
                                idempotency_key=key,
                                max_attempts=config.max_retries,
                                payload=_delivery_payload(alert, "recovered"),
                            )
                        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    refresh_notification_target_cache()
    return created


def create_test_delivery(target_id: int) -> dict[str, Any]:
    target = get_notification_target(target_id)
    config = load_alert_notification_config()
    payload = {
        "component": "異常通知測試",
        "description": "這是 LINE 管理中心送出的測試通知，代表通知目標與 LINE API 可正常連線。",
        "severity": "warning",
        "transition": "test",
        "first_detected_at": _utc_now_naive().isoformat(),
        "last_detected_at": _utc_now_naive().isoformat(),
        "resolved_at": "",
        "occurrence_count": 1,
    }
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            _insert_delivery(
                cursor,
                alert=None,
                target=target,
                transition="test",
                severity="warning",
                idempotency_key=f"alert-test:{target_id}:{uuid.uuid4()}",
                max_attempts=config.max_retries,
                payload=payload,
            )
            delivery_id = int(cursor.lastrowid)
            cursor.execute(
                "UPDATE line_alert_notification_targets SET last_tested_at=UTC_TIMESTAMP() WHERE id=%s",
                (target_id,),
            )
        conn.commit()
    finally:
        conn.close()
    return get_alert_delivery(delivery_id)


def get_alert_delivery(delivery_id: int) -> dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT d.*,t.display_name,t.target_type
                FROM line_alert_deliveries d
                JOIN line_alert_notification_targets t ON t.id=d.target_id
                WHERE d.id=%s
                """,
                (delivery_id,),
            )
            row = cursor.fetchone()
        if not row:
            raise AlertNotificationNotFoundError("找不到異常通知派送紀錄")
        return _serialize_row(row)
    finally:
        conn.close()


def list_alert_deliveries(limit: int = 100) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT d.*,t.display_name,t.target_type
                FROM line_alert_deliveries d
                JOIN line_alert_notification_targets t ON t.id=d.target_id
                ORDER BY d.id DESC LIMIT %s
                """,
                (limit,),
            )
            return [_serialize_row(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _claim_due_deliveries(
    limit: int = 20,
    *,
    only_delivery_id: int | None = None,
) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                UPDATE line_alert_deliveries
                SET status='retry_scheduled',processing_started_at=NULL,
                    next_retry_at=UTC_TIMESTAMP(),error_code='stale_recovered'
                WHERE status='processing'
                  AND processing_started_at < UTC_TIMESTAMP() - INTERVAL 10 MINUTE
                """
            )
            delivery_filter = " AND d.id=%s" if only_delivery_id is not None else ""
            parameters: tuple[Any, ...] = (
                (only_delivery_id, limit)
                if only_delivery_id is not None
                else (limit,)
            )
            cursor.execute(
                f"""
                SELECT d.*,t.target_type,t.line_target_id,u.linked_line_user_id,
                       u.enabled AS admin_enabled
                FROM line_alert_deliveries d
                JOIN line_alert_notification_targets t ON t.id=d.target_id
                LEFT JOIN admin_users u ON u.id=t.admin_user_id
                WHERE d.status IN ('pending','retry_scheduled')
                  AND (d.next_retry_at IS NULL OR d.next_retry_at<=UTC_TIMESTAMP())
                  AND t.enabled=TRUE
                  {delivery_filter}
                ORDER BY d.id LIMIT %s FOR UPDATE SKIP LOCKED
                """,
                parameters,
            )
            rows = cursor.fetchall()
            if rows:
                ids = [row["id"] for row in rows]
                placeholders = ",".join(["%s"] * len(ids))
                cursor.execute(
                    f"UPDATE line_alert_deliveries SET status='processing',processing_started_at=UTC_TIMESTAMP() WHERE id IN ({placeholders})",
                    ids,
                )
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _alert_message(payload: dict[str, Any]) -> dict[str, Any]:
    transition = payload.get("transition")
    severity = payload.get("severity", "warning")
    if transition == "recovered":
        title, color, alt = "系統已恢復", "#16A34A", "系統服務已恢復"
    elif transition == "test":
        title, color, alt = "異常通知測試", "#2563EB", "LINE 異常通知測試"
    elif severity == "critical":
        title, color, alt = "系統嚴重異常", "#DC2626", "系統嚴重異常"
    else:
        title, color, alt = "系統需要注意", "#D97706", "系統需要注意"
    detected = payload.get("resolved_at") if transition == "recovered" else payload.get("last_detected_at")
    body = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": color,
            "contents": [{"type": "text", "text": title, "color": "#FFFFFF", "weight": "bold", "size": "lg"}],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": str(payload.get("component") or "系統服務"), "weight": "bold", "wrap": True},
                {"type": "text", "text": str(payload.get("description") or "")[:500], "wrap": True, "color": "#374151"},
                {"type": "text", "text": f"時間：{detected or '-'}", "size": "sm", "color": "#6B7280"},
                {"type": "text", "text": f"偵測次數：{payload.get('occurrence_count', 1)}", "size": "sm", "color": "#6B7280"},
            ],
        },
    }
    liff_id = os.getenv("LINE_LIFF_ID", "").strip()
    if liff_id and liff_id != "your_liff_id_here":
        body["footer"] = {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#047857",
                    "action": {
                        "type": "uri",
                        "label": "查看系統狀態",
                        "uri": f"https://liff.line.me/{liff_id}?target=union-staff-portal&section=status",
                    },
                }
            ],
        }
    return {"type": "flex", "altText": alt, "contents": body}


def _send_line_push(destination: str, payload: dict[str, Any], retry_key: str) -> tuple[bool, bool, str, str]:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "mock_token").strip()
    if not token or token == "mock_token" or token.startswith("your_"):
        print(f"[LINE Alert Mock] to={destination} component={payload.get('component')}")
        return True, False, "", ""
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/push",
            json={"to": destination, "messages": [_alert_message(payload)]},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Line-Retry-Key": retry_key,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        return False, True, "network_error", str(exc)
    if response.status_code == 200:
        return True, False, "", ""
    return (
        False,
        response.status_code in RETRYABLE_HTTP,
        f"http_{response.status_code}",
        response.text[:4000],
    )


def _finish_delivery(
    item: dict[str, Any], *, success: bool, retryable: bool, code: str, message: str
) -> None:
    attempts = int(item.get("attempt_count") or 0) + 1
    max_attempts = int(item.get("max_attempts") or 1)
    retry = not success and retryable and attempts < max_attempts
    config = load_alert_notification_config()
    delay = min(config.retry_base_seconds * (2 ** max(0, attempts - 1)), 3600)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            if success:
                cursor.execute(
                    """
                    UPDATE line_alert_deliveries
                    SET status='sent',attempt_count=%s,sent_at=UTC_TIMESTAMP(),
                        processing_started_at=NULL,next_retry_at=NULL,error_code=NULL,error_message=NULL
                    WHERE id=%s
                    """,
                    (attempts, item["id"]),
                )
            elif retry:
                cursor.execute(
                    """
                    UPDATE line_alert_deliveries
                    SET status='retry_scheduled',attempt_count=%s,
                        next_retry_at=DATE_ADD(UTC_TIMESTAMP(),INTERVAL %s SECOND),
                        processing_started_at=NULL,error_code=%s,error_message=%s
                    WHERE id=%s
                    """,
                    (attempts, delay, code, message[:4000], item["id"]),
                )
            else:
                cursor.execute(
                    """
                    UPDATE line_alert_deliveries
                    SET status='failed',attempt_count=%s,failed_at=UTC_TIMESTAMP(),
                        processing_started_at=NULL,next_retry_at=NULL,error_code=%s,error_message=%s
                    WHERE id=%s
                    """,
                    (attempts, code, message[:4000], item["id"]),
                )
        conn.commit()
    finally:
        conn.close()


def process_due_alert_deliveries(
    limit: int = 20,
    *,
    only_delivery_id: int | None = None,
) -> int:
    if only_delivery_id is None and not load_alert_notification_config().enabled:
        return 0
    processed = 0
    for item in _claim_due_deliveries(limit, only_delivery_id=only_delivery_id):
        destination = (
            item.get("line_target_id")
            if item.get("target_type") == "group"
            else item.get("linked_line_user_id")
            if item.get("admin_enabled")
            else None
        )
        payload = _parse_json(item.get("payload_json")) or {}
        if not destination:
            result = (False, False, "target_unavailable", "通知對象尚未綁定有效 LINE 帳號")
        else:
            result = _send_line_push(str(destination), payload, str(item["line_retry_key"]))
        _finish_delivery(
            item,
            success=result[0],
            retryable=result[1],
            code=result[2],
            message=result[3],
        )
        processed += 1
    return processed


def process_snapshot_fallback_notifications(snapshot: dict[str, Any]) -> int:
    """Send critical/recovery notices from local cache while MySQL is unavailable.

    This path intentionally handles only critical transitions.  It uses a local
    idempotency state so the 15-second monitor loop does not spam recipients.
    """
    config = load_alert_notification_config()
    if not config.enabled:
        return 0
    try:
        cached = json.loads(TARGET_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    try:
        state = json.loads(FALLBACK_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        state = {"active": {}, "sent": {}}
    active = state.setdefault("active", {})
    sent = state.setdefault("sent", {})
    processed = 0
    checks = snapshot.get("checks") or {}
    for check_name, check in checks.items():
        component_enabled = config.components.get(check_name, True)
        if not component_enabled:
            continue
        status = check.get("status")
        for target in cached.get("targets") or []:
            destination = target.get("resolved_line_target_id")
            if not destination:
                continue
            target_key = f"{target['id']}:{check_name}"
            if status == "critical" and _severity_allowed(
                "critical", target.get("minimum_severity", "critical")
            ):
                fingerprint = f"{check_name}:{check.get('status_changed_at') or check.get('checked_at')}"
                delivery_key = f"fallback:{target['id']}:{fingerprint}:critical"
                if delivery_key in sent:
                    continue
                payload = {
                    "component": check.get("component") or check_name,
                    "description": str(check.get("message") or "系統服務異常")[:500],
                    "severity": "critical",
                    "transition": "opened",
                    "first_detected_at": check.get("status_changed_at") or "",
                    "last_detected_at": check.get("checked_at") or "",
                    "resolved_at": "",
                    "occurrence_count": int(check.get("consecutive_failures") or 1),
                }
                retry_key = str(uuid.uuid5(uuid.NAMESPACE_URL, delivery_key))
                success, _, _, _ = _send_line_push(str(destination), payload, retry_key)
                if success:
                    sent[delivery_key] = _utc_now_naive().isoformat()
                    active[target_key] = {"fingerprint": fingerprint, "payload": payload}
                    processed += 1
            elif (
                status == "healthy"
                and target_key in active
                and config.notify_recovery
                and target.get("notify_recovery", True)
            ):
                previous = active[target_key]
                delivery_key = f"fallback:{target['id']}:{previous['fingerprint']}:recovered"
                if delivery_key not in sent:
                    payload = {
                        **previous["payload"],
                        "description": f"{check.get('component') or check_name} 已恢復正常。",
                        "transition": "recovered",
                        "resolved_at": check.get("checked_at") or _utc_now_naive().isoformat(),
                    }
                    retry_key = str(uuid.uuid5(uuid.NAMESPACE_URL, delivery_key))
                    success, _, _, _ = _send_line_push(str(destination), payload, retry_key)
                    if success:
                        sent[delivery_key] = _utc_now_naive().isoformat()
                        processed += 1
                active.pop(target_key, None)
    _atomic_write_json(
        FALLBACK_STATE_PATH,
        {
            "updated_at": _utc_now_naive().isoformat(),
            "active": active,
            "sent": dict(list(sent.items())[-1000:]),
        },
    )
    return processed
