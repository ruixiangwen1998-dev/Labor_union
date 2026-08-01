"""
================================================================================
檔案名稱: ui/pages/03_calendar.py
功能說明: 服務人員行事曆與檔期調控獨立頁面 (CalendarUI)
專案名稱: Lobar Union - 服務人員與訂單管理系統
建立日期: 2026-07-03
架構規範: ADAD Version 18 (已從 OrderUI 完全解耦獨立)
================================================================================
職責與業務規則:
1. 提供服務人員 (月嫂) 檔期行事曆檢視與切換。
2. 兩階段操作選單 (ADR-v12-01, ADR-v13-01):
   - 「1. 執行操作」: [不連動，單純看行事曆 | 訂單匹配 | 出勤天數精算]
   - 「2. 訂單選擇」: 動態過濾對應狀態案件 (預設為無)。
3. 四色 HTML 月曆 (⚪白/🟡黃/🔴紅/🟢綠底):
   - 🟢 綠底休假: 輸入單日排休調整時，月曆表格即時同步呈現綠底標示。
   - 🔴 紅底工作日: 每增加 1 天綠底休假，後續紅底工作日與完工日自動向後動態順延展延。
   - ⚪ 解鎖備用期: 在「出勤天數精算」下，凡屬 target_order 且超出完工日之舊預排黃底日期強制抹除解鎖為白底。
4. 出勤天數精算與動態排假 (RULE[AGENTS.md]):
   - 確定實際服務開始日 (actual_start_date) 之案件解鎖精算面板。
   - 國定假日單日獨立個體決策: 勾選放假順延 1 天，未勾選照常上班。
5. 導覽約束: ui/app.py 動態載入與 Streamlit `/calendar` 直接入口都必須呼叫同一個 show()。
================================================================================
"""

import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import math
import re
import calendar
import uuid
import json
import requests

from ui import nav_helper
from ui.pages.shared import build_admin_headers, resolve_api_base_url
from ui.pages.scheduling.case_staffing import render_case_staffing
from ui.pages.scheduling.matching_center import render_matching_center

title = "多月嫂排班"
_MATCHING_QUEUE_KEY = "multi_caregiver_matching_case_picker"

def safe_float(val) -> float:
    if val is None:
        return 0.0
    try:
        f = float(val)
        return 0.0 if math.isnan(f) or math.isinf(f) else f
    except:
        return 0.0

def safe_int(val) -> int:
    """安全轉換整數，防護 None, NaN, Inf 及無效字串 (ADR-v18-03)"""
    if val is None:
        return 0
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return 0
        return int(round(f))
    except:
        return 0

