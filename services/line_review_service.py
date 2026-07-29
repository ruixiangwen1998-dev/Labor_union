"""
================================================================================
檔案名稱: services/line_review_service.py
功能說明: LINE 人工確認交易服務，安全處理月嫂身分與客戶重新綁定申請
================================================================================
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pymysql

from services.db_service import get_connection
from services.line_rich_menu_service import get_current_rich_menu_id
from services.line_task_service import enqueue_line_task


REQUEST_TYPES = {"staff_verification", "client_rebind"}
REQUEST_STATUSES = {"pending", "approved", "rejected", "cancelled"}
TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class LineReviewNotFoundError(LookupError):
    pass


class LineReviewStateConflictError(RuntimeError):
    def __init__(self, request_id: int, current_status: str):
        super().__init__(f"審查申請 #{request_id} 目前狀態為 {current_status}，不能重複處理")
        self.request_id = request_id
        self.current_status = current_status


class LineReviewDataConflictError(RuntimeError):
    pass


def _as_int(value: Any) -> int:
    return int(value or 0)


def _mask_line_id(value: str | None) -> str:
    text = (value or "").strip()
    if len(text) <= 8:
        return text or "-"
    return f"{text[:4]}…{text[-4:]}"


def _mask_identity_card(value: str | None) -> str:
    text = (value or "").strip().upper()
    if len(text) < 6:
        return "-" if not text else "***"
    return f"{text[:3]}{'*' * max(3, len(text) - 7)}{text[-4:]}"


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _load_text_templates() -> dict[str, str]:
    path = PROJECT_ROOT / "config" / "message_templates.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        item["id"]: item["content"]
        for item in payload.get("templates", [])
        if item.get("enabled", True)
        and item.get("message_type", "text") == "text"
        and item.get("id")
    }


def _template(template_id: str, fallback: str, **variables: str) -> str:
    content = _load_text_templates().get(template_id, fallback)
    for name, value in variables.items():
        content = content.replace("{" + name + "}", value)
    return content


def _staff_rich_menu_id() -> str:
    current_id = get_current_rich_menu_id("staff")
    if current_id:
        return current_id
    try:
        payload = json.loads(
            (PROJECT_ROOT / "config" / "rich_menu_ids.json").read_text(encoding="utf-8")
        )
        return str(payload.get("staff_rich_menu_id") or "")
    except (OSError, ValueError):
        return ""


def _ensure_order_for_case_no(cursor, client_id: int, case_no: str | None) -> None:
    normalized_case_no = str(case_no or "").strip()
    if not normalized_case_no:
        return
    cursor.execute("SELECT client_id FROM orders WHERE case_no=%s", (normalized_case_no,))
    existing = cursor.fetchone()
    if existing:
        existing_client_id = existing.get("client_id") if isinstance(existing, dict) else existing[0]
        if int(existing_client_id) != int(client_id):
            raise LineReviewDataConflictError(
                f"案件編號 {normalized_case_no} 已連結其他客戶，無法完成重新綁定"
            )
        return
    cursor.execute(
        "INSERT INTO orders (case_no,client_id) VALUES (%s,%s)",
        (normalized_case_no, client_id),
    )


def get_line_review_summary() -> dict[str, int]:
    taipei_now = datetime.now(TAIPEI_TIMEZONE)
    day_start = taipei_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_day_start = day_start.astimezone(timezone.utc).replace(tzinfo=None)
    utc_day_end = (day_start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    try:
        stale_hours = max(1, int(os.getenv("LINE_REVIEW_STALE_HOURS", "24")))
    except ValueError:
        stale_hours = 24
    stale_before = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=stale_hours)
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    SUM(status='pending') AS pending_total,
                    SUM(status='pending' AND request_type='staff_verification') AS staff_pending,
                    SUM(status='pending' AND request_type='client_rebind') AS rebind_pending,
                    SUM(status IN ('approved','rejected')
                        AND reviewed_at >= %s AND reviewed_at < %s) AS processed_today,
                    SUM(status='pending' AND created_at < %s) AS stale_pending
                FROM line_confirmation_requests
                """,
                (utc_day_start, utc_day_end, stale_before),
            )
            row = cursor.fetchone() or {}
        return {
            "pending_total": _as_int(row.get("pending_total")),
            "staff_pending": _as_int(row.get("staff_pending")),
            "rebind_pending": _as_int(row.get("rebind_pending")),
            "processed_today": _as_int(row.get("processed_today")),
            "stale_pending": _as_int(row.get("stale_pending")),
            "stale_hours": stale_hours,
        }
    finally:
        conn.close()


