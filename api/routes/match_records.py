"""
================================================================================
檔案名稱: api/routes/match_records.py
功能說明: 案件與月嫂媒合紀錄 API 路由 (MatchRecordRouter)
================================================================================
"""

from fastapi import APIRouter, HTTPException, Path
from typing import Dict, Any, List
from api.schemas.base import BaseResponse
from api.schemas.matches import MatchCreateRequest
from services import match_record_idempotent_service

router = APIRouter(prefix="/api/v1/orders", tags=["Match Records 媒合紀錄"])

@router.get("/{case_no}/matches", response_model=BaseResponse[List[Dict[str, Any]]])
def get_order_matches(
    case_no: str = Path(..., description="案件編號")
):
    """查詢案件之全量媒合紀錄"""
    try:
        data = match_record_idempotent_service.get_order_match_records(case_no)
        return BaseResponse(data=data, message="成功取得案件媒合紀錄")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{case_no}/matches", response_model=BaseResponse[Dict[str, Any]])
def create_or_get_match_record(
    req: MatchCreateRequest,
    case_no: str = Path(..., description="案件編號")
):
    """等冪建立或查詢案件與月嫂之媒合紀錄"""
    try:
        result = match_record_idempotent_service.create_or_get_match_record_idempotent(
            case_no=case_no,
            staff_id=req.staff_id,
        )
        return BaseResponse(data=result, message="成功處理媒合紀錄")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
