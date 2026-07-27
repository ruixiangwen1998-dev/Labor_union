from fastapi import APIRouter, Depends, HTTPException, Path
from typing import List, Dict, Any
from datetime import date
from api.dependencies.admin_auth import require_system_admin
from services import db_service
from services.admin_auth_service import AdminPrincipal
from api.schemas.base import BaseResponse
from api.schemas.holidays import HolidayCreateRequest

router = APIRouter(prefix="/api/v1/holidays", tags=["Holidays 國定假日設定"])

@router.get("", response_model=BaseResponse[List[Dict[str, Any]]])
def get_all_holidays(
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """取得中華民國國定假日設定列表"""
    try:
        data = db_service.get_table_data("holidays")
        return BaseResponse(data=data, message="成功取得國定假日列表")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("", response_model=BaseResponse[bool])
def add_or_update_holiday(
    req: HolidayCreateRequest,
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """新增或更新國定假日"""
    try:
        success = db_service.add_or_update_holiday(
            holiday_date=req.holiday_date,
            holiday_name=req.holiday_name,
            is_double_pay_default=req.is_double_pay_default
        )
        return BaseResponse(data=success, message="成功儲存國定假日")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{holiday_date}", response_model=BaseResponse[bool])
def delete_holiday(
    holiday_date: date = Path(..., description="假日日期 (YYYY-MM-DD)"),
    principal: AdminPrincipal = Depends(require_system_admin),
):
    """刪除指定國定假日"""
    try:
        success = db_service.delete_holiday(holiday_date)
        return BaseResponse(data=success, message="成功刪除國定假日")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
