"""
================================================================================
檔案名稱: api/schemas/line_admin_binding.py
功能說明: 工會人員 LINE 與管理後台帳號綁定公開 API 的輸入資料格式
================================================================================
"""

from pydantic import BaseModel, Field


class LineAdminBindingCompleteRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=512)
    line_id_token: str = Field(default="", max_length=4096)
    development_line_user_id: str = Field(default="", max_length=100)
