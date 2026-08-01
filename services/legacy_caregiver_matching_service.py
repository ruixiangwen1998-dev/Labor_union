"""Legacy single-caregiver matching operations behind the original four-step UX."""

from __future__ import annotations

from typing import Any

from services import db_service


def recommend_legacy_staff(
    *,
    case_no: str,
    filter_region: bool,
    filter_schedule: bool,
    filter_babies: bool,
    filter_time: bool,
) -> list[dict[str, Any]]:
    return db_service.get_recommended_staff_for_order(
        case_no=case_no,
        filter_region=filter_region,
        filter_schedule=filter_schedule,
        filter_babies=filter_babies,
        filter_time=filter_time,
    )


def send_legacy_matching_information(
    match_id: int,
    info_type: int,
) -> dict[str, Any]:
    """Record the original information action and return its visible delivery state."""
    if info_type not in (1, 2):
        raise ValueError("info_type must be 1 or 2")
    db_service.update_matching_info_sent(match_id, info_type)

    conn = db_service.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.id, m.case_no, m.staff_id, s.name, s.line_user_id,
                       c.name AS client_name
                FROM matching_records m
                JOIN staff s ON m.staff_id = s.id
                JOIN orders o ON m.case_no = o.case_no
                JOIN clients c ON o.client_id = c.id
                WHERE m.id = %s
                """,
                (match_id,),
            )
            staff_info = cursor.fetchone()
    finally:
        conn.close()

    line_pushed = bool(staff_info and staff_info.get("line_user_id"))
    staff_name = staff_info.get("name", "") if staff_info else ""
    if line_pushed and info_type == 1:
        message = (
            "已成功發送 LINE Flex Message 訂單資訊-1 至月嫂 "
            f"{staff_name} (LINE ID: {staff_info['line_user_id']})"
        )
    elif line_pushed:
        message = (
            "已成功發送 LINE 精篩照護圖譜訊息至月嫂 "
            f"{staff_name} (LINE ID: {staff_info['line_user_id']})"
        )
    else:
        message = f"發送時間已紀錄。提示：月嫂 {staff_name} 尚未綁定 LINE 帳號"

    return {
        "data": {
            "match_id": match_id,
            "line_pushed": line_pushed,
            "info_type": info_type,
        },
        "message": message,
    }


def record_legacy_matching_reply(match_id: int, accepted: bool | None) -> bool:
    return db_service.reply_matching_inquiry(match_id, accepted)


def send_legacy_resume_to_client(match_id: int) -> bool:
    """Preserve the original single-caregiver resume action contract."""
    if isinstance(match_id, bool) or not isinstance(match_id, int) or match_id <= 0:
        raise ValueError("match_id must be a positive integer")
    return db_service.mark_resume_sent(match_id)


def send_legacy_resume_for_case(case_no: str) -> int | None:
    """Record one accepted candidate resume from the anomaly center."""
    if not isinstance(case_no, str) or not case_no.strip():
        raise ValueError("case_no must be a non-empty string")
    return db_service.mark_resume_sent_for_case(case_no.strip())


def assign_legacy_staff_to_order(case_no: str, staff_id: int) -> bool:
    return db_service.assign_staff_to_order(case_no=case_no, staff_id=staff_id)
