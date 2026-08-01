"""
================================================================================
檔案名稱: api/routes/line_admin_binding.py
功能說明: 工會人員從 LIFF 驗證後台帳密並完成 LINE 帳號綁定的公開 API
================================================================================
"""

from fastapi import APIRouter, HTTPException, Query

from api.schemas.base import BaseResponse
from api.schemas.line_admin_binding import LineAdminBindingCompleteRequest
from line.worker import wake_worker
from services.line_admin_binding_service import (
    LineAdminBindingAuthenticationError,
    LineAdminBindingConflictError,
    LineAdminBindingError,
    LineAdminBindingNotFoundError,
    LineAdminBindingStateError,
    complete_line_admin_binding,
    get_line_admin_binding_state,
)


router = APIRouter(prefix="/api/line/admin-binding", tags=["LINE Admin Binding"])


def _raise_binding_error(exc: Exception) -> None:
    if isinstance(exc, LineAdminBindingNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, LineAdminBindingAuthenticationError):
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if isinstance(exc, (LineAdminBindingStateError, LineAdminBindingConflictError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, LineAdminBindingError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.get("", response_model=BaseResponse[dict])
def binding_state(token: str = Query(min_length=20, max_length=200)):
    try:
        result = get_line_admin_binding_state(token)
    except LineAdminBindingError as exc:
        _raise_binding_error(exc)
    return BaseResponse(data=result)


@router.post("/complete", response_model=BaseResponse[dict])
def complete_binding(payload: LineAdminBindingCompleteRequest):
    try:
        result = complete_line_admin_binding(
            token=payload.token,
            username=payload.username,
            password=payload.password,
            line_id_token=payload.line_id_token,
            development_line_user_id=payload.development_line_user_id,
        )
    except LineAdminBindingError as exc:
        _raise_binding_error(exc)
    wake_worker()
    return BaseResponse(data=result, message=result["message"])
