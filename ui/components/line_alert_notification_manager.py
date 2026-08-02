"""
================================================================================
檔案名稱: ui/components/line_alert_notification_manager.py
功能說明: LINE 管理中心異常通知規則、工會人員／群組對象與發送紀錄管理元件
================================================================================
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from ui.api_clients.line_api_client import LineAdminApiClient, LineAdminApiError


EDIT_ROLES = {"line_manager", "system_admin"}
TAIPEI = ZoneInfo("Asia/Taipei")
SEVERITY_LABELS = {"warning": "注意以上", "critical": "只通知嚴重異常"}
DELIVERY_STATUS = {
    "pending": "等待發送",
    "processing": "發送中",
    "retry_scheduled": "等待重試",
    "sent": "已送達 LINE",
    "failed": "發送失敗",
    "cancelled": "已取消",
}
TRANSITION_LABELS = {
    "opened": "發現異常",
    "escalated": "異常升級",
    "recovered": "恢復正常",
    "reminder": "持續異常提醒",
    "test": "測試通知",
}


def _time(value: Any) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")


def _handle_error(exc: LineAdminApiError) -> None:
    if exc.status_code == 409:
        st.warning(f"資料已更新，請重新整理後再操作：{exc}")
    else:
        st.error(str(exc))


def render_alert_notification_manager(
    client: LineAdminApiClient,
    token: str | None,
    profile: dict[str, Any],
) -> None:
    can_edit = profile.get("role") in EDIT_ROLES
    st.markdown("### 異常通知")
    st.caption("系統嚴重異常時主動通知指定工會人員或群組；不會傳送密碼、Token 或會員個資。")
    try:
        state = client.alert_notification_config(token)
        targets = client.alert_notification_targets(token)
        admins = client.alert_notification_available_admins(token)
        deliveries = client.alert_notification_deliveries(token, limit=100)
    except LineAdminApiError as exc:
        _handle_error(exc)
        return

    config = state["config"]
    with st.expander("通知規則", expanded=not targets):
        with st.form("line_alert_notification_config"):
            enabled = st.checkbox(
                "啟用 LINE 異常通知",
                value=bool(config.get("enabled", True)),
                disabled=not can_edit,
            )
            minimum = st.selectbox(
                "最低通知等級",
                ["critical", "warning"],
                index=["critical", "warning"].index(
                    config.get("minimum_severity", "critical")
                ),
                format_func=lambda value: SEVERITY_LABELS[value],
                disabled=not can_edit,
            )
            recovery = st.checkbox(
                "恢復正常時也通知",
                value=bool(config.get("notify_recovery", True)),
                disabled=not can_edit,
            )
            repeat = st.number_input(
                "持續異常再次提醒（分鐘，0 表示不重複）",
                min_value=0,
                max_value=10080,
                value=int(config.get("repeat_after_minutes", 60)),
                disabled=not can_edit,
            )
            submitted = st.form_submit_button("儲存通知規則", disabled=not can_edit)
        if submitted:
            updated = {
                **config,
                "enabled": enabled,
                "minimum_severity": minimum,
                "notify_recovery": recovery,
                "repeat_after_minutes": int(repeat),
            }
            try:
                client.update_alert_notification_config(
                    token, updated, revision=state["revision"]
                )
            except LineAdminApiError as exc:
                _handle_error(exc)
            else:
                st.success("異常通知規則已更新。")
                st.rerun()

    st.markdown("#### 通知對象")
    group_targets = [item for item in targets if item["target_type"] == "group"]
    user_targets = [item for item in targets if item["target_type"] == "user"]
    if not group_targets:
        st.info(
            "群組尚未綁定：先把官方 Bot 邀請進工會通知群組，再由 LINE 主管或系統管理員在群組輸入「綁定異常通知群組」。"
        )
    else:
        st.success(f"已綁定 {len(group_targets)} 個異常通知群組。")

    existing_admin_ids = {item.get("admin_user_id") for item in user_targets}
    available = [item for item in admins if item["id"] not in existing_admin_ids]
    if can_edit and available:
        labels = {
            item["id"]: f"{item['display_name']}（{item['role']}）" for item in available
        }
        with st.form("add_line_alert_user_target"):
            selected_admin = st.selectbox(
                "新增個人通知對象",
                list(labels),
                format_func=lambda value: labels[value],
            )
            add_clicked = st.form_submit_button("新增通知對象")
        if add_clicked:
            selected = next(item for item in available if item["id"] == selected_admin)
            try:
                client.create_alert_notification_target(
                    token,
                    {
                        "target_type": "user",
                        "admin_user_id": selected_admin,
                        "line_target_id": None,
                        "display_name": selected["display_name"],
                        "minimum_severity": "critical",
                        "notify_recovery": True,
                        "enabled": True,
                    },
                )
            except LineAdminApiError as exc:
                _handle_error(exc)
            else:
                st.success("個人通知對象已新增。")
                st.rerun()

    if not targets:
        st.warning("目前沒有任何通知對象；偵測到異常時只會保存在管理中心。")
    for target in targets:
        icon = "👥" if target["target_type"] == "group" else "👤"
        state_label = "啟用" if target.get("enabled") else "停用"
        with st.expander(f"{icon} {target['display_name']}｜{state_label}"):
            with st.form(f"line_alert_target_{target['id']}"):
                name = st.text_input(
                    "顯示名稱",
                    value=target["display_name"],
                    disabled=not can_edit,
                )
                severity = st.selectbox(
                    "此對象接收等級",
                    ["critical", "warning"],
                    index=["critical", "warning"].index(
                        target.get("minimum_severity", "critical")
                    ),
                    format_func=lambda value: SEVERITY_LABELS[value],
                    disabled=not can_edit,
                    key=f"target_severity_{target['id']}",
                )
                recovery = st.checkbox(
                    "接收恢復通知",
                    value=bool(target.get("notify_recovery", True)),
                    disabled=not can_edit,
                    key=f"target_recovery_{target['id']}",
                )
                enabled_target = st.checkbox(
                    "啟用此通知對象",
                    value=bool(target.get("enabled", True)),
                    disabled=not can_edit,
                    key=f"target_enabled_{target['id']}",
                )
                save = st.form_submit_button("儲存對象設定", disabled=not can_edit)
            if save:
                try:
                    client.update_alert_notification_target(
                        token,
                        int(target["id"]),
                        {
                            "display_name": name,
                            "minimum_severity": severity,
                            "notify_recovery": recovery,
                            "enabled": enabled_target,
                        },
                    )
                except LineAdminApiError as exc:
                    _handle_error(exc)
                else:
                    st.success("通知對象已更新。")
                    st.rerun()
            button1, button2 = st.columns(2)
            if button1.button(
                "傳送測試訊息",
                key=f"test_alert_target_{target['id']}",
                disabled=not can_edit or not target.get("enabled"),
                use_container_width=True,
            ):
                try:
                    result = client.test_alert_notification_target(token, int(target["id"]))
                except LineAdminApiError as exc:
                    _handle_error(exc)
                else:
                    if result.get("status") == "sent":
                        st.success("測試訊息已送出。")
                    else:
                        st.warning("測試訊息未送達，請查看下方發送紀錄。")
                    st.rerun()
            if button2.button(
                "刪除通知對象",
                key=f"delete_alert_target_{target['id']}",
                disabled=not can_edit,
                use_container_width=True,
            ):
                try:
                    client.delete_alert_notification_target(token, int(target["id"]))
                except LineAdminApiError as exc:
                    _handle_error(exc)
                else:
                    st.success("通知對象已刪除。")
                    st.rerun()

    st.markdown("#### 最近發送紀錄")
    if not deliveries:
        st.caption("目前沒有異常通知發送紀錄。")
        return
    rows = [
        {
            "時間": _time(item.get("sent_at") or item.get("created_at")),
            "對象": item.get("display_name"),
            "類型": TRANSITION_LABELS.get(item.get("transition"), item.get("transition")),
            "狀態": DELIVERY_STATUS.get(item.get("status"), item.get("status")),
            "嘗試次數": item.get("attempt_count", 0),
            "錯誤": item.get("error_message") or "-",
        }
        for item in deliveries
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

