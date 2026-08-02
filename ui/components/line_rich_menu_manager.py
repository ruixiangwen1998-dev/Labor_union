"""
================================================================================
檔案名稱: ui/components/line_rich_menu_manager.py
功能說明: LINE 聊天下方選單管理元件，編輯按鈕、預覽圖片並安全套用單頁或雙頁選單
================================================================================
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from ui.api_clients.line_api_client import LineAdminApiClient, LineAdminApiError


EDIT_ROLES = {"line_manager", "system_admin"}
FLASH_KEY = "line_rich_menu_flash"
PREVIEW_KEY = "line_rich_menu_preview"
TAIPEI = ZoneInfo("Asia/Taipei")
ROLE_LABELS = {
    "customer": "一般客戶／媽媽",
    "staff": "月嫂",
    "union_staff": "工會人員",
}
ACTION_LABELS = {
    "message": "傳送一段文字",
    "url": "開啟指定網頁",
    "liff": "開啟 LINE 內的服務頁面",
    "postback": "執行系統功能",
    "richmenuswitch": "切換選單頁面",
}
PUBLICATION_STATUS_LABELS = {
    "pending": "等待發布",
    "processing": "發布中",
    "published": "已發布",
    "failed": "發布失敗",
    "cancelled": "已取消",
}


def _button_rows(menu: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for button in menu["buttons"]:
        action = button["action"]
        value = (
            action.get("text")
            or action.get("rich_menu_alias_id")
            or action.get("data")
            or action.get("uri")
            or ""
        )
        action_kind = action["type"]
        if action["type"] == "uri":
            action_kind = "liff" if action.get("uri_source") == "liff" else "url"
        rows.append(
            {
                "id": button["id"],
                "label": button["label"],
                "text_color": button.get("text_color", "#FFFFFF"),
                "background_color": button.get("background_color", "#4A90E2"),
                "x": button["bounds"]["x"],
                "y": button["bounds"]["y"],
                "width": button["bounds"]["width"],
                "height": button["bounds"]["height"],
                "action_type": action["type"],
                "uri_source": action.get("uri_source", "literal"),
                "action_kind": ACTION_LABELS[action_kind],
                "action_value": value,
            }
        )
    return pd.DataFrame(rows)


def _build_menu_from_editor(
    *,
    original: dict[str, Any],
    name: str,
    audience_role: str,
    enabled: bool,
    selected: bool,
    set_as_default: bool,
    chat_bar_text: str,
    height: int,
    background_color: str,
    image_mode: str,
    rows: pd.DataFrame,
) -> dict[str, Any]:
    menu = deepcopy(original)
    menu.update(
        {
            "name": name.strip(),
            "audience_role": audience_role,
            "enabled": enabled,
            "selected": selected,
            "set_as_default": set_as_default,
            "chat_bar_text": chat_bar_text.strip(),
            "size": {"width": 2500, "height": int(height)},
        }
    )
    appearance = deepcopy(menu.get("appearance", {}))
    appearance["background_color"] = background_color
    appearance["image_mode"] = image_mode
    if image_mode == "generated":
        appearance["image_asset_id"] = None
    menu["appearance"] = appearance

    buttons = []
    for record in rows.to_dict("records"):
        if pd.isna(record.get("id")) or not str(record.get("id") or "").strip():
            continue
        action_label = str(record.get("action_kind") or ACTION_LABELS["message"])
        action_kind = next(
            (key for key, label in ACTION_LABELS.items() if label == action_label),
            "message",
        )
        action_type = "uri" if action_kind in {"url", "liff"} else action_kind
        uri_source = "liff" if action_kind == "liff" else "literal"
        value = str(record.get("action_value") or "").strip()
        action = {
            "type": action_type,
            "text": value if action_type == "message" else None,
            "data": value if action_type == "postback" else None,
            "uri": value if action_type == "uri" and value else None,
            "uri_source": uri_source if action_type == "uri" else "literal",
            "rich_menu_alias_id": value if action_type == "richmenuswitch" else None,
        }
        if action_type == "richmenuswitch":
            action["data"] = f"menu={value}"
        buttons.append(
            {
                "id": str(record["id"]).strip(),
                "label": str(record.get("label") or "").strip(),
                "text_color": str(record.get("text_color") or "#FFFFFF"),
                "background_color": str(
                    record.get("background_color") or "#4A90E2"
                ),
                "bounds": {
                    "x": int(record.get("x") or 0),
                    "y": int(record.get("y") or 0),
                    "width": int(record.get("width") or 0),
                    "height": int(record.get("height") or 0),
                },
                "action": action,
            }
        )
    menu["buttons"] = buttons
    return menu


def _replace_menu(config: dict[str, Any], updated: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(config)
    result["menus"] = [
        updated if item["id"] == updated["id"] else item for item in result["menus"]
    ]
    return result


def _taipei_time(value: Any) -> str:
    if not value:
        return "-"
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")


def render_rich_menu_manager(
    client: LineAdminApiClient,
    token: str | None,
    profile: dict[str, Any],
) -> None:
    st.subheader("LINE 聊天室下方選單")
    st.caption("選擇使用者身分後，可修改選單文字、點擊後的動作與圖片。")
    flash = st.session_state.pop(FLASH_KEY, None)
    if flash:
        st.success(flash)
    can_edit = profile.get("role") in EDIT_ROLES

    try:
        state = client.line_menu_state(token)
    except LineAdminApiError as exc:
        st.error(f"無法載入 LINE 下方選單：{exc}")
        return
    config = state["config"]
    menus = config.get("menus", [])
    if not menus:
        st.warning("目前沒有 LINE 下方選單設定。")
        return

    selected_id = st.selectbox(
        "選擇要修改的選單",
        [item["id"] for item in menus],
        format_func=lambda value: next(
            f"{item['name']}（{ROLE_LABELS.get(item['audience_role'], '使用者')}）"
            for item in menus
            if item["id"] == value
        ),
    )
    selected_menu = next(item for item in menus if item["id"] == selected_id)
    if selected_menu.get("menu_group_id"):
        st.info(
            "這是工會人員雙頁選單的一部分；發布時會一起檢查並套用同組頁面。"
        )
    if not can_edit:
        st.info("目前帳號可查看與預覽，但不能儲存或發布。")

    with st.form(f"rich_menu_editor_{selected_id}"):
        left, right = st.columns(2)
        name = left.text_input("選單名稱", value=selected_menu["name"], disabled=not can_edit)
        audience_role = right.selectbox(
            "顯示給誰看",
            ["customer", "staff", "union_staff"],
            index=["customer", "staff", "union_staff"].index(
                selected_menu["audience_role"]
            ),
            format_func=lambda value: ROLE_LABELS[value],
            disabled=not can_edit,
        )
        col1, col2, col3 = st.columns(3)
        enabled = col1.checkbox("啟用", value=selected_menu["enabled"], disabled=not can_edit)
        selected = col2.checkbox(
            "開啟聊天室時展開", value=selected_menu["selected"], disabled=not can_edit
        )
        set_as_default = col3.checkbox(
            "設為新好友預設選單",
            value=selected_menu["set_as_default"],
            disabled=not can_edit,
        )
        col4, col5 = st.columns(2)
        chat_bar_text = col4.text_input(
            "聊天列文字",
            value=selected_menu["chat_bar_text"],
            max_chars=14,
            disabled=not can_edit,
        )
        heights = [843, 1686]
        height = col5.selectbox(
            "選單大小",
            heights,
            index=heights.index(selected_menu["size"]["height"]),
            format_func=lambda value: "標準" if value == 843 else "大型",
            disabled=not can_edit,
        )
        appearance = selected_menu.get("appearance", {})
        color_col, mode_col = st.columns(2)
        background_color = color_col.color_picker(
            "背景顏色",
            value=appearance.get("background_color", "#F5F5F5"),
            disabled=not can_edit,
        )
        modes = ["generated", "uploaded"]
        image_mode = mode_col.radio(
            "選單外觀",
            modes,
            index=modes.index(appearance.get("image_mode", "generated")),
            format_func=lambda value: "使用系統配色" if value == "generated" else "使用自訂圖片",
            horizontal=True,
            disabled=not can_edit,
        )
        st.markdown("#### 選單按鈕")
        st.caption("可修改按鈕名稱，以及使用者點下後要傳送文字、開啟網頁或執行功能。")
        rows = st.data_editor(
            _button_rows(selected_menu),
            num_rows="fixed",
            disabled=not can_edit,
            use_container_width=True,
            column_order=["label", "action_kind", "action_value"],
            column_config={
                "label": st.column_config.TextColumn("按鈕名稱", required=True),
                "action_kind": st.column_config.SelectboxColumn(
                    "點擊後要做什麼",
                    options=list(ACTION_LABELS.values()),
                    required=True,
                ),
                "action_value": st.column_config.TextColumn(
                    "傳送文字或網址"
                ),
            },
            key=f"rich_menu_buttons_{selected_id}",
        )
        preview_col, save_col = st.columns(2)
        preview_clicked = preview_col.form_submit_button(
            "查看預覽", use_container_width=True
        )
        save_clicked = save_col.form_submit_button(
            "儲存修改", type="primary", disabled=not can_edit, use_container_width=True
        )

    if preview_clicked or save_clicked:
        try:
            draft = _build_menu_from_editor(
                original=selected_menu,
                name=name,
                audience_role=audience_role,
                enabled=enabled,
                selected=selected,
                set_as_default=set_as_default,
                chat_bar_text=chat_bar_text,
                height=height,
                background_color=background_color,
                image_mode=image_mode,
                rows=rows,
            )
            preview = client.preview_line_menu(token, draft)
        except (ValueError, LineAdminApiError) as exc:
            st.error(f"選單內容有問題：{exc}")
        else:
            st.session_state[PREVIEW_KEY] = preview
            if save_clicked:
                try:
                    client.update_line_menus(
                        token,
                        _replace_menu(config, draft),
                        revision=state["revision"],
                    )
                except LineAdminApiError as exc:
                    st.error(f"儲存失敗：{exc}")
                else:
                    st.session_state[FLASH_KEY] = "選單修改已儲存，尚未套用到 LINE。"
                    st.rerun()

    preview = st.session_state.get(PREVIEW_KEY)
    if preview:
        st.markdown("#### 選單預覽")
        st.image(preview, use_container_width=True)

    st.markdown("#### 自訂選單圖片")
    st.caption("若不上傳圖片，系統會依上方顏色自動產生選單。")
    uploaded = st.file_uploader(
        "上傳 JPEG／PNG",
        type=["jpg", "jpeg", "png"],
        disabled=not can_edit,
        key=f"rich_menu_upload_{selected_id}",
    )
    if st.button("上傳並套用至選單", disabled=not can_edit or uploaded is None):
        try:
            asset = client.upload_line_menu_image(
                token,
                selected_id,
                filename=uploaded.name,
                content=uploaded.getvalue(),
                content_type=uploaded.type or "application/octet-stream",
            )
            updated = deepcopy(selected_menu)
            updated["appearance"]["image_mode"] = "uploaded"
            updated["appearance"]["image_asset_id"] = asset["id"]
            client.update_line_menus(
                token,
                _replace_menu(config, updated),
                revision=state["revision"],
            )
        except LineAdminApiError as exc:
            st.error(f"圖片上傳失敗：{exc}")
        else:
            st.session_state[FLASH_KEY] = "自訂圖片已套用至選單。"
            st.rerun()

    st.markdown("#### 套用到 LINE")
    st.warning("請先儲存修改並確認預覽，再套用到使用者的 LINE。")
    reason = st.text_input("本次修改備註（選填）", key=f"publish_reason_{selected_id}")
    confirmed = st.checkbox(
        "我已確認選單內容，要套用到 LINE",
        key=f"publish_confirm_{selected_id}",
    )
    group_id = selected_menu.get("menu_group_id")
    publish_label = "一起套用雙頁選單" if group_id else "套用到 LINE"
    if st.button(
        publish_label,
        type="primary",
        disabled=not can_edit or not confirmed,
    ):
        try:
            publication = (
                client.publish_line_menu_group(token, group_id, reason=reason)
                if group_id
                else client.publish_line_menu(token, selected_id, reason=reason)
            )
        except LineAdminApiError as exc:
            st.error(f"無法建立發布工作：{exc}")
        else:
            st.session_state[FLASH_KEY] = "選單已排入套用流程，請稍後重新整理查看結果。"
            st.rerun()

    st.markdown("#### 套用紀錄")
    if st.button("重新整理紀錄"):
        st.rerun()
    try:
        history = client.line_menu_publications(token, menu_id=selected_id)
    except LineAdminApiError as exc:
        st.error(f"無法載入發布紀錄：{exc}")
        return
    if not history["items"]:
        st.caption("此選單尚無發布紀錄。")
        return
    table = [
        {
            "狀態": PUBLICATION_STATUS_LABELS.get(item["status"], item["status"]),
            "目前版本": "是" if item["is_current"] else "否",
            "開始時間": _taipei_time(item.get("created_at")),
            "完成時間": _taipei_time(item.get("published_at")),
            "說明": "請重新套用" if item.get("error_code") else "",
        }
        for item in history["items"]
    ]
    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)
    failed = [item for item in history["items"] if item["status"] == "failed"]
    if failed and can_edit:
        retry_id = st.selectbox(
            "選擇要重新套用的紀錄",
            [item["id"] for item in failed],
            format_func=lambda value: next(
                _taipei_time(item.get("created_at"))
                for item in failed
                if item["id"] == value
            ),
        )
        retry_reason = st.text_input("處理備註", key=f"retry_reason_{retry_id}")
        retry_confirmed = st.checkbox("我確認要重新套用這個選單")
        if st.button("重新套用", disabled=not retry_confirmed):
            try:
                client.retry_line_menu_publication(
                    token, retry_id, reason=retry_reason
                )
            except LineAdminApiError as exc:
                st.error(f"重新排入失敗：{exc}")
            else:
                st.session_state[FLASH_KEY] = "選單已重新排入套用流程。"
                st.rerun()
