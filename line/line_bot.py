# -*- coding: utf-8 -*-
"""
================================================================================
檔案名稱: line/line_bot.py
功能說明: LINE Bot 子路由，負責 Webhook、LIFF、使用者事件、身分切換與 LINE 訊息任務建立
================================================================================
"""
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
import pymysql
import os
import json
import asyncio
import sys
import secrets
import requests
from typing import Any, Dict, Optional
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from fastapi.responses import FileResponse
from line.worker import wake_worker
from line.security import verify_line_signature
from services.line_task_service import enqueue_line_task
from services.line_review_service import (
    LineReviewDataConflictError,
    LineReviewNotFoundError,
    LineReviewStateConflictError,
    approve_line_review,
    get_line_review,
    list_line_reviews,
    reject_line_review,
)
from services.line_rich_menu_service import get_current_rich_menu_id
from services.webhook_event_service import register_event
from services.line_liff_identity_service import (
    LiffIdentityError,
    liff_token_required,
    resolve_line_user_id,
)
from services.line_staff_verification_service import (
    build_staff_verification_url,
    issue_staff_verification_token,
)
from services.line_admin_binding_service import (
    build_line_admin_binding_url,
    issue_line_admin_binding_token,
)

# 載入環境變數
load_dotenv()

# 確保 sys.path 能載入 services
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from services.db_service import get_connection as get_db_connection, calculate_attendance_schedule

def get_setting(key: str, default: str = "") -> str:
    """從環境變數讀取設定，取代舊版 admin.settings_manager"""
    env_key = key.upper()
    return os.getenv(env_key, default)

def load_message_templates():
    """Return enabled text templates keyed by template id."""
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "message_templates.json")
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {
            item["id"]: item["content"]
            for item in data.get("templates", [])
            if item.get("enabled", True) and item.get("message_type", "text") == "text"
        }
    except Exception as e:
        print(f"[LINE Webhook] Failed to load message templates: {e}")
        return {}


def _load_rich_menu_id(role: str) -> str:
    current_id = get_current_rich_menu_id(role)
    if current_id:
        return current_id
    key_by_role = {
        "staff": "staff_rich_menu_id",
        "union_staff": "union_staff_rich_menu_id",
        "customer": "default_rich_menu_id",
    }
    path = os.path.join(os.path.dirname(__file__), "..", "config", "rich_menu_ids.json")
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream).get(key_by_role[role], "")
    except (OSError, ValueError, KeyError):
        return ""


def _require_internal_api_key(x_internal_api_key: str | None) -> None:
    expected = os.getenv("INTERNAL_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Internal API authentication is not configured")
    if not secrets.compare_digest(x_internal_api_key or "", expected):
        raise HTTPException(status_code=401, detail="Invalid internal API key")


def _notify_development_reviewer(request_type: str, request_id: str | int) -> None:
    """Push one review event to the local dev supervisor; never affect webhook success."""
    notify_url = os.getenv("DEV_REVIEW_NOTIFY_URL", "").strip()
    internal_key = os.getenv("INTERNAL_API_KEY", "").strip()
    if not notify_url or not internal_key:
        return
    try:
        response = requests.post(
            notify_url,
            json={"type": request_type, "request_id": str(request_id)},
            headers={"X-Internal-API-Key": internal_key},
            timeout=1,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"[LINE Review] Development notification failed: {exc}")


