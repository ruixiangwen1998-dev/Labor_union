"""
================================================================================
檔案名稱: tests/test_match_record_service.py
功能說明: 驗證 MatchRecordIdempotentService 等冪防爆與多重點擊防護功能
================================================================================
"""

import pytest
from services.db_service import get_connection
from services.match_record_idempotent_service import (
    create_or_get_match_record_idempotent,
    get_order_match_records,
)

def test_create_or_get_match_record_idempotence():
    """驗證重複呼叫建立媒合紀錄具備等冪性，且不拋出 500 外鍵/主鍵衝突"""
    conn = get_connection()
    case_no = "MATCH_CASE_888"
    staff_id = 8881

    try:
        with conn.cursor() as cursor:
            # 1. 建立測試資料
            cursor.execute("""
                INSERT INTO clients (id, case_no, name)
                VALUES (8888, %s, '媒合測試客戶')
                ON DUPLICATE KEY UPDATE name='媒合測試客戶'
            """, (case_no,))

            cursor.execute("""
                INSERT INTO orders (case_no, client_id, status)
                VALUES (%s, 8888, '洽談中')
                ON DUPLICATE KEY UPDATE status='洽談中'
            """, (case_no,))

            cursor.execute("""
                INSERT INTO staff (id, name, phone)
                VALUES (%s, '媒合測試月嫂', '0900008881')
                ON DUPLICATE KEY UPDATE name='媒合測試月嫂'
            """, (staff_id,))
            conn.commit()

        # 2. 第一次建立
        res1 = create_or_get_match_record_idempotent(case_no, staff_id)
        assert res1["success"] is True
        match_id_1 = res1["match_id"]
        assert match_id_1 > 0

        # 3. 第二次重複建立（模擬重複點擊或併發）
        res2 = create_or_get_match_record_idempotent(case_no, staff_id)
        assert res2["success"] is True
        match_id_2 = res2["match_id"]

        # 4. 斷言兩次回傳相同 match_id
        assert match_id_1 == match_id_2

        # 5. 驗證查詢列表
        matches = get_order_match_records(case_no)
        assert len(matches) == 1
        assert matches[0]["staff_name"] == "媒合測試月嫂"

    finally:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM matching_records WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM orders WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM clients WHERE case_no = %s", (case_no,))
            cursor.execute("DELETE FROM staff WHERE id = %s", (staff_id,))
            conn.commit()
        conn.close()
