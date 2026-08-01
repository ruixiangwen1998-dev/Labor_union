"""
================================================================================
檔案名稱: api/routes/line_admin.py
功能說明: LINE 管理中心總覽 API，讀取主動監控快照、Worker 狀態與管理功能清單
================================================================================
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends

from api.dependencies.admin_auth import require_line_viewer
from api.schemas.base import BaseResponse
from services.line_monitor_service import get_monitoring_overview


router = APIRouter(
    prefix="/api/v1/line/admin",
    tags=["LINE Admin"],
    dependencies=[Depends(require_line_viewer)],
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _configured(name: str) -> bool:
    value = os.getenv(name, "").strip()
    return bool(value and not value.startswith("your_") and value != "mock_token")


@router.get("/health", response_model=BaseResponse[dict])
def line_admin_health():
    monitoring = get_monitoring_overview()
    checks = monitoring.get("checks", {})
    database_check = checks.get("database", {})
    worker_check = checks.get("worker", {})
    database_status = database_check.get("status", "unknown")
    database = {
        "ok": database_status == "healthy",
        "monitor_status": database_status,
        "last_checked_at": database_check.get("checked_at"),
        "details": database_check.get("details") or {},
    }
    worker_running = worker_check.get("status") in {"healthy", "warning"} and not monitoring.get("monitor_stale", True)
    overall = monitoring.get("overall_status", "unknown")
    status_text = "healthy" if overall == "healthy" else "degraded"
    return BaseResponse(
        data={
            "status": status_text,
            "database": database,
            "worker": {"running": worker_running},
            "monitoring": monitoring,
            "line_credentials": {
                "channel_secret": _configured("LINE_CHANNEL_SECRET"),
                "channel_access_token": _configured("LINE_CHANNEL_ACCESS_TOKEN"),
                "liff_id": _configured("LINE_LIFF_ID"),
            },
        }
    )


@router.get("/capabilities", response_model=BaseResponse[dict])
def line_admin_capabilities():
    return BaseResponse(
        data={
            "stage": "5.6",
            "available": {
                "health_overview": True,
                "message_template_api": True,
                "message_schedule_api": True,
                "message_schedule_editor": True,
                "line_task_admin_api": True,
                "line_task_attempt_history": True,
                "rich_menu_api": True,
                "rich_menu_editor": True,
                "rich_menu_publication_history": True,
                "liff_config_api": True,
                "liff_config_editor": True,
                "liff_runtime_config": True,
                "liff_revision_history": True,
                "customer_service_config_api": True,
                "staff_review_api": True,
                "staff_review_management": True,
                "admin_session": True,
                "role_permissions": True,
                "audit_log": True,
            },
            "planned_pages": [
                "LINE 設定中心",
                "客服入口",
                "操作紀錄",
            ],
            "config_files": {
                "message_templates": (PROJECT_ROOT / "config/message_templates.json").exists(),
                "message_schedules": (PROJECT_ROOT / "config/message_schedules.json").exists(),
                "line_menus": (PROJECT_ROOT / "config/line_menu.json").exists(),
                "liff": (PROJECT_ROOT / "config/liff_settings.json").exists(),
                "customer_service": (PROJECT_ROOT / "config/customer_service.json").exists(),
            },
        }
    )
