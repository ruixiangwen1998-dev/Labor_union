from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Dict, Any
from datetime import date
from decimal import Decimal
from services import db_service
from services.order_assignment_synchronization import (
    apply_order_assignment_sync,
    preview_order_assignment_sync,
)
from api.schemas.base import BaseResponse
from api.schemas.orders import (
    OrderFullUpdateRequest, 
    OrderStatusUpdateRequest, 
    ScheduleCalculationRequest,
)



router = APIRouter(prefix="/api/v1/orders", tags=["Orders 訂單管理"])


class OrderAssignmentSynchronizationOrderChange(BaseModel):
    """The complete non-cancellation order target owned by one sync transaction."""

    model_config = ConfigDict(extra="forbid")

    client_name: str = Field(..., min_length=1)
    service_days: int = Field(..., ge=1)
    service_hours_per_day: Decimal = Field(..., gt=0)
    floor_fee: Decimal = Field(..., ge=0)
    deposit_date: date | None = None
    start_date: date
    end_date: date
    actual_start_date: date
    actual_end_date: date


class OrderAssignmentSynchronizationPreviewRequest(BaseModel):
    order_change: OrderAssignmentSynchronizationOrderChange
    assignment_plan: List[Dict[str, Any]] = Field(..., min_length=1)


class OrderAssignmentSynchronizationApplyRequest(OrderAssignmentSynchronizationPreviewRequest):
    schedule_change_plan: Dict[str, Any]
    applied_by: str = Field(..., min_length=1)

@router.get("", response_model=BaseResponse[List[Dict[str, Any]]])
def get_all_orders():
    """取得全量訂單 36 欄位計算視圖資料清單"""
    try:
        data = db_service.get_case_order_details()
        return BaseResponse(data=data, message="成功取得全量訂單列表")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{case_no}", response_model=BaseResponse[Dict[str, Any]])
def get_order_by_case_no(case_no: str = Path(..., description="案件編號")):
    """依案件編號取得單筆訂單詳細資訊。"""
    try:
        order = db_service.get_order_by_case_no(case_no)
        if not order:
            raise HTTPException(status_code=404, detail=f"找不到案件編號為 {case_no} 的訂單")
        return BaseResponse(data=order, message="成功取得單筆訂單資訊")
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/{case_no}/full-details", response_model=BaseResponse[bool])
def update_order_full_details(
    req: OrderFullUpdateRequest,
    case_no: str = Path(..., description="案件編號")
):
    """更新單筆訂單主要資料（天數、時數、樓層費、起訖日與客戶姓名）。"""
    try:
        update_dict = req.model_dump()
        success = db_service.update_order_full_details(case_no=case_no, data=update_dict)
        return BaseResponse(data=success, message="成功更新訂單 36 欄位資料")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/{case_no}/status", response_model=BaseResponse[bool])
def update_order_status(
    req: OrderStatusUpdateRequest,
    case_no: str = Path(..., description="案件編號")
):
    """更新訂單成立狀態與取消原因"""
    try:
        success = db_service.update_order_status(
            case_no=case_no,
            status=req.status,
            cancel_reason=req.cancel_reason,
        )
        return BaseResponse(data=success, message="成功更新訂單狀態")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))






@router.post(
    "/{case_no}/assignment-synchronization/preview",
    response_model=BaseResponse[Dict[str, Any]],
)
def preview_order_assignment_synchronization(
    req: OrderAssignmentSynchronizationPreviewRequest,
    case_no: str = Path(..., description="案件編號"),
):
    """Return a read-only preview for one explicit multi-caregiver plan."""
    try:
        result = preview_order_assignment_sync(
            case_no=case_no,
            order_change=req.order_change.model_dump(),
            assignment_plan=req.assignment_plan,
        )
        return BaseResponse(data=result, message="成功取得訂單與月嫂指派同步預覽")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail="Failed to preview order assignment synchronization") from error


@router.post(
    "/{case_no}/assignment-synchronization/apply",
    response_model=BaseResponse[Dict[str, Any]],
)
def apply_order_assignment_synchronization(
    req: OrderAssignmentSynchronizationApplyRequest,
    case_no: str = Path(..., description="案件編號"),
):
    """Apply only a complete, explicitly confirmed multi-caregiver plan."""
    if not req.applied_by.strip():
        raise HTTPException(status_code=422, detail="applied_by is required")
    removal_ids = req.schedule_change_plan.get("remove_schedule_ids")
    if not isinstance(removal_ids, list):
        raise HTTPException(status_code=422, detail="remove_schedule_ids is required")

    try:
        result = apply_order_assignment_sync(
            case_no=case_no,
            order_change=req.order_change.model_dump(),
            assignment_plan=req.assignment_plan,
            schedule_change_plan=req.schedule_change_plan,
            applied_by=req.applied_by,
        )
        if result.get("sync_status") in {"locked", "requires_review", "requires_allocation"}:
            raise HTTPException(status_code=409, detail=result)
        return BaseResponse(data=result, message="成功套用訂單與月嫂指派同步")
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail="Failed to apply order assignment synchronization") from error
