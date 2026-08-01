"""
================================================================================
檔案名稱: tests/test_line_monitoring.py
功能說明: 主動監控、心跳、防抖、管理 API、同層程序單例與雙向恢復的回歸測試
================================================================================
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app
from services.db_service import get_connection
from services.line_health_checks import (
    HealthCheckResult,
    check_development_supervisor,
    check_worker,
)
from services.line_monitor_service import (
    persist_check_result,
    record_service_heartbeat,
    record_supervisor_event,
)
from services.runtime_supervision_service import (
    SingleInstanceLock,
    clear_intentional_shutdown,
    heartbeat_pid,
    intentional_shutdown_requested,
    mark_intentional_shutdown,
)


ROOT = Path(__file__).resolve().parent.parent


def _cleanup(check_name: str, instance_id: str | None = None) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM system_alerts WHERE fingerprint=%s", (f"line-monitor:{check_name}",))
            cursor.execute("DELETE FROM system_health_status WHERE check_name=%s", (check_name,))
            if instance_id:
                cursor.execute("DELETE FROM service_heartbeats WHERE instance_id=%s", (instance_id,))
        conn.commit()
    finally:
        conn.close()


def test_schema_contains_active_monitoring_tables():
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS service_heartbeats" in schema
    assert "CREATE TABLE IF NOT EXISTS system_health_status" in schema
    assert "idx_alert_fingerprint_status" in schema


def test_worker_heartbeat_is_read_as_healthy():
    instance_id = f"pytest:{uuid.uuid4().hex}"
    try:
        record_service_heartbeat("line_worker", instance_id, details={"test": True})
        result = check_worker({"stale_after_seconds": 45})
        assert result.status == "healthy"
        assert result.details["instance_id"] == instance_id
    finally:
        _cleanup("unused", instance_id)


def test_development_supervisor_heartbeat_is_checked_only_when_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_DEVELOPMENT_SUPERVISOR_CHECK", "false")
    disabled = check_development_supervisor({"stale_after_seconds": 45})
    assert disabled.status == "maintenance"

    instance_id = f"pytest-supervisor:{uuid.uuid4().hex}"
    try:
        record_service_heartbeat("development_supervisor", instance_id, details={"test": True})
        monkeypatch.setenv("ENABLE_DEVELOPMENT_SUPERVISOR_CHECK", "true")
        enabled = check_development_supervisor({"stale_after_seconds": 45})
        assert enabled.status == "healthy"
        assert enabled.details["instance_id"] == instance_id
    finally:
        _cleanup("unused", instance_id)


def test_critical_check_is_debounced_and_recovery_resolves_alert():
    check_name = f"pytest_monitor_{uuid.uuid4().hex}"
    config = {"failure_threshold": 3, "recovery_threshold": 2}
    snapshot = {"checks": {}}
    try:
        for expected in ("warning", "warning", "critical"):
            result = HealthCheckResult(
                check_name,
                "測試元件",
                "critical",
                "測試異常",
                "2026-07-29T00:00:00",
            )
            current = persist_check_result(result, config, snapshot)
            snapshot["checks"][check_name] = current
            assert current["status"] == expected

        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT status,severity FROM system_alerts WHERE fingerprint=%s",
                    (f"line-monitor:{check_name}",),
                )
                alert = cursor.fetchone()
                assert alert == {"status": "pending", "severity": "critical"}
        finally:
            conn.close()

        for expected in ("warning", "healthy"):
            result = HealthCheckResult(
                check_name,
                "測試元件",
                "healthy",
                "已恢復",
                "2026-07-29T00:01:00",
            )
            current = persist_check_result(result, config, snapshot)
            snapshot["checks"][check_name] = current
            assert current["status"] == expected

        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT status,resolved_by FROM system_alerts WHERE fingerprint=%s",
                    (f"line-monitor:{check_name}",),
                )
                assert cursor.fetchone() == {"status": "resolved", "resolved_by": "monitor"}
        finally:
            conn.close()
    finally:
        _cleanup(check_name)


def test_monitoring_routes_are_protected_and_available(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENABLE_ADMIN_AUTH", "false")
    monkeypatch.setenv("INTERNAL_API_KEY", "monitor-test-key")
    with TestClient(app) as client:
        unauthorized = client.get("/api/v1/line/monitoring/status")
        assert unauthorized.status_code == 401
        authorized = client.get(
            "/api/v1/line/monitoring/status",
            headers={"X-Internal-API-Key": "monitor-test-key"},
        )
        assert authorized.status_code == 200
        assert "overall_status" in authorized.json()["data"]


def test_monitor_runs_as_independent_process_and_ui_has_no_fixed_polling():
    monitor = (ROOT / "line" / "monitor.py").read_text(encoding="utf-8")
    launcher = (ROOT / "start_fastapi_ngrok.py").read_text(encoding="utf-8")
    ui = (ROOT / "ui" / "components" / "line_health_monitor.py").read_text(encoding="utf-8")
    assert "run_monitor_cycle" in monitor
    assert '"-m", "line.monitor"' in launcher
    assert "st_autorefresh" not in ui
    assert "time.sleep" not in ui


def test_development_supervisor_manages_all_runtime_services():
    launcher = (ROOT / "start_fastapi_ngrok.py").read_text(encoding="utf-8")
    monitor = (ROOT / "line" / "monitor.py").read_text(encoding="utf-8")
    start_batch = (ROOT / "start.bat").read_text(encoding="utf-8")
    runtime_tools = (ROOT / "services" / "runtime_supervision_service.py").read_text(
        encoding="utf-8"
    )
    assert "def run_fastapi" in launcher
    assert "def run_ngrok" in launcher
    assert "def run_streamlit" in launcher
    assert "def run_monitor" in launcher
    assert "SERVICE_RESTART_DELAYS_SECONDS = (1, 3, 10)" in launcher
    assert "_stcore/health" in launcher
    assert "line_health.json" in launcher
    assert "streamlit run ui/app.py" not in start_batch
    assert 'start_fastapi_ngrok.py"' in start_batch
    assert "-m line.monitor" in start_batch
    assert "run_development_supervisor.bat" not in start_batch
    assert "_restart_development_supervisor" in monitor
    assert "_restart_monitor_peer" in launcher
    assert "SingleInstanceLock" in runtime_tools
    assert not (ROOT / "scripts" / "run_development_supervisor.bat").exists()


def test_peer_supervision_singleton_and_heartbeat_pid():
    lock_name = f"pytest_{uuid.uuid4().hex}"
    first = SingleInstanceLock(lock_name)
    second = SingleInstanceLock(lock_name)
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert second.acquire() is True
    second.release()
    assert heartbeat_pid({"instance_id": "host:1234", "details": {}}) == 1234
    assert heartbeat_pid({"instance_id": "host:1", "details": {"pid": 5678}}) == 5678
    marker_name = f"pytest_marker_{uuid.uuid4().hex}"
    mark_intentional_shutdown(marker_name)
    assert intentional_shutdown_requested(marker_name) is True
    clear_intentional_shutdown(marker_name)
    assert intentional_shutdown_requested(marker_name) is False


def test_supervisor_event_is_resolved_after_service_recovers():
    service_name = f"pytest_service_{uuid.uuid4().hex}"
    fingerprint = f"service-supervisor:{service_name}"
    try:
        record_supervisor_event(service_name, "unavailable", "測試服務中斷")
        record_supervisor_event(
            service_name,
            "recovered",
            "測試服務已由監督器恢復",
            details={"restart_attempt": 1},
        )
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT event_type,status,resolved_by FROM system_alerts WHERE fingerprint=%s",
                    (fingerprint,),
                )
                assert cursor.fetchone() == {
                    "event_type": "service_supervisor",
                    "status": "resolved",
                    "resolved_by": "development_supervisor",
                }
        finally:
            conn.close()
    finally:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM system_alerts WHERE fingerprint=%s", (fingerprint,))
            conn.commit()
        finally:
            conn.close()
