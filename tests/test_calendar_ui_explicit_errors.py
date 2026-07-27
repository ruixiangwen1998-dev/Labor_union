"""
================================================================================
檔案名稱: tests/test_calendar_ui_explicit_errors.py
功能說明: 驗證 CalendarUI 顯性錯誤提示、無 db_service 引用與 assignment-schedules REST 端點對齊
================================================================================
"""

import pytest
import ast
from pathlib import Path

def test_calendar_ui_decoupled_from_db_service():
    """驗證 ui/pages/03_calendar.py 完全解耦，不再匯入 db_service"""
    file_content = Path("ui/pages/03_calendar.py").read_text(encoding="utf-8")
    assert "from services import db_service" not in file_content
    assert "importlib.reload(db_service)" not in file_content

def test_calendar_ui_uses_assignment_schedules_endpoint():
    """驗證 ui/pages/03_calendar.py 使用 assignment-schedules 休假保存端點"""
    file_content = Path("ui/pages/03_calendar.py").read_text(encoding="utf-8")
    assert "/api/v1/assignment-schedules/" in file_content
    assert "/orders/{target_order['case_no']}/rest-dates" not in file_content


def test_calendar_holidays_request_uses_formal_admin_headers():
    """國定假日管理 API 不得由 CalendarUI 裸送請求。"""
    source = Path("ui/pages/03_calendar.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    holiday_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "requests"
        and node.func.attr == "get"
        and node.args
        and "/api/v1/holidays" in ast.unparse(node.args[0])
    ]

    assert len(holiday_calls) == 1
    keyword_names = {keyword.arg for keyword in holiday_calls[0].keywords}
    headers_keyword = next(
        keyword for keyword in holiday_calls[0].keywords if keyword.arg == "headers"
    )
    assert {"headers", "timeout"} <= keyword_names
    assert ast.unparse(headers_keyword.value) == "admin_headers"
    assert "admin_headers = build_admin_headers()" in source
    assert "resolve_api_base_url()" in ast.unparse(holiday_calls[0].args[0])
