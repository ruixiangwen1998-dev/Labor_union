"""
================================================================================
檔案名稱: services/data_browser_admin_schema_service.py
功能說明: 資料庫原始資料動態主鍵、中繼權限 SSOT 與單列微調服務 (DataBrowserAdminSchemaService)
================================================================================
"""

from typing import Any, Dict
from services import db_service
from services.data_browser_admin_audit_log_service import record_data_browser_patch_audit

# 白名單資料表
ALLOWED_TABLES = {
    "clients",
    "staff",
    "orders",
    "beclass_records",
    "holidays",
    "matching_records",
    "staff_bank_accounts",
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
}

# 可編輯欄位白名單
EDITABLE_COLUMNS = {
    'clients': [
        'reject_reason', 'ip_address', 'name', 'gender', 'phone', 'city', 'address',
        'service_time', 'due_month', 'service_start_date', 'notes',
        'service_days', 'residence_type', 'delivery_type', 'service_type', 'baby_info',
        'line_id', 'admin_notes',
    ],
    'staff': [
        'registered_at', 'ip_address', 'phone', 'tel', 'tel_ext', 'email', 'city',
        'zip_code', 'address', 'has_massage_cert', 'weekly_rest_days', 'service_regions',
        'special_skills', 'name', 'identity_card', 'birthday', 'care_babies',
    ],
    'orders': [
        'service_days', 'service_hours_per_day', 'floor_fee', 'custom_rest_dates',
    ],
    'holidays': [
        'holiday_name', 'is_double_pay',
    ],
    'matching_records': [
        'caregiver_accepted',
    ],
}

# 唯讀表清單 (client_payments 與 staff_payments 財務關聯表強制鎖定唯讀)
READ_ONLY_TABLES = {
    "beclass_records",
    "holidays",
    "matching_records",
    "staff_bank_accounts",
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
}


# 下拉選單中繼資料 (SSOT)
COLUMN_VALID_OPTIONS = {
    'clients': {
        'gender': ['女', '男'],
        'residence_type': ['電梯大樓', '公寓', '透天', '其他'],
        'delivery_type': ['自然產', '剖腹產', '未定'],
        'service_type': ['24小時', '9小時', '4小時', '其他'],
    },
    'staff': {
        'has_massage_cert': ['有', '無'],
    },
    'matching_records': {
        'caregiver_accepted': ['0', '1'],
    },
}

def get_data_browser_table_schema(table_name: str) -> Dict[str, Any]:
    """
    動態取得資料表之資料列、欄位清單與中繼權限 SSOT。
    關鍵修復：主鍵由 TABLE_PRIMARY_KEYS 動態回傳 (例如 orders 轉為 case_no)。
    """
    if table_name not in ALLOWED_TABLES:
        raise ValueError(f"不允許存取的資料表: {table_name}")

    rows = db_service.get_table_data(table_name)
    cols = db_service.get_table_columns(table_name)
    pk_col = db_service.TABLE_PRIMARY_KEYS.get(table_name, "id")
    is_read_only = table_name in READ_ONLY_TABLES
    editable = [] if is_read_only else EDITABLE_COLUMNS.get(table_name, [])

    return {
        "rows": rows,
        "columns": cols,
        "primary_key": pk_col,
        "editable_columns": editable,
        "valid_options": COLUMN_VALID_OPTIONS.get(table_name, {}),
        "read_only": is_read_only,
    }

def patch_data_browser_table_row(
    table_name: str,
    row_id: str,
    updates: Dict[str, Any],
    operator_id: str = "admin_ui",
    operator_role: str = "admin",
) -> bool:
    """
    更新特定資料表單列，支援字串主鍵 (如 case_no)，並自動寫入稽核紀錄。
    """
    if table_name not in ALLOWED_TABLES:
        raise ValueError(f"不允許存取的資料表: {table_name}")
    if table_name in READ_ONLY_TABLES:
        raise ValueError(f"資料表 {table_name} 屬於全表唯讀保護，禁止直接修改。")

    if not updates:
        raise ValueError("沒有包含任何更新欄位。")

    # 1. 權限白名單 fail-closed 檢查（只要有非法欄位即全數拒絕）
    allowed_cols = set(EDITABLE_COLUMNS.get(table_name, []))
    invalid_cols = [k for k in updates.keys() if k not in allowed_cols]
    if invalid_cols:
        raise ValueError(f"欄位 {invalid_cols} 不在可編輯白名單中，更新已取消。")

    if not allowed_cols:
        raise ValueError("此資料表未設定可編輯欄位。")

    if table_name not in db_service.TABLE_PRIMARY_KEYS:
        raise ValueError(f"不允許存取的資料表: {table_name}")

    pk_col = db_service.TABLE_PRIMARY_KEYS[table_name]

    conn = db_service.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM `{table_name}` WHERE `{pk_col}` = %s FOR UPDATE", (row_id,))
            before_snapshot = cursor.fetchone()
            if before_snapshot is None:
                raise ValueError("指定資料列不存在，更新已取消。")

            updated = db_service.update_table_row(
                table_name=table_name,
                row_id=row_id,
                updates=updates,
                cursor=cursor,
            )
            if not updated:
                raise ValueError("指定資料列不存在或欄位變更未生效，更新已取消。")

            cursor.execute(f"SELECT * FROM `{table_name}` WHERE `{pk_col}` = %s", (row_id,))
            after_snapshot = cursor.fetchone()
            audited = record_data_browser_patch_audit(
                table_name=table_name,
                pk_value=str(row_id),
                changed_fields=updates,
                operator_id=operator_id,
                actor=operator_id,
                role=operator_role,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
                cursor=cursor,
            )
            if not audited:
                raise RuntimeError("資料異動稽核寫入失敗，更新已回滾。")

            conn.commit()
            return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
