"""
================================================================================
檔案名稱: ui/components/line_liff_manager.py
功能說明: LINE LIFF 服務頁面管理元件，編輯頁面文字、顏色、入口與動態表單問題
================================================================================
"""

from __future__ import annotations

import html
import json
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from ui.api_clients.line_api_client import LineAdminApiClient, LineAdminApiError


EDIT_ROLES = {"line_manager", "system_admin"}
FLASH_KEY = "line_liff_flash"
PAGE_LABELS = {
    "gateway": "入口選擇頁",
    "bind": "舊客戶綁定頁",
    "registration": "新客戶登記頁",
    "union_staff_binding": "工會人員帳號綁定頁",
}
FIELD_TYPES = [
    "text",
    "password",
    "textarea",
    "phone",
    "email",
    "date",
    "number",
    "single_choice",
    "multiple_choice",
    "boolean",
]
FIELD_TYPE_LABELS = {
    "text": "單行文字",
    "password": "密碼",
    "textarea": "多行文字",
    "phone": "電話號碼",
    "email": "電子信箱",
    "date": "日期",
    "number": "數字",
    "single_choice": "單選題",
    "multiple_choice": "複選題",
    "boolean": "是／否",
}
CONTENT_LABELS = {
    "existing_customer": "舊客戶入口文字",
    "new_customer": "新客戶入口文字",
    "warning": "提醒文字",
    "privacy_notice": "個資說明",
    "help": "協助說明",
}


def _field_rows(page: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": field["id"],
                "label": field["label"],
                "type": FIELD_TYPE_LABELS.get(field["type"], field["type"]),
                "required": field.get("required", False),
                "enabled": field.get("enabled", True),
                "order": field.get("order", 0),
                "placeholder": field.get("placeholder", ""),
                "help_text": field.get("help_text", ""),
                "system_field": field.get("system_field", False),
                "options_text": "、".join(field.get("options", [])),
            }
            for field in page.get("fields", [])
        ]
    )


def _action_rows(page: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(page.get("actions", []))


def _content_rows(page: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "key": key,
                "label": CONTENT_LABELS.get(key, "補充文字"),
                "text": value,
            }
            for key, value in page.get("content", {}).items()
        ]
    )


def _clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _build_page(
    original: dict[str, Any],
    *,
    title: str,
    subtitle: str,
    submit_button: str,
    success_title: str,
    success_description: str,
    loading_text: str,
    content_rows: pd.DataFrame,
    action_rows: pd.DataFrame | None,
    field_rows: pd.DataFrame | None,
) -> dict[str, Any]:
    page = deepcopy(original)
    page.update(
        {
            "title": title.strip(),
            "subtitle": subtitle.strip(),
            "submit_button": submit_button.strip(),
            "success_title": success_title.strip(),
            "success_description": success_description.strip(),
            "loading_text": loading_text.strip(),
        }
    )
    page["content"] = {}
    for row in content_rows.to_dict("records"):
        key = _clean(row.get("key")) or f"custom_text_{uuid4().hex[:8]}"
        if _clean(row.get("text")):
            page["content"][key] = _clean(row.get("text"))
    if action_rows is not None:
        page["actions"] = [
            {
                "id": _clean(row.get("id")),
                "label": _clean(row.get("label")),
                "description": _clean(row.get("description")),
                "icon": _clean(row.get("icon")),
                "path": _clean(row.get("path")),
                "enabled": bool(row.get("enabled", True)),
                "order": int(row.get("order") or 0),
            }
            for row in action_rows.to_dict("records")
            if _clean(row.get("id"))
        ]
    if field_rows is not None:
        original_system = {
            item["id"]: item for item in original.get("fields", []) if item.get("system_field")
        }
        fields = []
        seen: set[str] = set()
        for row in field_rows.to_dict("records"):
            field_id = _clean(row.get("id")) or f"custom_{uuid4().hex[:8]}"
            if field_id in seen:
                continue
            seen.add(field_id)
            protected = original_system.get(field_id)
            options_text = _clean(row.get("options_text"))
            options = [
                value.strip()
                for value in options_text.replace("，", "、").replace(",", "、").split("、")
                if value.strip()
            ]
            selected_type = _clean(row.get("type")) or FIELD_TYPE_LABELS["text"]
            field_type = next(
                (key for key, label in FIELD_TYPE_LABELS.items() if label == selected_type),
                selected_type if selected_type in FIELD_TYPES else "text",
            )
            field = {
                "id": field_id,
                "label": _clean(row.get("label")),
                "type": field_type,
                "required": bool(row.get("required", False)),
                "enabled": bool(row.get("enabled", True)),
                "order": int(row.get("order") or 0),
                "placeholder": _clean(row.get("placeholder")),
                "help_text": _clean(row.get("help_text")),
                "system_field": False,
                "options": options,
            }
            if protected:
                field.update(
                    {
                        "id": protected["id"],
                        "type": protected["type"],
                        "required": True,
                        "enabled": True,
                        "system_field": True,
                    }
                )
            fields.append(field)
        for field_id, protected in original_system.items():
            if field_id not in seen:
                fields.append(deepcopy(protected))
        page["fields"] = sorted(fields, key=lambda item: item["order"])
    return page


