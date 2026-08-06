"""
================================================================================
檔案名稱: services/line_order_group_service.py
功能說明: 訂單 LINE 群組綁定、預期成員、一次性邀請任務與網址遮蔽服務
================================================================================
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any
from urllib.parse import urlparse

import pymysql

from services.admin_auth_service import ROLE_LEVELS
from services.db_service import get_connection
from services.line_task_service import enqueue_line_task


ACTIVE_BINDING_STATUSES = {"awaiting_invite", "inviting", "active"}
INVITE_COMMAND_PREFIX = "發送邀請連結"
INVITE_REDACTED_VALUE = "[REDACTED]"
ORDER_BIND_RE = re.compile(r"^綁定訂單\s+([A-Za-z0-9_-]{1,50})$")
INVITE_COMMAND_RE = re.compile(r"^發送邀請連結\s+(\S+)$")


class LineOrderGroupError(RuntimeError):
    pass


class LineOrderGroupPermissionError(LineOrderGroupError):
    pass


class LineOrderGroupNotFoundError(LineOrderGroupError):
    pass


class LineOrderGroupConflictError(LineOrderGroupError):
    pass


def _require_linked_admin(cursor, line_user_id: str, minimum_role: str = "line_agent") -> dict:
    cursor.execute(
        """
        SELECT id, display_name, role
        FROM admin_users
        WHERE linked_line_user_id=%s AND enabled=1
        """,
        (line_user_id,),
    )
    admin = cursor.fetchone()
    if not admin or ROLE_LEVELS.get(admin["role"], 0) < ROLE_LEVELS[minimum_role]:
        raise LineOrderGroupPermissionError(
            f"此操作需要已綁定 LINE 的 {minimum_role} 或更高權限工會人員。"
        )
    return admin


def _validate_invite_url(value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "line.me":
        raise LineOrderGroupError("邀請連結必須是 https://line.me 的 LINE 群組邀請網址。")
    if not re.fullmatch(r"/(?:R/)?ti/g/[^/?#]+", parsed.path):
        raise LineOrderGroupError("LINE 群組邀請網址格式不正確。")
    if parsed.params or parsed.query or parsed.fragment:
        raise LineOrderGroupError("LINE 群組邀請網址不可包含額外參數。")
    return url


def redact_invite_text(text: str) -> str:
    match = INVITE_COMMAND_RE.fullmatch((text or "").strip())
    return f"{INVITE_COMMAND_PREFIX} {INVITE_REDACTED_VALUE}" if match else text


def redact_invite_event(event: dict[str, Any]) -> dict[str, Any]:
    sanitized = copy.deepcopy(event)
    message = sanitized.get("message") or {}
    if message.get("type") == "text":
        message["text"] = redact_invite_text(str(message.get("text") or ""))
    return sanitized


def _redacted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    if "invite_url" in sanitized:
        sanitized["invite_url"] = INVITE_REDACTED_VALUE
        sanitized["invite_url_redacted"] = True
    return sanitized


def sanitize_task_for_output(task: dict[str, Any]) -> dict[str, Any]:
    result = dict(task)
    if result.get("task_type") != "order_group_invite":
        return result
    payload = result.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    result["payload_json"] = json.dumps(
        _redacted_payload(payload or {}), ensure_ascii=False
    )
    return result


def bind_order_group(
    cursor,
    *,
    group_id: str,
    case_no: str,
    actor_line_user_id: str,
) -> dict[str, Any]:
    admin = _require_linked_admin(cursor, actor_line_user_id)
    cursor.execute(
        """
        SELECT o.case_no, o.status AS order_status, o.line_group_id,
               c.id AS client_id, c.name AS client_name, c.line_user_id AS client_line_user_id,
               s.id AS staff_id, s.name AS staff_name, s.line_user_id AS staff_line_user_id
        FROM orders o
        JOIN clients c ON c.id=o.client_id
        LEFT JOIN staff s ON s.id=o.staff_id
        WHERE o.case_no=%s
        FOR UPDATE
        """,
        (case_no,),
    )
    order = cursor.fetchone()
    if not order:
        raise LineOrderGroupNotFoundError(f"找不到訂單 {case_no}。")
    if order["order_status"] == "訂單取消":
        raise LineOrderGroupConflictError("已取消的訂單不能綁定服務群組。")
    if not order.get("staff_id"):
        raise LineOrderGroupConflictError("此訂單尚未指派月嫂，不能建立三方服務群組。")

    cursor.execute(
        "SELECT case_no,status,id FROM line_order_group_bindings WHERE line_group_id=%s FOR UPDATE",
        (group_id,),
    )
    existing_group = cursor.fetchone()
    if existing_group:
        if existing_group["case_no"] == case_no and existing_group["status"] in ACTIVE_BINDING_STATUSES:
            return get_order_group_by_binding_id(cursor, int(existing_group["id"]))
        raise LineOrderGroupConflictError("此 LINE 群組已綁定其他訂單。")
    if order.get("line_group_id") and order["line_group_id"] != group_id:
        raise LineOrderGroupConflictError("此訂單已有服務群組，請由 LINE 管理中心先解除或更換。")

    cursor.execute(
        """
        SELECT 1 FROM line_alert_notification_targets
        WHERE target_type='group' AND line_target_id=%s AND enabled=1
        """,
        (group_id,),
    )
    if cursor.fetchone():
        raise LineOrderGroupConflictError("異常通知群組不能同時作為訂單服務群組。")

    cursor.execute("UPDATE orders SET line_group_id=%s WHERE case_no=%s", (group_id, case_no))
    cursor.execute(
        """
        INSERT INTO line_order_group_bindings (
            case_no, line_group_id, bound_by_admin_user_id, bound_by_line_user_id
        ) VALUES (%s,%s,%s,%s)
        """,
        (case_no, group_id, admin["id"], actor_line_user_id),
    )
    binding_id = int(cursor.lastrowid)
    members = [
        ("client", order["client_id"], order.get("client_line_user_id")),
        ("staff", order["staff_id"], order.get("staff_line_user_id")),
    ]
    for participant_type, record_id, line_user_id in members:
        status = "pending" if line_user_id else "not_ready"
        cursor.execute(
            """
            INSERT INTO line_order_group_members (
                binding_id, participant_type, participant_record_id,
                line_user_id, invitation_status
            ) VALUES (%s,%s,%s,%s,%s)
            """,
            (binding_id, participant_type, record_id, line_user_id, status),
        )
    return get_order_group_by_binding_id(cursor, binding_id)


def create_invite_tasks(
    cursor,
    *,
    group_id: str,
    actor_line_user_id: str,
    invite_url: str,
    source_event_id: str | None,
) -> dict[str, Any]:
    admin = _require_linked_admin(cursor, actor_line_user_id)
    invite_url = _validate_invite_url(invite_url)
    cursor.execute(
        """
        SELECT b.id,b.case_no,b.status
        FROM line_order_group_bindings b
        WHERE b.line_group_id=%s AND b.status IN ('awaiting_invite','inviting','active')
        FOR UPDATE
        """,
        (group_id,),
    )
    binding = cursor.fetchone()
    if not binding:
        raise LineOrderGroupNotFoundError("本群組尚未綁定訂單，請先輸入「綁定訂單 案件編號」。")

    cursor.execute(
        """
        SELECT m.*, lu.status AS line_status
        FROM line_order_group_members m
        LEFT JOIN line_users lu ON lu.line_user_id=m.line_user_id
        WHERE m.binding_id=%s AND m.invitation_status<>'joined'
        ORDER BY m.participant_type
        FOR UPDATE
        """,
        (binding["id"],),
    )
    members = list(cursor.fetchall())
    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    for member in members:
        label = "媽媽" if member["participant_type"] == "client" else "月嫂"
        if not member.get("line_user_id"):
            skipped.append(f"{label}尚未綁定 LINE")
            continue
        if member.get("line_status") == "blocked":
            skipped.append(f"{label}已封鎖官方帳號")
            continue
        task_id = enqueue_line_task(
            cursor,
            to_user_id=member["line_user_id"],
            task_type="order_group_invite",
            message_content=f"訂單 {binding['case_no']} 服務群組邀請（{label}）",
            payload={
                "binding_id": int(binding["id"]),
                "case_no": binding["case_no"],
                "participant_type": member["participant_type"],
                "participant_record_id": int(member["participant_record_id"]),
                "invite_url": invite_url,
                "issued_by_admin_user_id": int(admin["id"]),
            },
            source_event_id=source_event_id,
            idempotency_key=f"order-group-invite:{source_event_id}:{member['id']}",
        )
        if task_id:
            cursor.execute(
                """
                UPDATE line_order_group_members
                SET invitation_status='pending', invite_task_id=%s
                WHERE id=%s
                """,
                (task_id, member["id"]),
            )
            created.append({"task_id": int(task_id), "recipient": label})
    if created:
        cursor.execute(
            "UPDATE line_order_group_bindings SET status='inviting' WHERE id=%s",
            (binding["id"],),
        )
    return {"case_no": binding["case_no"], "created": created, "skipped": skipped}


def build_invite_flex_message(task: dict[str, Any]) -> list[dict[str, Any]]:
    payload = json.loads(task.get("payload_json") or "{}")
    invite_url = payload.get("invite_url")
    if not invite_url or invite_url == INVITE_REDACTED_VALUE:
        raise ValueError("邀請網址已清除，請由工會人員重新發送邀請連結。")
    label = "媽媽" if payload.get("participant_type") == "client" else "月嫂"
    case_no = str(payload.get("case_no") or "")
    return [
        {
            "type": "flex",
            "altText": f"訂單 {case_no} 服務群組邀請",
            "contents": {
                "type": "bubble",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "md",
                    "contents": [
                        {"type": "text", "text": "服務群組邀請", "weight": "bold", "size": "xl"},
                        {"type": "text", "text": f"案件編號：{case_no}", "size": "sm", "color": "#666666"},
                        {"type": "text", "text": f"{label}您好，請點下方按鈕加入本案的 LINE 服務群組。", "wrap": True},
                    ],
                },
                "footer": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "button",
                            "style": "primary",
                            "color": "#06C755",
                            "action": {"type": "uri", "label": "加入服務群組", "uri": invite_url},
                        }
                    ],
                },
            },
        }
    ]


def finalize_invite_task(cursor, task: dict[str, Any], final_status: str) -> None:
    if task.get("task_type") != "order_group_invite" or final_status == "pending":
        return
    payload = json.loads(task.get("payload_json") or "{}")
    member_status = "sent" if final_status == "sent" else "failed"
    cursor.execute(
        """
        UPDATE line_order_group_members
        SET invitation_status=%s, sent_at=IF(%s='sent',UTC_TIMESTAMP(),sent_at)
        WHERE invite_task_id=%s
        """,
        (member_status, member_status, task["id"]),
    )
    cursor.execute(
        "UPDATE line_tasks SET payload_json=%s WHERE id=%s",
        (json.dumps(_redacted_payload(payload), ensure_ascii=False), task["id"]),
    )


def expire_stale_invite_tasks() -> int:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT * FROM line_tasks
                WHERE task_type='order_group_invite' AND status='pending'
                  AND created_at < UTC_TIMESTAMP() - INTERVAL 24 HOUR
                FOR UPDATE SKIP LOCKED
                """
            )
            tasks = list(cursor.fetchall())
            for task in tasks:
                cursor.execute(
                    """
                    UPDATE line_tasks
                    SET status='cancelled', next_retry_at=NULL,
                        error_code='invite_expired', error_message='邀請連結已超過 24 小時保留期限'
                    WHERE id=%s
                    """,
                    (task["id"],),
                )
                finalize_invite_task(cursor, task, "cancelled")
        conn.commit()
        return len(tasks)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_joined_members(cursor, group_id: str, user_ids: list[str]) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT id,case_no FROM line_order_group_bindings
        WHERE line_group_id=%s AND status IN ('awaiting_invite','inviting','active')
        FOR UPDATE
        """,
        (group_id,),
    )
    binding = cursor.fetchone()
    if not binding:
        return {"matched": 0, "unexpected": len(user_ids), "case_no": None}
    matched = 0
    for user_id in user_ids:
        cursor.execute(
            """
            UPDATE line_order_group_members
            SET invitation_status='joined', joined_at=UTC_TIMESTAMP(), left_at=NULL
            WHERE binding_id=%s AND line_user_id=%s
            """,
            (binding["id"], user_id),
        )
        matched += cursor.rowcount
    cursor.execute(
        """
        SELECT SUM(invitation_status<>'joined') AS remaining
        FROM line_order_group_members WHERE binding_id=%s
        """,
        (binding["id"],),
    )
    row = cursor.fetchone() or {}
    remaining = int(row.get("remaining") or 0)
    if remaining == 0:
        cursor.execute(
            "UPDATE line_order_group_bindings SET status='active' WHERE id=%s",
            (binding["id"],),
        )
    return {
        "matched": matched,
        "unexpected": max(0, len(user_ids) - matched),
        "remaining": remaining,
        "case_no": binding["case_no"],
    }


def record_left_members(cursor, group_id: str, user_ids: list[str]) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT id,case_no FROM line_order_group_bindings
        WHERE line_group_id=%s AND status IN ('awaiting_invite','inviting','active')
        FOR UPDATE
        """,
        (group_id,),
    )
    binding = cursor.fetchone()
    if not binding:
        return {"matched": 0, "case_no": None}
    matched = 0
    for user_id in user_ids:
        cursor.execute(
            """
            UPDATE line_order_group_members
            SET invitation_status='left',left_at=UTC_TIMESTAMP()
            WHERE binding_id=%s AND line_user_id=%s
            """,
            (binding["id"], user_id),
        )
        matched += cursor.rowcount
    if matched:
        cursor.execute(
            "UPDATE line_order_group_bindings SET status='inviting' WHERE id=%s",
            (binding["id"],),
        )
    return {"matched": matched, "case_no": binding["case_no"]}


