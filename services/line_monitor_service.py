"""
================================================================================
檔案名稱: services/line_monitor_service.py
功能說明: 主動健康監控排程、狀態防抖、DB／本機快照保存及異常事件生命週期
================================================================================
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymysql

from services.db_service import get_connection
from services.line_health_checks import CHECK_FUNCTIONS, HealthCheckResult


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "line_monitoring.json"
STATE_DIR = PROJECT_ROOT / ".monitor_state"
SNAPSHOT_PATH = STATE_DIR / "line_health.json"
ABNORMAL_STATUSES = {"warning", "critical"}
DEFAULT_MONITORING_CONFIG = {
    "monitor_interval_seconds": 15,
    "failure_threshold": 3,
    "recovery_threshold": 2,
    "checks": {},
}


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def load_monitoring_config() -> dict[str, Any]:
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("monitoring config must be an object")
        return loaded
    except (OSError, ValueError, json.JSONDecodeError):
        # 設定檔本身故障時仍以安全預設值維持監控；config check 會回報該錯誤。
        return dict(DEFAULT_MONITORING_CONFIG)


def monitor_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def record_service_heartbeat(
    service_name: str,
    instance_id: str,
    *,
    status: str = "healthy",
    details: dict[str, Any] | None = None,
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO service_heartbeats (
                    service_name,instance_id,status,started_at,last_seen_at,details_json
                ) VALUES (%s,%s,%s,UTC_TIMESTAMP(),UTC_TIMESTAMP(),%s)
                ON DUPLICATE KEY UPDATE status=VALUES(status),last_seen_at=UTC_TIMESTAMP(),
                    details_json=VALUES(details_json)
                """,
                (service_name, instance_id, status, json.dumps(details or {}, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


def get_latest_service_heartbeat(service_name: str) -> dict[str, Any] | None:
    """Return the newest heartbeat, including parsed details, for peer supervision."""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT service_name,instance_id,status,started_at,last_seen_at,details_json
                FROM service_heartbeats
                WHERE service_name=%s
                ORDER BY last_seen_at DESC LIMIT 1
                """,
                (service_name,),
            )
            row = cursor.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    details = row.get("details_json") or {}
    if isinstance(details, str):
        details = json.loads(details)
    return {
        **row,
        "started_at": _iso(row.get("started_at")),
        "last_seen_at": _iso(row.get("last_seen_at")),
        "details": details,
    }


def record_supervisor_event(
    service_name: str,
    state: str,
    description: str,
    *,
    severity: str = "warning",
    details: dict[str, Any] | None = None,
) -> None:
    """Record one supervised-process outage/restart lifecycle in system alerts.

    The development supervisor calls this on a best-effort basis.  It must never
    prevent a child service from being restarted when the database is itself
    unavailable.
    """
    fingerprint = f"service-supervisor:{service_name}"
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            if state == "recovered":
                cursor.execute(
                    """
                    UPDATE system_alerts
                    SET status='resolved',resolved_at=UTC_TIMESTAMP(),
                        resolved_by='development_supervisor',
                        last_detected_at=UTC_TIMESTAMP(),description=%s,
                        details_json=%s
                    WHERE fingerprint=%s AND status='pending'
                    """,
                    (
                        description,
                        json.dumps(details or {}, ensure_ascii=False),
                        fingerprint,
                    ),
                )
            else:
                cursor.execute(
                    """
                    SELECT id FROM system_alerts
                    WHERE fingerprint=%s AND status='pending'
                    ORDER BY id DESC LIMIT 1 FOR UPDATE
                    """,
                    (fingerprint,),
                )
                pending = cursor.fetchone()
                serialized = json.dumps(
                    {"state": state, **(details or {})},
                    ensure_ascii=False,
                )
                normalized_severity = "critical" if severity == "critical" else "warning"
                if pending:
                    cursor.execute(
                        """
                        UPDATE system_alerts
                        SET severity=%s,description=%s,last_detected_at=UTC_TIMESTAMP(),
                            occurrence_count=occurrence_count+1,details_json=%s
                        WHERE id=%s
                        """,
                        (normalized_severity, description, serialized, pending["id"]),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO system_alerts (
                            event_type,description,status,component,severity,fingerprint,
                            first_detected_at,last_detected_at,details_json
                        ) VALUES (
                            'service_supervisor',%s,'pending',%s,%s,%s,
                            UTC_TIMESTAMP(),UTC_TIMESTAMP(),%s
                        )
                        """,
                        (
                            description,
                            service_name,
                            normalized_severity,
                            fingerprint,
                            serialized,
                        ),
                    )
        conn.commit()
    finally:
        conn.close()


def read_snapshot() -> dict[str, Any]:
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"generated_at": None, "overall_status": "unknown", "checks": {}}


