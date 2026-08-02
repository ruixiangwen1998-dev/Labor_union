"""
================================================================================
檔案名稱: ui/components/line_message_manager.py
功能說明: LINE 訊息內容管理元件，維護常用回覆、主動通知及新好友排程訊息
================================================================================
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pandas as pd
import streamlit as st

from ui.api_clients.line_api_client import LineAdminApiClient, LineAdminApiError


SELECTED_KEY = "line_message_template_selected"
NEW_SEED_KEY = "line_message_template_new_seed"
DELETE_KEY = "line_message_template_delete_pending"
PREVIEW_KEY = "line_message_template_preview"
FLASH_KEY = "line_message_template_flash"

CATEGORIES = ["webhook_reply", "push", "scheduled_push", "customer_service"]
USAGES = ["webhook", "push", "schedule", "customer_service"]
EDIT_ROLES = {"line_manager", "system_admin"}
USE_CASES = {
    "auto_reply": {
        "label": "收到訊息時自動回覆",
        "category": "webhook_reply",
        "usage": ["webhook"],
    },
    "manual_reply": {
        "label": "服務人員常用回覆",
        "category": "customer_service",
        "usage": ["customer_service"],
    },
    "push": {
        "label": "主動通知使用者",
        "category": "push",
        "usage": ["push"],
    },
    "schedule": {
        "label": "新好友加入後定時通知",
        "category": "scheduled_push",
        "usage": ["schedule"],
    },
}
def _empty_template() -> dict[str, Any]:
    return {
        "id": f"message_{uuid4().hex[:12]}",
        "name": "新訊息",
        "category": "customer_service",
        "message_type": "text",
        "enabled": True,
        "content": "",
        "variables": [],
        "usage": ["customer_service"],
    }


def _use_case_key(item: dict[str, Any]) -> str:
    usage = set(item.get("usage", []))
    if "schedule" in usage:
        return "schedule"
    if "customer_service" in usage:
        return "manual_reply"
    if "push" in usage:
        return "push"
    return "auto_reply"


def _preview_values(item: dict[str, Any]) -> dict[str, str]:
    examples = {
        "name": "王小明",
        "client_name": "王小明",
        "case_no": "115000001",
        "status_code": "200",
        "bind_url": "https://example.com/bind",
    }
    return {
        variable["name"]: examples.get(
            variable["name"], variable.get("description") or "範例資料"
        )
        for variable in item.get("variables", [])
        if variable.get("name")
    }


def _copy_id(source_id: str, existing_ids: set[str]) -> str:
    base = f"{source_id}_copy"
    candidate = base
    index = 2
    while candidate in existing_ids:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def _clean_cell(value: Any, default: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return default
    return str(value).strip()


def _payload_from_form(
    *,
    template_id: str,
    name: str,
    category: str,
    message_type: str,
    enabled: bool,
    content_source: str,
    usage: list[str],
    variable_rows: pd.DataFrame,
) -> dict[str, Any]:
    content: str | dict[str, Any]
    if message_type == "flex":
        content = json.loads(content_source)
        if not isinstance(content, dict):
            raise ValueError("Flex Message 內容必須是 JSON object")
    else:
        content = content_source

    variables = []
    for row in variable_rows.to_dict("records"):
        variable_name = _clean_cell(row.get("name"))
        if not variable_name:
            continue
        variables.append(
            {
                "name": variable_name,
                "required": bool(row.get("required", True)),
                "description": _clean_cell(row.get("description")),
            }
        )
    return {
        "id": template_id.strip(),
        "name": name.strip(),
        "category": category,
        "message_type": message_type,
        "enabled": enabled,
        "content": content,
        "variables": variables,
        "usage": usage,
    }


def _render_preview(preview: dict[str, Any] | None) -> None:
    if not preview:
        return
    st.markdown("#### 使用者看到的內容")
    if preview.get("message_type") == "flex":
        st.info("這是一則卡片訊息，內容結構已通過檢查。卡片版面由系統統一管理。")
    else:
        st.code(str(preview.get("content", "")), language=None, wrap_lines=True)


def render_message_manager(
    client: LineAdminApiClient,
    token: str | None,
    profile: dict[str, Any],
) -> None:
    st.subheader("常用訊息與自動通知")
    st.caption("選擇一則訊息後，即可修改名稱、用途及使用者會看到的文字。")

    flash = st.session_state.pop(FLASH_KEY, None)
    if flash:
        st.success(flash)

    try:
        state = client.message_template_state(token)
    except LineAdminApiError as exc:
        st.error(f"無法載入訊息範本：{exc}")
        return

    revision = state["revision"]
    config = state["config"]
    templates = list(config.get("templates", []))
    by_id = {item["id"]: item for item in templates}
    can_edit = profile.get("role") in EDIT_ROLES

    if not can_edit:
        st.info("目前帳號只有查看權限；如需修改，請聯絡 LINE 主管。")

    filter_col1, filter_col2, filter_col3 = st.columns([2, 1, 1])
    search = filter_col1.text_input("搜尋訊息", placeholder="輸入訊息名稱")
    category_filter = filter_col2.selectbox(
        "用途",
        ["全部", *[item["label"] for item in USE_CASES.values()]],
    )
    enabled_filter = filter_col3.selectbox("狀態", ["全部", "啟用", "停用"])

    filtered = []
    for item in templates:
        if search and search.lower() not in item["name"].lower():
            continue
        if (
            category_filter != "全部"
            and USE_CASES[_use_case_key(item)]["label"] != category_filter
        ):
            continue
        if enabled_filter == "啟用" and not item["enabled"]:
            continue
        if enabled_filter == "停用" and item["enabled"]:
            continue
        filtered.append(item)

    list_col, action_col = st.columns([4, 2])
    option_ids = [item["id"] for item in filtered]
    current = st.session_state.get(SELECTED_KEY)
    if current == "__new__":
        option_ids = ["__new__", *option_ids]
    elif current not in option_ids:
        current = option_ids[0] if option_ids else None
        st.session_state[SELECTED_KEY] = current

    if option_ids:
        selected = list_col.selectbox(
            "選擇訊息",
            option_ids,
            index=option_ids.index(current) if current in option_ids else 0,
            format_func=lambda item_id: (
                "➕ 新訊息（尚未儲存）"
                if item_id == "__new__"
                else f"{'🟢' if by_id[item_id]['enabled'] else '⚪'} {by_id[item_id]['name']}"
            ),
        )
        st.session_state[SELECTED_KEY] = selected
    else:
        selected = None
        list_col.info("目前篩選條件沒有符合的訊息。")

    if action_col.button("新增訊息", disabled=not can_edit, use_container_width=True):
        st.session_state[NEW_SEED_KEY] = _empty_template()
        st.session_state[SELECTED_KEY] = "__new__"
        st.session_state.pop(PREVIEW_KEY, None)
        st.rerun()

    if selected and selected != "__new__" and action_col.button(
        "複製訊息", disabled=not can_edit, use_container_width=True
    ):
        seed = deepcopy(by_id[selected])
        seed["id"] = _copy_id(seed["id"], set(by_id))
        seed["name"] = f"{seed['name']}（複製）"
        st.session_state[NEW_SEED_KEY] = seed
        st.session_state[SELECTED_KEY] = "__new__"
        st.session_state.pop(PREVIEW_KEY, None)
        st.rerun()

    if selected is None:
        return
    is_new = selected == "__new__"
    item = deepcopy(
        st.session_state.get(NEW_SEED_KEY, _empty_template()) if is_new else by_id[selected]
    )

    st.divider()
    with st.form(f"line_message_template_form_{selected}"):
        template_id = item["id"]
        name_col, enabled_col = st.columns([3, 1])
        name = name_col.text_input("訊息名稱", value=item["name"], disabled=not can_edit)
        enabled = enabled_col.checkbox(
            "允許使用", value=item["enabled"], disabled=not can_edit
        )
        original_use_case = _use_case_key(item)
        use_case = st.selectbox(
            "這則訊息用在哪裡？",
            list(USE_CASES),
            index=list(USE_CASES).index(original_use_case),
            format_func=lambda value: USE_CASES[value]["label"],
            disabled=not can_edit,
        )
        if use_case == original_use_case:
            category = item["category"]
            usage = list(item.get("usage", []))
        else:
            category = USE_CASES[use_case]["category"]
            usage = list(USE_CASES[use_case]["usage"])
        message_type = item["message_type"]
        content_value = (
            json.dumps(item["content"], ensure_ascii=False, indent=2)
            if isinstance(item["content"], dict)
            else str(item["content"])
        )
        if message_type == "text":
            content_source = st.text_area(
                "使用者會看到的文字",
                value=content_value,
                height=220,
                disabled=not can_edit,
            )
            if item.get("variables"):
                st.caption("姓名、案件編號等個人資料會由系統在發送時自動帶入。")
        else:
            content_source = content_value
            st.info("這是一則卡片訊息。為避免版面損壞，此頁只能修改名稱、用途與啟用狀態。")

        variable_rows = pd.DataFrame(
            item.get("variables", []), columns=["name", "required", "description"]
        )

        button_col1, button_col2 = st.columns(2)
        preview_clicked = button_col1.form_submit_button("查看預覽", use_container_width=True)
        save_clicked = button_col2.form_submit_button(
            "儲存訊息",
            type="primary",
            disabled=not can_edit,
            use_container_width=True,
        )

    if preview_clicked or save_clicked:
        try:
            payload = _payload_from_form(
                template_id=template_id,
                name=name,
                category=category,
                message_type=message_type,
                enabled=enabled,
                content_source=content_source,
                usage=usage,
                variable_rows=variable_rows,
            )
            preview_values = _preview_values(item)
        except (ValueError, json.JSONDecodeError) as exc:
            st.error(f"訊息內容格式有誤：{exc}")
        else:
            if preview_clicked:
                try:
                    st.session_state[PREVIEW_KEY] = client.preview_message_template(
                        token, payload, preview_values
                    )
                except LineAdminApiError as exc:
                    st.error(f"預覽失敗：{exc}")
            if save_clicked:
                try:
                    if is_new:
                        client.create_message_template(token, payload, revision=revision)
                    else:
                        client.update_message_template(
                            token, selected, payload, revision=revision
                        )
                except LineAdminApiError as exc:
                    st.error(f"儲存失敗：{exc}")
                else:
                    st.session_state[SELECTED_KEY] = payload["id"]
                    st.session_state.pop(NEW_SEED_KEY, None)
                    st.session_state.pop(PREVIEW_KEY, None)
                    st.session_state[FLASH_KEY] = f"已儲存「{payload['name']}」"
                    st.rerun()

    _render_preview(st.session_state.get(PREVIEW_KEY))

    if not is_new and can_edit:
        st.divider()
        if st.session_state.get(DELETE_KEY) != selected:
            if st.button("刪除這則訊息", type="secondary"):
                st.session_state[DELETE_KEY] = selected
                st.rerun()
        else:
            st.warning(f"確定刪除「{item['name']}」？此操作無法從管理介面復原。")
            confirm_col, cancel_col = st.columns(2)
            if confirm_col.button("確認刪除", type="primary", use_container_width=True):
                try:
                    client.delete_message_template(token, selected, revision=revision)
                except LineAdminApiError as exc:
                    st.error(f"刪除失敗：{exc}")
                else:
                    st.session_state.pop(DELETE_KEY, None)
                    st.session_state.pop(SELECTED_KEY, None)
                    st.session_state.pop(PREVIEW_KEY, None)
                    st.session_state[FLASH_KEY] = f"已刪除「{item['name']}」"
                    st.rerun()
            if cancel_col.button("取消", use_container_width=True):
                st.session_state.pop(DELETE_KEY, None)
                st.rerun()