def _preview(theme: dict[str, Any], page: dict[str, Any]) -> None:
    fields = "".join(
        f"<label>{html.escape(field['label'])}</label>"
        f"<div class='input'>{html.escape(field.get('placeholder') or field['type'])}</div>"
        for field in sorted(page.get("fields", []), key=lambda item: item["order"])
        if field.get("enabled", True)
    )
    actions = "".join(
        f"<div class='action'><b>{html.escape(action.get('icon', ''))} "
        f"{html.escape(action['label'])}</b><small>{html.escape(action.get('description', ''))}</small></div>"
        for action in sorted(page.get("actions", []), key=lambda item: item["order"])
        if action.get("enabled", True)
    )
    body = actions or fields or "<p>此頁沒有可顯示的欄位。</p>"
    components.html(
        f"""
        <style>
          body {{ margin:0; padding:16px; background:{theme['background']};
                  font-family:{theme['font_family']}; color:{theme['text_color']}; }}
          .phone {{ max-width:360px; margin:auto; padding:22px; border-radius:20px; background:#ffffffdd;
                    box-shadow:0 8px 24px #00000018; border-top:6px solid {theme['primary_color']}; }}
          h2 {{ margin:0 0 8px; }} p {{ color:{theme['muted_text_color']}; }}
          label {{ display:block; font-weight:600; margin-top:12px; }}
          .input {{ border:1px solid #ccd6e0; border-radius:8px; padding:10px; color:#789; margin-top:4px; }}
          .action {{ padding:14px; border:1px solid #dce4ec; border-radius:12px; margin:10px 0; }}
          small {{ display:block; color:{theme['muted_text_color']}; margin-top:5px; }}
          button {{ width:100%; padding:11px; border:0; border-radius:9px; margin-top:16px;
                    background:{theme['primary_color']}; color:white; font-weight:700; }}
        </style>
        <div class="phone"><h2>{html.escape(page['title'])}</h2>
        <p>{html.escape(page.get('subtitle', ''))}</p>{body}
        {f'<button>{html.escape(page.get("submit_button", "送出"))}</button>' if page['page_type'] != 'navigation' else ''}</div>
        """,
        height=540,
        scrolling=True,
    )


