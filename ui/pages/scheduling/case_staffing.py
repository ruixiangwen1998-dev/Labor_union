"""Case-centred formal assignment configuration."""

from __future__ import annotations

from datetime import date
from typing import Any

import requests
import streamlit as st

from ui.pages.order.tab2_assign import _build_sync_request
from ui.pages.shared import build_admin_headers, resolve_api_base_url


def _request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None):
    response = requests.request(
        method,
        f"{resolve_api_base_url()}{path}",
        headers=build_admin_headers(),
        json=payload,
        timeout=15,
    )
    body = response.json()
    if not response.ok or not body.get("success"):
        raise ValueError(body.get("detail") or body.get("message") or "API request failed")
    return body.get("data") or {}


def _date_value(*values: Any) -> date:
    for value in values:
        if value:
            return date.fromisoformat(str(value)[:10])
    return date.today()


def render_case_staffing() -> None:
    """Render one row per assignment and require preview before apply."""
    st.subheader("案件人力配置")
    try:
        orders = _request("/api/v1/orders")
        staff = _request("/api/v1/staff")
    except Exception as error:
        st.error(f"正式人力資料載入失敗：{error}")
        return

    eligible = [
        row for row in orders
        if row.get("order_status") in {"訂單成立", "服務中"}
    ]
    if not eligible:
        st.info("目前沒有訂單成立或服務中的案件。")
        return

    cases = {
        f"{row.get('case_no')}｜{row.get('client_name', '')}｜{row.get('order_status')}": row
        for row in eligible
    }
    selected = cases[st.selectbox("案件", list(cases), key="staffing_case")]
    case_no = selected["case_no"]
    try:
        current = _request(f"/api/v1/cases/{case_no}/assignment-schedules").get(
            "assignments", []
        )
    except Exception as error:
        st.error(f"正式指派載入失敗：{error}")
        return

    count = st.selectbox(
        "服務區段數",
        [1, 2, 3, 4],
        index=min(max(len(current), 1), 4) - 1,
        key=f"staffing_count_{case_no}",
    )
    caregivers = {
        f"#{row['id']}｜{row.get('name', '')}": row["id"]
        for row in staff if isinstance(row.get("id"), int)
    }
    if not caregivers:
        st.warning("目前沒有可配置的月嫂。")
        return

    plan = []
    for index in range(count):
        old = current[index] if index < len(current) else {}
        preferred_staff_id = old.get("staff_id")
        if preferred_staff_id is None and index == 0 and not current:
            preferred_staff_id = selected.get("staff_id")
        labels = list(caregivers)
        selected_index = next(
            (
                position
                for position, label in enumerate(labels)
                if caregivers[label] == preferred_staff_id
            ),
            0,
        )
        caregiver_key = f"staffing_staff_{case_no}_{index}"
        seed_key = f"staffing_staff_seeded_{case_no}_{index}"
        if caregiver_key not in st.session_state or (
            not current and index == 0 and not st.session_state.get(seed_key)
        ):
            st.session_state[caregiver_key] = labels[selected_index]
        if not current and index == 0 and not st.session_state.get(seed_key):
            st.session_state[seed_key] = True
        columns = st.columns(3)
        caregiver = columns[0].selectbox(
            f"第 {index + 1} 段月嫂",
            labels,
            key=caregiver_key,
        )
        start = columns[1].date_input(
            f"第 {index + 1} 段開始日",
            value=_date_value(
                old.get("assigned_start_date"),
                selected.get("actual_start_date"),
                selected.get("start_date"),
            ),
            key=f"staffing_start_{case_no}_{index}",
        )
        end = columns[2].date_input(
            f"第 {index + 1} 段結束日",
            value=_date_value(
                old.get("assigned_end_date"),
                selected.get("actual_end_date"),
                selected.get("end_date"),
            ),
            key=f"staffing_end_{case_no}_{index}",
        )
        plan.append(
            {
                "assignment_id": old.get("id"),
                "staff_id": caregivers[caregiver],
                "assignment_sequence": index + 1,
                "assigned_start_date": start.isoformat(),
                "assigned_end_date": end.isoformat(),
            }
        )

    omitted = current[count:]
    if omitted:
        st.warning(
            "取消候選："
            + "、".join(f"#{row.get('id')} {row.get('staff_name', '')}" for row in omitted)
        )

    try:
        order_change = _build_sync_request(selected)
    except ValueError as error:
        st.error(f"案件日期或服務資料不完整：{error}")
        return
    request = {"order_change": order_change, "assignment_plan": plan}
    state_key = f"staffing_preview_{case_no}"
    if st.button("預覽調整", key=f"staffing_preview_button_{case_no}"):
        try:
            st.session_state[state_key] = {
                "request": request,
                "result": _request(
                    f"/api/v1/orders/{case_no}/assignment-synchronization/preview",
                    method="POST",
                    payload=request,
                ),
            }
            st.rerun()
        except Exception as error:
            st.error(f"預覽失敗：{error}")

    state = st.session_state.get(state_key)
    if not state or state.get("request") != request:
        return
    preview = state["result"]
    st.markdown("#### 調整前")
    st.dataframe(current, hide_index=True, width="stretch")
    st.markdown("#### 調整後")
    st.dataframe(plan, hide_index=True, width="stretch")
    if preview.get("blocking_reasons"):
        st.error("阻擋原因：" + "、".join(map(str, preview["blocking_reasons"])))
        return
    if preview.get("sync_status") == "requires_allocation":
        st.error(
            "時數尚未守恆，無法套用。"
            f"目標 {preview.get('target_hours', 0)} 小時、"
            f"目前配置 {preview.get('proposed_actual_hours', 0)} 小時、"
            f"差額 {preview.get('difference', 0)} 小時；"
            "請調整月嫂或服務區段後重新預覽。"
        )
        return

    removals = preview.get("required_schedule_removals", [])
    confirmed = st.checkbox(
        "確認套用上述調整與受影響排班",
        key=f"staffing_confirm_{case_no}",
    )
    actor = st.text_input("操作人員", key=f"staffing_actor_{case_no}")
    if st.button("確認並套用", disabled=not confirmed, key=f"staffing_apply_{case_no}"):
        if not actor.strip():
            st.error("操作人員不可空白。")
        else:
            try:
                _request(
                    f"/api/v1/orders/{case_no}/assignment-synchronization/apply",
                    method="POST",
                    payload={
                        **request,
                        "schedule_change_plan": {
                            "remove_schedule_ids": [
                                row["schedule_id"] for row in removals
                            ]
                        },
                        "applied_by": actor.strip(),
                    },
                )
                st.session_state.pop(state_key, None)
                st.success("人力配置已套用。")
                st.rerun()
            except Exception as error:
                st.error(f"套用失敗：{error}")

    if st.button("取消調整", key=f"staffing_cancel_{case_no}"):
        st.session_state.pop(state_key, None)
        st.rerun()