def mark_group_left(cursor, group_id: str) -> bool:
    cursor.execute(
        """
        SELECT id,case_no FROM line_order_group_bindings
        WHERE line_group_id=%s AND status IN ('awaiting_invite','inviting','active')
        FOR UPDATE
        """,
        (group_id,),
    )
    binding = cursor.fetchone()
    if not binding:
        return False
    cursor.execute(
        "UPDATE line_order_group_bindings SET status='left',deactivated_at=UTC_TIMESTAMP() WHERE id=%s",
        (binding["id"],),
    )
    cursor.execute(
        "UPDATE orders SET line_group_id=NULL WHERE case_no=%s AND line_group_id=%s",
        (binding["case_no"], group_id),
    )
    return True


def get_order_group_by_binding_id(cursor, binding_id: int) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT b.*, a.display_name AS bound_by_name
        FROM line_order_group_bindings b
        JOIN admin_users a ON a.id=b.bound_by_admin_user_id
        WHERE b.id=%s
        """,
        (binding_id,),
    )
    binding = cursor.fetchone()
    if not binding:
        raise LineOrderGroupNotFoundError("找不到訂單群組綁定。")
    cursor.execute(
        """
        SELECT participant_type,participant_record_id,line_user_id,
               invitation_status,sent_at,joined_at,left_at
        FROM line_order_group_members WHERE binding_id=%s
        ORDER BY participant_type
        """,
        (binding_id,),
    )
    binding["members"] = list(cursor.fetchall())
    return binding


def list_order_groups(*, status: str | None = None, case_no: str | None = None) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if status:
        clauses.append("b.status=%s")
        params.append(status)
    if case_no:
        clauses.append("b.case_no LIKE %s")
        params.append(f"%{case_no.strip()}%")
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT b.id,b.case_no,b.status,b.created_at,b.updated_at,b.deactivated_at,
                       a.display_name AS bound_by_name,
                       SUM(m.invitation_status='joined') AS joined_count,
                       COUNT(m.id) AS expected_count
                FROM line_order_group_bindings b
                JOIN admin_users a ON a.id=b.bound_by_admin_user_id
                LEFT JOIN line_order_group_members m ON m.binding_id=b.id
                WHERE {' AND '.join(clauses)}
                GROUP BY b.id
                ORDER BY b.created_at DESC
                LIMIT 200
                """,
                params,
            )
            return list(cursor.fetchall())
    finally:
        conn.close()


