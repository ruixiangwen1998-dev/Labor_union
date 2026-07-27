"""
================================================================================
檔案名稱: services/data_browser_admin_service.py
功能說明: Data Browser 白名單管理與單列更新 Service
================================================================================
"""

from typing import Any, Dict, List
from services import db_service

EDITABLE_COLUMNS: Dict[str, set] = {
    'clients': {
        'reject_reason', 'ip_address', 'name', 'gender', 'phone', 'city', 'address',
        'service_time', 'due_month', 'service_start_date', 'notes',
        'service_days', 'residence_type', 'delivery_type', 'service_type', 'baby_info',
        'line_id', 'admin_notes',
    },
    'staff': {
        'registered_at', 'ip_address', 'phone', 'tel', 'tel_ext', 'email', 'city',
        'zip_code', 'address', 'has_massage_cert', 'weekly_rest_days', 'service_regions',
        'special_skills', 'name', 'identity_card', 'birthday', 'care_babies',
    },
    'orders': {
        'line_group_id', 'contract_id',
    },
    'beclass_records': {
        'seq_num', 'email', 'tel', 'ext', 'city', 'zip_code', 'address',
        'refund_bank_code', 'refund_account_no', 'admin_notes',
    },
    'staff_bank_accounts': {
        'bank_code', 'branch_code', 'account_no', 'is_primary',
    },
}

READ_ONLY_TABLES = {
    "payments",
    "matching_records",
    "holidays",
    "line_confirmation_requests",
    "staff_bookings",
    "staff_regions",
    "staff_cooking_skills",
    "staff_weekly_rest",
    "staff_time_slots",
    "staff_transportation",
    "staff_holiday_availability",
    "staff_baby_types",
    "case_staff_assignments",
    "client_payments",
    "client_payment_transactions",
    "actual_hours_adjustments",
    "staff_payments",
    "staff_payment_transactions",
    "payment_migration_reviews",
    "staff_schedule",
}


def get_table_admin_data(table_name: str) -> Dict[str, Any]:
    """讀取白名單控制之表格資料與中繼資訊。"""
    if table_name in READ_ONLY_TABLES and table_name == "payments":
        raise ValueError("legacy payments table is strictly freeze-locked")

    rows = db_service.get_table_data(table_name)
    cols = db_service.get_table_columns(table_name) if hasattr(db_service, "get_table_columns") else []
    if not cols and rows:
        cols = list(rows[0].keys())

    editable = list(EDITABLE_COLUMNS.get(table_name, set()))
    is_read_only = table_name in READ_ONLY_TABLES or not editable

    return {
        "rows": rows,
        "columns": cols,
        "primary_key": "id",
        "editable_columns": editable,
        "valid_options": {},
        "read_only": is_read_only,
    }


def patch_table_row_data(table_name: str, row_id: int, updates: Dict[str, Any]) -> bool:
    """經由欄位白名單微調單列記錄。"""
    if table_name in READ_ONLY_TABLES or table_name == "payments":
        raise ValueError(f"Table {table_name} is read-only or fail-closed")

    allowed_fields = EDITABLE_COLUMNS.get(table_name, set())
    invalid_fields = [k for k in updates.keys() if k not in allowed_fields]
    if invalid_fields:
        raise ValueError(f"Fields {invalid_fields} are not in the editable whitelist for {table_name}")

    return db_service.update_table_row(table_name, row_id, updates)
