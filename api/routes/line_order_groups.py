"""
================================================================================
檔案名稱: api/routes/line_order_groups.py
功能說明: 訂單 LINE 服務群組清單、明細與解除綁定管理 API
================================================================================
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.dependencies.admin_auth import require_line_manager, require_line_viewer
from api.schemas.base import BaseResponse
from api.schemas.line_order_groups import LineOrderGroupUnbindRequest
from services.line_order_group_service import (
    LineOrderGroupConflictError,
    LineOrderGroupNotFoundError,
    get_order_group,
    list_order_groups,
    unbind_order_group,
)


router = APIRouter(
    prefix="/api/v1/line/order-groups",
    tags=["LINE Order Groups"],
    dependencies=[Depends(require_line_viewer)],
)


def _translate_error(exc: Exception) -> None:
    if isinstance(exc, LineOrderGroupNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, LineOrderGroupConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise exc


@router.get("", response_model=BaseResponse[list[dict]])
def order_group_list(
    status: str | None = None,
    case_no: str | None = Query(default=None, max_length=50),
):
    return BaseResponse(data=list_order_groups(status=status, case_no=case_no))


@router.get("/by-case/{case_no}", response_model=BaseResponse[dict])
def order_group_by_case(case_no: str):
    try:
        result = get_order_group(case_no=case_no)
    except LineOrderGroupNotFoundError as exc:
        _translate_error(exc)
    return BaseResponse(data=result)


@router.get("/{binding_id}", response_model=BaseResponse[dict])
def order_group_detail(binding_id: int):
    try:
        result = get_order_group(binding_id=binding_id)
    except LineOrderGroupNotFoundError as exc:
        _translate_error(exc)
    return BaseResponse(data=result)


@router.post(
    "/{binding_id}/unbind",
    response_model=BaseResponse[dict],
    dependencies=[Depends(require_line_manager)],
)
def order_group_unbind(
    binding_id: int,
    payload: LineOrderGroupUnbindRequest,
    request: Request,
):
    principal = request.state.admin_principal
    try:
        result = unbind_order_group(binding_id, actor_admin_user_id=principal.id)
    except (LineOrderGroupNotFoundError, LineOrderGroupConflictError) as exc:
        _translate_error(exc)
    request.state.audit_action = "line.order_group.unbind"
    request.state.audit_resource_type = "line_order_group_binding"
    request.state.audit_resource_id = str(binding_id)
    request.state.audit_details = {"reason": payload.reason.strip()}
    return BaseResponse(data=result, message="訂單服務群組已解除綁定")