def list_line_reviews(
    *,
    request_type: str | None = None,
    status: str | None = "pending",
    search: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    if request_type and request_type not in REQUEST_TYPES:
        raise ValueError("不支援的審查類型")
    if status and status not in REQUEST_STATUSES:
        raise ValueError("不支援的審查狀態")
    clauses = ["1=1"]
    params: list[Any] = []
    if request_type:
        clauses.append("r.request_type=%s")
        params.append(request_type)
    if status:
        clauses.append("r.status=%s")
        params.append(status)
    if search and search.strip():
        keyword = f"%{search.strip()}%"
        clauses.append(
            "(CAST(r.id AS CHAR) LIKE %s OR r.client_name LIKE %s "
            "OR r.submitted_name LIKE %s "
            "OR r.line_user_id LIKE %s OR r.old_line_user_id LIKE %s "
            "OR r.new_line_user_id LIKE %s)"
        )
        params.extend([keyword] * 6)
    if created_from:
        clauses.append("r.created_at >= %s")
        params.append(_utc_naive(created_from))
    if created_to:
        clauses.append("r.created_at <= %s")
        params.append(_utc_naive(created_to))

    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size
    where_sql = " AND ".join(clauses)
    order_sql = "r.created_at ASC, r.id ASC" if status == "pending" else "r.created_at DESC, r.id DESC"
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                f"SELECT COUNT(*) AS total FROM line_confirmation_requests r WHERE {where_sql}",
                params,
            )
            total = _as_int((cursor.fetchone() or {}).get("total"))
            cursor.execute(
                f"""
                SELECT r.id, r.request_type, r.status, r.client_id, r.client_name,
                       r.line_user_id, r.old_line_user_id, r.new_line_user_id,
                       r.matched_staff_id, r.match_status, r.submitted_name,
                       r.submitted_birthday, r.submitted_identity_last4, r.submitted_at,
                       r.decision_reason, r.created_at, r.reviewed_at, r.resolved_at,
                       a.display_name AS reviewer_display_name
                FROM line_confirmation_requests r
                LEFT JOIN admin_users a ON a.id=r.reviewed_by_admin_user_id
                WHERE {where_sql}
                ORDER BY {order_sql}
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, offset],
            )
            rows = list(cursor.fetchall())
        items = []
        for row in rows:
            item = dict(row)
            item["line_user_id_masked"] = _mask_line_id(item.pop("line_user_id", None))
            item["old_line_user_id_masked"] = _mask_line_id(item.pop("old_line_user_id", None))
            item["new_line_user_id_masked"] = _mask_line_id(item.pop("new_line_user_id", None))
            item["display_name"] = (
                item.get("submitted_name")
                or item.get("client_name")
                or item["line_user_id_masked"]
            )
            items.append(item)
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }
    finally:
        conn.close()


def get_line_review(request_id: int) -> dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT r.*, c.case_no,
                       c.line_user_id AS current_client_line_user_id,
                       s.name AS matched_staff_name,
                       s.phone AS matched_staff_phone,
                       s.identity_card AS matched_staff_identity_card,
                       s.birthday AS matched_staff_birthday,
                       s.status AS matched_staff_status,
                       s.line_user_id AS matched_staff_line_user_id,
                       lu.role AS current_line_role,
                       lu.status AS current_line_status,
                       a.username AS reviewer_username,
                       a.display_name AS reviewer_display_name
                FROM line_confirmation_requests r
                LEFT JOIN clients c ON c.id=r.client_id
                LEFT JOIN staff s ON s.id=r.matched_staff_id
                LEFT JOIN line_users lu ON lu.line_user_id=r.line_user_id
                LEFT JOIN admin_users a ON a.id=r.reviewed_by_admin_user_id
                WHERE r.id=%s
                """,
                (request_id,),
            )
            item = cursor.fetchone()
        if not item:
            raise LineReviewNotFoundError(f"找不到審查申請 #{request_id}")
        result = dict(item)
        result["matched_staff_identity_masked"] = _mask_identity_card(
            result.pop("matched_staff_identity_card", None)
        )
        return result
    finally:
        conn.close()


