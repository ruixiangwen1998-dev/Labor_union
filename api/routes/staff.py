from fastapi import APIRouter, HTTPException, Path, Query
from typing import List, Dict, Any
from services import db_service
from api.schemas.base import BaseResponse

router = APIRouter(prefix="/api/v1/staff", tags=["Staff 服務人員/月嫂名冊"])

@router.get("", response_model=BaseResponse[List[Dict[str, Any]]])
def get_all_staff():
    """取得全量服務人員/月嫂名冊資料表"""
    try:
        data = db_service.get_table_data("staff")
        return BaseResponse(data=data, message="成功取得服務人員列表")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
