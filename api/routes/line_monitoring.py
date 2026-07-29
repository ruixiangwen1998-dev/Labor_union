"""
================================================================================
檔案名稱: api/routes/line_monitoring.py
功能說明: LINE 管理中心主動監控狀態與異常／恢復事件查詢 API
================================================================================
"""

from fastapi import APIRouter, Depends, Query

from api.dependencies.admin_auth import require_line_viewer
from api.schemas.base import BaseResponse
from api.schemas.line_monitoring import MonitoringOverview
from services.line_monitor_service import get_monitoring_overview, list_monitoring_events


router = APIRouter(
    prefix="/api/v1/line/monitoring",
    tags=["LINE Monitoring"],
    dependencies=[Depends(require_line_viewer)],
)


@router.get("/status", response_model=BaseResponse[MonitoringOverview])
def monitoring_status():
    return BaseResponse(data=get_monitoring_overview())


@router.get("/events", response_model=BaseResponse[list[dict]])
def monitoring_events(limit: int = Query(default=50, ge=1, le=200)):
    return BaseResponse(data=list_monitoring_events(limit))
