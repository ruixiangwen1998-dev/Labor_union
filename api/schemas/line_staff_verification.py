"""
================================================================================
檔案名稱: api/schemas/line_staff_verification.py
功能說明: 月嫂 LIFF 身分比對公開 API 的輸入與輸出資料格式
================================================================================
"""

from datetime import date

from pydantic import BaseModel, Field


class StaffVerificationSubmitRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    identity_card: str = Field(min_length=8, max_length=20)
    birthday: date
    line_id_token: str = Field(default="", max_length=4096)
    development_line_user_id: str = Field(default="", max_length=100)

