"""
================================================================================
檔案名稱: services/line_health_checks.py
功能說明: 主動監控單項檢查，涵蓋 API、DB 連線與結構、Worker、任務、LINE、LIFF、服務監督器、設定與儲存空間
================================================================================
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pymysql
import requests

from services.db_service import get_connection
from services.runtime_supervision_service import intentional_shutdown_requested


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LINE_BOT_INFO_URL = "https://api.line.me/v2/bot/info"


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(slots=True)
class HealthCheckResult:
    check_name: str
    component: str
    status: str
    message: str
    checked_at: str
    response_ms: int | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _timed(check: Callable[[], tuple[str, str, dict[str, Any] | None]]) -> tuple[str, str, dict[str, Any] | None, int]:
    started = time.perf_counter()
    status, message, details = check()
    elapsed = int((time.perf_counter() - started) * 1000)
    return status, message, details, elapsed


def check_api(settings: dict[str, Any]) -> HealthCheckResult:
    url = os.getenv("MONITOR_API_URL", "http://127.0.0.1:8000/health").strip()

    def execute():
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return "healthy", "FastAPI 回應正常", {"url": url, "http_status": response.status_code}

    try:
        status, message, details, elapsed = _timed(execute)
        if elapsed >= int(settings.get("warning_response_ms", 1500)):
            status, message = "warning", "FastAPI 可以連線，但回應速度偏慢"
    except requests.RequestException as exc:
        status, message, details, elapsed = "critical", "FastAPI 無法連線", {"error": str(exc)}, None
    return HealthCheckResult("api", "FastAPI", status, message, _utc_now_naive().isoformat(), elapsed, details)


def check_database(settings: dict[str, Any]) -> HealthCheckResult:
    def execute():
        conn = get_connection()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute("SELECT 1 AS ok")
                ok = bool(cursor.fetchone()["ok"])
                cursor.execute("SELECT COUNT(*) AS total FROM line_tasks")
                task_total = int(cursor.fetchone()["total"])
        finally:
            conn.close()
        return ("healthy" if ok else "critical"), "MySQL 連線與查詢正常", {"line_task_total": task_total}

    try:
        status, message, details, elapsed = _timed(execute)
        if elapsed >= int(settings.get("warning_response_ms", 1000)):
            status, message = "warning", "MySQL 可以查詢，但回應速度偏慢"
    except Exception as exc:
        status, message, details, elapsed = "critical", "MySQL 無法連線或查詢", {"error": str(exc)}, None
    return HealthCheckResult("database", "資料庫", status, message, _utc_now_naive().isoformat(), elapsed, details)


REQUIRED_MONITORING_SCHEMA = {
    "system_alerts": {"alert_code"},
    "service_monitor_alerts": {"event_type"},
    "service_heartbeats": {"service_name"},
    "system_health_status": {"check_name"},
}


def check_database_schema(_settings: dict[str, Any]) -> HealthCheckResult:
    """Verify that the running database matches the monitoring code contract."""
    try:
        conn = get_connection()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                placeholders = ",".join(["%s"] * len(REQUIRED_MONITORING_SCHEMA))
                cursor.execute(
                    f"""
                    SELECT TABLE_NAME,COLUMN_NAME
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=DATABASE()
                      AND TABLE_NAME IN ({placeholders})
                    """,
                    tuple(REQUIRED_MONITORING_SCHEMA),
                )
                rows = cursor.fetchall()
        finally:
            conn.close()

        available: dict[str, set[str]] = {}
        for row in rows:
            available.setdefault(row["TABLE_NAME"], set()).add(row["COLUMN_NAME"])
        missing = [
            f"{table}.{column}"
            for table, columns in REQUIRED_MONITORING_SCHEMA.items()
            for column in sorted(columns - available.get(table, set()))
        ]
        details = {
            "required_tables": sorted(REQUIRED_MONITORING_SCHEMA),
            "missing_requirements": missing,
        }
        if missing:
            return HealthCheckResult(
                "database_schema",
                "資料庫結構",
                "critical",
                "資料庫結構與目前程式版本不一致",
                _utc_now_naive().isoformat(),
                details=details,
            )
        return HealthCheckResult(
            "database_schema",
            "資料庫結構",
            "healthy",
            "監控所需資料表與欄位完整",
            _utc_now_naive().isoformat(),
            details=details,
        )
    except Exception as exc:
        return HealthCheckResult(
            "database_schema",
            "資料庫結構",
            "unknown",
            "目前無法檢查資料庫結構",
            _utc_now_naive().isoformat(),
            details={"error": str(exc)},
        )


def check_worker(settings: dict[str, Any]) -> HealthCheckResult:
    stale_after = int(settings.get("stale_after_seconds", 45))
    progress_stale_after = int(settings.get("progress_stale_after_seconds", 90))
    try:
        conn = get_connection()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT instance_id,status,last_seen_at,details_json
                    FROM service_heartbeats
                    WHERE service_name='line_worker'
                    ORDER BY last_seen_at DESC LIMIT 1
                    """
                )
                row = cursor.fetchone()
        finally:
            conn.close()
        if not row:
            return HealthCheckResult("worker", "自動發送", "unknown", "尚未收到 Worker 心跳", _utc_now_naive().isoformat())
        age_seconds = max(0, int((_utc_now_naive() - row["last_seen_at"]).total_seconds()))
        heartbeat_details = row.get("details_json") or {}
        if isinstance(heartbeat_details, str):
            heartbeat_details = json.loads(heartbeat_details)
        details = {"instance_id": row["instance_id"], "last_seen_at": row["last_seen_at"].isoformat(), "age_seconds": age_seconds, **heartbeat_details}
        if age_seconds > stale_after:
            return HealthCheckResult("worker", "自動發送", "critical", f"Worker 心跳已中斷 {age_seconds} 秒", _utc_now_naive().isoformat(), details=details)
        if row["status"] != "healthy":
            return HealthCheckResult("worker", "自動發送", "warning", "Worker 回報非正常狀態", _utc_now_naive().isoformat(), details=details)
        last_cycle_at = heartbeat_details.get("last_cycle_at")
        if last_cycle_at:
            try:
                cycle_age = int((_utc_now_naive() - datetime.fromisoformat(last_cycle_at)).total_seconds())
                details["cycle_age_seconds"] = max(0, cycle_age)
                if cycle_age > progress_stale_after:
                    return HealthCheckResult("worker", "自動發送", "critical", "Worker 程序仍在，但工作迴圈長時間沒有進度", _utc_now_naive().isoformat(), details=details)
            except ValueError:
                pass
        return HealthCheckResult("worker", "自動發送", "healthy", "Worker 心跳正常", _utc_now_naive().isoformat(), details=details)
    except Exception as exc:
        return HealthCheckResult("worker", "自動發送", "unknown", "目前無法讀取 Worker 心跳", _utc_now_naive().isoformat(), details={"error": str(exc)})


