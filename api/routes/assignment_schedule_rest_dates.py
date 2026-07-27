"""
================================================================================
檔案名稱: api/routes/assignment_schedule_rest_dates.py
功能說明: 以 assignment_id 為唯一權屬之月嫂排休更新 API 路由 (AssignmentScheduleRestDateRouter)
================================================================================
"""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Path

from api.schemas.base import BaseResponse
from api.schemas.orders import AssignmentRestDatesUpdateRequest
from services import assignment_schedule_rest_date_service

router = APIRouter(
    prefix="/api/v1/assignment-schedules",
    tags=["Assignment Schedules 排班與排休"],
)


def _http_status_for_service_result(result: Dict[str, Any]) -> int:
    match result.get("status"):
        case "not_found":
            return 404
        case "validation_error":
            return 422
        case "conflict" | "locked":
            return 409
        case "ok":
            return 200
    return 500


def _is_successful(result: Dict[str, Any]) -> bool:
    return bool(result.get("success")) and result.get("status") == "ok"


@router.put("/{assignment_id}/rest-dates", response_model=BaseResponse[Dict[str, Any]])
def save_assignment_rest_dates(
    req: AssignmentRestDatesUpdateRequest,
    assignment_id: int = Path(..., description="服務指派識別碼 assignment_id"),
):
    """更新特定指派 (assignment_id) 之排休與動態順延完工日"""
    try:
        result = assignment_schedule_rest_date_service.save_assignment_rest_dates(
            assignment_id=assignment_id,
            rest_dates=req.rest_dates,
        )
        if not _is_successful(result):
            status_code = _http_status_for_service_result(result)
            raise HTTPException(status_code=status_code, detail=result.get("message"))
        return BaseResponse(data=result, message="成功更新指派排休與動態完工日")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