def safe_date(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return val.date()
    if hasattr(val, "date"):
        return val
    if isinstance(val, (str, bytes)):
        try:
            clean_str = str(val).split(" ")[0].strip()
            return datetime.strptime(clean_str, "%Y-%m-%d").date()
        except:
            return datetime.today().date()
    return val


def _normalise_calendar_schedule_map(value):
    """Restore integer day keys after the API's JSON object serialization."""
    if not isinstance(value, dict):
        return {}
    normalised = {}
    for raw_day, row in value.items():
        try:
            day = int(raw_day)
        except (TypeError, ValueError):
            continue
        if 1 <= day <= 31 and isinstance(row, dict):
            normalised[day] = row
    return normalised


def _multi_caregiver_request(path, *, method="GET", payload=None):
    """Use only the assignment-aware APIs for the multi-caregiver panel."""
    response = requests.request(
        method,
        f"{resolve_api_base_url()}{path}",
        headers=build_admin_headers(),
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("success", False):
        raise ValueError(body.get("error") or body.get("message") or "多月嫂排班 API 請求失敗")
    return body.get("data") or {}


def _current_admin_actor() -> str:
    profile = st.session_state.get("line_admin_profile") or {}
    username = profile.get("username") if isinstance(profile, dict) else None
    return str(username or "development-bypass").strip()


def _calendar_has_unsaved_leave_changes() -> bool:
    return any(
        (
            key.startswith("leave_batch_dates_")
            and isinstance(value, list)
            and bool(value)
        )
        or (
            key.startswith("leave_batch_preview_state_")
            and isinstance(value, dict)
            and bool(value)
        )
        for key, value in st.session_state.items()
    )


def _discard_calendar_leave_drafts() -> None:
    prefixes = (
        "leave_batch_dates_",
        "leave_batch_preview_state_",
        "leave_batch_reason_",
        "leave_batch_confirm_",
    )
    for key in list(st.session_state):
        if key.startswith(prefixes):
            st.session_state.pop(key, None)


def _multi_caregiver_error(error):
    if isinstance(error, requests.HTTPError) and error.response is not None:
        try:
            detail = error.response.json().get("detail")
        except ValueError:
            detail = error.response.text
        return f"HTTP {error.response.status_code}: {detail}"
    return str(error)


def _coerce_staff_id(value):
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_iso_date_strict(value):
    if not isinstance(value, str):
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _parse_stored_rest_dates(raw_custom_json):
    """Parse legacy persisted rest dates without accepting ambiguous dates."""
    if not raw_custom_json:
        return set(), None

    try:
        persisted_list = (
            json.loads(raw_custom_json)
            if isinstance(raw_custom_json, str)
            else raw_custom_json
        )
    except (TypeError, json.JSONDecodeError, ValueError):
        return set(), "先前儲存的排休資料非有效 JSON，已忽略該欄位。"

    if not isinstance(persisted_list, list):
        return set(), "先前儲存的排休資料不是清單格式，已忽略該欄位。"

    parsed_dates = set()
    invalid_items = []
    for raw_item in persisted_list:
        parsed = _coerce_iso_date_strict(raw_item)
        if parsed is None:
            invalid_items.append(raw_item)
        else:
            parsed_dates.add(parsed)

    if invalid_items:
        return set(), (
            "先前儲存的排休資料含有不合法日期，已忽略該欄位："
            + ", ".join(str(item) for item in invalid_items)
        )

    return parsed_dates, None


def _extract_case_assignments_for_staff(assignments, staff_id):
    if not isinstance(assignments, list):
        return []
    target_staff_id = _coerce_staff_id(staff_id)
    if target_staff_id is None:
        return []

    active = []
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        if assignment.get("status") == "cancelled":
            continue
        assignment_staff_id = _coerce_staff_id(assignment.get("staff_id"))
        if assignment_staff_id == target_staff_id:
            active.append(assignment)
    return active


def _render_assignment_leave_resolution(
    case_no,
    assignment_id,
    assignments,
    *,
    read_only=False,
):
    """Render leave/defer/substitution controls for an already selected assignment."""
    st.markdown("---")
    st.markdown("### 休假、順延與代班")
    st.caption("目前案件與正式服務指派沿用上方選擇；每個休假日期需逐筆決定順延或代班。")
    try:
        schedule_data = _multi_caregiver_request(
            f"/api/v1/assignment-schedules/{assignment_id}"
        )
    except (requests.RequestException, ValueError) as error:
        st.error(f"無法讀取指派排班：{_multi_caregiver_error(error)}")
        return

    assignment = schedule_data.get("assignment", {})
    schedule_days = schedule_data.get("schedule_days", [])
    summary_left, summary_middle, summary_right = st.columns(3)
    summary_left.metric("月嫂", assignment.get("staff_name", "-"))
    summary_middle.metric("服務區段", f"{assignment.get('assigned_start_date', '-')} ～ {assignment.get('assigned_end_date', '-')}")
    summary_right.metric("目前實際時數", assignment.get("actual_hours", 0))

    if not schedule_days:
        if st.button("產生此指派的日排班", key=f"generate_assignment_{assignment_id}"):
            try:
                _multi_caregiver_request(
                    f"/api/v1/assignment-schedules/{assignment_id}/generate",
                    method="POST",
                )
                st.success("已產生日排班。")
                st.rerun()
            except (requests.RequestException, ValueError) as error:
                st.error(f"無法產生日排班：{_multi_caregiver_error(error)}")
        return set()

    display_rows = [
        {
            "日期": item.get("work_date"),
            "狀態": "🔴 工作日" if item.get("is_work_day") else "🟢 休假",
            "雙倍薪": "是" if item.get("is_double_pay") else "否",
            "備註": item.get("notes") or "",
        }
        for item in schedule_days
    ]
    st.dataframe(display_rows, width="stretch", hide_index=True)

    if read_only:
        st.info("此訂單已完成，排班僅供歷史查閱，不開放休假、順延或代班調整。")
        return set()

    rest_day_options = {
        str(item["work_date"]): item
        for item in schedule_days
        if item.get("is_work_day")
    }
    selected_rest_dates = st.multiselect(
        "休假日期",
        list(rest_day_options),
        key=f"leave_batch_dates_{assignment_id}",
    )
    leave_items = []
    if selected_rest_dates:
        try:
            staff_records = _multi_caregiver_request("/api/v1/staff")
        except (requests.RequestException, ValueError):
            staff_records = []
        case_staff_ids = {
            item.get("staff_id")
            for item in assignments
            if isinstance(item.get("staff_id"), int)
        }
        same_case = [
            row for row in staff_records if row.get("id") in case_staff_ids
        ]
        external = [
            row for row in staff_records if row.get("id") not in case_staff_ids
        ]
        substitute_options = {"尚未選擇": None}
        substitute_options.update(
            {
                f"同案件｜{row.get('name', '')}": row.get("id")
                for row in same_case
            }
        )
        substitute_options.update(
            {
                f"外部支援｜{row.get('name', '')}": row.get("id")
                for row in external
            }
        )
        for index, work_date in enumerate(selected_rest_dates):
            columns = st.columns(4)
            columns[0].text_input(
                "休假日期",
                value=work_date,
                disabled=True,
                key=f"leave_date_{assignment_id}_{index}",
            )
            resolution = columns[1].selectbox(
                "休假調整",
                ["順延", "代班"],
                key=f"leave_resolution_{assignment_id}_{index}",
            )
            substitute_label = columns[2].selectbox(
                "代班人員",
                list(substitute_options),
                disabled=resolution == "順延",
                key=f"leave_substitute_{assignment_id}_{index}",
            )
            is_double_pay = columns[3].checkbox(
                "代班雙倍薪",
                value=False,
                disabled=resolution == "順延",
                key=f"leave_double_pay_{assignment_id}_{index}",
                help="代班日即使為國定假日也預設不加倍；需要時由管理員明確勾選。",
            )
            leave_items.append(
                {
                    "original_schedule_id": int(
                        rest_day_options[work_date]["id"]
                        if rest_day_options[work_date].get("id") is not None
                        else rest_day_options[work_date]["schedule_id"]
                    ),
                    "work_date": work_date,
                    "resolution_type": (
                        "defer_following_assignments"
                        if resolution == "順延"
                        else "substitute"
                    ),
                    "substitute_staff_id": substitute_options[substitute_label],
                    "is_double_pay": bool(is_double_pay)
                    if resolution == "代班"
                    else False,
                }
            )

        if st.button(
            "預覽休假調整",
            key=f"leave_batch_preview_{assignment_id}",
        ):
            if any(
                item["resolution_type"] == "substitute"
                and item["substitute_staff_id"] is None
                for item in leave_items
            ):
                st.error("選擇代班時必須指定代班人員。")
            else:
                try:
                    preview = _multi_caregiver_request(
                        f"/api/v1/assignment-schedules/{assignment_id}/rest-dates/leave-resolution/batch-preview",
                        method="POST",
                        payload={
                            "contract_version": "assignment-leave-substitution-batch-preview/v1",
                            "case_no": case_no,
                            "original_assignment_id": assignment_id,
                            "items": leave_items,
                        },
                    )
                    st.session_state[f"leave_batch_preview_state_{assignment_id}"] = {
                        "preview": preview,
                        "request": {
                            "case_no": case_no,
                            "original_assignment_id": assignment_id,
                            "items": leave_items,
                        },
                        "batch_key": (
                            f"leave-{assignment_id}-{uuid.uuid4().hex}"
                        ),
                    }
                except (requests.RequestException, ValueError) as error:
                    st.error(f"休假調整預覽失敗：{_multi_caregiver_error(error)}")

    batch_preview_state = st.session_state.get(
        f"leave_batch_preview_state_{assignment_id}"
    )
    if batch_preview_state:
        batch_preview = batch_preview_state.get("preview") or {}
        transition = batch_preview.get("service_plan_transition") or {}
        st.markdown("#### 調整前／調整後")
        left, right = st.columns(2)
        left.json(transition.get("before") or {})
        right.json(transition.get("after") or {})
        if batch_preview.get("status") == "blocked":
            st.error(
                "阻擋原因："
                + "、".join(
                    item.get("code", str(item))
                    for item in (
                        batch_preview.get("canonical_eligibility", {}).get(
                            "blocking_diagnostics", []
                        )
                    )
                )
            )
        else:
            reason = st.text_input(
                "調整原因",
                key=f"leave_batch_reason_{assignment_id}",
                max_chars=255,
            )
            confirmed = st.checkbox(
                "我已確認調整前後、服務日期與薪資影響",
                key=f"leave_batch_confirm_{assignment_id}",
            )
            if st.button(
                "確認並套用",
                key=f"leave_batch_apply_{assignment_id}",
                disabled=not confirmed or not reason.strip(),
                type="primary",
            ):
                try:
                    stored_request = batch_preview_state["request"]
                    applied = _multi_caregiver_request(
                        f"/api/v1/assignment-schedules/{assignment_id}/rest-dates/leave-resolution/batch-apply",
                        method="POST",
                        payload={
                            "contract_version": "assignment-leave-substitution-batch-apply/v1",
                            "case_no": stored_request["case_no"],
                            "original_assignment_id": stored_request[
                                "original_assignment_id"
                            ],
                            "items": stored_request["items"],
                            "preview_fingerprint": batch_preview[
                                "preview_fingerprint"
                            ],
                            "batch_key": batch_preview_state["batch_key"],
                            "actor": _current_admin_actor(),
                            "reason": reason.strip(),
                        },
                    )
                    st.success(
                        "已套用多日期休假調整。"
                        if applied.get("status") == "applied"
                        else "此批次先前已完成，已安全讀取既有結果。"
                    )
                    st.session_state.pop(
                        f"leave_batch_preview_state_{assignment_id}", None
                    )
                    st.rerun()
                except (requests.RequestException, ValueError, KeyError) as error:
                    st.error(
                        "套用失敗；系統已使用最新資料重新驗證，請查看衝突後再預覽："
                        f"{_multi_caregiver_error(error)}"
                    )
    return {
        _coerce_iso_date_strict(value)
        for value in selected_rest_dates
        if _coerce_iso_date_strict(value) is not None
    }

def _render_staff_calendar():
    """服務人員行事曆與檔期調控獨立頁面入口 (CalendarUI)"""
    st.subheader("服務人員月曆")
    st.write("本系統提供月嫂動態檔期月曆、訂單匹配檔期預估以及確定開始日案件之出勤天數與完工日精算。")

    try:
        admin_headers = build_admin_headers()

        resp_staff = requests.get(
            f"{resolve_api_base_url()}/api/v1/staff",
            headers=admin_headers,
            timeout=10,
        )
        resp_staff.raise_for_status()
        staff_payload = resp_staff.json()
        staff_list = staff_payload.get("data") if isinstance(staff_payload, dict) and staff_payload.get("success") else []
        if not isinstance(staff_list, list):
            staff_list = []
    except Exception as e:
        st.error(f"初始化載入服務人員資料失敗: {e}")
        return

    if not staff_list:
        st.warning("請先在服務人員名冊中建立服務人員。")
        return

    try:
        # 1. 選擇月嫂與年月（同一列）
        staff_options = {f"{s['name']} ({s['phone']})": s['id'] for s in staff_list if s.get('name')}
        if not staff_options:
            st.warning("目前無可用的服務人員姓名資料，無法載入日曆。")
            return
        
        today = datetime.today()
        st.session_state.setdefault("calendar_view_year", today.year)
        st.session_state.setdefault("calendar_view_month", today.month)
        view_year = int(st.session_state["calendar_view_year"])
        view_month = int(st.session_state["calendar_view_month"])
        if st.session_state.pop("calendar_reset_choices", False):
            st.session_state["cal_year_choice"] = view_year
            st.session_state["cal_month_choice"] = view_month

        staff_col, year_col, month_col = st.columns(3)
        with staff_col:
            selected_staff_label = st.selectbox(
                "選擇要查看的服務人員/月嫂",
                list(staff_options.keys()),
                key="cal_staff_main",
            )
            cal_staff_id = staff_options[selected_staff_label]
        with year_col:
            current_year = datetime.today().year
            year_options = list(range(current_year - 2, current_year + 4))
            st.session_state.setdefault(
                "cal_year_choice",
                view_year if view_year in year_options else current_year,
            )
            requested_year = st.selectbox(
                "選擇年份",
                year_options,
                key="cal_year_choice",
            )
        with month_col:
            st.session_state.setdefault("cal_month_choice", view_month)
            requested_month = st.selectbox(
                "選擇月份",
                list(range(1, 13)),
                key="cal_month_choice",
            )

        previous_col, next_col, current_col = st.columns(3)
        pending_month = None
        if (requested_year, requested_month) != (view_year, view_month):
            pending_month = (requested_year, requested_month)
        if previous_col.button("上個月", key="calendar_previous_month"):
            target = datetime(view_year, view_month, 1) - timedelta(days=1)
            pending_month = (target.year, target.month)
        if next_col.button("下個月", key="calendar_next_month"):
            target = datetime(
                view_year + (1 if view_month == 12 else 0),
                1 if view_month == 12 else view_month + 1,
                1,
            )
            pending_month = (target.year, target.month)
        if current_col.button("回到本月", key="calendar_current_month"):
            pending_month = (today.year, today.month)

        st.markdown(f"#### 正在查看：{view_year} 年 {view_month} 月")

        if pending_month is not None and pending_month != (view_year, view_month):
            if _calendar_has_unsaved_leave_changes():
                st.session_state["calendar_pending_month"] = pending_month
            else:
                st.session_state["calendar_view_year"] = pending_month[0]
                st.session_state["calendar_view_month"] = pending_month[1]
                st.session_state["calendar_reset_choices"] = True
                st.rerun()

        pending_month = st.session_state.get("calendar_pending_month")
        if pending_month:
            st.warning("目前有尚未套用的休假調整；切換月份會放棄這些內容。")
            discard_col, stay_col = st.columns(2)
            if discard_col.button(
                "放棄未儲存調整並切換",
                key="calendar_confirm_discard_drafts",
            ):
                _discard_calendar_leave_drafts()
                st.session_state["calendar_view_year"] = pending_month[0]
                st.session_state["calendar_view_month"] = pending_month[1]
                st.session_state["calendar_reset_choices"] = True
                st.session_state.pop("calendar_pending_month", None)
                st.rerun()
            if stay_col.button("留在本月", key="calendar_keep_drafts"):
                st.session_state["calendar_reset_choices"] = True
                st.session_state.pop("calendar_pending_month", None)
                st.rerun()

        cal_year, cal_month = view_year, view_month
            
        # 2. 獲取該月嫂當月的排班狀態與國定假日
        try:
            resp_sched = requests.get(
                f"{resolve_api_base_url()}/api/v1/staff/{cal_staff_id}/monthly-schedule",
                headers=admin_headers,
                params={"year": cal_year, "month": cal_month},
                timeout=10,
            )
            resp_sched.raise_for_status()
            sched_payload = resp_sched.json()
            sched_data = sched_payload.get("data") or {}
            monthly_schedules = _normalise_calendar_schedule_map(
                sched_data.get("schedule_map")
            )
            monthly_schedule_rows = {}
            for row in sched_data.get("days") or []:
                work_date = safe_date(row.get("work_date"))
                if work_date and (
                    row.get("assignment_id") is not None
                    or row.get("status") == "waiting_deposit_lock"
                ):
                    monthly_schedule_rows.setdefault(work_date.day, []).append(row)
        except Exception as err_sched:
            st.warning(f"⚠️ 月度排班資料 API 讀取失敗: {err_sched}")
            monthly_schedules = {}
            monthly_schedule_rows = {}

        try:
            resp_h = requests.get(
                f"{resolve_api_base_url()}/api/v1/holidays",
                headers=admin_headers,
                timeout=10,
            )
            resp_h.raise_for_status()
            h_payload = resp_h.json()
            holidays_raw = h_payload.get("data") if isinstance(h_payload, dict) and h_payload.get("success") else []
            if not isinstance(holidays_raw, list):
                holidays_raw = []
        except Exception as err_h:
            st.warning(f"⚠️ 國定假日資料 API 讀取失敗: {err_h}")
            holidays_raw = []
        
        holiday_map = {}
        for h in holidays_raw:
            h_date = safe_date(h['holiday_date'])
            if h_date and h_date.year == cal_year and h_date.month == cal_month:
                holiday_map[h_date.day] = h['holiday_name']

        # 3. 兩階段操作選單
        try:
            resp_o = requests.get(
                f"{resolve_api_base_url()}/api/v1/orders",
                headers=admin_headers,
                timeout=10,
            )
            resp_o.raise_for_status()
            o_payload = resp_o.json()
            all_orders = o_payload.get("data") if isinstance(o_payload, dict) and o_payload.get("success") else []
            if not isinstance(all_orders, list):
                all_orders = []
        except Exception as err_o:
            st.warning(f"⚠️ 訂單資料 API 讀取失敗: {err_o}")
            all_orders = []
        calc_res = None
        target_order = None
        case_assignments = []
        preview_days_set = set()
        buffer_days_set = set()
        
        green_days_set = set()      # 🟢 綠底休假日期集合
        calc_red_days_set = set()   # 🔴 算術推進後的紅底工作日集合
        
        col_op1, col_op2 = st.columns([1, 2])
        with col_op1:
            action_mode = st.radio(
                "1. 執行操作",
                ["訂單匹配", "出勤天數精算"],
                index=0
            )
            
        with col_op2:
            # 根據 1. 執行操作 動態過濾符合條件的訂單
            if action_mode == "訂單匹配":
                # 篩選洽談中且無硬衝突的案件
                filtered_orders = []
                for o in all_orders:
                    if o.get('order_status') == '洽談中':
                        st_d_check = safe_date(o['actual_start_date']) or safe_date(o['start_date'])
                        days_cnt_check = o['service_days'] or 20
                        ed_d_check = (
                            safe_date(o.get('actual_end_date'))
                            or safe_date(o.get('end_date'))
                            or (
                                st_d_check + timedelta(days=days_cnt_check - 1)
                                if st_d_check
                                else None
                            )
                        )
                        
                        has_conflict = False
                        if st_d_check and ed_d_check:
                            curr_c = st_d_check
                            while curr_c <= ed_d_check:
                                if curr_c.year == cal_year and curr_c.month == cal_month:
                                    ex = monthly_schedules.get(curr_c.day)
                                    if ex and (ex['status'] == 'red' or (ex['status'] == 'yellow' and "預留備用期" not in ex['client_name'])):
                                        has_conflict = True
                                        break
                                curr_c += timedelta(days=1)
                        filtered_orders.append({**o, "_calendar_conflict": has_conflict})
            elif action_mode == "出勤天數精算":
                # 正式調整只處理成立／服務中案件；當月完成案件保留唯讀歷史查閱。
                visible_calendar_case_nos = {
                    row.get("case_no")
                    for rows in monthly_schedule_rows.values()
                    for row in rows
                    if row.get("case_no")
                }
                filtered_orders = [
                    o for o in all_orders
                    if bool(o.get('actual_start_date'))
                    and (
                        o.get('order_status') in {'訂單成立', '服務中'}
                        or (
                            o.get('order_status') == '訂單完成'
                            and o.get('case_no') in visible_calendar_case_nos
                        )
                    )
                ]
            else:
                filtered_orders = []
                
            order_menu_opts = {"無 (單純查看行事曆)": None}
            for o in filtered_orders:
                st_d_tmp = safe_date(o['actual_start_date']) or safe_date(o['start_date'])
                days_cnt_tmp = o['service_days'] or 20
                ed_d_tmp = (
                    safe_date(o.get('actual_end_date'))
                    or safe_date(o.get('end_date'))
                    or (
                        st_d_tmp + timedelta(days=days_cnt_tmp - 1)
                        if st_d_tmp
                        else None
                    )
                )
                st_str = st_d_tmp.strftime('%Y-%m-%d') if st_d_tmp else '未定'
                ed_str = ed_d_tmp.strftime('%Y-%m-%d') if ed_d_tmp else '未定'
                label = f"訂單 #{o['case_no']} {o['client_name']} {o['order_status']} ({st_str} ~ {ed_str})"
                order_menu_opts[label] = o['case_no']
                
            selected_order_label = st.selectbox(
                "2. 訂單選擇", 
                list(order_menu_opts.keys()), 
                index=0,
                disabled=(action_mode == "不連動，單純看行事曆")
            )
            calc_case_no = order_menu_opts[selected_order_label]
            calc_assignment_id = None
            if calc_case_no:
                try:
                    case_assignments = _multi_caregiver_request(
                        f"/api/v1/cases/{calc_case_no}/assignment-schedules"
                    ).get("assignments", [])
                    active_assignments = _extract_case_assignments_for_staff(
                        case_assignments, cal_staff_id
                    )
                    if len(active_assignments) == 1:
                        calc_assignment_id = active_assignments[0].get("id")
                        st.caption(
                            "已自動使用該月嫂目前唯一有效的正式服務指派，"
                            "可直接進入排休保存流程。"
                        )
                    elif len(active_assignments) > 1:
                        st.warning("此案件目前有多位有效正式服務指派，請先選擇服務指派後再儲存排休。")
                        assignment_options = {
                            "請先選擇服務指派": None
                        }
                        for item in active_assignments:
                            assignment_label = (
                                f"#{item.get('id')} "
                                f"{item.get('staff_name', '') or ''} "
                                f"{item.get('assigned_start_date', '')} ～ {item.get('assigned_end_date', '')}"
                            ).strip()
                            assignment_options[assignment_label] = item.get("id")
                        selected_assignment_label = st.selectbox(
                            "2-1. 選擇正式服務指派",
                            list(assignment_options.keys()),
                            index=0,
                            key=f"calendar_case_assignment_{calc_case_no}_{cal_staff_id}",
                        )
                        calc_assignment_id = assignment_options.get(selected_assignment_label)
                    else:
                        st.warning("此案件目前沒有可用的正式服務指派（未取消、且屬於該月嫂）。")
                except (requests.RequestException, ValueError) as error:
                    st.error(f"無法取得案例正式服務指派：{_multi_caregiver_error(error)}")

            if calc_case_no:
                target_order = next((o for o in all_orders if o['case_no'] == calc_case_no), None)

        # 4. 訂單匹配模式的黃底試算準備
        if action_mode == "訂單匹配" and target_order:
            st_d = safe_date(target_order['actual_start_date']) or safe_date(target_order['start_date'])
            days_cnt = target_order['service_days'] or 20
            ed_d = st_d + timedelta(days=days_cnt - 1) if st_d else None
            
            if st_d and ed_d:
                curr = st_d
                while curr <= ed_d:
                    if curr.year == cal_year and curr.month == cal_month:
                        preview_days_set.add(curr.day)
                    curr += timedelta(days=1)
                    
                buf_start = ed_d + timedelta(days=1)
                buf_end = ed_d + timedelta(days=7)
                curr = buf_start
                while curr <= buf_end:
                    if curr.year == cal_year and curr.month == cal_month:
                        buffer_days_set.add(curr.day)
                    curr += timedelta(days=1)
            st.info(f"🤝 正在預覽案件 #{target_order['case_no']} ({target_order['client_name']}) 的預排檔期 (黃底) 與 7 天預留備用期 (黃底)。")

        # 5. 出勤天數精算模式：在繪製月曆前優先執行精算控制面板 (確保解鎖預留備用期與連動月曆)
        if action_mode == "出勤天數精算" and target_order:
            st_d = safe_date(target_order['actual_start_date']) or safe_date(target_order['start_date'])
            calc_days = target_order['service_days'] or 20
            potential_dates = [st_d + timedelta(days=i) for i in range(calc_days + 40)]
            custom_leave_dates = set()

            if calc_assignment_id:
                custom_leave_dates = (
                    _render_assignment_leave_resolution(
                        calc_case_no,
                        calc_assignment_id,
                        case_assignments,
                        read_only=target_order.get("order_status") == "訂單完成",
                    )
                    or set()
                )
            else:
                st.info("請先選擇此月嫂在本案件中的正式服務指派，再進行休假、順延或代班。")
            
            holiday_dates_map = {}
            for h in holidays_raw:
                hd = safe_date(h['holiday_date'])
                if hd in potential_dates:
                    label = f"🔴 {h['holiday_name']} ({hd.strftime('%Y-%m-%d')})"
                    holiday_dates_map[label] = hd
                    
            st.markdown("---")
            st.markdown(f"### ⚙️ 出勤天數精算控制面板 (案件編號: `{target_order['case_no']}` - {target_order['client_name']})")
            
            col_m1, col_m2 = st.columns(2)
            with col_m1:
                raw_service_mode = target_order.get('service_mode') or '週休1日'
                st.markdown(f"📋 **登記服務方式**: `{raw_service_mode}`")
                st.caption("💡 提示：勾選下方放假或排休選項，月曆將即時同步呈現 🟢 綠底休假 與 🔴 紅底順延完工日 (預留備用期已自動解鎖為白底)。")
                
                if holiday_dates_map:
                    selected_holiday_rest_labels = st.multiselect(
                        "🧧 國定假日單日放假勾選 (勾選放假順延1天，未勾選照常上班)",
                        list(holiday_dates_map.keys()),
                        default=list(holiday_dates_map.keys()),
                        key="holiday_rest_ms_page"
                    )
                    custom_holiday_rest_dates = {holiday_dates_map[k] for k in selected_holiday_rest_labels}
                else:
                    st.info("ℹ️ 該服務區間與月份未涵蓋中華民國國定假日。")
                    custom_holiday_rest_dates = set()
                
            with col_m2:
                try:
                    resp_calc1 = requests.post(
                        f"{resolve_api_base_url()}/api/v1/orders/calculate-schedule",
                        headers=admin_headers,
                        json={
                            "actual_start_date": str(st_d),
                            "target_service_days": calc_days,
                            "service_mode": raw_service_mode,
                        },
                        timeout=10,
                    )
                    resp_calc1.raise_for_status()
                    init_calc = resp_calc1.json().get("data") or {}
                except Exception:
                    init_calc = {}
                st.metric("本次休假調整日期", f"{len(custom_leave_dates)} 天")
                st.caption("正式寫入只能由上方 Preview／確認／Apply 流程完成；取消草稿不會修改正式排班。")
                
            base_salary = safe_float(target_order.get('service_salary')) or (calc_days * 2000.0)
            
            try:
                resp_calc2 = requests.post(
                    f"{resolve_api_base_url()}/api/v1/orders/calculate-schedule",
                    headers=admin_headers,
                    json={
                        "actual_start_date": str(st_d),
                        "target_service_days": calc_days,
                        "service_mode": raw_service_mode,
                        "custom_leave_dates": [str(d) for d in custom_leave_dates],
                        "custom_holiday_rest_dates": [str(d) for d in custom_holiday_rest_dates],
                        "monthly_salary_base": base_salary,
                    },
                    timeout=10,
                )
                resp_calc2.raise_for_status()
                calc_res = resp_calc2.json().get("data") or {}
            except Exception:
                calc_res = {}
            
            if calc_res:
                for item in calc_res.get('day_by_day', []):
                    item_date = safe_date(item['date'])
                    if item_date and item_date.year == cal_year and item_date.month == cal_month:
                        if item['is_rest_day']:
                            green_days_set.add(item_date.day)
                        else:
                            calc_red_days_set.add(item_date.day)


    except Exception as e_step2:
        st.error(f"資料庫與選單加載失敗: {e_step2}")
        st.exception(e_step2)
        return

    try:
        # 6. 繪製四色 HTML 月曆表格 (即時反映 ⚪白 / 🟡黃 / 🔴紅 / 🟢綠底)
        first_weekday, num_days = calendar.monthrange(cal_year, cal_month)
        first_weekday_sun = (first_weekday + 1) % 7
        
        html = """<style>
.cal-table { width: 100%; border-collapse: collapse; font-family: sans-serif; margin-top: 15px; margin-bottom: 20px; }
.cal-table th { background-color: #f3f4f6; color: #374151; padding: 10px; text-align: center; border: 1px solid #e5e7eb; font-weight: bold; }
.cal-table td { height: 110px; width: 14%; border: 1px solid #e5e7eb; vertical-align: top; padding: 8px; position: relative; }
.day-num { font-weight: bold; font-size: 1.1em; color: #4b5563; }
.day-holiday { font-size: 0.8em; color: #ef4444; margin-top: 2px; font-weight: bold; }
.day-status { font-size: 0.85em; margin-top: 6px; padding: 4px 6px; border-radius: 4px; font-weight: 500; text-align: center; }
.status-white { background-color: #ffffff; color: #1f2937; }
.status-yellow { background-color: #fef08a; color: #854d0e; }
.status-red { background-color: #fca5a5; color: #991b1b; }
.status-green { background-color: #bbf7d0; color: #166534; }
.status-label-white { color: #10b981; font-weight: bold; }
.status-label-yellow { color: #b45309; font-weight: bold; }
.status-label-red { color: #b91c1c; font-weight: bold; }
.status-label-green { color: #15803d; font-weight: bold; }
.client-text { font-size: 0.9em; margin-top: 4px; display: block; }
</style>
<table class="cal-table"><thead><tr><th>星期日</th><th>星期一</th><th>星期二</th><th>星期三</th><th>星期四</th><th>星期五</th><th>星期六</th></tr></thead><tbody>"""
        
        day = 1
        for row in range(6):
            html += "<tr>"
            for col in range(7):
                cell_idx = row * 7 + col
                if cell_idx < first_weekday_sun or day > num_days:
                    html += "<td class='status-white'></td>"
                else:
                    day_info = monthly_schedules.get(day, None)
                    day_rows = monthly_schedule_rows.get(day, [])
                    holiday_name = holiday_map.get(day, None)
                    
                    bg_class = "status-white"
                    status_label = "<span class='status-label-white'>⚪ 可接案</span>"
                    client_text = ""
                    
                    is_target_order_record = False
                    
                    # 1. 既有資料庫記錄之狀態 (預設)
                    if day_info:
                        if action_mode == "出勤天數精算" and target_order:
                            rec_client = day_info.get('client_name', '')
                            if target_order['client_name'] in rec_client or "預留備用期" in rec_client:
                                is_target_order_record = True
                                
                        if day_info['status'] == 'yellow':
                            if not is_target_order_record:
                                bg_class = "status-yellow"
                                status_label = "<span class='status-label-yellow'>🟡 已鎖定／待成立</span>"
                                client_text = f"<span class='client-text'><b>客戶: {day_info['client_name']}</b></span>"
                        elif day_info['status'] == 'green':
                            if not is_target_order_record:
                                bg_class = "status-green"
                                status_label = "<span class='status-label-green'>🟢 排定休假</span>"
                                client_text = f"<span class='client-text'><b>休假: {day_info['client_name']}</b></span>"
                        elif day_info['status'] == 'red':
                            if not is_target_order_record:
                                bg_class = "status-red"
                                status_label = "<span class='status-label-red'>🔴 服務工作日</span>"
                            client_text = f"<span class='client-text'><b>客戶: {day_info['client_name']}</b></span>"

                    if day_rows:
                        client_text = "".join(
                            "<span class='client-text'><b>"
                            + f"{row.get('client_name') or '-'}｜{row.get('order_status') or '-'}｜{row.get('staff_name') or '-'}"
                            + "</b></span>"
                            for row in day_rows
                        )
                    
                    # 2. 訂單匹配模式下疊加黃底預排試算
                    if action_mode == "訂單匹配" and target_order and bg_class == "status-white":
                        if day in preview_days_set:
                            bg_class = "status-yellow"
                            status_label = "<span class='status-label-yellow'>🟡 試算預排檔期</span>"
                            client_text = f"<span class='client-text'><b>預覽: {target_order['client_name']}</b></span>"
                        elif day in buffer_days_set:
                            bg_class = "status-yellow"
                            status_label = "<span class='status-label-yellow'>🟡 試算預留備用期</span>"

                    # 3. 出勤天數精算模式：即時四色疊加 (🟢 綠底休假 / 🔴 紅底工作日 / ⚪ 完全淨化解鎖為白底)
                    if action_mode == "出勤天數精算" and target_order:
                        if day in green_days_set:
                            bg_class = "status-green"
                            status_label = "<span class='status-label-green'>🟢 綠底休假/請假</span>"
                            client_text = f"<span class='client-text'><b>休假: {target_order['client_name']} 案</b></span>"
                        elif day in calc_red_days_set:
                            bg_class = "status-red"
                            status_label = "<span class='status-label-red'>🔴 服務工作日</span>"
                            client_text = f"<span class='client-text'><b>客戶: {target_order['client_name']}</b></span>"
                        elif is_target_order_record:
                            bg_class = "status-white"
                            status_label = "<span class='status-label-white'>⚪ 可接案</span>"
                            client_text = ""

                    holiday_text = f"<div class='day-holiday'>🔴 {holiday_name}</div>" if holiday_name else ""
                    
                    html += f"<td class='{bg_class}'><div class='day-num'>{day}</div>{holiday_text}<div class='day-status'>{status_label}{client_text}</div></td>"
                    day += 1
            html += "</tr>"
            if day > num_days:
                break
        html += "</tbody></table>"
        
        st.markdown(html, unsafe_allow_html=True)
    except Exception as e_step3:
        st.error(f"❌ 月曆 HTML 繪製失敗: {e_step3}")
        st.exception(e_step3)
        return

    # 7. 出勤天數精算面板之算術結果展現
    if action_mode == "出勤天數精算" and target_order and calc_res:
        try:
            st.markdown("#### 📊 出勤天數與完工日算術結果")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("目標服務天數 N", f"{calc_res['target_service_days']} 天")
            c2.metric("總日曆天數", f"{calc_res['total_calendar_days']} 天")
            c3.metric("🟢 綠底休假/請假天數", f"{calc_res['rest_days_count']} 天 (🔴 紅底已自動順延)")
            c4.metric("算術最終完工日", f"{calc_res['actual_end_date']}")
            
            st.markdown("#### 🔴 國定假日與月嫂自主出勤統計 (短期契約無雙倍薪條款)")
            if calc_res['national_holidays_found']:
                for h in calc_res['national_holidays_found']:
                    status_str = "🟢 月嫂選擇照常出勤 (計為正常工作日)" if h['is_worked'] else "🔴 月嫂選擇放假 (完工日已自動順延1天)"
                    st.write(f"- **{h['name']}** ({h['date']}) → `{status_str}`")
            else:
                st.write("該服務區間內未涵蓋中華民國國定假日。")
                
            st.info(f"💡 預估月嫂應領總薪資: **{calc_res['total_estimated_salary']:,.0f} 元** (短期契約依約固定不加計雙倍薪加給)。")
                
            with st.expander("📋 點擊展開「週報精細統計與每日出勤拆解」"):
                df_w = pd.DataFrame(calc_res['weekly_stats'])
                df_w.columns = ["週次", "週開始日", "週結束日", "工作天數", "休假天數", "國定假日天數"]
                st.dataframe(df_w, width='stretch', hide_index=True)
        except Exception as e_step4:
            st.error(f"精算結果渲染失敗: {e_step4}")
            st.exception(e_step4)
            return


def _load_matching_center_data():
    headers = build_admin_headers()
    base_url = resolve_api_base_url()
    orders_response = requests.get(
        f"{base_url}/api/v1/orders", headers=headers, timeout=10
    )
    staff_response = requests.get(
        f"{base_url}/api/v1/staff", headers=headers, timeout=10
    )
    orders_response.raise_for_status()
    staff_response.raise_for_status()
    return (
        orders_response.json().get("data") or [],
        staff_response.json().get("data") or [],
    )


def show():
    """多月嫂排班集中入口。"""
    st.title("多月嫂排班")
    queue_item = nav_helper.current_queue_item(_MATCHING_QUEUE_KEY)
    if queue_item is not None:
        queue = nav_helper.current_queue(_MATCHING_QUEUE_KEY)
        st.warning(
            f"來自異常警示中心的配對佇列：第 {queue['index'] + 1} / "
            f"{len(queue['items'])} 筆｜案件 {queue_item['case_no']}"
        )
        next_col, exit_col = st.columns(2)
        if next_col.button("下一筆案件", key="matching_queue_next"):
            nav_helper.advance_queue(_MATCHING_QUEUE_KEY)
            st.rerun()
        if exit_col.button("結束配對佇列", key="matching_queue_exit"):
            nav_helper.end_queue()
            st.rerun()
        try:
            orders, staff = _load_matching_center_data()
            render_matching_center(
                orders,
                staff,
                preferred_case_no=str(queue_item["case_no"]),
            )
        except Exception as error:
            st.error(f"月嫂配對中心載入失敗：{error}")
        return

    calendar_tab, matching_tab, staffing_tab = st.tabs(
        ["服務人員月曆", "月嫂配對中心", "案件人力配置"]
    )

    with calendar_tab:
        _render_staff_calendar()

    with matching_tab:
        try:
            orders, staff = _load_matching_center_data()
            render_matching_center(orders, staff)
        except Exception as error:
            st.error(f"月嫂配對中心載入失敗：{error}")

    with staffing_tab:
        render_case_staffing()


if __name__ == "__main__":
    show()
