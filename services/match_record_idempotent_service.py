"""
================================================================================
檔案名稱: services/match_record_idempotent_service.py
功能說明: 案件與月嫂媒合紀錄建立與查詢等冪防爆服務 (MatchRecordIdempotentService)
================================================================================
"""

from typing import Dict, Any, List
from services.db_service import get_connection

def create_or_get_match_record_idempotent(case_no: str, staff_id: int) -> Dict[str, Any]:
    """
    原子化且防範高併發 Duplicate Key 之媒合紀錄建立與查詢。
    關鍵約束：
    1. 採用 ON DUPLICATE KEY UPDATE 與語意事務，防止二次點擊或併發寫入時噴出 HTTP 500 (IntegrityError)。
    2. 等冪回傳對應之 match_id。
    """
    if not case_no or not staff_id:
        return {"success": False, "message": "case_no 與 staff_id 為必填欄位"}

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 1. 嘗試先查既有紀錄
            cursor.execute("""
                SELECT id, case_no, staff_id, caregiver_accepted
                FROM matching_records
                WHERE case_no = %s AND staff_id = %s
            """, (case_no, staff_id))
            row = cursor.fetchone()
            if row:
                return {
                    "success": True,
                    "match_id": row["id"],
                    "case_no": row["case_no"],
                    "staff_id": row["staff_id"],
                    "is_new": False,
                }

            # 2. 若無則執行防爆 INSERT
            cursor.execute("""
                INSERT INTO matching_records (case_no, staff_id)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
            """, (case_no, staff_id))
            match_id = cursor.lastrowid
            conn.commit()

            return {
                "success": True,
                "match_id": match_id,
                "case_no": case_no,
                "staff_id": staff_id,
                "is_new": True,
            }

    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_order_match_records(case_no: str) -> List[Dict[str, Any]]:
    """查詢特定案件之全量媒合紀錄列表"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT mr.id AS match_id, mr.case_no, mr.staff_id, mr.caregiver_accepted,
                       s.name AS staff_name, s.phone AS staff_phone
                FROM matching_records mr
                JOIN staff s ON mr.staff_id = s.id
                WHERE mr.case_no = %s
                ORDER BY mr.id ASC
            """, (case_no,))
            return cursor.fetchall()
    finally:
        conn.close()
