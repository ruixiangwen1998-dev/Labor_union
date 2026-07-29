"""
================================================================================
檔案名稱: services/line_staff_verification_service.py
功能說明: 月嫂 LIFF 一次性申請 Token、基本資料比對與安全提交服務
================================================================================
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import pymysql

from services.db_service import get_connection
from services.line_liff_identity_service import LiffIdentityError, resolve_line_user_id


MAX_SUBMISSION_ATTEMPTS = 5
TOKEN_VALID_HOURS = 24
IDENTITY_PATTERN = re.compile(r"^[A-Z][12]\d{8}$")


class StaffVerificationError(ValueError):
    pass


class StaffVerificationNotFoundError(StaffVerificationError):
    pass


class StaffVerificationStateError(StaffVerificationError):
    pass


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def issue_staff_verification_token(cursor, *, request_id: int) -> str:
    """Attach a new short-lived secret to a pending staff verification request."""
    token = secrets.token_urlsafe(32)
    expires_at = _utc_now_naive() + timedelta(hours=TOKEN_VALID_HOURS)
    cursor.execute(
        """
        UPDATE line_confirmation_requests
        SET verification_token_hash=%s, verification_token_expires_at=%s,
            submission_attempts=0, match_status='not_submitted',
            matched_staff_id=NULL, submitted_at=NULL
        WHERE id=%s AND request_type='staff_verification' AND status='pending'
        """,
        (_token_hash(token), expires_at, request_id),
    )
    if cursor.rowcount != 1:
        raise StaffVerificationStateError("月嫂身分申請已失效，請重新申請")
    return token


def build_staff_verification_url(token: str) -> str:
    dedicated_liff_id = os.getenv("LINE_STAFF_VERIFICATION_LIFF_ID", "").strip()
    if dedicated_liff_id and not dedicated_liff_id.startswith("your_"):
        return (
            f"https://liff.line.me/{quote(dedicated_liff_id, safe='')}"
            f"?token={quote(token, safe='')}"
        )

    # 沿用既有 Gateway LIFF：先讓 LINE 在已登記的 Endpoint 完成登入，
    # 再由 gateway.html 導向月嫂驗證頁，避免任意 redirectUri 被 LINE 拒絕為 400。
    shared_liff_id = os.getenv("LINE_LIFF_ID", "").strip()
    if shared_liff_id and not shared_liff_id.startswith("your_"):
        return (
            f"https://liff.line.me/{quote(shared_liff_id, safe='')}"
            f"?target=staff-verification&token={quote(token, safe='')}"
        )

    base_url = os.getenv("BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
    return f"{base_url}/staff-verification-page?token={quote(token, safe='')}"


def _get_request_by_token(cursor, token: str, *, for_update: bool) -> dict:
    suffix = " FOR UPDATE" if for_update else ""
    cursor.execute(
        f"""
        SELECT id,line_user_id,status,match_status,matched_staff_id,
               verification_token_expires_at,submission_attempts,submitted_at
        FROM line_confirmation_requests
        WHERE request_type='staff_verification' AND verification_token_hash=%s
        {suffix}
        """,
        (_token_hash(token),),
    )
    item = cursor.fetchone()
    if not item:
        raise StaffVerificationNotFoundError("驗證連結無效，請回到 LINE 重新申請")
    item = dict(item)
    if item["status"] != "pending":
        raise StaffVerificationStateError("這筆月嫂身分申請已經處理完成")
    expires_at = item.get("verification_token_expires_at")
    if not expires_at or expires_at < _utc_now_naive():
        raise StaffVerificationStateError("驗證連結已過期，請回到 LINE 重新申請")
    if int(item.get("submission_attempts") or 0) >= MAX_SUBMISSION_ATTEMPTS:
        raise StaffVerificationStateError("驗證嘗試次數已達上限，請聯絡工會人員")
    return item


def get_staff_verification_form_state(token: str) -> dict:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            item = _get_request_by_token(cursor, token.strip(), for_update=False)
        return {
            "request_id": int(item["id"]),
            "submitted": item.get("submitted_at") is not None,
            "match_status": item.get("match_status") or "not_submitted",
            "remaining_attempts": MAX_SUBMISSION_ATTEMPTS
            - int(item.get("submission_attempts") or 0),
        }
    finally:
        conn.close()


def _normalize_identity_card(value: str) -> str:
    normalized = re.sub(r"\s+", "", value or "").upper()
    if not IDENTITY_PATTERN.fullmatch(normalized):
        raise StaffVerificationError("身分證字號格式不正確")
    return normalized


def submit_staff_verification(
    *,
    token: str,
    name: str,
    identity_card: str,
    birthday: date,
    line_id_token: str | None,
    development_line_user_id: str | None,
) -> dict:
    normalized_name = " ".join((name or "").strip().split())
    if not normalized_name:
        raise StaffVerificationError("請輸入姓名")
    normalized_identity = _normalize_identity_card(identity_card)
    try:
        trusted_line_user_id = resolve_line_user_id(
            id_token=line_id_token,
            development_user_id=development_line_user_id,
        )
    except LiffIdentityError as exc:
        raise StaffVerificationError(str(exc)) from exc

    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            item = _get_request_by_token(cursor, token.strip(), for_update=True)
            if not secrets.compare_digest(
                str(item.get("line_user_id") or ""), trusted_line_user_id
            ):
                raise StaffVerificationStateError("此驗證連結不屬於目前登入的 LINE 帳號")
            cursor.execute(
                """
                SELECT id,name,phone,identity_card,birthday,status,line_user_id
                FROM staff
                WHERE name=%s AND UPPER(identity_card)=%s AND birthday=%s
                ORDER BY id
                FOR UPDATE
                """,
                (normalized_name, normalized_identity, birthday),
            )
            candidates = list(cursor.fetchall())
            matched_staff_id = None
            if len(candidates) == 1:
                candidate = candidates[0]
                bound_line_id = str(candidate.get("line_user_id") or "").strip()
                if bound_line_id and bound_line_id != trusted_line_user_id:
                    match_status = "already_bound"
                else:
                    match_status = "matched"
                    matched_staff_id = int(candidate["id"])
            elif len(candidates) > 1:
                match_status = "conflict"
            else:
                match_status = "not_found"
            cursor.execute(
                """
                UPDATE line_confirmation_requests
                SET submitted_name=%s, submitted_birthday=%s,
                    submitted_identity_last4=%s, matched_staff_id=%s,
                    match_status=%s, submission_attempts=submission_attempts+1,
                    submitted_at=UTC_TIMESTAMP()
                WHERE id=%s
                """,
                (
                    normalized_name,
                    birthday,
                    normalized_identity[-4:],
                    matched_staff_id,
                    match_status,
                    item["id"],
                ),
            )
        conn.commit()
        return {
            "request_id": int(item["id"]),
            "submitted": True,
            "message": "資料已送交工會人員確認，審核結果將透過 LINE 通知您。",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
