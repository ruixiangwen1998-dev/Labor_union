"""
================================================================================
檔案名稱: tests/test_line_alert_notifications.py
功能說明: LINE 系統異常通知設定、群組權限、指定派送與 DB 故障備援的回歸測試
================================================================================
"""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.main import app
from api.schemas.line_alert_notifications import (
    AlertNotificationTargetCreate,
    LineAlertNotificationConfig,
)
import services.line_alert_notification_service as alert_service
from services.line_alert_notification_service import (
    AlertNotificationPermissionError,
    bind_notification_group,
    get_alert_delivery,
    process_due_alert_deliveries,
    process_snapshot_fallback_notifications,
    stage_monitor_alert_deliveries,
)
from services.db_service import get_connection


ROOT = Path(__file__).resolve().parent.parent


class _BindingCursor:
    def __init__(self, rows: list[dict | None]):
        self.rows = list(rows)
        self.executed: list[tuple[str, tuple | None]] = []
        self.rowcount = 1

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0)


def test_schema_and_config_register_alert_notification_contract():
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    migration = (
        ROOT / "db" / "schema_parts" / "106_line_alert_notifications.sql"
    ).read_text(encoding="utf-8")
    config = json.loads(
        (ROOT / "config" / "line_alert_notifications.json").read_text(
            encoding="utf-8"
        )
    )

    assert "CREATE TABLE IF NOT EXISTS line_alert_notification_targets" in schema
    assert "CREATE TABLE IF NOT EXISTS line_alert_deliveries" in schema
    assert "idempotency_key" in migration
    assert LineAlertNotificationConfig.model_validate(config).enabled is True


def test_target_schema_does_not_mix_person_and_group_identity():
    user = AlertNotificationTargetCreate(
        target_type="user",
        admin_user_id=10,
        display_name="測試管理員",
    )
    group = AlertNotificationTargetCreate(
        target_type="group",
        line_target_id="C-test-group",
        display_name="測試群組",
    )

    assert user.line_target_id is None
    assert group.admin_user_id is None
    with pytest.raises(ValidationError):
        AlertNotificationTargetCreate(
            target_type="user",
            admin_user_id=10,
            line_target_id="U-should-not-be-stored-here",
            display_name="錯誤資料",
        )


def test_group_binding_requires_linked_line_manager():
    denied_cursor = _BindingCursor(
        [{"id": 2, "display_name": "一般服務人員", "role": "line_agent"}]
    )
    with pytest.raises(AlertNotificationPermissionError):
        bind_notification_group(
            denied_cursor,
            group_id="C-denied",
            actor_line_user_id="U-agent",
        )

    target = {
        "id": 5,
        "target_type": "group",
        "admin_user_id": None,
        "line_target_id": "C-allowed",
        "display_name": "系統異常通知群組",
        "minimum_severity": "critical",
        "notify_recovery": True,
        "enabled": True,
        "resolved_line_target_id": "C-allowed",
    }
    allowed_cursor = _BindingCursor(
        [
            {"id": 1, "display_name": "主管", "role": "line_manager"},
            target,
        ]
    )
    result = bind_notification_group(
        allowed_cursor,
        group_id="C-allowed",
        actor_line_user_id="U-manager",
    )

    assert result["line_target_id"] == "C-allowed"
    assert any("INSERT INTO line_alert_notification_targets" in sql for sql, _ in allowed_cursor.executed)


def test_manual_test_delivery_is_claimed_by_exact_id(monkeypatch):
    claimed: list[tuple[int, int | None]] = []
    finished: list[dict] = []

    def fake_claim(limit: int, *, only_delivery_id: int | None = None):
        claimed.append((limit, only_delivery_id))
        return [
            {
                "id": only_delivery_id,
                "target_type": "group",
                "line_target_id": "C-test",
                "payload_json": {"component": "測試"},
                "line_retry_key": "retry-key",
            }
        ]

    monkeypatch.setattr(alert_service, "_claim_due_deliveries", fake_claim)
    monkeypatch.setattr(
        alert_service,
        "_send_line_push",
        lambda destination, payload, retry_key: (True, False, "", ""),
    )
    monkeypatch.setattr(
        alert_service,
        "_finish_delivery",
        lambda item, **result: finished.append({"item": item, **result}),
    )

    processed = process_due_alert_deliveries(limit=1, only_delivery_id=77)

    assert processed == 1
    assert claimed == [(1, 77)]
    assert finished[0]["item"]["id"] == 77
    assert finished[0]["success"] is True


def test_disabled_config_stops_automatic_delivery(monkeypatch):
    monkeypatch.setattr(
        alert_service,
        "load_alert_notification_config",
        lambda: LineAlertNotificationConfig(enabled=False),
    )
    monkeypatch.setattr(
        alert_service,
        "_claim_due_deliveries",
        lambda *args, **kwargs: pytest.fail("停用時不應領取自動派送"),
    )

    assert process_due_alert_deliveries() == 0


