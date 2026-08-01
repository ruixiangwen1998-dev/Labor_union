"""
================================================================================
檔案名稱: tests/test_data_browser_admin_service.py
功能說明: 驗證 DataBrowserAdmin 服務動態主鍵、SSOT 中繼資料與稽核日誌寫入
================================================================================
"""

import pytest
import services.data_browser_admin_schema_service as data_browser_admin_schema_service
from services.data_browser_admin_schema_service import get_data_browser_table_schema


DATA_BROWSER_UI_TABLES = [
    "staff",
    "clients",
    "line_confirmation_requests",
    "staff_bookings",
    "case_staff_assignments",
    "client_payments",
    "client_payment_transactions",
    "actual_hours_adjustments",
    "staff_payments",
    "staff_payment_transactions",
    "payment_migration_reviews",
    "staff_schedule",
    "orders",
    "beclass_records",
    "matching_records",
    "holidays",
    "staff_bank_accounts",
]
def test_data_browser_admin_schema_ssot():
    """驗證 orders 的動態主鍵為 case_no (字串)，且包含權限中繼資料"""
    schema = get_data_browser_table_schema("orders")
    assert schema["primary_key"] == "case_no"
    assert "service_days" in schema["editable_columns"]
    assert schema["read_only"] is False

    schema_payments = get_data_browser_table_schema("client_payments")
    assert schema_payments["read_only"] is True


@pytest.mark.parametrize("table_name", DATA_BROWSER_UI_TABLES)
def test_data_browser_admin_schema_accepts_every_ui_table(monkeypatch, table_name):
    monkeypatch.setattr(data_browser_admin_schema_service.db_service, "get_table_data", lambda _table: [])
    monkeypatch.setattr(data_browser_admin_schema_service.db_service, "get_table_columns", lambda _table: ["id"])

    schema = get_data_browser_table_schema(table_name)

    assert schema["primary_key"] == data_browser_admin_schema_service.db_service.TABLE_PRIMARY_KEYS[table_name]
    assert schema["columns"] == ["id"]


def test_staff_schedule_metadata_is_read_only(monkeypatch):
    monkeypatch.setattr(data_browser_admin_schema_service.db_service, "get_table_data", lambda _table: [])
    monkeypatch.setattr(data_browser_admin_schema_service.db_service, "get_table_columns", lambda _table: ["id"])

    schema = get_data_browser_table_schema("staff_schedule")

    assert schema["primary_key"] == "id"
    assert schema["editable_columns"] == []
    assert schema["read_only"] is True


def test_read_only_table_never_exposes_editable_columns(monkeypatch):
    monkeypatch.setattr(data_browser_admin_schema_service.db_service, "get_table_data", lambda _table: [])
    monkeypatch.setattr(data_browser_admin_schema_service.db_service, "get_table_columns", lambda _table: ["holiday_date"])

    schema = get_data_browser_table_schema("holidays")

    assert schema["editable_columns"] == []
    assert schema["read_only"] is True
