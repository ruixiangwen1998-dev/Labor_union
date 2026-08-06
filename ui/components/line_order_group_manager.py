"""
================================================================================
檔案名稱: ui/components/line_order_group_manager.py
功能說明: LINE 管理中心的訂單服務群組查詢、成員進度與解除綁定介面
================================================================================
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from ui.api_clients.line_api_client import LineAdminApiClient, LineAdminApiError


STATUS_LABELS = {
    "awaiting_invite": "等待發送邀請",
    "inviting": "邀請中",
    "active": "成員已到齊",
    "left": "機器人已離開",
    "replaced": "已更換群組",
    "cancelled": "已解除綁定",
}
MEMBER_LABELS = {
    "not_ready": "尚未綁定 LINE",
    "pending": "等待發送",
    "sent": "邀請已送出",
    "joined": "已加入群組",
    "failed": "發送失敗",
    "left": "已離開群組",
}


def render_order_group_manager(
    client: LineAdminApiClient,
    token: str | None,
    profile: dict[str, Any],
) -> None:
    st.subheader("訂單 LINE 服務群組")
    st.caption(
        "由工會人員在 LINE 群組輸入「綁定訂單 案件編號」，再輸入「發送邀請連結 網址」。"
    )
    case_filter = st.text_input("搜尋案件編號", placeholder="例如 115000001")
    try:
        groups = client.line_order_groups(token, case_no=case_filter or None)
    except LineAdminApiError as exc:
        st.error(f"無法載入訂單群組：{exc}")
        return
    if not groups:
        st.info("目前沒有符合條件的訂單服務群組。")
        return

    rows = [
        {
            "案件編號": item["case_no"],
            "狀態": STATUS_LABELS.get(item["status"], item["status"]),
            "已加入": f"{int(item.get('joined_count') or 0)}/{int(item.get('expected_count') or 0)}",
            "綁定人員": item.get("bound_by_name") or "-",
            "建立時間": item.get("created_at"),
        }
        for item in groups
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    choices = {f"{item['case_no']}｜{STATUS_LABELS.get(item['status'], item['status'])}": item for item in groups}
    selected = choices[st.selectbox("查看群組詳情", list(choices))]
    try:
        detail = client.line_order_group_detail(token, int(selected["id"]))
    except LineAdminApiError as exc:
        st.error(f"無法載入群組詳情：{exc}")
        return

    for member in detail.get("members") or []:
        who = "媽媽" if member["participant_type"] == "client" else "月嫂"
        st.write(f"- **{who}**：{MEMBER_LABELS.get(member['invitation_status'], member['invitation_status'])}")

    if profile.get("role") in {"line_manager", "system_admin"} and detail["status"] in {
        "awaiting_invite",
        "inviting",
        "active",
    }:
        with st.expander("解除這個群組的訂單綁定"):
            reason = st.text_input("解除原因", key=f"order_group_unbind_reason_{detail['id']}")
            if st.button(
                "確認解除綁定",
                type="primary",
                disabled=len(reason.strip()) < 2,
                key=f"order_group_unbind_{detail['id']}",
            ):
                try:
                    client.unbind_line_order_group(token, int(detail["id"]), reason=reason)
                except LineAdminApiError as exc:
                    st.error(f"解除失敗：{exc}")
                    return
                st.success("已解除訂單服務群組綁定。")
                st.rerun()
