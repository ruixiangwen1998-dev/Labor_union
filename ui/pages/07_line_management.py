"""
================================================================================
檔案名稱: ui/pages/07_line_management.py
功能說明: Streamlit LINE 管理中心主頁，整合主動監控、訊息、自動通知、選單、表單、人工確認與發送紀錄
================================================================================
"""

from __future__ import annotations

import streamlit as st

from ui.components.line_message_manager import render_message_manager
from ui.components.line_liff_manager import render_liff_manager
from ui.components.line_review_manager import render_review_manager
from ui.components.line_rich_menu_manager import render_rich_menu_manager
from ui.components.line_schedule_manager import render_schedule_manager
from ui.components.line_task_manager import render_task_manager
from ui.components.line_health_monitor import render_line_health_monitor
from ui.api_clients.line_api_client import LineAdminApiClient, LineAdminApiError


title = "💬 LINE 管理中心"
TOKEN_KEY = "line_admin_access_token"
ADMIN_KEY = "line_admin_profile"


def _clear_session() -> None:
    st.session_state.pop(TOKEN_KEY, None)
    st.session_state.pop(ADMIN_KEY, None)


def _login(client: LineAdminApiClient) -> None:
    st.subheader("工會人員登入")
    st.caption("此登入只用於內部管理頁；LINE 一般使用者不會看到。")
    with st.form("line_admin_login"):
        username = st.text_input("帳號")
        password = st.text_input("密碼", type="password")
        submitted = st.form_submit_button("登入", type="primary")
    if submitted:
        try:
            session = client.login(username, password)
        except LineAdminApiError as exc:
            st.error(str(exc))
            return
        st.session_state[TOKEN_KEY] = session["access_token"]
        st.session_state[ADMIN_KEY] = session["admin"]
        st.rerun()


ROLE_LABELS = {
    "line_agent": "服務人員",
    "line_manager": "LINE 主管",
    "system_admin": "系統管理員",
}


def _overview(
    client: LineAdminApiClient,
    token: str | None,
    profile: dict,
) -> None:
    try:
        health = client.health(token)
        capabilities = client.capabilities(token)
        monitoring = client.monitoring_status(token)
        monitoring_events = client.monitoring_events(token, limit=50)
    except LineAdminApiError as exc:
        if exc.status_code == 401:
            _clear_session()
            st.warning("登入已過期，請重新登入。")
            st.rerun()
        st.error(str(exc))
        return

    render_line_health_monitor(monitoring, monitoring_events)

    if profile.get("role") == "system_admin":
        with st.expander("系統管理資訊"):
            available = capabilities.get("available", {})
            for name, enabled in available.items():
                st.write(("✅" if enabled else "⬜") + f" {name}")
            st.caption("下列設定僅供系統管理員檢查，不會顯示實際金鑰內容。")
            st.json(health.get("line_credentials", {}))


def _planned_panel(name: str, description: str) -> None:
    st.subheader(name)
    st.info(f"5.1 已完成安全入口與接口骨架。{description}將在後續 5.x 接上現有 API。")


def show() -> None:
    st.title(title)
    client = LineAdminApiClient()
    if not client.configured:
        st.error("尚未設定 INTERNAL_API_KEY，LINE 管理中心已拒絕啟用。")
        st.code("請在 .env 設定 INTERNAL_API_KEY，或使用 start.bat 共同啟動前後端。")
        return

    bypassed = client.admin_auth_bypassed
    token = st.session_state.get(TOKEN_KEY)
    if not bypassed and not token:
        _login(client)
        return

    try:
        profile = client.me(token)
    except LineAdminApiError as exc:
        _clear_session()
        st.warning(f"請重新登入：{exc}")
        return
    st.session_state[ADMIN_KEY] = profile

    if bypassed:
        st.warning(
            "開發模式：已略過管理員登入。內部 API 金鑰仍在驗證；正式環境會強制恢復登入。"
        )

    header_left, header_right = st.columns([4, 1])
    header_left.caption(
        f"登入者：{profile['display_name']}（{ROLE_LABELS.get(profile['role'], '服務人員')}）"
    )
    if not bypassed and header_right.button("登出", use_container_width=True):
        try:
            client.logout(token)
        except LineAdminApiError:
            pass
        _clear_session()
        st.rerun()

    tabs = st.tabs(
        [
            "使用狀態",
            "訊息內容",
            "自動通知",
            "LINE 下方選單",
            "LINE 表單",
            "待確認申請",
            "客服入口",
            "操作紀錄",
        ]
    )
    with tabs[0]:
        _overview(client, token, profile)
    with tabs[1]:
        render_message_manager(client, token, profile)

    with tabs[2]:
        schedule_tab, task_tab = st.tabs(["新好友通知設定", "發送紀錄"])
        with schedule_tab:
            render_schedule_manager(client, token, profile)
        with task_tab:
            render_task_manager(client, token, profile)

    with tabs[3]:
        render_rich_menu_manager(client, token, profile)

    with tabs[4]:
        render_liff_manager(client, token, profile)

    with tabs[5]:
        render_review_manager(client, token, profile)

    panels = [
        ("客服入口", "工會人員客服系統"),
        ("操作紀錄", "管理員異動稽核"),
    ]
    for tab, (name, description) in zip(tabs[6:], panels):
        with tab:
            _planned_panel(name, description)
