"""
================================================================================
檔案名稱: api/routes/order_schedule_calculation.py
功能說明: 出勤排班與順延完工日精算 API 路由 (OrderScheduleCalculationRouter)
================================================================================
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any
from api.schemas.base import BaseResponse
from api.schemas.orders import ScheduleCalculationRequest
from services import order_schedule_calculation_service

router = APIRouter(prefix="/api/v1/orders", tags=["Orders 訂單與排班精算"])

@router.post("/calculate-schedule", response_model=BaseResponse[Dict[str, Any]])
def calculate_schedule(req: ScheduleCalculationRequest):
    """精算服務人員出勤日、扣除排休與國定假日順延完工日"""
    try:
        res = order_schedule_calculation_service.calculate_order_attendance_schedule(
            actual_start_date=req.actual_start_date,
            target_service_days=req.target_service_days,
            service_mode=req.service_mode,
            custom_holiday_rest_dates=req.custom_holiday_rest_dates,
            custom_leave_dates=req.custom_leave_dates,
            custom_rest_weekdays=req.custom_rest_weekdays,
            monthly_salary_base=req.monthly_salary_base,
        )
        return BaseResponse(data=res, message="成功完成排班與順延完工日試算")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
