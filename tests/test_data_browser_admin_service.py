"""
================================================================================
檔案名稱: tests/test_data_browser_admin_service.py
功能說明: 驗證 DataBrowserAdmin 服務動態主鍵、SSOT 中繼資料與稽核日誌寫入
================================================================================
"""

import pytest
from pathlib import Path
from services.db_service import get_connection
import services.data_browser_admin_schema_service as data_browser_admin_schema_service
from services.data_browser_admin_schema_service import (
    get_data_browser_table_schema,
    patch_data_browser_table_row,
)


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


def _run_schema_sql_file(cursor, sql_path: Path) -> None:
    sql_content = sql_path.read_text(encoding="utf-8")
    statements = []
    current_stmt = []

    for line in sql_content.splitlines():
        if line.strip().startswith("--"):
            continue
        segments = line.split(";")
        for segment_index, segment in enumerate(segments):
            current_stmt.append(segment)
            if segment_index < len(segments) - 1:
                statement = "\n".join(current_stmt).strip()
                if statement:
                    statements.append(statement)
                current_stmt = []

    trailing = "\n".join(current_stmt).strip()
    if trailing:
        statements.append(trailing)

    for statement in statements:
        cursor.execute(statement)


@pytest.fixture(scope="session", autouse=True)
def _ensure_data_browser_audit_schema() -> None:
    """在測試 DB 上先套用 Data Browser audit schema migration，避免缺欄位造成 fail-closed 誤判。"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            _run_schema_sql_file(
                cursor,
                Path(__file__).resolve().parents[1]
                / "db"
                / "schema_parts"
                / "99_data_browser_admin_audit_logs.sql",
            )
        conn.commit()
    finally:
        conn.close()

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


def test_data_browser_admin_patch_rejects_invalid_fields():
    """含非法欄位時應 fail-closed，不更新，也不寫入 audit。"""
    conn = get_connection()
    case_no = "TEST_PATCH_FAIL_778"

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO clients (id, case_no, name)
                VALUES (7778, %s, 'PATCH Fail 測試客戶')
                ON DUPLICATE KEY UPDATE name='PATCH Fail 測試客戶'
                """,
                (case_no,),
            )
            cursor.execute(
                """
                INSERT INTO orders (case_no, client_id, service_days)
                VALUES (%s, 7778, 20)
                ON DUPLICATE KEY UPDATE service_days=20
                """,
                (case_no,),
            )
            cursor.execute(
                "SELECT COUNT(*) AS total FROM audit_logs WHERE table_name = 'orders' AND pk_value = %s",
                (case_no,),
            )
            before_count = cursor.fetchone()["total"]
            conn.commit()

        with pytest.raises(ValueError, match="不在可編輯白名單中"):
            patch_data_browser_table_row("orders", case_no, {"service_days": 21, "invalid_field": "x"})

        with conn.cursor() as cursor:
            cursor.execute("SELECT service_days FROM orders WHERE case_no = %s", (case_no,))
            row = cursor.fetchone()
            assert row["service_days"] == 20

            cursor.execute(
                "SELECT COUNT(*) AS total FROM audit_logs WHERE table_name = 'orders' AND pk_value = %s",
                (case_no,),
            )
            after_count = cursor.fetchone()["total"]
            assert after_count == before_count

    finally:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM audit_logs WHERE table_name = 'orders' AND pk_value = %s", (case_no,))
            cursor.execute("DELETE FROM orders WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM clients WHERE case_no = %s", (case_no,))
            conn.commit()
        conn.close()


