"""
================================================================================
檔案名稱: services/order_schedule_calculation_service.py
功能說明: 出勤排班與順延完工日精算服務 (OrderScheduleCalculationService)
================================================================================
"""

from typing import Dict, Any, List, Optional
from datetime import date
from services import db_service

def calculate_order_attendance_schedule(
    actual_start_date: date,
    target_service_days: int = 20,
    service_mode: str = "週休1日",
    custom_holiday_rest_dates: Optional[List[date]] = None,
    custom_leave_dates: Optional[List[date]] = None,
    custom_rest_weekdays: Optional[List[int]] = None,
    monthly_salary_base: Optional[float] = None,
) -> Dict[str, Any]:
    """
    精算服務人員出勤日、扣除排休與國定假日順延完工日。
    關鍵修復：
    1. custom_holiday_rest_dates 與 custom_leave_dates 採用聯集合併，避免 or 運算子吞掉自訂排休。
    2. 轉傳 custom_rest_weekdays 與 monthly_salary_base 給底層精算函式。
    """
    holiday_set = set(custom_holiday_rest_dates or [])
    leave_set = set(custom_leave_dates or [])
    combined_rest_dates = list(holiday_set | leave_set) if (holiday_set or leave_set) else None

    result = db_service.calculate_attendance_schedule(
        actual_start_date=actual_start_date,
        target_service_days=target_service_days,
        service_mode=service_mode,
        custom_leave_dates=combined_rest_dates,
        custom_holiday_rest_dates=None,
        custom_rest_weekdays=custom_rest_weekdays,
        monthly_salary_base=monthly_salary_base,
    )
    return result
