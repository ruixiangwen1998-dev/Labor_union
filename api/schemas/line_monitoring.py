"""
================================================================================
檔案名稱: api/schemas/line_monitoring.py
功能說明: LINE 主動監控目前狀態與異常事件 API 資料格式
================================================================================
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


HealthStatus = Literal["healthy", "warning", "critical", "unknown", "maintenance"]


class MonitoringCheck(BaseModel):
    check_name: str | None = None
    component: str
    status: HealthStatus
    raw_status: HealthStatus | None = None
    message: str
    response_ms: int | None = None
    checked_at: str | None = None
    last_success_at: str | None = None
    persistence_status: Literal["stored", "failed"] | None = None
    persistence_error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class MonitoringOverview(BaseModel):
    generated_at: str | None = None
    overall_status: HealthStatus
    monitor_stale: bool
    monitor_persistence_status: Literal["healthy", "degraded"] = "healthy"
    monitor_persistence_message: str | None = None
    checks: dict[str, MonitoringCheck]