def _write_snapshot(snapshot: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix="line_health_", suffix=".json", dir=STATE_DIR)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        os.replace(temp_name, SNAPSHOT_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _effective_status(previous: dict[str, Any] | None, raw_status: str, config: dict[str, Any]) -> tuple[str, int, int]:
    previous = previous or {}
    failures = int(previous.get("consecutive_failures") or 0)
    successes = int(previous.get("consecutive_successes") or 0)
    previous_status = previous.get("status", "unknown")
    if raw_status == "healthy":
        failures = 0
        successes += 1
        if previous_status in ABNORMAL_STATUSES and successes < int(config.get("recovery_threshold", 2)):
            return "warning", failures, successes
        return "healthy", failures, successes
    if raw_status == "critical":
        successes = 0
        failures += 1
        if failures < int(config.get("failure_threshold", 3)):
            return "warning", failures, successes
        return "critical", failures, successes
    if raw_status == "warning":
        return "warning", failures + 1, 0
    return raw_status, failures, successes


def _upsert_alert(cursor, check_name: str, result: dict[str, Any], previous_status: str | None) -> None:
    current_status = result["status"]
    fingerprint = f"line-monitor:{check_name}"
    if current_status in ABNORMAL_STATUSES:
        cursor.execute(
            "SELECT id FROM system_alerts WHERE fingerprint=%s AND status='pending' ORDER BY id DESC LIMIT 1 FOR UPDATE",
            (fingerprint,),
        )
        pending = cursor.fetchone()
        details = json.dumps(result.get("details") or {}, ensure_ascii=False)
        if pending:
            cursor.execute(
                """
                UPDATE system_alerts SET severity=%s,description=%s,last_detected_at=UTC_TIMESTAMP(),
                    occurrence_count=occurrence_count+1,details_json=%s WHERE id=%s
                """,
                (current_status, result["message"], details, pending["id"]),
            )
        elif previous_status != current_status:
            cursor.execute(
                """
                INSERT INTO system_alerts (
                    event_type,description,status,component,severity,fingerprint,
                    first_detected_at,last_detected_at,details_json
                ) VALUES ('line_health',%s,'pending',%s,%s,%s,UTC_TIMESTAMP(),UTC_TIMESTAMP(),%s)
                """,
                (result["message"], result["component"], current_status, fingerprint, details),
            )
    elif current_status == "healthy" and previous_status in ABNORMAL_STATUSES:
        cursor.execute(
            """
            UPDATE system_alerts SET status='resolved',resolved_at=UTC_TIMESTAMP(),resolved_by='monitor',
                last_detected_at=UTC_TIMESTAMP()
            WHERE fingerprint=%s AND status='pending'
            """,
            (fingerprint,),
        )


def persist_check_result(result: HealthCheckResult, config: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    previous = (snapshot.get("checks") or {}).get(result.check_name)
    effective, failures, successes = _effective_status(previous, result.status, config)
    changed_at = result.checked_at if not previous or previous.get("status") != effective else previous.get("status_changed_at", result.checked_at)
    current = {
        **result.to_dict(),
        "raw_status": result.status,
        "status": effective,
        "consecutive_failures": failures,
        "consecutive_successes": successes,
        "last_success_at": result.checked_at if result.status == "healthy" else (previous or {}).get("last_success_at"),
        "status_changed_at": changed_at,
    }
    try:
        conn = get_connection()
        try:
            conn.begin()
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute("SELECT status FROM system_health_status WHERE check_name=%s FOR UPDATE", (result.check_name,))
                db_previous = cursor.fetchone()
                previous_status = db_previous["status"] if db_previous else (previous or {}).get("status")
                cursor.execute(
                    """
                    INSERT INTO system_health_status (
                        check_name,component,status,raw_status,message,response_ms,
                        consecutive_failures,consecutive_successes,last_checked_at,last_success_at,
                        status_changed_at,details_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE component=VALUES(component),status=VALUES(status),
                        raw_status=VALUES(raw_status),message=VALUES(message),response_ms=VALUES(response_ms),
                        consecutive_failures=VALUES(consecutive_failures),
                        consecutive_successes=VALUES(consecutive_successes),
                        last_checked_at=VALUES(last_checked_at),last_success_at=VALUES(last_success_at),
                        status_changed_at=VALUES(status_changed_at),details_json=VALUES(details_json)
                    """,
                    (
                        result.check_name, result.component, effective, result.status, result.message,
                        result.response_ms, failures, successes, datetime.fromisoformat(result.checked_at),
                        datetime.fromisoformat(current["last_success_at"]) if current.get("last_success_at") else None,
                        datetime.fromisoformat(changed_at), json.dumps(result.details or {}, ensure_ascii=False),
                    ),
                )
                _upsert_alert(cursor, result.check_name, current, previous_status)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # DB 本身可能正是異常項目；本機原子快照仍須能保存診斷結果。
        pass
    return current


def _overall_status(checks: dict[str, dict[str, Any]]) -> str:
    statuses = {item.get("status", "unknown") for item in checks.values()}
    if "critical" in statuses:
        return "critical"
    if "warning" in statuses:
        return "warning"
    if "unknown" in statuses or not statuses:
        return "unknown"
    if statuses == {"maintenance"}:
        return "maintenance"
    return "healthy"


def run_monitor_cycle(last_run: dict[str, datetime] | None = None) -> tuple[dict[str, Any], dict[str, datetime]]:
    config = load_monitoring_config()
    last_run = last_run or {}
    now = _utc_now_naive()
    snapshot = read_snapshot()
    checks = dict(snapshot.get("checks") or {})
    for name, function in CHECK_FUNCTIONS.items():
        settings = (config.get("checks") or {}).get(name, {})
        if not settings.get("enabled", True):
            continue
        interval = int(settings.get("interval_seconds", config.get("monitor_interval_seconds", 15)))
        if name in last_run and (now - last_run[name]).total_seconds() < interval:
            continue
        try:
            result = function(settings)
        except Exception as exc:
            result = HealthCheckResult(name, name, "critical", "監控檢查發生未預期錯誤", now.isoformat(), details={"error": str(exc)})
        checks[name] = persist_check_result(result, config, {**snapshot, "checks": checks})
        last_run[name] = now
    snapshot = {"generated_at": now.isoformat(), "overall_status": _overall_status(checks), "checks": checks}
    _write_snapshot(snapshot)
    try:
        record_service_heartbeat(
            "line_monitor",
            monitor_instance_id(),
            details={"pid": os.getpid(), "overall_status": snapshot["overall_status"]},
        )
    except Exception:
        pass
    return snapshot, last_run


def get_monitoring_overview() -> dict[str, Any]:
    snapshot = read_snapshot()
    try:
        conn = get_connection()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute("SELECT * FROM system_health_status ORDER BY check_name")
                rows = cursor.fetchall()
        finally:
            conn.close()
        db_checks = {}
        for row in rows:
            details = row.get("details_json")
            if isinstance(details, str):
                details = json.loads(details)
            db_checks[row["check_name"]] = {
                **row,
                "last_checked_at": _iso(row.get("last_checked_at")),
                "last_success_at": _iso(row.get("last_success_at")),
                "status_changed_at": _iso(row.get("status_changed_at")),
                "checked_at": _iso(row.get("last_checked_at")),
                "details": details or {},
            }
        if db_checks:
            latest_db = max((item.get("checked_at") or "" for item in db_checks.values()), default="")
            if latest_db >= str(snapshot.get("generated_at") or ""):
                snapshot = {"generated_at": latest_db, "overall_status": _overall_status(db_checks), "checks": db_checks}
    except Exception:
        pass
    generated = snapshot.get("generated_at")
    stale = True
    if generated:
        try:
            stale = (_utc_now_naive() - datetime.fromisoformat(generated)).total_seconds() > 90
        except ValueError:
            pass
    return {**snapshot, "monitor_stale": stale}


def list_monitoring_events(limit: int = 100) -> list[dict[str, Any]]:
    try:
        conn = get_connection()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT id,event_type,description,status,component,severity,fingerprint,
                           first_detected_at,last_detected_at,occurrence_count,resolved_at,resolved_by
                    FROM system_alerts
                    WHERE event_type IN ('line_health','service_supervisor')
                    ORDER BY COALESCE(last_detected_at,created_at) DESC LIMIT %s
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
            return [{key: _iso(value) if isinstance(value, datetime) else value for key, value in row.items()} for row in rows]
        finally:
            conn.close()
    except Exception:
        # DB 可能正是異常元件；總覽仍應能使用本機快照顯示狀態。
        return []
