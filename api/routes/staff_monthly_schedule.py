"""
================================================================================
檔案名稱: api/routes/staff_monthly_schedule.py
功能說明: 月嫂月度檔期視圖 API 路由 (StaffMonthlyCalendarScheduleRouter)
================================================================================
"""

from fastapi import APIRouter, HTTPException, Path, Query
from typing import Dict, Any
from api.schemas.base import BaseResponse
from services import staff_monthly_calendar_schedule_service

router = APIRouter(prefix="/api/v1/staff", tags=["Staff 服務人員月度檔期排班"])


def _http_status_for_service_error(error_message: str) -> int:
    if "不存在" in error_message:
        return 404
    return 422


@router.get("/{staff_id}/monthly-schedule", response_model=BaseResponse[Dict[str, Any]])
def get_staff_monthly_schedule(
    staff_id: int = Path(..., description="服務人員 ID"),
    year: int = Query(..., description="年份", ge=1900, le=2100),
    month: int = Query(..., description="月份", ge=1, le=12),
):
    """取得月嫂月度檔期排班視圖 (含 days: [] 陣列與 schedule_map)"""
    try:
        data = staff_monthly_calendar_schedule_service.get_staff_monthly_calendar_schedule(
            staff_id=staff_id,
            year=year,
            month=month,
        )
        return BaseResponse(data=data, message="成功取得月嫂月度檔期排班視圖")
    except ValueError as exc:
        raise HTTPException(status_code=_http_status_for_service_error(str(exc)), detail=str(exc))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
