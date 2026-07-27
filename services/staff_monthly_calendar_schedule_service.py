"""
================================================================================
檔案名稱: services/staff_monthly_calendar_schedule_service.py
功能說明: 由 case_staff_assignments 查詢月嫂指定年月檔期排班視圖服務 (StaffMonthlyCalendarScheduleService)
================================================================================
"""

from typing import Dict, Any, List
from calendar import monthrange
from datetime import date, datetime
from services.db_service import get_connection


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _coerce_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return bool(value)


def _priority_status(status: str) -> int:
    return {"red": 3, "green": 2, "yellow": 1}.get(status, 0)

def get_staff_monthly_calendar_schedule(staff_id: int, year: int, month: int) -> Dict[str, Any]:
    """
    查詢月嫂在指定年月的每日檔期排班視圖。
    關鍵約束：
    1. 輸出包含計畫要求的 days: [...] 標準陣列，內含 assignment_id、case_no、client_name、status。
    2. 同時包含相容舊版 UI 的 schedule_map。
    """
    conn = get_connection()
    days_list: List[Dict[str, Any]] = []
    grouped_rows: Dict[int, List[Dict[str, Any]]] = {}
    schedule_map: Dict[int, Dict[str, Any]] = {}

    try:
        num_days = monthrange(year, month)[1]
        month_start = date(year, month, 1)
        month_end = date(year, month, num_days)

        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 AS staff_exists FROM staff WHERE id = %s", (staff_id,))
            if cursor.fetchone() is None:
                raise ValueError(f"服務人員不存在：{staff_id}")

            cursor.execute("""
                SELECT
                    ss.work_date,
                    ss.is_work_day,
                    ss.is_double_pay,
                    ss.notes,
                    ss.id AS schedule_id,
                    csa.case_no,
                    csa.staff_id,
                    csa.id AS assignment_id,
                    c.name AS client_name
                FROM staff_schedule ss
                JOIN case_staff_assignments csa ON ss.assignment_id = csa.id
                JOIN orders o ON csa.case_no = o.case_no
                JOIN clients c ON o.client_id = c.id
                WHERE csa.staff_id = %s
                  AND ss.assignment_id IS NOT NULL
                  AND ss.work_date BETWEEN %s AND %s
                ORDER BY ss.work_date, ss.id
            """, (staff_id, month_start, month_end))
            schedule_rows = cursor.fetchall()

            for row in schedule_rows:
                work_date = _as_date(row.get("work_date"))
                if work_date is None:
                    continue
                assignment_id = row.get("assignment_id")
                if assignment_id is None:
                    # 排除 legacy 無法確認 ownership 的排班列，避免誤映到 assignment_id
                    continue

                is_work_day = _coerce_bool(row.get("is_work_day"))
                is_double_pay = _coerce_bool(row.get("is_double_pay"))
                day = work_date.day
                item = {
                    "work_date": work_date.strftime("%Y-%m-%d"),
                    "status": "working" if is_work_day else "resting",
                    "assignment_id": assignment_id,
                    "case_no": row.get("case_no"),
                    "staff_id": row.get("staff_id", staff_id),
                    "client_name": row.get("client_name"),
                    "is_work_day": is_work_day,
                    "is_double_pay": is_double_pay,
                    "notes": row.get("notes"),
                }
                grouped_rows.setdefault(day, []).append(item)

                day_status = "red" if is_work_day else "green"
                candidate = {
                    "status": day_status,
                    "case_no": row.get("case_no"),
                    "client_name": row.get("client_name"),
                    "is_work_day": is_work_day,
                    "is_double_pay": is_double_pay,
                    "assignment_id": assignment_id,
                }
                current = schedule_map.get(day)
                if current is None or _priority_status(day_status) > _priority_status(current["status"]) or (
                    current["status"] == day_status and current.get("assignment_id") is None and assignment_id is not None
                ):
                    schedule_map[day] = candidate

            for d in range(1, num_days + 1):
                cur_d = date(year, month, d)
                cur_str = cur_d.strftime("%Y-%m-%d")
                day_rows = grouped_rows.get(d, [])
                if not day_rows:
                    days_list.append({
                        "work_date": cur_str,
                        "status": "available",
                        "assignment_id": None,
                        "case_no": None,
                        "staff_id": staff_id,
                        "client_name": None,
                        "is_work_day": False,
                        "is_double_pay": False,
                        "notes": None,
                    })
                    schedule_map[d] = {
                        "status": "white",
                        "case_no": None,
                        "client_name": None,
                        "is_work_day": False,
                        "is_double_pay": False,
                        "assignment_id": None,
                    }
                else:
                    days_list.extend(day_rows)

        return {
            "staff_id": staff_id,
            "year": year,
            "month": month,
            "days": days_list,
            "schedule_map": schedule_map,
        }
    finally:
        conn.close()
