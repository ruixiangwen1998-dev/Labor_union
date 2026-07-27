"""
================================================================================
檔案名稱: tests/test_assignment_rest_date_service.py
功能說明: 驗證 AssignmentScheduleRestDateService 以 assignment_id 為專屬權屬獨立更新排休，防範跨指派刪除
================================================================================
"""

from datetime import date, datetime

import pytest
from services.db_service import get_connection
from services.assignment_schedule_rest_date_service import _normalise_rest_dates, save_assignment_rest_dates


def test_normalise_rest_dates_rejects_invalid_elements():
    """rest_dates 應為嚴格 YYYY-MM-DD 字串陣列，任一非法元素整筆失敗。"""
    assert _normalise_rest_dates([]) == []
    assert _normalise_rest_dates(["2026-08-01"]) == ["2026-08-01"]

    with pytest.raises(ValueError, match="must be an array"):
        _normalise_rest_dates("2026-08-01")
    with pytest.raises(ValueError, match="must be an array"):
        _normalise_rest_dates(None)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _normalise_rest_dates(["2026-8-1"])
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _normalise_rest_dates([" 2026-08-01 "])
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _normalise_rest_dates([None])
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _normalise_rest_dates([date(2026, 8, 1)])
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _normalise_rest_dates([datetime(2026, 8, 1, 0, 0, 0)])


def test_save_assignment_rest_dates_rejects_invalid_rest_dates():
    """非法 rest_dates 應回傳 validation_error，不更新排班資料。"""
    conn = get_connection()
    case_no = "TEST_CASE_998"
    staff_id = 998

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO clients (id, case_no, name)
                VALUES (9998, %s, '測試客戶')
                ON DUPLICATE KEY UPDATE name='測試客戶'
                """,
                (case_no,),
            )
            cursor.execute(
                """
                INSERT INTO orders (case_no, client_id, service_days, service_hours_per_day, start_date, status)
                VALUES (%s, 9998, 10, 8, '2026-08-01', '服務中')
                ON DUPLICATE KEY UPDATE status='服務中'
                """,
                (case_no,),
            )
            cursor.execute(
                """
                INSERT INTO staff (id, name, phone)
                VALUES (%s, '測試月嫂Invalid', '0900000998')
                ON DUPLICATE KEY UPDATE name='測試月嫂Invalid'
                """,
                (staff_id,),
            )
            cursor.execute(
                """
                INSERT INTO case_staff_assignments (case_no, staff_id, assignment_sequence, assigned_start_date)
                VALUES (%s, %s, 1, '2026-08-01')
                """,
                (case_no, staff_id),
            )
            assign_id = cursor.lastrowid
            cursor.execute(
                """
                UPDATE case_staff_assignments
                   SET assigned_end_date = '2026-08-20', planned_hours = 160, status = 'active'
                 WHERE id = %s
                """,
                (assign_id,),
            )
            conn.commit()

        result = save_assignment_rest_dates(assign_id, ["2026-8-1"])
        assert result["success"] is False
        assert result["status"] == "validation_error"
        assert result["error_code"] == "invalid_rest_dates"
    finally:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM staff_schedule WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM case_staff_assignments WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM orders WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM clients WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM staff WHERE id = %s", (staff_id,))
            conn.commit()
        conn.close()


def test_save_assignment_rest_dates_scope_isolation():
    """驗證更新指派 A 的排休不會刪除同一案件下指派 B 的排班紀錄"""
    conn = get_connection()
    case_no = "TEST_CASE_999"
    staff_a_id = 901
    staff_b_id = 902

    try:
        with conn.cursor() as cursor:
            # 1. 建立測試客戶與訂單
            cursor.execute("""
                INSERT INTO clients (id, case_no, name)
                VALUES (9999, %s, '測試客戶')
                ON DUPLICATE KEY UPDATE name='測試客戶'
            """, (case_no,))

            cursor.execute("""
                INSERT INTO orders (case_no, client_id, service_days, service_hours_per_day, start_date, status)
                VALUES (%s, 9999, 10, 8, '2026-08-01', '服務中')
                ON DUPLICATE KEY UPDATE status='服務中'
            """, (case_no,))


            # 2. 建立測試月嫂與指派紀錄
            cursor.execute("""
                INSERT INTO staff (id, name, phone)
                VALUES (901, '測試月嫂A', '0900000901')
                ON DUPLICATE KEY UPDATE name='測試月嫂A'
            """)
            cursor.execute("""
                INSERT INTO staff (id, name, phone)
                VALUES (902, '測試月嫂B', '0900000902')
                ON DUPLICATE KEY UPDATE name='測試月嫂B'
            """)

            cursor.execute("""
                INSERT INTO case_staff_assignments (case_no, staff_id, assignment_sequence, assigned_start_date)
                VALUES (%s, %s, 1, '2026-08-01')
            """, (case_no, staff_a_id))
            assign_a_id = cursor.lastrowid

            cursor.execute(
                """UPDATE case_staff_assignments
                      SET assigned_end_date = '2026-08-20', planned_hours = 160, status = 'active'
                    WHERE id = %s
                """,
                (assign_a_id,),
            )

            cursor.execute("""
                INSERT INTO case_staff_assignments (case_no, staff_id, assignment_sequence, assigned_start_date)
                VALUES (%s, %s, 2, '2026-09-01')
            """, (case_no, staff_b_id))
            assign_b_id = cursor.lastrowid

            cursor.execute(
                """UPDATE case_staff_assignments
                      SET assigned_end_date = '2026-09-30', planned_hours = 160, status = 'active'
                    WHERE id = %s
                """,
                (assign_b_id,),
            )


            # 3. 為指派 B 建立初始排班紀錄
            cursor.execute("""
                INSERT INTO staff_schedule (assignment_id, case_no, staff_id, work_date, is_work_day)
                VALUES (%s, %s, %s, '2026-08-01', 1)

            """, (assign_b_id, case_no, staff_b_id))
            conn.commit()

        # 4. 執行指派 A 的排休更新
        res_a = save_assignment_rest_dates(assign_a_id, ["2026-08-05"])
        assert res_a.get("success") is True
        assert res_a.get("status") == "ok"
        assert res_a.get("assignment_id") == assign_a_id
        assert res_a.get("rest_dates") == ["2026-08-05"]

        # 5. 驗證指派 B 的排班紀錄依然完整完好，未被 DELETE
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM staff_schedule WHERE assignment_id = %s", (assign_b_id,))
            b_schedules = cursor.fetchall()
            assert len(b_schedules) == 1
            assert b_schedules[0]["staff_id"] == staff_b_id

            # 驗證指派 A 成功寫入排班與排休
            cursor.execute("SELECT * FROM staff_schedule WHERE assignment_id = %s", (assign_a_id,))
            a_schedules = cursor.fetchall()
            assert len(a_schedules) > 0

            cursor.execute("SELECT custom_rest_dates FROM orders WHERE case_no = %s", (case_no,))
            orders_row = cursor.fetchone()
            assert orders_row["custom_rest_dates"] is None

    finally:
        # 清理測試資料
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM staff_schedule WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM case_staff_assignments WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM orders WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM clients WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM staff WHERE id IN (901, 902)")
            conn.commit()

        conn.close()