def test_data_browser_admin_patch_and_audit_log():
    """驗證支援字串主鍵 (case_no) 單列微調與寫入 audit_logs"""
    conn = get_connection()
    case_no = "TEST_PATCH_777"

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO clients (id, case_no, name)
                VALUES (7777, %s, 'PATCH 測試客戶')
                ON DUPLICATE KEY UPDATE name='PATCH 測試客戶'
            """, (case_no,))

            cursor.execute("""
                INSERT INTO orders (case_no, client_id, service_days)
                VALUES (%s, 7777, 20)
                ON DUPLICATE KEY UPDATE service_days=20
            """, (case_no,))
            conn.commit()

        # 執行 PATCH 微調
        success = patch_data_browser_table_row("orders", case_no, {"service_days": 25})
        assert success is True

        # 驗證資料庫 orders 內容已更新
        with conn.cursor() as cursor:
            cursor.execute("SELECT service_days FROM orders WHERE case_no = %s", (case_no,))
            row = cursor.fetchone()
            assert row["service_days"] == 25

            # 驗證 audit_logs 已成功記錄
            cursor.execute("SELECT * FROM audit_logs WHERE table_name = 'orders' AND pk_value = %s", (case_no,))
            audit = cursor.fetchone()
            assert audit is not None
            assert audit["action"] == "DATA_BROWSER_PATCH"
            assert "25" in audit["changed_fields"]

    finally:
        with conn.cursor() as cursor:
            try:
                cursor.execute("DELETE FROM audit_logs WHERE table_name = 'orders' AND pk_value = %s", (case_no,))
            except Exception:
                pass
            cursor.execute("DELETE FROM orders WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM clients WHERE case_no = %s", (case_no,))
            conn.commit()
        conn.close()


def test_data_browser_admin_patch_non_existent_row_rejects_and_no_audit():
    """不存在目標列時不應更新也不應寫入 audit."""
    conn = get_connection()
    case_no = "TEST_PATCH_MISS_779"

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS total FROM audit_logs WHERE table_name = 'orders' AND pk_value = %s",
                (case_no,),
            )
            before_count = cursor.fetchone()["total"]

        with pytest.raises(ValueError, match="指定資料列不存在"):
            patch_data_browser_table_row("orders", case_no, {"service_days": 30})

        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM audit_logs WHERE table_name = 'orders' AND pk_value = %s", (case_no,))
            after_count = cursor.fetchone()["total"]
            assert after_count == before_count
            cursor.execute("SELECT COUNT(*) AS total FROM orders WHERE case_no = %s", (case_no,))
            assert cursor.fetchone()["total"] == 0
    finally:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM audit_logs WHERE table_name = 'orders' AND pk_value = %s", (case_no,))
            conn.commit()
        conn.close()


def test_data_browser_admin_patch_audit_failure_rolls_back_update(monkeypatch):
    """audit 寫入失敗時應 rollback 更新，避免孤立資料變更。"""
    conn = get_connection()
    case_no = "TEST_PATCH_AUDIT_FAIL_780"

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO clients (id, case_no, name) VALUES (7780, %s, 'PATCH Audit 失敗測試') ON DUPLICATE KEY UPDATE name='PATCH Audit 失敗測試'",
                (case_no,),
            )
            cursor.execute(
                "INSERT INTO orders (case_no, client_id, service_days) VALUES (%s, 7780, 20) ON DUPLICATE KEY UPDATE service_days=20",
                (case_no,),
            )
            conn.commit()

            cursor.execute(
                "SELECT COUNT(*) AS total FROM audit_logs WHERE table_name = 'orders' AND pk_value = %s",
                (case_no,),
            )
            before_count = cursor.fetchone()["total"]

        def _failed_audit(*args, **kwargs):
            raise RuntimeError("audit failed")

        monkeypatch.setattr(data_browser_admin_schema_service, "record_data_browser_patch_audit", _failed_audit)

        with pytest.raises(RuntimeError, match="audit failed"):
            data_browser_admin_schema_service.patch_data_browser_table_row("orders", case_no, {"service_days": 35})

        with conn.cursor() as cursor:
            cursor.execute("SELECT service_days FROM orders WHERE case_no = %s", (case_no,))
            row = cursor.fetchone()
            assert row["service_days"] == 20

            cursor.execute("SELECT COUNT(*) AS total FROM audit_logs WHERE table_name = 'orders' AND pk_value = %s", (case_no,))
            assert cursor.fetchone()["total"] == before_count
    finally:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM audit_logs WHERE table_name = 'orders' AND pk_value = %s", (case_no,))
            cursor.execute("DELETE FROM orders WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM clients WHERE case_no = %s", (case_no,))
            conn.commit()
        conn.close()