def test_database_outage_fallback_sends_once_and_then_recovery(monkeypatch, tmp_path):
    target_cache = tmp_path / "targets.json"
    fallback_state = tmp_path / "fallback.json"
    target_cache.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "id": 8,
                        "target_type": "group",
                        "display_name": "測試群組",
                        "minimum_severity": "critical",
                        "notify_recovery": True,
                        "resolved_line_target_id": "C-fallback",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    sent: list[dict] = []
    monkeypatch.setattr(alert_service, "TARGET_CACHE_PATH", target_cache)
    monkeypatch.setattr(alert_service, "FALLBACK_STATE_PATH", fallback_state)
    monkeypatch.setattr(
        alert_service,
        "load_alert_notification_config",
        lambda: LineAlertNotificationConfig(enabled=True, notify_recovery=True),
    )
    monkeypatch.setattr(
        alert_service,
        "_send_line_push",
        lambda destination, payload, retry_key: (
            sent.append(
                {
                    "destination": destination,
                    "transition": payload["transition"],
                    "retry_key": retry_key,
                }
            )
            is None,
            False,
            "",
            "",
        ),
    )
    critical = {
        "checks": {
            "database": {
                "component": "資料庫",
                "status": "critical",
                "message": "MySQL 無法連線",
                "checked_at": "2026-08-02T01:00:00",
                "status_changed_at": "2026-08-02T01:00:00",
                "consecutive_failures": 3,
            }
        }
    }
    healthy = {
        "checks": {
            "database": {
                "component": "資料庫",
                "status": "healthy",
                "message": "MySQL 已恢復",
                "checked_at": "2026-08-02T01:05:00",
            }
        }
    }

    assert process_snapshot_fallback_notifications(critical) == 1
    assert process_snapshot_fallback_notifications(critical) == 0
    assert process_snapshot_fallback_notifications(healthy) == 1
    assert [item["transition"] for item in sent] == ["opened", "recovered"]
    assert sent[0]["destination"] == "C-fallback"


def test_database_delivery_is_idempotent_and_records_recovery(monkeypatch):
    suffix = uuid.uuid4().hex
    group_id = f"C-alert-pytest-{suffix}"
    fingerprint = f"pytest-line-alert-{suffix}"
    conn = get_connection()
    target_id = None
    alert_id = None
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO line_alert_notification_targets (
                    target_type,line_target_id,display_name,minimum_severity,
                    notify_recovery,enabled,verified_at
                ) VALUES ('group',%s,'pytest 異常通知群組','critical',TRUE,TRUE,UTC_TIMESTAMP())
                """,
                (group_id,),
            )
            target_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO service_monitor_alerts (
                    event_type,description,status,component,severity,fingerprint,
                    first_detected_at,last_detected_at,occurrence_count
                ) VALUES (
                    'line_health','pytest 模擬服務異常','pending','api','critical',%s,
                    UTC_TIMESTAMP(),UTC_TIMESTAMP(),3
                )
                """,
                (fingerprint,),
            )
            alert_id = int(cursor.lastrowid)
        conn.commit()

        stage_monitor_alert_deliveries()
        stage_monitor_alert_deliveries()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id,status FROM line_alert_deliveries
                WHERE monitor_alert_id=%s AND target_id=%s AND transition='opened'
                """,
                (alert_id, target_id),
            )
            opened = cursor.fetchall()
        assert len(opened) == 1

        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "mock_token")
        assert process_due_alert_deliveries(
            limit=1,
            only_delivery_id=int(opened[0]["id"]),
        ) == 1
        assert get_alert_delivery(int(opened[0]["id"]))["status"] == "sent"

        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE service_monitor_alerts
                SET status='resolved',resolved_at=UTC_TIMESTAMP(),resolved_by='pytest'
                WHERE id=%s
                """,
                (alert_id,),
            )
        conn.commit()
        stage_monitor_alert_deliveries()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS total FROM line_alert_deliveries
                WHERE monitor_alert_id=%s AND target_id=%s AND transition='recovered'
                """,
                (alert_id, target_id),
            )
            assert int(cursor.fetchone()["total"]) == 1
    finally:
        with conn.cursor() as cursor:
            if alert_id is not None:
                cursor.execute("DELETE FROM service_monitor_alerts WHERE id=%s", (alert_id,))
            if target_id is not None:
                cursor.execute(
                    "DELETE FROM line_alert_notification_targets WHERE id=%s",
                    (target_id,),
                )
        conn.commit()
        conn.close()


def test_line_management_ui_uses_staff_friendly_group_binding_instructions():
    source = (
        ROOT / "ui" / "components" / "line_alert_notification_manager.py"
    ).read_text(encoding="utf-8")
    webhook = (ROOT / "line" / "line_bot.py").read_text(encoding="utf-8")

    assert "綁定異常通知群組" in source
    assert "綁定異常通知群組" in webhook
    assert "解除異常通知群組" in webhook
    assert "line_target_id = st." not in source


def test_alert_notification_routes_require_internal_key(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENABLE_ADMIN_AUTH", "false")
    monkeypatch.setenv("INTERNAL_API_KEY", "alert-notification-test-key")

    with TestClient(app) as client:
        unauthorized = client.get("/api/v1/line/alert-notifications/config")
        assert unauthorized.status_code == 401

        headers = {"X-Internal-API-Key": "alert-notification-test-key"}
        config = client.get(
            "/api/v1/line/alert-notifications/config",
            headers=headers,
        )
        targets = client.get(
            "/api/v1/line/alert-notifications/targets",
            headers=headers,
        )

    assert config.status_code == 200
    assert config.json()["data"]["config"]["enabled"] is True
    assert targets.status_code == 200
