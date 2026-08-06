"""
================================================================================
檔案名稱: services/line_task_admin_service.py
功能說明: LINE 任務管理服務，提供統計、清單、明細及人工執行、取消與重送操作
================================================================================
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pymysql

from services.db_service import get_connection
from services.line_order_group_service import (
    INVITE_REDACTED_VALUE,
    finalize_invite_task,
    sanitize_task_for_output,
)


TASK_STATUSES = {"pending", "processing", "sent", "failed", "cancelled"}
TASK_TYPES = {
    "line_push",
    "line_push_messages",
    "rag_reply",
    "rich_menu_link",
    "rich_menu_unlink",
    "order_group_invite",
}
TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")


class LineTaskNotFoundError(LookupError):
    pass


class LineTaskStateConflictError(RuntimeError):
    def __init__(self, task_id: int, current_status: str, action: str):
        super().__init__(f"任務 #{task_id} 目前狀態為 {current_status}，不能執行{action}")
        self.task_id = task_id
        self.current_status = current_status


def _as_int(value: Any) -> int:
    return int(value or 0)


def get_line_task_summary() -> dict[str, Any]:
    taipei_now = datetime.now(TAIPEI_TIMEZONE)
    taipei_day_start = taipei_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_day_start = taipei_day_start.astimezone(timezone.utc).replace(tzinfo=None)
    utc_day_end = (taipei_day_start + timedelta(days=1)).astimezone(timezone.utc).replace(
        tzinfo=None
    )
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    SUM(status='pending') AS pending,
                    SUM(status='pending' AND scheduled_at <= UTC_TIMESTAMP()
                        AND (next_retry_at IS NULL OR next_retry_at <= UTC_TIMESTAMP())) AS due,
                    SUM(status='processing') AS processing,
                    SUM(status='sent' AND sent_at >= %s AND sent_at < %s) AS sent_today,
                    SUM(status='failed') AS failed,
                    SUM(status='cancelled') AS cancelled,
                    MIN(CASE WHEN status='pending'
                        THEN GREATEST(scheduled_at,COALESCE(next_retry_at,scheduled_at)) END) AS next_run_at
                FROM line_tasks
                """,
                (utc_day_start, utc_day_end),
            )
            row = cursor.fetchone() or {}
        return {
            "pending": _as_int(row.get("pending")),
            "due": _as_int(row.get("due")),
            "processing": _as_int(row.get("processing")),
            "sent_today": _as_int(row.get("sent_today")),
            "failed": _as_int(row.get("failed")),
            "cancelled": _as_int(row.get("cancelled")),
            "next_run_at": row.get("next_run_at"),
        }
    finally:
        conn.close()


def list_line_tasks(
    *,
    status: str | None = None,
    task_type: str | None = None,
    user_id: str | None = None,
    onboarding_only: bool = False,
    scheduled_from: datetime | None = None,
    scheduled_to: datetime | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    if status and status not in TASK_STATUSES:
        raise ValueError("不支援的任務狀態")
    if task_type and task_type not in TASK_TYPES:
        raise ValueError("不支援的任務類型")

    clauses = ["1=1"]
    params: list[Any] = []
    if status:
        clauses.append("status=%s")
        params.append(status)
    if task_type:
        clauses.append("task_type=%s")
        params.append(task_type)
    if user_id:
        clauses.append("to_user_id LIKE %s")
        params.append(f"%{user_id.strip()}%")
    if onboarding_only:
        clauses.append("idempotency_key LIKE 'onboarding:%'")
    if scheduled_from:
        clauses.append("scheduled_at >= %s")
        params.append(scheduled_from)
    if scheduled_to:
        clauses.append("scheduled_at <= %s")
        params.append(scheduled_to)

    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size
    where_sql = " AND ".join(clauses)
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(f"SELECT COUNT(*) AS total FROM line_tasks WHERE {where_sql}", params)
            total = _as_int((cursor.fetchone() or {}).get("total"))
            cursor.execute(
                f"""
                SELECT id, to_user_id, task_type, status,
                       LEFT(message_content,200) AS message_preview,
                       scheduled_at, processing_started_at, retry_count, max_retries,
                       next_retry_at, sent_at, failed_at, error_code,
                       source_event_id, idempotency_key, created_at, updated_at
                FROM line_tasks
                WHERE {where_sql}
                ORDER BY scheduled_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                [*params, page_size, offset],
            )
            items = list(cursor.fetchall())
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }
    finally:
        conn.close()


def get_line_task(task_id: int) -> dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT * FROM line_tasks WHERE id=%s", (task_id,))
            task = cursor.fetchone()
            if not task:
                raise LineTaskNotFoundError(f"找不到 LINE 任務 #{task_id}")
            cursor.execute(
                """
                SELECT id, attempt_no, outcome, retryable, error_code,
                       error_message, line_request_id, started_at, finished_at
                FROM line_task_attempts
                WHERE task_id=%s
                ORDER BY attempt_no DESC
                """,
                (task_id,),
            )
            attempts = list(cursor.fetchall())
        return {"task": sanitize_task_for_output(task), "attempts": attempts}
    finally:
        conn.close()


def _transition_task(task_id: int, *, action: str, allowed_status: str, sql: str) -> dict:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT id,status FROM line_tasks WHERE id=%s FOR UPDATE", (task_id,))
            task = cursor.fetchone()
            if not task:
                raise LineTaskNotFoundError(f"找不到 LINE 任務 #{task_id}")
            if task["status"] != allowed_status:
                raise LineTaskStateConflictError(task_id, task["status"], action)
            cursor.execute(sql, (task_id,))
        conn.commit()
        return {"task_id": task_id, "previous_status": allowed_status, "action": action}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_line_task(task_id: int) -> dict:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT * FROM line_tasks WHERE id=%s FOR UPDATE", (task_id,))
            task = cursor.fetchone()
            if not task:
                raise LineTaskNotFoundError(f"找不到 LINE 任務 #{task_id}")
            if task["status"] != "pending":
                raise LineTaskStateConflictError(task_id, task["status"], "取消")
            cursor.execute(
                """
                UPDATE line_tasks SET status='cancelled', next_retry_at=NULL,
                    processing_started_at=NULL WHERE id=%s
                """,
                (task_id,),
            )
            finalize_invite_task(cursor, task, "cancelled")
        conn.commit()
        return {
            "task_id": task_id,
            "previous_status": "pending",
            "action": "取消",
            "status": "cancelled",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_line_task_now(task_id: int) -> dict:
    result = _transition_task(
        task_id,
        action="立即執行",
        allowed_status="pending",
        sql="""
            UPDATE line_tasks
            SET scheduled_at=UTC_TIMESTAMP(), next_retry_at=NULL
            WHERE id=%s
        """,
    )
    result["status"] = "pending"
    return result


def retry_line_task(task_id: int) -> dict:
    task = get_line_task(task_id)["task"]
    if (
        task.get("task_type") == "order_group_invite"
        and INVITE_REDACTED_VALUE in str(task.get("payload_json") or "")
    ):
        raise ValueError("邀請網址已清除，請由工會人員在群組重新輸入發送指令。")
    result = _transition_task(
        task_id,
        action="重新執行",
        allowed_status="failed",
        sql="""
            UPDATE line_tasks
            SET status='pending', scheduled_at=UTC_TIMESTAMP(), retry_count=0,
                next_retry_at=NULL, failed_at=NULL, processing_started_at=NULL,
                error_code=NULL, error_message=NULL
            WHERE id=%s
        """,
    )
    result["status"] = "pending"
    return result
