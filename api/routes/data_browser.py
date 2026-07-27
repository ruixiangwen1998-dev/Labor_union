"""
================================================================================
檔案名稱: api/routes/data_browser.py
功能說明: Data Browser Admin API 路由
================================================================================
"""

from fastapi import APIRouter, HTTPException, Path
from typing import Dict, Any
from api.schemas.base import BaseResponse
from api.schemas.data_browser import DataBrowserTableResponse, DataBrowserPatchRequest
from services import data_browser_admin_service

router = APIRouter(prefix="/api/v1/admin/data-browser", tags=["Data Browser Admin"])


@router.get("/{table}", response_model=BaseResponse[DataBrowserTableResponse])
def get_data_browser_table(table: str = Path(..., description="資料表名稱")):
    """白名單讀取數據表結構與列資料。"""
    try:
        data = data_browser_admin_service.get_table_admin_data(table)
        return BaseResponse(data=DataBrowserTableResponse(**data), message=f"成功取得 {table} 資料表紀錄")
    except ValueError as ve:
        raise HTTPException(status_code=403, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{table}/{row_id}", response_model=BaseResponse[bool])
def patch_data_browser_row(
    req: DataBrowserPatchRequest,
    table: str = Path(..., description="資料表名稱"),
    row_id: int = Path(..., description="列 ID"),
):
    """白名單微調更新指定列記錄。"""
    try:
        success = data_browser_admin_service.patch_table_row_data(table, row_id, req.updates)
        return BaseResponse(data=success, message=f"成功更新 {table} id={row_id} 之欄位")
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
