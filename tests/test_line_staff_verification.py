"""月嫂 LIFF 基本資料比對、一次性 Token 與 staff 綁定流程測試。"""

from __future__ import annotations

import os
import uuid
from datetime import date

from services.db_service import get_connection
from services.line_review_service import approve_line_review, get_line_review
from services.line_staff_verification_service import (
    build_staff_verification_url,
    issue_staff_verification_token,
    submit_staff_verification,
)


def test_staff_verification_url_reuses_gateway_liff(monkeypatch):
    monkeypatch.delenv("LINE_STAFF_VERIFICATION_LIFF_ID", raising=False)
    monkeypatch.setenv("LINE_LIFF_ID", "123456-sharedLiff")

    url = build_staff_verification_url("secret token")

    assert url.startswith("https://liff.line.me/123456-sharedLiff?")
    assert "target=staff-verification" in url
    assert "token=secret%20token" in url


def test_staff_verification_url_keeps_dedicated_liff_compatibility(monkeypatch):
    monkeypatch.setenv("LINE_LIFF_ID", "123456-sharedLiff")
    monkeypatch.setenv("LINE_STAFF_VERIFICATION_LIFF_ID", "123456-staffLiff")

    url = build_staff_verification_url("secret-token")

    assert url == "https://liff.line.me/123456-staffLiff?token=secret-token"


def test_staff_liff_submission_matches_and_approval_binds_staff():
    suffix = uuid.uuid4().hex
    user_id = f"U-staff-liff-{suffix}"
    staff_name = f"月嫂測試{suffix[:8]}"
    identity_card = f"A1{uuid.uuid4().int % 100000000:08d}"
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO staff (name,identity_card,birthday) VALUES (%s,%s,%s)",
                (staff_name, identity_card, date(1990, 1, 2)),
            )
            staff_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO line_users (line_user_id,role,status) VALUES (%s,'customer','active')",
                (user_id,),
            )
            cursor.execute(
                "INSERT INTO line_confirmation_requests (request_type,line_user_id) VALUES ('staff_verification',%s)",
                (user_id,),
            )
            request_id = int(cursor.lastrowid)
            token = issue_staff_verification_token(cursor, request_id=request_id)
        conn.commit()
    finally:
        conn.close()

    previous = {key: os.environ.get(key) for key in ("APP_ENV", "LIFF_REQUIRE_ID_TOKEN")}
    os.environ["APP_ENV"] = "development"
    os.environ["LIFF_REQUIRE_ID_TOKEN"] = "false"
    try:
        submitted = submit_staff_verification(
            token=token,
            name=staff_name,
            identity_card=identity_card.lower(),
            birthday=date(1990, 1, 2),
            line_id_token="",
            development_line_user_id=user_id,
        )
        assert submitted["submitted"] is True
        detail = get_line_review(request_id)
        assert detail["match_status"] == "matched"
        assert int(detail["matched_staff_id"]) == staff_id
        assert detail["submitted_identity_last4"] == identity_card[-4:]
        assert "matched_staff_identity_card" not in detail

        approved = approve_line_review(
            request_id,
            admin_user_id=None,
            reason="LIFF 資料與月嫂主檔一致",
        )
        assert approved["status"] == "approved"
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT line_user_id FROM staff WHERE id=%s", (staff_id,))
                assert cursor.fetchone()["line_user_id"] == user_id
                cursor.execute("SELECT role FROM line_users WHERE line_user_id=%s", (user_id,))
                assert cursor.fetchone()["role"] == "staff"
        finally:
            conn.close()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM line_tasks WHERE idempotency_key=%s", (f"staff-review-approved:{request_id}",))
                cursor.execute("DELETE FROM line_confirmation_requests WHERE id=%s", (request_id,))
                cursor.execute("DELETE FROM line_users WHERE line_user_id=%s", (user_id,))
                cursor.execute("DELETE FROM staff WHERE id=%s", (staff_id,))
            conn.commit()
        finally:
            conn.close()