def _create_onboarding_tasks(cursor, user_id: str, source_event_id: str | None) -> None:
    schedule_path = os.path.join(os.path.dirname(__file__), "..", "config", "message_schedules.json")
    templates = load_message_templates()
    try:
        with open(schedule_path, "r", encoding="utf-8") as stream:
            schedule_config = json.load(stream)
            schedules = schedule_config.get("schedules", [])
    except (OSError, ValueError):
        return
    onboarding = next((item for item in schedules if item.get("id") == "new_user_onboarding" and item.get("enabled")), None)
    if not onboarding:
        return
    restart_on_refollow = bool(onboarding.get("restart_on_refollow", False))
    if restart_on_refollow:
        cursor.execute(
            """
            UPDATE line_tasks
            SET status='cancelled'
            WHERE to_user_id=%s AND status='pending'
              AND idempotency_key LIKE 'onboarding:%%'
            """,
            (user_id,),
        )
    for step in onboarding.get("steps", []):
        template_id = step.get("template_id")
        content = templates.get(template_id)
        if not content:
            continue
        send_time = step.get("send_time", "10:00")
        day = int(step.get("day", 0))
        hour, minute = map(int, send_time.split(":"))
        schedule_zone = ZoneInfo(schedule_config.get("timezone", "Asia/Taipei"))
        local_now = datetime.now(schedule_zone)
        local_target = (local_now + timedelta(days=day)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        # MySQL currently stores UTC in a timezone-naive DATETIME column.
        scheduled_at = local_target.astimezone(timezone.utc).replace(tzinfo=None)
        if restart_on_refollow and source_event_id:
            idempotency_key = f"onboarding:{user_id}:{source_event_id}:d{day}"
        else:
            idempotency_key = f"onboarding:{user_id}:d{day}"
        enqueue_line_task(
            cursor, to_user_id=user_id, message_content=content,
            scheduled_at=scheduled_at, source_event_id=source_event_id,
            idempotency_key=idempotency_key,
        )


def ensure_order_for_case_no(cursor, client_id: int, case_no: str) -> None:
    """正式案件編號核發後，建立或取得該案件唯一的訂單。"""
    if not case_no or not str(case_no).strip():
        return

    normalized_case_no = str(case_no).strip()
    cursor.execute(
        "SELECT client_id FROM orders WHERE case_no = %s",
        (normalized_case_no,),
    )
    existing_order = cursor.fetchone()
    if existing_order:
        existing_client_id = (
            existing_order.get("client_id")
            if isinstance(existing_order, dict)
            else existing_order[0]
        )
        if existing_client_id != client_id:
            raise ValueError(f"案件編號 {normalized_case_no} 已連結其他客戶")
        return

    cursor.execute("""
        INSERT INTO orders (case_no, client_id)
        VALUES (%s, %s)
    """, (normalized_case_no, client_id))


router = APIRouter(tags=["LINE"])

# LINE LIFF 配置獲取端點
@router.get("/api/line/config")
async def get_line_config():
    liff_id = os.getenv("LINE_LIFF_ID", "")
    if not liff_id or liff_id == "your_liff_id_here":
        liff_id = get_setting("line_liff_id", "")
    return {
        "liff_id": liff_id,
        "staff_verification_liff_id": os.getenv(
            "LINE_STAFF_VERIFICATION_LIFF_ID", ""
        ).strip(),
        "admin_binding_liff_id": os.getenv(
            "LINE_ADMIN_BINDING_LIFF_ID", ""
        ).strip(),
        "identity_verification_required": liff_token_required(),
        "line_login_channel_id_configured": bool(
            os.getenv("LINE_LOGIN_CHANNEL_ID", "").strip()
        ),
    }


async def _trusted_line_user_id(id_token: str | None, fallback_user_id: str | None) -> str:
    try:
        return await asyncio.to_thread(
            resolve_line_user_id,
            id_token=id_token,
            development_user_id=fallback_user_id,
        )
    except LiffIdentityError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

async def _find_client_info(trusted_user_id: str):
    """查詢使用者的 LINE ID 是否已有綁定紀錄，有的話回傳最近一筆姓名電話以利自動帶入"""
    conn = get_db_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT name, phone FROM clients WHERE line_user_id = %s ORDER BY id DESC LIMIT 1", (trusted_user_id,))
            client = cursor.fetchone()
            if client:
                return {"status": "success", "client": client}
            return {"status": "not_found"}
    except Exception as e:
        print(f"[API Client Info] Error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()

# 帳號綁定請求 Model
class LineBindPayload(BaseModel):
    name: str
    phone: str
    line_user_id: str = ""
    line_id_token: str = ""
    force_rebind: bool = False

@router.post("/api/line/bind")
async def line_bind(payload: LineBindPayload):
    name = payload.name.strip()
    phone = payload.phone.strip()
    line_user_id = await _trusted_line_user_id(
        payload.line_id_token,
        payload.line_user_id,
    )
    
    print(f"[API Bind] Attempting to bind name={name}, phone={phone} to line_user_id={line_user_id}")
    
    # 正規化電話：去除所有空白與 "-" 
    normalized_phone = phone.replace(" ", "").replace("-", "")
    
    conn = get_db_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # 1. 進行姓名與電話之雙重精確比對（防同名同姓錯誤綁定），同時查詢現有的 line_user_id
            cursor.execute("""
                SELECT id, name, case_no, line_user_id FROM clients
                WHERE name = %s AND REPLACE(REPLACE(phone, '-', ''), ' ', '') = %s 
                ORDER BY id DESC LIMIT 1
            """, (name, normalized_phone))
            
            client = cursor.fetchone()
            
            if not client:
                return {
                    "status": "error",
                    "message": "查無此姓名與電話之登記資料，請確認輸入是否正確，或聯絡公會專員。\n如尚未登記政府補助請先至政府官網登記"
                }
                
            client_id = client["id"]
            db_name = client["name"]
            case_no = client["case_no"]
            db_line_user_id = client["line_user_id"]
            
            # 2. 處理 line_user_id 寫入與覆蓋邏輯
            if db_line_user_id and db_line_user_id.strip() != "" and db_line_user_id != line_user_id:
                if not payload.force_rebind:
                    # 發現有不同帳號，且未強制覆蓋，暫停並回傳確認重綁狀態
                    return {
                        "status": "confirm_rebind",
                        "message": "本筆訂單已有綁定另一個帳戶，請問是否重新綁定？"
                    }
                else:
                    # 使用者同意重新綁定，寫入統一確認請求表等待人工確認。
                    cursor.execute(
                        """
                        UPDATE line_confirmation_requests
                        SET status='cancelled', resolved_at=NOW()
                        WHERE request_type='client_rebind' AND client_id=%s
                          AND line_user_id=%s AND status='pending'
                        """,
                        (client_id, line_user_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO line_confirmation_requests (
                            request_type, line_user_id, client_id, client_name,
                            old_line_user_id, new_line_user_id
                        ) VALUES ('client_rebind', %s, %s, %s, %s, %s)
                        """,
                        (line_user_id, client_id, db_name, db_line_user_id, line_user_id),
                    )
                    request_id = cursor.lastrowid
                    conn.commit()
                    _notify_development_reviewer("client_rebind", request_id)
                    print(
                        f"[API Bind] Rebind request {request_id} saved "
                        f"for client_id={client_id}"
                    )
                    
                    return {
                        "status": "pending_approval",
                        "message": "您的帳號重新綁定申請已送出，請耐心等待服務人員審核與確認。"
                    }
            elif not db_line_user_id or db_line_user_id.strip() == "":
                # 原本為空，正常綁定
                cursor.execute("""
                    UPDATE clients 
                    SET line_user_id = %s 
                    WHERE id = %s
                """, (line_user_id, client_id))
                print(f"[API Bind] Conditional write success: client_id={client_id} bound to line_user_id={line_user_id}")
            else:
                # 已經綁定過了，且 ID 相同，不需要重複寫入
                print(f"[API Bind] Skipped write: client_id={client_id} already bound to current line_user_id")
            
            # 3. 取得正式案件編號後，才建立或取得唯一訂單。
            ensure_order_for_case_no(cursor, client_id, case_no)

            success_msg = f"【系統通知】\n服務綁定與查詢成功！您的 LINE 帳號已連結至客戶「{db_name}」的登記資料。\n"
            if case_no:
                success_msg += f"您的案件編號為：{case_no}。\n"
            else:
                success_msg += "您的案件編號尚待行政核發；完成核對後將主動通知您。\n"
            success_msg += "後續有最新媒合進度或排班通知，系統將會主動為您推播。"
            
            cursor.execute("""
                INSERT INTO line_tasks (to_user_id, message_content, status)
                VALUES (%s, %s, 'pending')
            """, (line_user_id, success_msg))
            
            conn.commit()
            wake_worker()
            print(f"[API Bind] Successfully processed client_id={client_id} for line_user_id={line_user_id}. Case no: {case_no}")
            
            return {
                "status": "success",
                "message": "綁定與查詢成功！",
                "client_name": db_name,
                "client_id": client_id,
                "case_no": case_no
            }
    except Exception as e:
        conn.rollback()
        print(f"[API Bind] Binding process failed: {e}")
        return {
            "status": "error",
            "message": f"伺服器錯誤：{str(e)}"
        }
    finally:
        conn.close()

class RebindActionPayload(BaseModel):
    request_id: int

@router.get("/api/line/rebind_requests")
def get_rebind_requests(x_internal_api_key: str | None = Header(default=None)):
    """
    [前端管理 API] 取得所有待確認的重新綁定申請
    """
    _require_internal_api_key(x_internal_api_key)
    try:
        result = list_line_reviews(
            request_type="client_rebind", status="pending", page_size=100
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "status": "success",
        "data": [
            {
                "request_id": detail["id"],
                "client_id": detail.get("client_id"),
                "client_name": detail.get("client_name"),
                "old_line_user_id": detail.get("old_line_user_id"),
                "new_line_user_id": detail.get("new_line_user_id"),
                "request_time": detail.get("created_at"),
            }
            for item in result["items"]
            for detail in [get_line_review(int(item["id"]))]
        ],
    }


class LineIdentityPayload(BaseModel):
    line_user_id: str = ""
    line_id_token: str = ""


@router.post("/api/line/client-info")
async def post_client_info(payload: LineIdentityPayload):
    trusted_user_id = await _trusted_line_user_id(
        payload.line_id_token,
        payload.line_user_id,
    )
    return await _find_client_info(trusted_user_id)


@router.get("/api/line/client-info")
async def get_client_info(userId: str = ""):
    """Development-compatible legacy endpoint; production requires POST with an ID token."""
    trusted_user_id = await _trusted_line_user_id(None, userId)
    return await _find_client_info(trusted_user_id)

@router.post("/api/line/rebind_requests/approve")
def approve_rebind_request(
    payload: RebindActionPayload,
    x_internal_api_key: str | None = Header(default=None),
):
    """
    [前端管理 API] 確認並執行重新綁定申請
    """
    _require_internal_api_key(x_internal_api_key)
    try:
        detail = get_line_review(int(payload.request_id))
        if detail["request_type"] != "client_rebind":
            raise HTTPException(status_code=409, detail="Review request type does not match")
        result = approve_line_review(
            int(payload.request_id), admin_user_id=None, reason="開發終端核准"
        )
        wake_worker()
        return {"status": "success", "message": result["message"], "data": result}
    except LineReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (LineReviewStateConflictError, LineReviewDataConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@router.post("/api/line/rebind_requests/reject")
def reject_rebind_request(
    payload: RebindActionPayload,
    x_internal_api_key: str | None = Header(default=None),
):
    """
    [前端管理 API] 拒絕重新綁定申請
    """
    _require_internal_api_key(x_internal_api_key)
    try:
        detail = get_line_review(int(payload.request_id))
        if detail["request_type"] != "client_rebind":
            raise HTTPException(status_code=409, detail="Review request type does not match")
        result = reject_line_review(
            int(payload.request_id),
            admin_user_id=None,
            reason="開發終端拒絕",
        )
        wake_worker()
        return {"status": "success", "message": result["message"], "data": result}
    except LineReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (LineReviewStateConflictError, LineReviewDataConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

from typing import Optional, Dict, Any

class LineRegisterPayload(BaseModel):
    name: str
    phone: str
    expected_date: str
    service_days: int
    address: str
    line_user_id: str = ""
    line_id_token: str = ""
    liff_config_revision: Optional[str] = ""
    id_number: Optional[str] = ""
    birth_date: Optional[str] = ""
    gender: Optional[str] = ""
    email: Optional[str] = ""
    tel: Optional[str] = ""
    ext: Optional[str] = ""
    city: Optional[str] = ""
    zip_code: Optional[str] = ""
    survey_details: Dict[str, Any] = {}


@router.post("/api/line/register")
async def line_register(payload: LineRegisterPayload):
    name = payload.name.strip()
    phone = payload.phone.strip()
    line_user_id = await _trusted_line_user_id(
        payload.line_id_token,
        payload.line_user_id,
    )
    
    normalized_phone = phone.replace(" ", "").replace("-", "")
    
    conn = get_db_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # 1. 寫入 clients
            cursor.execute("""
                INSERT INTO clients (name, phone, address, service_days, due_month, line_user_id, gender, city)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (name, normalized_phone, payload.address, payload.service_days, payload.expected_date, line_user_id, payload.gender, payload.city))
            client_id = conn.insert_id()
            
            # 2. 寫入 beclass_records 確保後台查詢一致性。
            # LINE 原生登記尚未取得正式 case_no，此時不得建立 orders。
            final_survey = payload.survey_details.copy()
            final_survey["預產期"] = payload.expected_date
            final_survey["預計服務天數"] = payload.service_days
            final_survey["身分證字號"] = payload.id_number
            final_survey["性別"] = payload.gender
            final_survey["資料來源"] = "LINE 原生表單"
            final_survey["_liff_meta"] = {
                "page": "registration",
                "config_revision": (payload.liff_config_revision or "").strip(),
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }
            
            survey_details_json = json.dumps(final_survey, ensure_ascii=False)
            
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("""
                INSERT INTO beclass_records (name, email, birth_date, phone, tel, ext, city, zip_code, address, created_at, survey_details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (name, payload.email, payload.birth_date if payload.birth_date else None, normalized_phone, payload.tel, payload.ext, payload.city, payload.zip_code, payload.address, now_str, survey_details_json))
            
            # 3. 寫入推播任務。尚未取得正式案件編號時只保留登記資料。
            success_msg = f"【系統通知】\n服務登記與綁定成功！\n您的 LINE 帳號已連結至客戶「{name}」的專屬資料庫。\n"
            success_msg += "您的案件編號尚待行政核發；完成核對後將主動通知您。\n"
            success_msg += "工會行政專員將於上班時間透過 LINE 與您聯繫確認服務細節，請您耐心等候！"
            
            cursor.execute("""
                INSERT INTO line_tasks (to_user_id, message_content, status)
                VALUES (%s, %s, 'pending')
            """, (line_user_id, success_msg))
            
            conn.commit()
            wake_worker()
            return {
                "status": "success",
                "client_id": client_id,
                "client_name": name,
                "case_no": None
            }
    except Exception as e:
        conn.rollback()
        print(f"[API Register] Error: {e}")
        return {"status": "error", "message": f"建檔失敗: {str(e)}"}
    finally:
        conn.close()

@router.get("/")
async def health_check():
    db_ok = False
    db_msg = ""
    try:
        conn = get_db_connection()
        conn.close()
        db_ok = True
        db_msg = "Database connected"
    except Exception as e:
        db_ok = False
        db_msg = str(e)
        
    return {
        "status": "healthy",
        "api_version": "1.0.0",
        "database": {
            "connected": db_ok,
            "message": db_msg
        }
    }

@router.post("/")
async def root_post(payload: dict, request: Request):
    if "events" in payload:
        raise HTTPException(status_code=400, detail="Use /webhook/line so the raw signed body can be verified")
    return {"status": "active", "message": "Root POST active"}

@router.get("/liff-page")
@router.get("/gateway")
async def serve_gateway_page():
    """前導選擇頁面 (自動相容舊版 LIFF 設定)"""
    return FileResponse("line/static/gateway.html")

@router.get("/bind-page")
async def serve_bind_page():
    """提供舊客查詢與綁定專用的路徑"""
    return FileResponse("line/static/bind.html")

@router.get("/register-page")
async def serve_register_page():
    """全新客戶原生註冊頁面"""
    return FileResponse("line/static/register.html")


@router.get("/staff-verification-page")
async def serve_staff_verification_page():
    """提供月嫂既有資料比對與 LINE 綁定申請頁面。"""
    return FileResponse("line/static/staff_verification.html")


@router.get("/union-staff-binding-page")
async def serve_union_staff_binding_page():
    """提供工會人員驗證後台帳密並綁定 LINE 的 LIFF 頁面。"""
    return FileResponse("line/static/union_staff_binding.html")


@router.get("/api/line/staff/review-requests")
def list_staff_review_requests(
    request_type: str | None = None,
    x_internal_api_key: str | None = Header(default=None),
):
    """Unified union-staff queue for rebind and service-staff role requests."""
    _require_internal_api_key(x_internal_api_key)
    if request_type not in {None, "client_rebind", "staff_verification"}:
        raise HTTPException(status_code=422, detail="Unsupported review request type")

    result = list_line_reviews(
        request_type=request_type,
        status="pending",
        page_size=100,
    )
    items = []
    for summary in result["items"]:
        details = get_line_review(int(summary["id"]))
        items.append(
            {
                "type": details["request_type"],
                "request_id": str(details["id"]),
                "status": details["status"],
                "created_at": details["created_at"],
                "display_name": details.get("client_name") or details.get("line_user_id"),
                "details": details,
                "actions": ["approve", "reject"],
            }
        )
    return {"status": "success", "data": items}


@router.post("/api/line/staff/review-requests/{request_type}/{request_id}/approve")
def approve_staff_review_request(
    request_type: str,
    request_id: str,
    x_internal_api_key: str | None = Header(default=None),
):
    """Approve a rebind or directly approve a service-staff LINE role request."""
    _require_internal_api_key(x_internal_api_key)
    if request_type not in {"client_rebind", "staff_verification"}:
        raise HTTPException(status_code=422, detail="Unsupported review request type")
    try:
        detail = get_line_review(int(request_id))
        if detail["request_type"] != request_type:
            raise HTTPException(status_code=409, detail="Review request type does not match")
        result = approve_line_review(
            int(request_id), admin_user_id=None, reason="開發終端核准"
        )
        wake_worker()
        return {
            "status": "success",
            "message": result["message"],
            "data": result,
        }
    except LineReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (LineReviewStateConflictError, LineReviewDataConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/line/staff/review-requests/{request_type}/{request_id}/reject")
def reject_staff_review_request(
    request_type: str,
    request_id: str,
    x_internal_api_key: str | None = Header(default=None),
):
    """Reject a rebind or a pending service-staff role request."""
    _require_internal_api_key(x_internal_api_key)
    if request_type not in {"client_rebind", "staff_verification"}:
        raise HTTPException(status_code=422, detail="Unsupported review request type")
    try:
        detail = get_line_review(int(request_id))
        if detail["request_type"] != request_type:
            raise HTTPException(status_code=409, detail="Review request type does not match")
        result = reject_line_review(
            int(request_id), admin_user_id=None, reason="開發終端拒絕"
        )
        wake_worker()
        return {"status": "success", "message": result["message"], "data": result}
    except LineReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (LineReviewStateConflictError, LineReviewDataConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/api/line/users/{user_id}/role/{role}")
def set_line_user_role(user_id: str, role: str, x_internal_api_key: str | None = Header(default=None)):
    """Internal role administration endpoint for customer/staff/union_staff."""
    _require_internal_api_key(x_internal_api_key)
    if role not in {"customer", "staff", "union_staff"}:
        raise HTTPException(status_code=422, detail="Unsupported LINE user role")
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO line_users (line_user_id, role, status, last_event_at)
                VALUES (%s,%s,'active',NOW())
                ON DUPLICATE KEY UPDATE role=VALUES(role), status='active', last_event_at=NOW()
                """,
                (user_id, role),
            )
            conn.commit()
        return {"status": "success", "line_user_id": user_id, "role": role}
    finally:
        conn.close()

# ----------------- 1. LINE WEBHOOK 接收 -----------------
class LineWebhookPayload(BaseModel):
    events: list = []
    destination: str = ""

@router.get("/webhook/line")
@router.get("/webhook/line/")
@router.get("/webhook")
@router.get("/webhook/")
async def line_webhook_get():
    print("[LINE Webhook] Received GET request (possibly URL verification or redirect)")
    return {"status": "ok", "message": "LINE Webhook endpoint is active"}

@router.post("/webhook/line")
@router.post("/webhook/line/")
@router.post("/webhook")
@router.post("/webhook/")
async def line_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("x-line-signature", "")
    channel_secret = os.getenv("LINE_CHANNEL_SECRET", "")
    if not verify_line_signature(raw_body, signature, channel_secret):
        raise HTTPException(status_code=401, detail="Invalid LINE webhook signature")
    try:
        payload = LineWebhookPayload.model_validate_json(raw_body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid LINE webhook payload") from exc
    print(f"[LINE Webhook] Received line webhook. Events count: {len(payload.events)}")
    
    review_notifications: list[tuple[str, int]] = []
    conn = get_db_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            for event in payload.events:
                if not register_event(cursor, event):
                    print(f"[LINE Webhook] Duplicate event ignored: {event.get('webhookEventId')}")
                    continue
                event_type = event.get("type")
                
                # 處理新用戶加入好友 (follow) 事件
                if event_type == "follow":
                    source = event.get("source", {})
                    user_id = source.get("userId", "")
                    print(f"[LINE Webhook] Follow event received from User: {user_id}")
                    
                    if user_id:
                        cursor.execute(
                            """
                            INSERT INTO line_users (line_user_id, status, followed_at, last_event_at, onboarding_started_at)
                            VALUES (%s, 'active', NOW(), NOW(), NOW())
                            ON DUPLICATE KEY UPDATE status='active', followed_at=NOW(),
                                blocked_at=NULL, last_event_at=NOW()
                            """,
                            (user_id,),
                        )
                        liff_id = os.getenv("LINE_LIFF_ID", "")
                        if not liff_id or liff_id == "your_liff_id_here":
                            liff_id = get_setting("line_liff_id", "")
                            
                        # 決定 LIFF 網頁的綁定連結 (若無真實 LIFF ID 則回退至測試 URL)
                        if liff_id and liff_id != "your_liff_id_here" and liff_id.strip() != "":
                            bind_url = f"https://liff.line.me/{liff_id}"
                        else:
                            base_url = os.getenv("BASE_URL", "").strip().rstrip("/")
                            if base_url:
                                bind_url = f"{base_url}/gateway?userId={user_id}"
                            else:
                                host = request.headers.get("host", "127.0.0.1:8000")
                                proto = request.headers.get("x-forwarded-proto", "http")
                                bind_url = f"{proto}://{host}/gateway?userId={user_id}"
                            
                        welcome_msg = (
                            "您好！感謝您加入新竹市月子公會官方帳號。\n"
                            "為了提供您更完整的服務，請點擊以下連結進行帳號與訂單綁定：\n\n"
                            f"{bind_url}\n\n"
                            "請於網頁中填寫您在政府補助登記時的姓名與電話，以利系統進行安全配對。如有任何疑問，歡迎隨時聯絡公會專員。"
                        )
                        
                        # 寫入推播任務佇列，由背景發送
                        enqueue_line_task(
                            cursor, to_user_id=user_id, message_content=welcome_msg,
                            source_event_id=event.get("webhookEventId"),
                            idempotency_key=f"welcome:{event.get('webhookEventId') or user_id}",
                        )
                        _create_onboarding_tasks(cursor, user_id, event.get("webhookEventId"))
                        print(f"[LINE Webhook] Queued welcome message for new user {user_id}")

                elif event_type == "unfollow":
                    source = event.get("source", {})
                    user_id = source.get("userId", "")
                    if user_id:
                        cursor.execute(
                            """
                            UPDATE line_users SET status='blocked', blocked_at=NOW(), last_event_at=NOW()
                            WHERE line_user_id=%s
                            """,
                            (user_id,),
                        )
                        cursor.execute(
                            """
                            UPDATE line_tasks SET status='cancelled'
                            WHERE to_user_id=%s AND status='pending'
                              AND idempotency_key LIKE 'onboarding:%%'
                            """,
                            (user_id,),
                        )
                
                elif event_type == "postback":
                    postback_data = event["postback"].get("data", "")
                    print(f"[LINE Webhook] Postback data received: {postback_data}")
                    
                    params = {}
                    for item in postback_data.split("&"):
                        if "=" in item:
                            k, v = item.split("=", 1)
                            params[k] = v
                            
                    action = params.get("action")
                    case_no = params.get("case_no")
                    staff_id = params.get("staff_id")
                    
                    if not action or not case_no:
                        continue
                        
                    # 月嫂同意
                    if action == "willing":
                        cursor.execute("""
                            UPDATE matching_records 
                            SET caregiver_accepted = 1, replied_at = CURRENT_TIMESTAMP
                            WHERE case_no = %s AND staff_id = %s
                        """, (case_no, staff_id))
                        
                        msg = "感謝您的確認！您已同意接案，工會已將您的履歷推播給客戶，後續有進一步消息會立刻通知您！"
                        cursor.execute("""
                            INSERT INTO line_tasks (to_user_id, message_content, status)
                            VALUES (COALESCE((SELECT line_user_id FROM staff WHERE id = %s), 'mock_staff_line_id'), %s, 'pending')
                        """, (staff_id, msg))
                        print(f"[LINE Webhook] Staff #{staff_id} willing for case #{case_no}")
                        
                    # 月嫂拒絕
                    elif action == "unwilling":
                        cursor.execute("""
                            UPDATE matching_records 
                            SET caregiver_accepted = 0, replied_at = CURRENT_TIMESTAMP
                            WHERE case_no = %s AND staff_id = %s
                        """, (case_no, staff_id))
                        
                        msg = "已記錄您的回覆，期待下次為您媒合合適的案件！"
                        cursor.execute("""
                            INSERT INTO line_tasks (to_user_id, message_content, status)
                            VALUES (COALESCE((SELECT line_user_id FROM staff WHERE id = %s), 'mock_staff_line_id'), %s, 'pending')
                        """, (staff_id, msg))
                        print(f"[LINE Webhook] Staff #{staff_id} unwilling for case #{case_no}")
                        
                    # 客戶滿意
                    elif action == "client_approve":
                        cursor.execute("UPDATE orders SET client_approved = 1 WHERE case_no = %s", (case_no,))
                        
                        msg = "感謝您的確認！行政專員正為您產製電子契約條款，完成後會發送連結至您的 LINE 進行線上簽署。"
                        cursor.execute("""
                            INSERT INTO line_tasks (to_user_id, message_content, status)
                            VALUES (COALESCE((SELECT line_user_id FROM clients WHERE case_no = %s), 'mock_client_line_id'), %s, 'pending')
                        """, (case_no, msg))
                        print(f"[LINE Webhook] Client approved resume for case #{case_no}")
                        
                    # 客戶拒絕
                    elif action == "client_reject":
                        cursor.execute("UPDATE orders SET client_approved = 2 WHERE case_no = %s", (case_no,))
                        
                        msg = "已收到您的回饋，工會將為您重新進行媒合篩選，請稍候。"
                        cursor.execute("""
                            INSERT INTO line_tasks (to_user_id, message_content, status)
                            VALUES (COALESCE((SELECT line_user_id FROM clients WHERE case_no = %s), 'mock_client_line_id'), %s, 'pending')
                        """, (case_no, msg))
                        print(f"[LINE Webhook] Client rejected resume for case #{case_no}")
                
                # 處理文字對答與 RAG
                elif event_type == "message":
                    message = event.get("message", {})
                    if message.get("type") == "text":
                        user_text = message.get("text", "")
                        source = event.get("source", {})
                        user_id = source.get("userId", "")
                        reply_token = event.get("replyToken", "")
                        print(f"[LINE Webhook] Text message received from {user_id}: {user_text}")

                        cursor.execute("SELECT role FROM line_users WHERE line_user_id=%s", (user_id,))
                        role_row = cursor.fetchone()
                        current_role = role_row["role"] if role_row else "customer"

                        # 工會人員綁定只能從官方帳號一對一聊天室發起；
                        # 群組中不建立含帳密流程的連結，避免其他成員取得一次性 Token。
                        if user_text.strip() == "綁定後台帳號":
                            source_type = source.get("type") or (
                                "group" if source.get("groupId") else
                                "room" if source.get("roomId") else "user"
                            )
                            if source_type != "user":
                                conversation_id = source.get("groupId") or source.get("roomId")
                                if conversation_id:
                                    enqueue_line_task(
                                        cursor,
                                        to_user_id=conversation_id,
                                        message_content=(
                                            "為保護後台帳號安全，請私訊官方帳號並輸入「綁定後台帳號」。"
                                        ),
                                        source_event_id=event.get("webhookEventId"),
                                        idempotency_key=f"admin-binding-private-only:{event.get('webhookEventId')}",
                                    )
                                continue

                            binding_request_id, binding_token = issue_line_admin_binding_token(
                                cursor, line_user_id=user_id
                            )
                            binding_url = build_line_admin_binding_url(binding_token)
                            enqueue_line_task(
                                cursor,
                                to_user_id=user_id,
                                message_content=(
                                    "請點擊以下連結登入您的管理後台帳號，完成工會人員 LINE 綁定。\n\n"
                                    f"{binding_url}\n\n"
                                    "連結 15 分鐘內有效；工會不會在 LINE 訊息中要求您提供密碼。"
                                ),
                                source_event_id=event.get("webhookEventId"),
                                idempotency_key=f"admin-binding-request:{binding_request_id}",
                            )
                            print(
                                f"[LINE Webhook] Admin binding request #{binding_request_id} "
                                f"created for {user_id}"
                            )
                            continue

                        if current_role == "union_staff" and user_text.strip() in {"工會選單", "開啟客服系統", "月嫂驗證管理"}:
                            enqueue_line_task(
                                cursor, to_user_id=user_id, task_type="rich_menu_link",
                                payload={
                                    "rich_menu_id": _load_rich_menu_id("union_staff"),
                                    "success_message": "已切換至工會人員客服選單。",
                                },
                                source_event_id=event.get("webhookEventId"),
                                idempotency_key=f"union-menu:{event.get('webhookEventId')}",
                            )
                            continue

                        # 攔截「我是月嫂」並建立人工確認請求，不直接切換身分。
                        if "我是月嫂" in user_text:
                            cursor.execute(
                                "UPDATE line_confirmation_requests SET status='cancelled', resolved_at=NOW() WHERE request_type='staff_verification' AND line_user_id=%s AND status='pending'",
                                (user_id,),
                            )
                            cursor.execute(
                                """
                                INSERT INTO line_confirmation_requests (request_type, line_user_id)
                                VALUES ('staff_verification',%s)
                                """,
                                (user_id,),
                            )
                            request_id = cursor.lastrowid
                            verification_token = issue_staff_verification_token(
                                cursor, request_id=request_id
                            )
                            verification_url = build_staff_verification_url(
                                verification_token
                            )
                            review_notifications.append(("staff_verification", request_id))
                            templates = load_message_templates()
                            request_message = templates.get(
                                "staff_verification_requested",
                                "請填寫基本資料，完成後將交由工會人員確認。",
                            )
                            enqueue_line_task(
                                cursor, to_user_id=user_id,
                                message_content=(
                                    f"{request_message}\n\n"
                                    f"請點擊以下連結填寫姓名、身分證與生日：\n"
                                    f"{verification_url}"
                                ),
                                source_event_id=event.get("webhookEventId"),
                                idempotency_key=f"staff-verification-request:{request_id}",
                            )
                            print(f"[LINE Webhook] Staff verification request #{request_id} created for {user_id}")
                            continue
                            
                        # 攔截「esc」關鍵字恢復預設選單
                        if user_text.lower().strip() == "esc":
                            replies = load_message_templates()
                            enqueue_line_task(
                                cursor, to_user_id=user_id, task_type="rich_menu_unlink",
                                payload={"success_message": replies.get("esc_success", "已切換回一般用戶選單。")},
                                source_event_id=event.get("webhookEventId"),
                                idempotency_key=f"menu-unlink:{event.get('webhookEventId')}",
                            )
                            continue

                        # 攔截「查詢訂單」或「綁定」關鍵字對話流
                        if "查詢訂單" in user_text or "綁定" in user_text:
                            liff_id = os.getenv("LINE_LIFF_ID", "")
                            if not liff_id or liff_id == "your_liff_id_here":
                                liff_id = get_setting("line_liff_id", "")
                                
                            if liff_id and liff_id != "your_liff_id_here" and liff_id.strip() != "":
                                bind_url = f"https://liff.line.me/{liff_id}"
                            else:
                                base_url = os.getenv("BASE_URL", "").strip().rstrip("/")
                                if base_url:
                                    bind_url = f"{base_url}/gateway?userId={user_id}"
                                else:
                                    host = request.headers.get("host", "127.0.0.1:8000")
                                    proto = request.headers.get("x-forwarded-proto", "http")
                                    bind_url = f"{proto}://{host}/gateway?userId={user_id}"
                                
                            replies = load_message_templates()
                            reply_msg = replies.get("bind_link_msg").replace("{bind_url}", bind_url)
                            
                            enqueue_line_task(
                                cursor, to_user_id=user_id, message_content=reply_msg,
                                source_event_id=event.get("webhookEventId"),
                                idempotency_key=f"bind-link:{event.get('webhookEventId')}",
                            )
                            print(f"[LINE Webhook] Intercepted keyword '{user_text}', queued query link for User: {user_id}")
                            continue
                        
                        enqueue_line_task(
                            cursor, to_user_id=user_id, task_type="rag_reply",
                            payload={"user_text": user_text},
                            source_event_id=event.get("webhookEventId"),
                            idempotency_key=f"rag:{event.get('webhookEventId')}",
                        )

                        
            completed_event_ids = [
                event.get("webhookEventId") for event in payload.events
                if event.get("webhookEventId")
            ]
            if completed_event_ids:
                placeholders = ",".join(["%s"] * len(completed_event_ids))
                cursor.execute(
                    f"""
                    UPDATE line_webhook_events
                    SET processing_status='completed', processed_at=NOW(), error_message=NULL
                    WHERE webhook_event_id IN ({placeholders})
                    """,
                    completed_event_ids,
                )
            conn.commit()
            wake_worker()
            for request_type, request_id in review_notifications:
                _notify_development_reviewer(request_type, request_id)
    except Exception as e:
        conn.rollback()
        print(f"[LINE Webhook] Webhook handler failed: {e}")
        raise HTTPException(status_code=500, detail="LINE webhook processing failed") from e
    finally:
        conn.close()
        
    return {"status": "ok"}

# ----------------- 2. 好好簽 WEBHOOK 接收 -----------------
class BreezySignWebhookPayload(BaseModel):
    event: str
    contract_id: str
    status: str
    signed_at: str = ""

@router.post("/webhook/breezysign")
async def breezysign_webhook(payload: BreezySignWebhookPayload):
    print(f"[Breezy Webhook] Received webhook for contract {payload.contract_id} with status {payload.status}")
    
    if payload.status in ["completed", "signed"]:
        conn = get_db_connection()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute("""
                    SELECT o.*, c.name as client_name, c.service_days, c.due_month, c.service_start_date,
                           s.weekly_rest_days, s.name as staff_name
                    FROM orders o
                    JOIN clients c ON o.client_id = c.id
                    LEFT JOIN staff s ON o.staff_id = s.id
                    WHERE o.contract_id = %s
                """, (payload.contract_id,))
                ord = cursor.fetchone()
                
                if ord:
                    case_no = ord["case_no"]
                    staff_id = ord["staff_id"]
                    client_id = ord["client_id"]
                    service_days = ord["service_days"] if ord["service_days"] else 24
                    
                    print(f"[Breezy Webhook] Found case #{case_no}, triggering auto schedule refiner for {ord['staff_name']}")
                    
                    start_d = ord["actual_start_date"]
                    if not start_d:
                        start_d = date.today() + timedelta(days=10)
                        
                    rest_days = []
                    if ord["weekly_rest_days"]:
                        try:
                            rest_days = json.loads(ord["weekly_rest_days"]) if isinstance(ord["weekly_rest_days"], str) else ord["weekly_rest_days"]
                        except Exception:
                            rest_days = []
                            
                    # 自動執行天數精算順延
                    schedule_res = calculate_attendance_schedule(
                        start_d, service_days, "週休1日", rest_days, set()
                    )
                    end_d = schedule_res.get('actual_end_date')
                    
                    # 更新狀態
                    cursor.execute("""
                        UPDATE orders 
                        SET status = '訂單成立', actual_start_date = %s, actual_end_date = %s, service_mode = '週休 1 日'
                        WHERE case_no = %s
                    """, (start_d, end_d, case_no))
                    
                    # 清除舊排班
                    cursor.execute("DELETE FROM staff_bookings WHERE staff_id = %s AND client_id = %s", (staff_id, client_id))
                    
                    refined_details = schedule_res.get('day_by_day', [])
                    # 寫入工作日排班
                    for day in refined_details:
                        d_date = day["date"]
                        if day["is_work_day"]:
                            cursor.execute("""
                                INSERT INTO staff_bookings (staff_id, client_id, start_date, end_date)
                                VALUES (%s, %s, %s, %s)
                            """, (staff_id, client_id, d_date, d_date))
                            
                    # 發送 LINE 訊息 (移除 emoji 以免 CP950 錯誤)
                    client_msg = f"恭喜！您與月嫂 {ord['staff_name']} 的服務契約已線上簽署完畢！系統已自動為您登載排班出勤。實際服務區間為：{start_d.strftime('%Y-%m-%d')} ~ {end_d.strftime('%Y-%m-%d')}。"
                    cursor.execute("""
                        INSERT INTO line_tasks (to_user_id, message_content, status)
                        VALUES (COALESCE((SELECT line_user_id FROM clients WHERE id = %s), 'mock_client_line_id'), %s, 'pending')
                    """, (client_id, client_msg))
                    
                    staff_msg = f"恭喜！您與客戶 {ord['client_name']} 的服務合約已完成線上簽署！系統已為您登載排班日程：{start_d.strftime('%Y-%m-%d')} ~ {end_d.strftime('%Y-%m-%d')}，請做好服務準備。"
                    cursor.execute("""
                        INSERT INTO line_tasks (to_user_id, message_content, status)
                        VALUES (COALESCE((SELECT line_user_id FROM staff WHERE id = %s), 'mock_staff_line_id'), %s, 'pending')
                    """, (staff_id, staff_msg))
                    
                    conn.commit()
                    wake_worker()
                    print(f"[Breezy Webhook] Case #{case_no} processed. Schedule set to {start_d} ~ {end_d}")
            
        except Exception as e:
            conn.rollback()
            print(f"[Breezy Webhook] Failed to process webhook: {e}")
        finally:
            conn.close()
            
    return {"status": "success"}


