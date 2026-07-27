"""
================================================================================
檔案名稱: api/schemas/data_browser.py
功能說明: Data Browser Admin Pydantic Schemas
================================================================================
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class DataBrowserTableResponse(BaseModel):
    rows: List[Dict[str, Any]]
    columns: List[str]
    primary_key: str = "id"
    editable_columns: List[str]
    valid_options: Dict[str, List[str]] = Field(default_factory=dict)
    read_only: bool = False


class DataBrowserPatchRequest(BaseModel):
    updates: Dict[str, Any] = Field(..., description="要微調更新的欄位與值")