def render_liff_manager(
    client: LineAdminApiClient,
    token: str | None,
    profile: dict[str, Any],
) -> None:
    st.subheader("LINE 服務頁面設定")
    st.caption("修改使用者在 LINE 內開啟的綁定、登記頁面文字與表單問題。")
    flash = st.session_state.pop(FLASH_KEY, None)
    if flash:
        st.success(flash)
    can_edit = profile.get("role") in EDIT_ROLES
    try:
        state = client.liff_config_state(token)
    except LineAdminApiError as exc:
        st.error(f"無法載入 LINE 服務頁面：{exc}")
        return
    config = state["config"]
    revision = state["revision"]
    page_id = st.selectbox(
        "編輯頁面",
        list(PAGE_LABELS),
        format_func=lambda value: PAGE_LABELS[value],
    )
    page = config["pages"][page_id]
    if not can_edit:
        st.info("目前帳號只有查看權限。")

    with st.form(f"liff_editor_{page_id}"):
        st.markdown("#### 頁面顏色")
        theme = config["theme"]
        color1, color2 = st.columns(2)
        primary = color1.color_picker("主要顏色", theme["primary_color"], disabled=not can_edit)
        current_background = theme["background"]
        simple_background = (
            current_background
            if isinstance(current_background, str)
            and current_background.startswith("#")
            and len(current_background) in {4, 7}
            else "#F7FAFC"
        )
        background = color2.color_picker("背景顏色", simple_background, disabled=not can_edit)
        hover = theme["primary_hover_color"]
        text_color = theme["text_color"]
        muted = theme["muted_text_color"]
        font_family = theme["font_family"]

        st.markdown(f"#### {PAGE_LABELS[page_id]}")
        title = st.text_input("標題", page["title"], disabled=not can_edit)
        subtitle = st.text_area("說明", page.get("subtitle", ""), disabled=not can_edit)
        col1, col2 = st.columns(2)
        submit_button = col1.text_input("送出按鈕", page.get("submit_button", "送出"), disabled=not can_edit)
        loading_text = col2.text_input(
            "資料讀取中顯示文字", page.get("loading_text", ""), disabled=not can_edit
        )
        success_title = col1.text_input("成功標題", page.get("success_title", ""), disabled=not can_edit)
        success_description = col2.text_area("成功說明", page.get("success_description", ""), disabled=not can_edit)
        st.markdown("##### 其他固定文字")
        content_rows = st.data_editor(
            _content_rows(page),
            num_rows="dynamic" if can_edit else "fixed",
            disabled=not can_edit,
            use_container_width=True,
            column_order=["label", "text"],
            column_config={
                "label": st.column_config.TextColumn("用途說明", disabled=True),
                "text": st.column_config.TextColumn("顯示文字"),
            },
            key=f"liff_content_{page_id}",
        )
        action_rows = None
        field_rows = None
        if page["page_type"] == "navigation":
            st.markdown("##### 入口卡片")
            action_rows = st.data_editor(
                _action_rows(page),
                num_rows="fixed",
                disabled=not can_edit,
                use_container_width=True,
                column_order=["label", "description", "enabled", "order"],
                column_config={
                    "label": st.column_config.TextColumn("入口名稱"),
                    "description": st.column_config.TextColumn("入口說明"),
                    "enabled": st.column_config.CheckboxColumn("顯示"),
                    "order": st.column_config.NumberColumn("順序", min_value=0, step=1),
                },
                key=f"liff_actions_{page_id}",
            )
        else:
            st.markdown("##### 表單欄位")
            st.caption("可修改問題文字、是否必填與顯示順序；姓名、電話等必要欄位會由系統保護。")
            field_rows = st.data_editor(
                _field_rows(page),
                num_rows="dynamic" if can_edit else "fixed",
                disabled=not can_edit,
                use_container_width=True,
                column_order=[
                    "label",
                    "type",
                    "required",
                    "enabled",
                    "order",
                    "placeholder",
                    "help_text",
                    "options_text",
                ],
                column_config={
                    "label": st.column_config.TextColumn("問題文字", required=True),
                    "type": st.column_config.SelectboxColumn(
                        "回答方式", options=list(FIELD_TYPE_LABELS.values()), required=True
                    ),
                    "required": st.column_config.CheckboxColumn("必填"),
                    "enabled": st.column_config.CheckboxColumn("顯示"),
                    "order": st.column_config.NumberColumn("順序", min_value=0, step=1),
                    "placeholder": st.column_config.TextColumn("輸入提示"),
                    "help_text": st.column_config.TextColumn("補充說明"),
                    "options_text": st.column_config.TextColumn("選項（用、分隔）"),
                },
                key=f"liff_fields_{page_id}",
            )
        preview_clicked = st.form_submit_button("查看手機預覽")
        save_clicked = st.form_submit_button("儲存並啟用", type="primary", disabled=not can_edit)

    try:
        updated = deepcopy(config)
        updated["theme"] = {
            "primary_color": primary,
            "primary_hover_color": hover,
            "background": background.strip(),
            "text_color": text_color,
            "muted_text_color": muted,
            "font_family": font_family,
        }
        updated_page = _build_page(
            page,
            title=title,
            subtitle=subtitle,
            submit_button=submit_button,
            success_title=success_title,
            success_description=success_description,
            loading_text=loading_text,
            content_rows=content_rows,
            action_rows=action_rows,
            field_rows=field_rows,
        )
        updated["pages"][page_id] = updated_page
    except (ValueError, json.JSONDecodeError) as exc:
        st.error(f"頁面設定有誤：{exc}")
        return

    if preview_clicked:
        try:
            client.validate_liff_config(token, updated)
        except LineAdminApiError as exc:
            st.error(f"驗證失敗：{exc}")
        else:
            st.success("設定內容正確，以下為手機版示意預覽。")
            _preview(updated["theme"], updated_page)

    if save_clicked:
        try:
            client.update_liff_config(token, updated, revision=revision)
        except LineAdminApiError as exc:
            if exc.status_code == 409:
                st.warning(f"{exc}，請重新整理後再修改。")
            else:
                st.error(f"儲存失敗：{exc}")
        else:
            st.session_state[FLASH_KEY] = f"{PAGE_LABELS[page_id]}已儲存並啟用。"
            st.rerun()

    with st.expander("查看修改紀錄或還原"):
        try:
            history = client.liff_config_history(token).get("items", [])
        except LineAdminApiError as exc:
            st.error(f"無法載入版本紀錄：{exc}")
            history = []
        if not history:
            st.caption("尚無歷史版本；第一次修改後會保存修改前快照。")
        else:
            history_rows = [
                {
                    "修改時間": item.get("created_at") or "-",
                    "修改者": item.get("actor") or "-",
                    "備註": item.get("reason") or "-",
                }
                for item in history
            ]
            st.dataframe(history_rows, use_container_width=True, hide_index=True)
            restore_revision = st.selectbox(
                "選擇要還原的版本",
                [item["revision"] for item in history],
                format_func=lambda value: next(
                    str(item.get("created_at") or "先前版本")
                    for item in history
                    if item["revision"] == value
                ),
            )
            restore_reason = st.text_input("還原備註")
            confirmed = st.checkbox("我確認還原後，使用者重新開啟頁面就會看到舊設定")
            if st.button("還原此版本", disabled=not (can_edit and confirmed)):
                try:
                    client.rollback_liff_config(
                        token,
                        restore_revision,
                        current_revision=revision,
                        reason=restore_reason,
                    )
                except LineAdminApiError as exc:
                    st.error(f"還原失敗：{exc}")
                else:
                    st.session_state[FLASH_KEY] = "LINE 服務頁面設定已還原。"
                    st.rerun()