def check_development_supervisor(settings: dict[str, Any]) -> HealthCheckResult:
    """Detect a hung dev supervisor while Monitor itself is still running."""
    enabled = os.getenv("ENABLE_DEVELOPMENT_SUPERVISOR_CHECK", "false").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return HealthCheckResult(
            "development_supervisor",
            "開發啟動監督器",
            "maintenance",
            "目前不是由開發啟動監督器管理",
            _utc_now_naive().isoformat(),
        )
    if intentional_shutdown_requested("development_supervisor"):
        return HealthCheckResult(
            "development_supervisor",
            "開發啟動監督器",
            "maintenance",
            "服務監督器已由開發者正常關閉",
            _utc_now_naive().isoformat(),
        )
    stale_after = int(settings.get("stale_after_seconds", 45))
    try:
        conn = get_connection()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT instance_id,last_seen_at,details_json
                    FROM service_heartbeats
                    WHERE service_name='development_supervisor'
                    ORDER BY last_seen_at DESC LIMIT 1
                    """
                )
                row = cursor.fetchone()
        finally:
            conn.close()
        if not row:
            return HealthCheckResult(
                "development_supervisor",
                "開發啟動監督器",
                "unknown",
                "尚未收到開發啟動監督器心跳",
                _utc_now_naive().isoformat(),
            )
        age_seconds = max(0, int((_utc_now_naive() - row["last_seen_at"]).total_seconds()))
        details = row.get("details_json") or {}
        if isinstance(details, str):
            details = json.loads(details)
        result_details = {
            "instance_id": row["instance_id"],
            "last_seen_at": row["last_seen_at"].isoformat(),
            "age_seconds": age_seconds,
            **details,
        }
        if age_seconds > stale_after:
            return HealthCheckResult(
                "development_supervisor",
                "開發啟動監督器",
                "critical",
                f"啟動監督器心跳已中斷 {age_seconds} 秒",
                _utc_now_naive().isoformat(),
                details=result_details,
            )
        return HealthCheckResult(
            "development_supervisor",
            "開發啟動監督器",
            "healthy",
            "開發啟動監督器心跳正常",
            _utc_now_naive().isoformat(),
            details=result_details,
        )
    except Exception as exc:
        return HealthCheckResult(
            "development_supervisor",
            "開發啟動監督器",
            "unknown",
            "目前無法讀取開發啟動監督器心跳",
            _utc_now_naive().isoformat(),
            details={"error": str(exc)},
        )


def check_task_queue(settings: dict[str, Any]) -> HealthCheckResult:
    overdue_minutes = int(settings.get("overdue_minutes", 5))
    stuck_minutes = int(settings.get("stuck_minutes", 10))
    failed_warning_count = int(settings.get("failed_warning_count", 1))
    try:
        conn = get_connection()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT
                      SUM(status='pending' AND scheduled_at < UTC_TIMESTAMP() - INTERVAL %s MINUTE) AS overdue,
                      SUM(status='processing' AND processing_started_at < UTC_TIMESTAMP() - INTERVAL %s MINUTE) AS stuck,
                      SUM(status='failed' AND failed_at >= UTC_TIMESTAMP() - INTERVAL 1 HOUR) AS failed_recent,
                      SUM(status='sent' AND sent_at >= UTC_TIMESTAMP() - INTERVAL 24 HOUR) AS sent_24h
                    FROM line_tasks
                    """,
                    (overdue_minutes, stuck_minutes),
                )
                row = cursor.fetchone() or {}
        finally:
            conn.close()
        details = {key: int(row.get(key) or 0) for key in ("overdue", "stuck", "failed_recent", "sent_24h")}
        if details["stuck"]:
            status, message = "critical", f"有 {details['stuck']} 筆任務長時間卡在處理中"
        elif details["overdue"] or details["failed_recent"] >= failed_warning_count:
            status, message = "warning", f"逾期 {details['overdue']} 筆，最近失敗 {details['failed_recent']} 筆"
        else:
            status, message = "healthy", "任務隊列沒有逾期、卡住或近期失敗"
        return HealthCheckResult("task_queue", "任務隊列", status, message, _utc_now_naive().isoformat(), details=details)
    except Exception as exc:
        return HealthCheckResult("task_queue", "任務隊列", "unknown", "目前無法檢查任務隊列", _utc_now_naive().isoformat(), details={"error": str(exc)})


