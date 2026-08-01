"""
================================================================================
檔案名稱: services/line_admin_binding_service.py
功能說明: 工會人員 LINE 與管理後台帳號的一次性 Token、帳密驗證、綁定交易與稽核服務
================================================================================
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pymysql

from services.admin_auth_service import verify_admin_password
from services.db_service import get_connection
from services.line_liff_identity_service import LiffIdentityError, resolve_line_user_id
from services.line_rich_menu_service import get_current_rich_menu_id
from services.line_task_service import enqueue_line_task


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN_VALID_MINUTES = 15
MAX_BINDING_ATTEMPTS = 5


class LineAdminBindingError(ValueError):
    """Base error for the public administrator LINE binding flow."""


class LineAdminBindingNotFoundError(LineAdminBindingError):
    pass


class LineAdminBindingStateError(LineAdminBindingError):
    pass


class LineAdminBindingAuthenticationError(LineAdminBindingError):
    pass


class LineAdminBindingConflictError(LineAdminBindingError):
    pass


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_union_staff_rich_menu_id() -> str:
    current_id = get_current_rich_menu_id("union_staff")
    if current_id:
        return current_id
    try:
        data = json.loads(
            (PROJECT_ROOT / "config" / "rich_menu_ids.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return ""
    return str(data.get("union_staff_rich_menu_id") or "").strip()


def issue_line_admin_binding_token(cursor, *, line_user_id: str) -> tuple[int, str]:
    """Create one active short-lived binding request for a private LINE user."""
    normalized_line_user_id = (line_user_id or "").strip()
    if not normalized_line_user_id:
        raise LineAdminBindingError("缺少 LINE 使用者識別資料")

    cursor.execute(
        """
        INSERT INTO line_users (line_user_id, role, status, last_event_at)
        VALUES (%s,'customer','active',UTC_TIMESTAMP())
        ON DUPLICATE KEY UPDATE status='active', last_event_at=UTC_TIMESTAMP()
        """,
        (normalized_line_user_id,),
    )
    cursor.execute(
        """
        UPDATE line_admin_binding_requests
        SET status='cancelled', cancelled_at=UTC_TIMESTAMP()
        WHERE line_user_id=%s AND status='pending'
        """,
        (normalized_line_user_id,),
    )
    token = secrets.token_urlsafe(32)
    expires_at = _utc_now_naive() + timedelta(minutes=TOKEN_VALID_MINUTES)
    cursor.execute(
        """
        INSERT INTO line_admin_binding_requests (
            line_user_id, token_hash, expires_at
        ) VALUES (%s,%s,%s)
        """,
        (normalized_line_user_id, _token_hash(token), expires_at),
    )
    return int(cursor.lastrowid), token


def build_line_admin_binding_url(token: str) -> str:
    """Build a dedicated LIFF URL when configured, otherwise reuse the gateway."""
    dedicated_liff_id = os.getenv("LINE_ADMIN_BINDING_LIFF_ID", "").strip()
    if dedicated_liff_id and not dedicated_liff_id.startswith("your_"):
        return (
            f"https://liff.line.me/{quote(dedicated_liff_id, safe='')}"
            f"?token={quote(token, safe='')}"
        )

    shared_liff_id = os.getenv("LINE_LIFF_ID", "").strip()
    if shared_liff_id and not shared_liff_id.startswith("your_"):
        return (
            f"https://liff.line.me/{quote(shared_liff_id, safe='')}"
            f"?target=union-staff-binding&token={quote(token, safe='')}"
        )

    base_url = os.getenv("BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
    return f"{base_url}/union-staff-binding-page?token={quote(token, safe='')}"


def _get_request(cursor, token: str, *, for_update: bool) -> dict:
    suffix = " FOR UPDATE" if for_update else ""
    cursor.execute(
        f"""
        SELECT id,line_user_id,status,expires_at,attempt_count,admin_user_id,
               completed_at
        FROM line_admin_binding_requests
        WHERE token_hash=%s{suffix}
        """,
        (_token_hash((token or "").strip()),),
    )
    item = cursor.fetchone()
    if not item:
        raise LineAdminBindingNotFoundError("綁定連結無效，請回到 LINE 重新申請")
    return dict(item)


def _assert_request_available(cursor, item: dict) -> None:
    if item["status"] == "completed":
        return
    if item["status"] != "pending":
        raise LineAdminBindingStateError("這個綁定連結已失效，請回到 LINE 重新申請")
    if item["expires_at"] <= _utc_now_naive():
        cursor.execute(
            "UPDATE line_admin_binding_requests SET status='expired' WHERE id=%s",
            (item["id"],),
        )
        raise LineAdminBindingStateError("綁定連結已過期，請回到 LINE 重新申請")
    if int(item.get("attempt_count") or 0) >= MAX_BINDING_ATTEMPTS:
        cursor.execute(
            "UPDATE line_admin_binding_requests SET status='locked' WHERE id=%s",
            (item["id"],),
        )
        raise LineAdminBindingStateError("帳號驗證次數已達上限，請重新申請綁定")


def get_line_admin_binding_state(token: str) -> dict:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            item = _get_request(cursor, token, for_update=True)
            _assert_request_available(cursor, item)
        conn.commit()
        return {
            "request_id": int(item["id"]),
            "status": item["status"],
            "expires_at": item["expires_at"],
            "remaining_attempts": max(
                0, MAX_BINDING_ATTEMPTS - int(item.get("attempt_count") or 0)
            ),
        }
    except LineAdminBindingStateError:
        conn.commit()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _record_failed_attempt(cursor, item: dict) -> int:
    attempt_count = int(item.get("attempt_count") or 0) + 1
    next_status = "locked" if attempt_count >= MAX_BINDING_ATTEMPTS else "pending"
    cursor.execute(
        """
        UPDATE line_admin_binding_requests
        SET attempt_count=%s, last_attempt_at=UTC_TIMESTAMP(), status=%s
        WHERE id=%s
        """,
        (attempt_count, next_status, item["id"]),
    )
    return attempt_count


def complete_line_admin_binding(
    *,
    token: str,
    username: str,
    password: str,
    line_id_token: str | None,
    development_line_user_id: str | None,
) -> dict:
    """Verify LINE identity and admin credentials, then bind both records atomically."""
    try:
        trusted_line_user_id = resolve_line_user_id(
            id_token=line_id_token,
            development_user_id=development_line_user_id,
        )
    except LiffIdentityError as exc:
        raise LineAdminBindingAuthenticationError(str(exc)) from exc

    normalized_username = (username or "").strip().lower()
    if not normalized_username or not password:
        raise LineAdminBindingAuthenticationError("帳號或密碼不正確")

    rich_menu_id = _load_union_staff_rich_menu_id()
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            item = _get_request(cursor, token, for_update=True)
            _assert_request_available(cursor, item)
            if item["status"] == "completed":
                if not secrets.compare_digest(
                    str(item.get("line_user_id") or ""), trusted_line_user_id
                ):
                    raise LineAdminBindingAuthenticationError(
                        "此綁定連結不屬於目前登入的 LINE 帳號"
                    )
                conn.commit()
                return {
                    "request_id": int(item["id"]),
                    "status": "completed",
                    "already_completed": True,
                    "message": "此帳號已完成綁定。",
                }

            if not secrets.compare_digest(
                str(item.get("line_user_id") or ""), trusted_line_user_id
            ):
                _record_failed_attempt(cursor, item)
                conn.commit()
                raise LineAdminBindingAuthenticationError(
                    "此綁定連結不屬於目前登入的 LINE 帳號"
                )

            cursor.execute(
                """
                SELECT id,username,password_hash,display_name,role,enabled,
                       linked_line_user_id
                FROM admin_users
                WHERE username=%s
                FOR UPDATE
                """,
                (normalized_username,),
            )
            admin = cursor.fetchone()
            if (
                not admin
                or not admin.get("enabled")
                or not verify_admin_password(password, admin.get("password_hash") or "")
            ):
                _record_failed_attempt(cursor, item)
                conn.commit()
                raise LineAdminBindingAuthenticationError("帳號或密碼不正確")

            existing_admin_line = str(admin.get("linked_line_user_id") or "").strip()
            if existing_admin_line and existing_admin_line != trusted_line_user_id:
                raise LineAdminBindingConflictError(
                    "此後台帳號已綁定其他 LINE 帳號，請由系統管理員處理"
                )

            cursor.execute(
                """
                SELECT id,username FROM admin_users
                WHERE linked_line_user_id=%s AND id<>%s
                FOR UPDATE
                """,
                (trusted_line_user_id, admin["id"]),
            )
            if cursor.fetchone():
                raise LineAdminBindingConflictError(
                    "此 LINE 帳號已綁定其他後台帳號，請由系統管理員處理"
                )

            cursor.execute(
                "SELECT role FROM line_users WHERE line_user_id=%s FOR UPDATE",
                (trusted_line_user_id,),
            )
            line_user = cursor.fetchone()
            previous_line_role = (line_user or {}).get("role") or "customer"
            if previous_line_role == "staff":
                raise LineAdminBindingConflictError(
                    "此 LINE 帳號已綁定月嫂身分，不能直接改為工會人員"
                )

            cursor.execute(
                """
                UPDATE admin_users SET linked_line_user_id=%s WHERE id=%s
                """,
                (trusted_line_user_id, admin["id"]),
            )
            cursor.execute(
                """
                UPDATE line_users
                SET role='union_staff', status='active', blocked_at=NULL,
                    last_event_at=UTC_TIMESTAMP()
                WHERE line_user_id=%s
                """,
                (trusted_line_user_id,),
            )
            cursor.execute(
                """
                UPDATE line_admin_binding_requests
                SET status='completed', admin_user_id=%s,
                    attempt_count=attempt_count+1, last_attempt_at=UTC_TIMESTAMP(),
                    completed_at=UTC_TIMESTAMP()
                WHERE id=%s
                """,
                (admin["id"], item["id"]),
            )

            success_message = "後台帳號與 LINE 綁定完成，已切換為工會人員身分。"
            if rich_menu_id:
                enqueue_line_task(
                    cursor,
                    to_user_id=trusted_line_user_id,
                    task_type="rich_menu_link",
                    payload={
                        "rich_menu_id": rich_menu_id,
                        "success_message": success_message,
                    },
                    idempotency_key=f"admin-line-binding:{item['id']}",
                )
            else:
                enqueue_line_task(
                    cursor,
                    to_user_id=trusted_line_user_id,
                    message_content=(
                        f"{success_message}\n工會人員選單尚未發布，請聯絡系統管理員。"
                    ),
                    idempotency_key=f"admin-line-binding:{item['id']}",
                )

            cursor.execute(
                """
                INSERT INTO admin_audit_logs (
                    admin_user_id, action, resource_type, resource_id,
                    request_path, http_method, result_status, details_json
                ) VALUES (%s,'admin.line_binding.completed','admin_user',%s,
                          '/api/line/admin-binding/complete','POST',200,%s)
                """,
                (
                    admin["id"],
                    str(admin["id"]),
                    json.dumps(
                        {
                            "binding_request_id": int(item["id"]),
                            "previous_line_role": previous_line_role,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        conn.commit()
        return {
            "request_id": int(item["id"]),
            "status": "completed",
            "already_completed": False,
            "display_name": admin["display_name"],
            "role": admin["role"],
            "message": "綁定完成，請回到 LINE 使用工會人員選單。",
        }
    except LineAdminBindingAuthenticationError:
        raise
    except LineAdminBindingStateError:
        conn.commit()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