def get_order_group(*, binding_id: int | None = None, case_no: str | None = None) -> dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            if binding_id is None:
                cursor.execute(
                    """
                    SELECT id FROM line_order_group_bindings
                    WHERE case_no=%s ORDER BY created_at DESC LIMIT 1
                    """,
                    (case_no,),
                )
                row = cursor.fetchone()
                if not row:
                    raise LineOrderGroupNotFoundError(f"訂單 {case_no} 尚未建立 LINE 服務群組。")
                binding_id = int(row["id"])
            return get_order_group_by_binding_id(cursor, binding_id)
    finally:
        conn.close()


def unbind_order_group(binding_id: int, *, actor_admin_user_id: int | None) -> dict[str, Any]:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT * FROM line_order_group_bindings WHERE id=%s FOR UPDATE",
                (binding_id,),
            )
            binding = cursor.fetchone()
            if not binding:
                raise LineOrderGroupNotFoundError("找不到訂單群組綁定。")
            if binding["status"] not in ACTIVE_BINDING_STATUSES:
                raise LineOrderGroupConflictError("此群組綁定已經停止。")
            cursor.execute(
                "UPDATE line_order_group_bindings SET status='cancelled',deactivated_at=UTC_TIMESTAMP() WHERE id=%s",
                (binding_id,),
            )
            cursor.execute(
                "UPDATE orders SET line_group_id=NULL WHERE case_no=%s AND line_group_id=%s",
                (binding["case_no"], binding["line_group_id"]),
            )
        conn.commit()
        return {"id": binding_id, "case_no": binding["case_no"], "status": "cancelled"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