def check_line_api(settings: dict[str, Any]) -> HealthCheckResult:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token or token.startswith("your_") or token == "mock_token":
        return HealthCheckResult("line_api", "LINE API", "critical", "尚未設定有效的 LINE Channel Access Token", _utc_now_naive().isoformat())

    def execute():
        response = requests.get(LINE_BOT_INFO_URL, headers={"Authorization": f"Bearer {token}"}, timeout=8)
        if response.status_code in {401, 403}:
            return "critical", "LINE Channel Access Token 無效或權限不足", {"http_status": response.status_code}
        response.raise_for_status()
        body = response.json()
        return "healthy", "LINE Messaging API 驗證正常", {"http_status": response.status_code, "basic_id": body.get("basicId")}

    try:
        status, message, details, elapsed = _timed(execute)
        if status == "healthy" and elapsed >= int(settings.get("warning_response_ms", 2500)):
            status, message = "warning", "LINE API 可以連線，但回應速度偏慢"
    except (requests.RequestException, ValueError) as exc:
        status, message, details, elapsed = "critical", "無法連線至 LINE Messaging API", {"error": str(exc)}, None
    return HealthCheckResult("line_api", "LINE API", status, message, _utc_now_naive().isoformat(), elapsed, details)


def check_public_endpoint(settings: dict[str, Any]) -> HealthCheckResult:
    base_url = os.getenv("BASE_URL", "").strip().rstrip("/")
    if not base_url or "127.0.0.1" in base_url or "localhost" in base_url:
        return HealthCheckResult("public_endpoint", "公開入口", "unknown", "尚未設定可由外部存取的 BASE_URL", _utc_now_naive().isoformat())
    url = f"{base_url}/health"
    try:
        started = time.perf_counter()
        response = requests.get(url, timeout=8)
        elapsed = int((time.perf_counter() - started) * 1000)
        response.raise_for_status()
        status = "warning" if elapsed >= int(settings.get("warning_response_ms", 3000)) else "healthy"
        message = "公開入口回應偏慢" if status == "warning" else "公開入口可正常連線"
        details = {"url": url, "http_status": response.status_code}
    except requests.RequestException as exc:
        status, message, details, elapsed = "critical", "公開網址或 Tunnel 無法連線", {"url": url, "error": str(exc)}, None
    return HealthCheckResult("public_endpoint", "公開入口", status, message, _utc_now_naive().isoformat(), elapsed, details)


