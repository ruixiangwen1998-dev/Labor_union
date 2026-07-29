"""
================================================================================
檔案名稱: ui/components/line_review_manager.py
功能說明: LINE 待確認申請元件，處理月嫂身分認證與客戶帳號重新綁定
================================================================================
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from ui.api_clients.line_api_client import LineAdminApiClient, LineAdminApiError


FLASH_KEY = "line_review_flash"
PAGE_KEY = "line_review_page"
FILTER_KEY = "line_review_filter_signature"
MANAGER_ROLES = {"line_manager", "system_admin"}
TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")
TYPE_LABELS = {
    "staff_verification": "月嫂身分認證",
    "client_rebind": "客戶重新綁定",
}
STATUS_LABELS = {
    "pending": "待審核",
    "approved": "已核准",
    "rejected": "已拒絕",
    "cancelled": "已取消",
}
ROLE_LABELS = {
    "customer": "一般客戶",
    "staff": "月嫂",
    "union_staff": "工會人員",
}


def _mask_line_id(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "-"
    if len(text) <= 8:
        return text[:2] + "***"
    return text[:4] + "…" + text[-4:]


def _format_utc_as_taipei(value: Any) -> str:
    if not value:
        return "-"
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(TAIPEI_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _date_boundary(value: date, *, end: bool) -> str:
    local = datetime.combine(value, time.max if end else time.min, tzinfo=TAIPEI_TIMEZONE)
    return local.astimezone(timezone.utc).isoformat()


def _submit_decision(
    client: LineAdminApiClient,
    token: str | None,
    request_id: int,
    action: str,
    reason: str,
) -> None:
    try:
        result = client.line_review_action(
            token,
            request_id,
            action,
            reason=reason,
        )
    except LineAdminApiError as exc:
        st.error(f"審查處理失敗：{exc}")
        return
    st.session_state[FLASH_KEY] = result.get("message") or f"申請 #{request_id} 已處理"
    st.rerun()


def render_review_manager(
    client: LineAdminApiClient,
    token: str | None,
    profile: dict[str, Any],
) -> None:
    st.subheader("待確認申請")
    st.caption("確認月嫂身分，或處理客戶提出的 LINE 帳號重新綁定申請。")
    flash = st.session_state.pop(FLASH_KEY, None)
    if flash:
        st.success(flash)

    try:
        summary = client.line_review_summary(token)
    except LineAdminApiError as exc:
        st.error(f"無法載入審查統計：{exc}")
        return

    metrics = st.columns(5)
    metrics[0].metric("全部待審", summary["pending_total"])
    metrics[1].metric("月嫂認證", summary["staff_pending"])
    metrics[2].metric("重新綁定", summary["rebind_pending"])
    metrics[3].metric("今日已處理", summary["processed_today"])
    metrics[4].metric(
        f"逾 {summary['stale_hours']} 小時",
        summary["stale_pending"],
    )

    filter1, filter2, filter3 = st.columns([1, 1, 2])
    type_label = filter1.selectbox("申請類型", ["全部", *TYPE_LABELS.values()])
    status_label = filter2.selectbox("處理狀態", list(STATUS_LABELS.values()))
    search = filter3.text_input("搜尋申請編號或姓名")

    date_enabled = st.checkbox("依申請日期篩選", value=False)
    created_from = created_to = None
    if date_enabled:
        date1, date2 = st.columns(2)
        start_date = date1.date_input("開始日期", value=date.today())
        end_date = date2.date_input("結束日期", value=date.today())
        if start_date > end_date:
            st.error("開始日期不能晚於結束日期。")
            return
        created_from = _date_boundary(start_date, end=False)
        created_to = _date_boundary(end_date, end=True)

    if st.button("重新整理", key="line_review_refresh"):
        st.rerun()

    request_type = next(
        (key for key, label in TYPE_LABELS.items() if label == type_label),
        None,
    )
    status_value = next(
        key for key, label in STATUS_LABELS.items() if label == status_label
    )
    signature = (
        request_type,
        status_value,
        search.strip(),
        created_from,
        created_to,
    )
    if st.session_state.get(FILTER_KEY) != signature:
        st.session_state[FILTER_KEY] = signature
        st.session_state[PAGE_KEY] = 1
    page = st.session_state.get(PAGE_KEY, 1)

    try:
        result = client.line_reviews(
            token,
            filters={
                "request_type": request_type,
                "status": status_value,
                "search": search,
                "created_from": created_from,
                "created_to": created_to,
                "page": page,
                "page_size": 25,
            },
        )
    except LineAdminApiError as exc:
        st.error(f"無法載入審查清單：{exc}")
        return

    items = result["items"]
    if not items:
        if result["page"] > 1:
            st.session_state[PAGE_KEY] = 1
            st.rerun()
        st.info("目前沒有符合條件的待確認申請。")
        return

    rows = [
        {
            "申請編號": item["id"],
            "類型": TYPE_LABELS.get(item["request_type"], item["request_type"]),
            "狀態": STATUS_LABELS.get(item["status"], item["status"]),
            "申請者": item.get("display_name") or "-",
            "申請帳號": item.get("line_user_id_masked") or "-",
            "申請時間（台北）": _format_utc_as_taipei(item.get("created_at")),
            "處理者": item.get("reviewer_display_name") or "-",
        }
        for item in items
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    nav1, nav2, nav3 = st.columns([1, 2, 1])
    if nav1.button(
        "上一頁",
        key="line_review_previous_page",
        disabled=result["page"] <= 1,
        use_container_width=True,
    ):
        st.session_state[PAGE_KEY] = result["page"] - 1
        st.rerun()
    nav2.markdown(
        f"<div style='text-align:center'>第 {result['page']} / {result['total_pages']} 頁，共 {result['total']} 筆</div>",
        unsafe_allow_html=True,
    )
    if nav3.button(
        "下一頁",
        key="line_review_next_page",
        disabled=result["page"] >= result["total_pages"],
        use_container_width=True,
    ):
        st.session_state[PAGE_KEY] = result["page"] + 1
        st.rerun()

    request_id = st.selectbox(
        "查看申請詳細資料",
        [int(item["id"]) for item in items],
        format_func=lambda value: (
            f"#{value} · "
            f"{TYPE_LABELS.get(next(item['request_type'] for item in items if int(item['id']) == value), '')}"
        ),
    )
    try:
        detail = client.line_review_detail(token, request_id)
    except LineAdminApiError as exc:
        st.error(f"無法載入申請內容：{exc}")
        return

    st.markdown("#### 申請詳細資料")
    detail_rows = {
        "申請編號": detail["id"],
        "申請類型": TYPE_LABELS.get(detail["request_type"], detail["request_type"]),
        "狀態": STATUS_LABELS.get(detail["status"], detail["status"]),
        "申請時間（台北）": _format_utc_as_taipei(detail.get("created_at")),
        "申請帳號": _mask_line_id(detail.get("line_user_id")),
    }
    if detail["request_type"] == "staff_verification":
        detail_rows.update(
            {
                "資料填寫狀態": "已填寫" if detail.get("submitted_at") else "尚未填寫",
                "比對結果": {
                    "not_submitted": "尚未填寫資料",
                    "matched": "已找到唯一月嫂資料",
                    "not_found": "查無完全符合資料",
                    "conflict": "找到多筆資料，需要人工處理",
                    "already_bound": "月嫂資料已綁定其他 LINE",
                }.get(detail.get("match_status"), "尚未比對"),
                "填寫姓名": detail.get("submitted_name") or "-",
                "填寫生日": detail.get("submitted_birthday") or "-",
                "身分證末四碼": detail.get("submitted_identity_last4") or "-",
                "比對月嫂編號": detail.get("matched_staff_id") or "-",
                "月嫂主檔姓名": detail.get("matched_staff_name") or "-",
                "月嫂主檔電話": detail.get("matched_staff_phone") or "-",
                "月嫂主檔身分證": detail.get("matched_staff_identity_masked") or "-",
                "月嫂主檔生日": detail.get("matched_staff_birthday") or "-",
                "月嫂在職狀態": detail.get("matched_staff_status") or "-",
                "月嫂 LINE 綁定": "尚未綁定" if not detail.get("matched_staff_line_user_id") else "已有綁定",
                "目前身分": ROLE_LABELS.get(
                    detail.get("current_line_role"), "尚未設定"
                ),
                "LINE 好友狀態": "正常" if detail.get("current_line_status") == "active" else "需要確認",
            }
        )
    else:
        detail_rows.update(
            {
                "客戶姓名": detail.get("client_name") or "-",
                "案件編號": detail.get("case_no") or "尚未核發",
                "目前綁定帳號": _mask_line_id(
                    detail.get("current_client_line_user_id")
                ),
                "申請改綁帳號": _mask_line_id(detail.get("new_line_user_id")),
            }
        )
    st.dataframe(
        pd.DataFrame([{"欄位": key, "內容": value} for key, value in detail_rows.items()]),
        use_container_width=True,
        hide_index=True,
    )

    if detail["status"] != "pending":
        st.caption(
            f"處理者：{detail.get('reviewer_display_name') or '開發終端／舊流程'}｜"
            f"處理時間：{_format_utc_as_taipei(detail.get('reviewed_at'))}"
        )
        st.write("處理原因：", detail.get("decision_reason") or "未填寫")
        return

    if profile.get("role") not in MANAGER_ROLES:
        st.info("目前帳號可以查看申請；核准或拒絕需要主管權限。")
        return

    if (
        detail["request_type"] == "staff_verification"
        and detail.get("match_status") != "matched"
    ):
        st.warning("月嫂尚未完成唯一資料比對，目前不能核准綁定。")

    with st.form(f"line_review_decision_{request_id}"):
        decision_label = st.radio("處理決定", ["核准", "拒絕"], horizontal=True)
        reason = st.text_area(
            "處理原因",
            help="拒絕時必填；核准時可填寫供稽核使用的備註。",
            max_chars=1000,
        )
        confirmed = st.checkbox("我已核對上述資料，確認執行此操作")
        submitted = st.form_submit_button(
            "送出審查結果",
            type="primary",
            disabled=(
                decision_label == "核准"
                and detail["request_type"] == "staff_verification"
                and detail.get("match_status") != "matched"
            ),
        )
    if submitted:
        if not confirmed:
            st.error("請先勾選確認。")
        elif decision_label == "拒絕" and not reason.strip():
            st.error("拒絕申請時必須填寫原因。")
        else:
            _submit_decision(
                client,
                token,
                request_id,
                "approve" if decision_label == "核准" else "reject",
                reason,
            )
