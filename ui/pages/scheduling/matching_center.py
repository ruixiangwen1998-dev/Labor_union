"""Negotiation-stage segmented caregiver matching."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
import uuid

import requests
import streamlit as st

from ui.pages.order.tab2_assign import _render_tab2_assign
from ui.pages.shared import build_admin_headers, resolve_api_base_url


def _request(path: str, *, method: str = "GET", payload: Any = None):
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
    return body.get("data")


def _as_date(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


def _actor() -> str:
    profile = st.session_state.get("line_admin_profile") or {}
    return str(
        profile.get("username") if isinstance(profile, dict) else ""
    ).strip() or "development-bypass"


def _render_multi_segment_matching(
    order: dict[str, Any],
    staff: list[dict[str, Any]],
    *,
    preview_only: bool = False,
) -> None:
    """Render the multi-caregiver fallback for one negotiation-stage order."""
    case_no = order["case_no"]
    active_state_key = f"matching_active_state_{case_no}"
    active_state = {}
    if not preview_only:
        try:
            active_state = _request(
                f"/api/v1/orders/{case_no}/matching-plans/active"
            )
            st.session_state[active_state_key] = active_state
            if active_state:
                st.session_state[f"matching_plan_{case_no}"] = active_state.get("plan") or {}
                st.session_state[f"matching_contact_state_{case_no}"] = active_state
                if active_state.get("availability_lock"):
                    st.session_state[f"matching_lock_{case_no}"] = active_state[
                        "availability_lock"
                    ]
        except Exception:
            active_state = st.session_state.get(active_state_key) or {}
    planned_start = _as_date(order.get("actual_start_date") or order.get("start_date"))
    raw_end = order.get("actual_end_date") or order.get("end_date")
    planned_end = (
        _as_date(raw_end)
        if raw_end
        else planned_start + timedelta(days=max(int(order.get("service_days") or 1) - 1, 0))
    )
    if preview_only:
        st.markdown("#### 多月嫂配對測試預覽")
    else:
        st.warning("目前沒有月嫂可獨自承接完整期間，請開始多月嫂配對。")
    count = st.selectbox(
        "分段數", [2, 3, 4], key=f"matching_segment_count_{case_no}"
    )
    staff_labels = {
        f"#{row['id']}｜{row.get('name', '')}": row["id"]
        for row in staff if isinstance(row.get("id"), int)
    }
    span = max((planned_end - planned_start).days + 1, count)
    state_key = f"matching_availability_{case_no}_{count}"
    default_drafts = []
    for index in range(count):
        default_start = planned_start + timedelta(days=(span * index) // count)
        default_end = (
            planned_end
            if index == count - 1
            else planned_start + timedelta(days=(span * (index + 1)) // count - 1)
        )
        default_drafts.append(
            {
                "start_date": default_start.isoformat(),
                "end_date": default_end.isoformat(),
            }
        )
    if state_key not in st.session_state:
        try:
            st.session_state[state_key] = _request(
                f"/api/v1/orders/{case_no}/caregiver-segment-availability/search",
                method="POST",
                payload={
                    "segment_count": count,
                    "segment_drafts": default_drafts,
                    "as_of": date.today().isoformat(),
                },
            )
        except Exception as error:
            st.session_state[state_key] = None
            st.warning(f"初始檔期查詢失敗：{error}")

    availability = st.session_state.get(state_key) or {}
    cached_candidates = availability.get("segment_candidates") or []
    drafts = []
    for index in range(count):
        default_start = planned_start + timedelta(days=(span * index) // count)
        default_end = (
            planned_end
            if index == count - 1
            else planned_start + timedelta(days=(span * (index + 1)) // count - 1)
        )
        start_key = f"matching_start_{case_no}_{index}"
        end_key = f"matching_end_{case_no}_{index}"
        current_start = st.session_state.get(start_key, default_start)
        current_end = st.session_state.get(end_key, default_end)
        selected_staff_ids = {
            row["staff_id"] for row in drafts if row.get("staff_id") is not None
        }
        eligible_ids = {
            item.get("staff_id")
            for item in cached_candidates
            if item.get("segment_index") == index
            and _as_date(item.get("start_date")) <= current_start
            and _as_date(item.get("end_date")) >= current_end
        }
        candidate_labels = [
            label
            for label, staff_id in staff_labels.items()
            if staff_id in eligible_ids and staff_id not in selected_staff_ids
        ]
        columns = st.columns(3)
        label = columns[0].selectbox(
            f"第 {index + 1} 段月嫂",
            ["尚未選擇", *candidate_labels],
            key=f"matching_staff_{case_no}_{index}",
        )
        start = columns[1].date_input(
            f"第 {index + 1} 段開始日",
            value=default_start,
            key=start_key,
        )
        end = columns[2].date_input(
            f"第 {index + 1} 段結束日",
            value=default_end,
            key=end_key,
        )
        draft = {"start_date": start.isoformat(), "end_date": end.isoformat()}
        if label != "尚未選擇":
            draft["staff_id"] = staff_labels[label]
        drafts.append(draft)

    if st.button("重新查詢最新檔期", key=f"matching_refresh_{case_no}"):
        try:
            st.session_state[state_key] = _request(
                f"/api/v1/orders/{case_no}/caregiver-segment-availability/search",
                method="POST",
                payload={
                    "segment_count": count,
                    "segment_drafts": drafts,
                    "as_of": date.today().isoformat(),
                },
            )
        except Exception as error:
            st.error(f"檔期查詢失敗：{error}")

    availability = st.session_state.get(state_key)
    if availability:
        if availability.get("feasibility") == "partial":
            covered = {
                day
                for item in availability.get("segment_candidates", [])
                for day in (
                    _as_date(item["start_date"]) + timedelta(days=offset)
                    for offset in range(
                        (_as_date(item["end_date"]) - _as_date(item["start_date"])).days + 1
                    )
                )
            }
            uncovered = [
                (planned_start + timedelta(days=offset)).isoformat()
                for offset in range((planned_end - planned_start).days + 1)
                if planned_start + timedelta(days=offset) not in covered
            ]
            st.warning("目前只有部分可行人力；未覆蓋日期：" + "、".join(uncovered))
        conflicts = availability.get("conflicts") or []
        if conflicts:
            st.error(
                "阻擋原因："
                + "、".join(
                    f"月嫂 {item.get('staff_id') or '-'}／{item.get('work_date')}／{item.get('reason_code')}"
                    for item in conflicts
                )
            )
        candidates = availability.get("segment_candidates") or []
        if candidates:
            st.dataframe(candidates, hide_index=True, width="stretch")

    selected_segments = [row for row in drafts if row.get("staff_id")]
    if st.button(
        "聯繫與確認意願",
        key=f"matching_contact_{case_no}_{'preview' if preview_only else 'live'}",
        disabled=preview_only,
    ):
        if len(selected_segments) != count:
            st.error("每個區段都必須選擇月嫂。")
        else:
            try:
                plan = _request(
                    f"/api/v1/orders/{case_no}/matching-plans",
                    method="POST",
                    payload={
                        "segments": [
                            {
                                "segment_order": index + 1,
                                **segment,
                            }
                            for index, segment in enumerate(selected_segments)
                        ],
                        "created_by": _actor(),
                        "as_of": date.today().isoformat(),
                    },
                )
                st.session_state[f"matching_plan_{case_no}"] = plan
                st.success("方案已通過最新檔期驗證，可逐位發送訂單資訊。")
            except Exception as error:
                st.error(f"未發送：{error}")

    if preview_only:
        st.caption("測試預覽不會建立方案、鎖定檔期或發送任何聯繫資料。")
        return

    plan = st.session_state.get(f"matching_plan_{case_no}") or {}
    plan_id = plan.get("plan_id") or plan.get("id")
    contact_state_key = f"matching_contact_state_{case_no}"
    if plan_id:
        try:
            refreshed_contact_state = _request(
                f"/api/v1/orders/{case_no}/matching-plans/{plan_id}/contact-state"
            )
            for lifecycle_field in ("availability_lock", "deposit"):
                if lifecycle_field in active_state:
                    refreshed_contact_state[lifecycle_field] = active_state[
                        lifecycle_field
                    ]
            st.session_state[contact_state_key] = refreshed_contact_state
        except Exception as error:
            st.warning(f"聯繫紀錄讀取失敗：{error}")
    contact_state = st.session_state.get(contact_state_key) or {}
    contact_segments = contact_state.get("segments") or []
    lock_state_key = f"matching_lock_{case_no}"
    lock = (
        contact_state.get("availability_lock")
        or st.session_state.get(lock_state_key)
        or {}
    )
    lock_id = lock.get("lock_id") or lock.get("id")
    deposit = contact_state.get("deposit") or {}
    try:
        deposit_receivable = Decimal(str(deposit.get("deposit_receivable")))
        deposit_received = Decimal(str(deposit.get("deposit_received")))
    except (InvalidOperation, TypeError):
        deposit_receivable = Decimal("-1")
        deposit_received = Decimal("-2")
    deposit_confirmed = (
        deposit_receivable > 0 and deposit_received == deposit_receivable
    )
    if plan_id:
        st.markdown("#### 發送紀錄與月嫂意願")
        willingness_labels = {
            "待回覆": "pending",
            "願意": "willing",
            "無意願": "unwilling",
        }
        reverse_willingness = {
            value: label for label, value in willingness_labels.items()
        }
        for segment in contact_segments:
            segment_id = segment["segment_id"]
            with st.container(border=True):
                st.write(
                    f"{segment.get('staff_name') or '月嫂 ' + str(segment.get('staff_id'))}"
                    f"｜{segment.get('assigned_start_date')}～{segment.get('assigned_end_date')}"
                )
                info_1_col, info_2_col, willingness_col, update_col = st.columns(4)
                if info_1_col.button(
                    "發送訂單資訊-1",
                    key=f"matching_info_1_{case_no}_{segment_id}",
                ):
                    try:
                        _request(
                            f"/api/v1/orders/{case_no}/matching-plans/{plan_id}/segments/{segment_id}/information",
                            method="POST",
                            payload={
                                "info_type": 1,
                                "event_key": f"info1-{case_no}-{uuid.uuid4().hex}",
                                "actor": _actor(),
                            },
                        )
                        st.success("訂單資訊-1 已進入可靠發送佇列。")
                        st.rerun()
                    except Exception as error:
                        st.error(f"未發送：{error}")
                if info_2_col.button(
                    "發送訂單資訊-2",
                    key=f"matching_info_2_{case_no}_{segment_id}",
                ):
                    try:
                        _request(
                            f"/api/v1/orders/{case_no}/matching-plans/{plan_id}/segments/{segment_id}/information",
                            method="POST",
                            payload={
                                "info_type": 2,
                                "event_key": f"info2-{case_no}-{uuid.uuid4().hex}",
                                "actor": _actor(),
                            },
                        )
                        st.success("訂單資訊-2 已進入可靠發送佇列。")
                        st.rerun()
                    except Exception as error:
                        st.error(f"未發送：{error}")
                current = reverse_willingness.get(
                    segment.get("willingness"), "待回覆"
                )
                selected = willingness_col.selectbox(
                    "意願",
                    list(willingness_labels),
                    index=list(willingness_labels).index(current),
                    key=f"matching_willingness_{case_no}_{segment_id}",
                )
                if update_col.button(
                    "更新月嫂意願",
                    key=f"matching_willingness_update_{case_no}_{segment_id}",
                ):
                    try:
                        _request(
                            f"/api/v1/orders/{case_no}/matching-plans/{plan_id}/segments/{segment_id}/willingness",
                            method="PUT",
                            payload={
                                "willingness": willingness_labels[selected],
                                "event_key": f"will-{case_no}-{uuid.uuid4().hex}",
                                "actor": _actor(),
                            },
                        )
                        st.success("月嫂意願已更新。")
                        st.rerun()
                    except Exception as error:
                        st.error(f"意願更新失敗：{error}")
                st.caption(
                    "資訊-1："
                    + ("已發送" if segment.get("info_1_sent") else "未發送")
                    + "｜資訊-2："
                    + ("已發送" if segment.get("info_2_sent") else "未發送")
                    + "｜履歷："
                    + ("已發送" if segment.get("resume_sent") else "未發送")
                )

        cancel_reason = st.text_input(
            "取消目前組合原因",
            key=f"matching_cancel_reason_{case_no}",
        )
        cancel_confirmed = st.checkbox(
            "確認取消目前組合；既有發送與意願歷史仍會保留",
            key=f"matching_cancel_confirm_{case_no}",
        )
        if st.button(
            "取消目前組合",
            key=f"matching_cancel_plan_{case_no}",
            disabled=bool(lock_id),
        ):
            if not cancel_confirmed or not cancel_reason.strip():
                st.error("取消目前組合前必須填寫原因並確認。")
            else:
                try:
                    _request(
                        f"/api/v1/orders/{case_no}/matching-plans/{plan_id}/cancel",
                        method="POST",
                        payload={
                            "event_key": f"cancel-plan-{case_no}-{uuid.uuid4().hex}",
                            "actor": _actor(),
                            "reason": cancel_reason.strip(),
                        },
                    )
                    st.session_state.pop(f"matching_plan_{case_no}", None)
                    st.session_state.pop(contact_state_key, None)
                    st.success("目前組合已取消，可調整後重新聯繫。")
                    st.rerun()
                except Exception as error:
                    st.error(f"取消組合失敗：{error}")
        if lock_id:
            st.caption("目前方案已鎖定；若要取消案件，請使用既有訂單取消流程。")

    all_resumes_sent = bool(contact_segments) and all(
        segment.get("resume_sent") for segment in contact_segments
    )
    customer_confirmed = st.checkbox(
        "客戶已確認上述履歷與服務區段",
        key=f"matching_customer_confirmed_{case_no}",
        disabled=not all_resumes_sent or bool(lock_id),
    )
    if plan_id and st.button(
        "確認配對並鎖定服務日期",
        key=f"matching_lock_button_{case_no}",
        disabled=not all_resumes_sent or not customer_confirmed or bool(lock_id),
    ):
        try:
            lock = _request(
                f"/api/v1/orders/{case_no}/matching-plans/{plan_id}/availability-lock/acquire",
                method="POST",
                payload={
                    "event_key": f"lock-{case_no}-{uuid.uuid4().hex}",
                    "actor": _actor(),
                },
            )
            st.session_state[lock_state_key] = lock
            st.success("服務日期已鎖定，訂單維持洽談中等待訂金。")
            st.rerun()
        except Exception as error:
            st.error(f"鎖定失敗：{error}")

    if plan_id and lock_id:
        if deposit_confirmed:
            st.info("訂金已全額入帳；完成正式費率與樓層費分配後即可轉正式排班。")
        confirmed = st.checkbox(
            "確認回復未綁定狀態",
            key=f"matching_release_confirm_{case_no}",
            disabled=deposit_confirmed,
        )
        if st.button(
            "回復未綁定狀態",
            disabled=not confirmed or deposit_confirmed,
            key=f"matching_release_{case_no}",
        ):
            try:
                _request(
                    f"/api/v1/orders/{case_no}/matching-plans/{plan_id}/availability-locks/{lock_id}/release",
                    method="POST",
                    payload={
                        "event_key": f"release-{case_no}-{uuid.uuid4().hex}",
                        "actor": _actor(),
                        "reason": "管理員確認回復未綁定狀態",
                    },
                )
                st.session_state.pop(lock_state_key, None)
                st.success("已解除日期鎖定；履歷與意願歷史保留。")
                st.rerun()
            except Exception as error:
                st.error(f"回復未綁定失敗：{error}")

        if deposit_confirmed:
            st.markdown("#### 訂金入帳後轉正式指派")
            st.caption("每段費率必須明確輸入；樓層費分配合計必須等於本案樓層費。")
            assignment_terms = []
            floor_total = Decimal("0")
            terms_valid = bool(contact_segments)
            order_floor_fee = Decimal(str(order.get("floor_fee") or 0))
            for index, segment in enumerate(contact_segments):
                rate_col, floor_col = st.columns(2)
                hourly_rate = rate_col.number_input(
                    f"第 {index + 1} 段月嫂時薪",
                    min_value=0.0,
                    step=50.0,
                    key=f"matching_conversion_rate_{case_no}_{segment['segment_id']}",
                )
                default_floor = float(order_floor_fee) if len(contact_segments) == 1 else 0.0
                floor_fee = floor_col.number_input(
                    f"第 {index + 1} 段樓層費分配",
                    min_value=0.0,
                    value=default_floor,
                    step=100.0,
                    key=f"matching_conversion_floor_{case_no}_{segment['segment_id']}",
                )
                rate_value = Decimal(str(hourly_rate))
                floor_value = Decimal(str(floor_fee))
                terms_valid = terms_valid and rate_value > 0
                floor_total += floor_value
                assignment_terms.append(
                    {
                        "segment_id": segment["segment_id"],
                        "hourly_rate": str(rate_value),
                        "floor_fee_allocated": str(floor_value),
                    }
                )
            if floor_total != order_floor_fee:
                terms_valid = False
                st.error(
                    f"樓層費分配合計 {floor_total} 元，必須等於本案 {order_floor_fee} 元。"
                )
            conversion_confirmed = st.checkbox(
                "我已確認訂金、每段費率、樓層費分配與正式服務日期",
                key=f"matching_conversion_confirm_{case_no}",
            )
            if st.button(
                "轉為正式指派並寫入行事曆",
                type="primary",
                disabled=not terms_valid or not conversion_confirmed,
                key=f"matching_convert_{case_no}",
            ):
                try:
                    result = _request(
                        f"/api/v1/orders/{case_no}/availability-locks/{lock_id}/convert",
                        method="POST",
                        payload={
                            "event_key": f"convert-{case_no}-{uuid.uuid4().hex}",
                            "actor": _actor(),
                            "reason": "訂金全額入帳，確認轉正式指派",
                            "assignment_terms": assignment_terms,
                        },
                    )
                    for state_key in (
                        f"matching_plan_{case_no}",
                        contact_state_key,
                        lock_state_key,
                        active_state_key,
                    ):
                        st.session_state.pop(state_key, None)
                    st.success(
                        f"已建立 {len(result.get('assignments') or [])} 筆正式指派與行事曆。"
                    )
                    st.rerun()
                except Exception as error:
                    st.error(f"轉正式失敗，資料維持原狀：{error}")

    st.markdown("#### 傳送履歷給客戶")
    resume_note = st.text_area(
        "備註",
        placeholder="多月嫂案件請明確說明由多位月嫂共同完成。",
        key=f"matching_resume_note_{case_no}",
    )
    if st.button("傳送履歷", key=f"matching_resume_{case_no}"):
        if not contact_state.get("all_willing"):
            pending = [
                str(segment.get("staff_name") or segment.get("staff_id"))
                for segment in contact_segments
                if segment.get("willingness") != "willing"
            ]
            st.error("尚未同意的月嫂：" + "、".join(pending))
        elif not resume_note.strip():
            st.error("請先填寫要與履歷一併傳送的備註。")
        else:
            try:
                result = _request(
                    f"/api/v1/orders/{case_no}/matching-plans/{plan_id}/resumes",
                    method="POST",
                    payload={
                        "event_key": f"resume-{case_no}-{uuid.uuid4().hex}",
                        "actor": _actor(),
                        "note": resume_note.strip(),
                    },
                )
                st.success(
                    f"已逐位建立 {len(result.get('line_task_ids') or [])} 筆履歷發送任務。"
                )
                st.rerun()
            except Exception as error:
                st.error(f"履歷未發送：{error}")


def render_matching_center(
    orders: list[dict[str, Any]],
    staff: list[dict[str, Any]],
    *,
    preferred_case_no: str | None = None,
) -> None:
    """Render the original matching workflow with a multi-segment fallback."""
    _render_tab2_assign(
        orders,
        [],
        staff,
        multi_segment_renderer=_render_multi_segment_matching,
        multi_segment_preview_renderer=lambda order, staff_list: (
            _render_multi_segment_matching(
                order,
                staff_list,
                preview_only=True,
            )
        ),
        preferred_case_no=preferred_case_no,
    )