def check_liff(_settings: dict[str, Any]) -> HealthCheckResult:
    liff_id = os.getenv("LINE_LIFF_ID", "").strip()
    if not liff_id or liff_id.startswith("your_"):
        return HealthCheckResult("liff", "LINE 表單", "critical", "尚未設定有效的 LINE_LIFF_ID", _utc_now_naive().isoformat())
    base_url = os.getenv("BASE_URL", "").strip().rstrip("/")
    if not base_url:
        return HealthCheckResult("liff", "LINE 表單", "warning", "LIFF ID 已設定，但 BASE_URL 尚未設定", _utc_now_naive().isoformat())
    try:
        response = requests.get(f"{base_url}/gateway", timeout=8)
        response.raise_for_status()
        return HealthCheckResult("liff", "LINE 表單", "healthy", "LIFF Gateway 可以載入", _utc_now_naive().isoformat(), details={"http_status": response.status_code})
    except requests.RequestException as exc:
        return HealthCheckResult("liff", "LINE 表單", "critical", "LIFF Gateway 無法載入", _utc_now_naive().isoformat(), details={"error": str(exc)})


def check_config(_settings: dict[str, Any]) -> HealthCheckResult:
    files = ["message_templates.json", "message_schedules.json", "line_menu.json", "liff_settings.json", "customer_service.json", "line_monitoring.json"]
    errors: list[str] = []
    for filename in files:
        path = PROJECT_ROOT / "config" / filename
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{filename}: {exc}")
    if errors:
        return HealthCheckResult("config", "設定檔", "critical", f"有 {len(errors)} 個設定檔無法讀取", _utc_now_naive().isoformat(), details={"errors": errors})
    return HealthCheckResult("config", "設定檔", "healthy", "LINE 設定檔格式正常", _utc_now_naive().isoformat(), details={"checked_files": files})


def check_storage(settings: dict[str, Any]) -> HealthCheckResult:
    usage = shutil.disk_usage(PROJECT_ROOT)
    percent = round((usage.used / usage.total) * 100, 1)
    warning = float(settings.get("warning_percent", 85))
    critical = float(settings.get("critical_percent", 95))
    if percent >= critical:
        status, message = "critical", f"主機磁碟使用率已達 {percent}%"
    elif percent >= warning:
        status, message = "warning", f"主機磁碟使用率為 {percent}%"
    else:
        status, message = "healthy", f"主機磁碟使用率為 {percent}%"
    return HealthCheckResult("storage", "主機儲存空間", status, message, _utc_now_naive().isoformat(), details={"used_percent": percent, "free_bytes": usage.free})


CHECK_FUNCTIONS: dict[str, Callable[[dict[str, Any]], HealthCheckResult]] = {
    "api": check_api,
    "database": check_database,
    "database_schema": check_database_schema,
    "worker": check_worker,
    "development_supervisor": check_development_supervisor,
    "task_queue": check_task_queue,
    "line_api": check_line_api,
    "public_endpoint": check_public_endpoint,
    "liff": check_liff,
    "config": check_config,
    "storage": check_storage,
}