def _lock_pending_request(cursor, request_id: int) -> dict[str, Any]:
    cursor.execute(
        "SELECT * FROM line_confirmation_requests WHERE id=%s FOR UPDATE",
        (request_id,),
    )
    item = cursor.fetchone()
    if not item:
        raise LineReviewNotFoundError(f"找不到審查申請 #{request_id}")
    if item["status"] != "pending":
        raise LineReviewStateConflictError(request_id, item["status"])
    if item["request_type"] not in REQUEST_TYPES:
        raise LineReviewDataConflictError("審查申請類型不受支援")
    return dict(item)


def _record_decision(
    cursor,
    *,
    request_id: int,
    status: str,
    admin_user_id: int | None,
    reviewer_line_user_id: str | None,
    reason: str,
) -> None:
    cursor.execute(
        """
        UPDATE line_confirmation_requests
        SET status=%s, reviewed_by_admin_user_id=%s,
            reviewed_by_line_user_id=%s, decision_reason=%s,
            reviewed_at=UTC_TIMESTAMP(), resolved_at=UTC_TIMESTAMP()
        WHERE id=%s
        """,
        (
            status,
            admin_user_id,
            reviewer_line_user_id or None,
            reason.strip() or None,
            request_id,
        ),
    )


def approve_line_review(
    request_id: int,
    *,
    admin_user_id: int | None,
    reviewer_line_user_id: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            item = _lock_pending_request(cursor, request_id)
            if item["request_type"] == "staff_verification":
                line_user_id = str(item.get("line_user_id") or "").strip()
                if not line_user_id:
                    raise LineReviewDataConflictError("月嫂身分申請缺少 LINE 使用者")
                if item.get("match_status") != "matched" or not item.get("matched_staff_id"):
                    raise LineReviewDataConflictError(
                        "月嫂尚未完成 LIFF 資料比對，或未找到唯一的既有月嫂資料"
                    )
                cursor.execute(
                    "SELECT id,name,line_user_id,status FROM staff WHERE id=%s FOR UPDATE",
                    (item["matched_staff_id"],),
                )
                staff = cursor.fetchone()
                if not staff:
                    raise LineReviewDataConflictError("比對到的月嫂資料已不存在")
                bound_line_user_id = str(staff.get("line_user_id") or "").strip()
                if bound_line_user_id and bound_line_user_id != line_user_id:
                    raise LineReviewDataConflictError("此月嫂資料已綁定其他 LINE 帳號")
                cursor.execute(
                    "SELECT id FROM staff WHERE line_user_id=%s AND id<>%s LIMIT 1 FOR UPDATE",
                    (line_user_id, item["matched_staff_id"]),
                )
                if cursor.fetchone():
                    raise LineReviewDataConflictError("此 LINE 帳號已綁定其他月嫂資料")
                cursor.execute(
                    "UPDATE staff SET line_user_id=%s WHERE id=%s",
                    (line_user_id, item["matched_staff_id"]),
                )
                cursor.execute(
                    """
                    INSERT INTO line_users (line_user_id,role,status,last_event_at)
                    VALUES (%s,'staff','active',UTC_TIMESTAMP())
                    ON DUPLICATE KEY UPDATE role='staff',status='active',last_event_at=UTC_TIMESTAMP()
                    """,
                    (line_user_id,),
                )
                enqueue_line_task(
                    cursor,
                    to_user_id=line_user_id,
                    task_type="rich_menu_link",
                    payload={
                        "rich_menu_id": _staff_rich_menu_id(),
                        "success_message": _template(
                            "staff_switch_success", "月嫂身分已由工會確認通過。"
                        ),
                    },
                    idempotency_key=f"staff-review-approved:{request_id}",
                )
                message = "已核准月嫂身分並切換專屬選單"
            else:
                client_id = item.get("client_id")
                new_line_user_id = str(item.get("new_line_user_id") or "").strip()
                old_line_user_id = str(item.get("old_line_user_id") or "").strip()
                if not client_id or not new_line_user_id:
                    raise LineReviewDataConflictError("重新綁定申請缺少客戶或新 LINE 資料")
                cursor.execute(
                    "SELECT id,name,case_no,line_user_id FROM clients WHERE id=%s FOR UPDATE",
                    (client_id,),
                )
                client = cursor.fetchone()
                if not client:
                    raise LineReviewDataConflictError("客戶資料已不存在，無法重新綁定")
                current_line_user_id = str(client.get("line_user_id") or "").strip()
                if current_line_user_id != old_line_user_id:
                    raise LineReviewDataConflictError(
                        "客戶目前綁定資料已在申請後變更，請重新確認後再建立申請"
                    )
                cursor.execute(
                    "SELECT id FROM clients WHERE line_user_id=%s AND id<>%s LIMIT 1 FOR UPDATE",
                    (new_line_user_id, client_id),
                )
                if cursor.fetchone():
                    raise LineReviewDataConflictError("新的 LINE 帳號已綁定其他客戶")
                cursor.execute(
                    "UPDATE clients SET line_user_id=%s WHERE id=%s",
                    (new_line_user_id, client_id),
                )
                _ensure_order_for_case_no(cursor, int(client_id), client.get("case_no"))
                client_name = str(item.get("client_name") or client.get("name") or "")
                enqueue_line_task(
                    cursor,
                    to_user_id=new_line_user_id,
                    message_content=_template(
                        "client_rebind_approved",
                        "【系統通知】\n您的帳號重新綁定申請已審核通過。",
                        client_name=client_name,
                    ),
                    idempotency_key=f"client-rebind-approved:{request_id}",
                )
                message = "已確認並完成重新綁定"
            _record_decision(
                cursor,
                request_id=request_id,
                status="approved",
                admin_user_id=admin_user_id,
                reviewer_line_user_id=reviewer_line_user_id,
                reason=reason,
            )
        conn.commit()
        return {
            "request_id": request_id,
            "request_type": item["request_type"],
            "status": "approved",
            "message": message,
            "worker_wakeup_required": True,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reject_line_review(
    request_id: int,
    *,
    admin_user_id: int | None,
    reviewer_line_user_id: str | None = None,
    reason: str,
) -> dict[str, Any]:
    reason = reason.strip()
    if not reason:
        raise ValueError("拒絕申請時必須填寫原因")
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            item = _lock_pending_request(cursor, request_id)
            if item["request_type"] == "staff_verification":
                target_user_id = str(item.get("line_user_id") or "").strip()
                content = _template(
                    "staff_verification_rejected",
                    "您的月嫂身分驗證申請未通過，請聯絡工會服務人員。",
                )
                message = "已拒絕月嫂身分申請"
                idempotency_key = f"staff-review-rejected:{request_id}"
            else:
                target_user_id = str(item.get("new_line_user_id") or "").strip()
                content = _template(
                    "client_rebind_rejected",
                    "【系統通知】\n您的帳號重新綁定申請未通過。",
                )
                message = "已拒絕重新綁定申請"
                idempotency_key = f"client-rebind-rejected:{request_id}"
            if not target_user_id:
                raise LineReviewDataConflictError("審查申請缺少通知對象")
            enqueue_line_task(
                cursor,
                to_user_id=target_user_id,
                message_content=content,
                idempotency_key=idempotency_key,
            )
            _record_decision(
                cursor,
                request_id=request_id,
                status="rejected",
                admin_user_id=admin_user_id,
                reviewer_line_user_id=reviewer_line_user_id,
                reason=reason,
            )
        conn.commit()
        return {
            "request_id": request_id,
            "request_type": item["request_type"],
            "status": "rejected",
            "message": message,
            "worker_wakeup_required": True,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
