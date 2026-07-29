"""
================================================================================
檔案名稱: api/routes/line_staff_verification.py
功能說明: 月嫂 LIFF 基本資料比對與申請提交公開 API
================================================================================
"""

from fastapi import APIRouter, HTTPException, Query

from api.schemas.base import BaseResponse
from api.schemas.line_staff_verification import StaffVerificationSubmitRequest
from services.line_staff_verification_service import (
    StaffVerificationError,
    StaffVerificationNotFoundError,
    StaffVerificationStateError,
    get_staff_verification_form_state,
    submit_staff_verification,
)


router = APIRouter(prefix="/api/line/staff-verification", tags=["LINE Staff Verification"])


def _raise_verification_error(exc: Exception) -> None:
    if isinstance(exc, StaffVerificationNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, StaffVerificationStateError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, StaffVerificationError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.get("", response_model=BaseResponse[dict])
def form_state(token: str = Query(min_length=20, max_length=200)):
    try:
        result = get_staff_verification_form_state(token)
    except StaffVerificationError as exc:
        _raise_verification_error(exc)
    return BaseResponse(data=result)


@router.post("/submit", response_model=BaseResponse[dict])
def submit_form(payload: StaffVerificationSubmitRequest):
    try:
        result = submit_staff_verification(
            token=payload.token,
            name=payload.name,
            identity_card=payload.identity_card,
            birthday=payload.birthday,
            line_id_token=payload.line_id_token,
            development_line_user_id=payload.development_line_user_id,
        )
    except StaffVerificationError as exc:
        _raise_verification_error(exc)
    return BaseResponse(data=result, message=result["message"])

