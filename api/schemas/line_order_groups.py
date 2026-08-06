"""
================================================================================
檔案名稱: api/schemas/line_order_groups.py
功能說明: 訂單 LINE 服務群組管理 API 的輸入資料驗證模型
================================================================================
"""

from pydantic import BaseModel, Field


class LineOrderGroupUnbindRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
