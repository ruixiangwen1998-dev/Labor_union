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
5. 導覽約束: 本檔案末尾嚴禁包含頂層 show() 呼叫，由 ui/app.py 動態載入。
================================================================================
"""

import streamlit as st
import pandas as pd
import json
from datetime import datetime, timedelta
import math
import re
import calendar
import requests

from ui.pages.shared import build_admin_headers, resolve_api_base_url

title = "📅 服務人員行事曆與休假安排"

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
        return datetime.today().date()
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


def _parse_stored_rest_dates(raw_custom_json):
    if not raw_custom_json:
        return set(), None

    try:
        persisted_list = json.loads(raw_custom_json) if isinstance(raw_custom_json, str) else raw_custom_json
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


def _render_multi_caregiver_panel(all_orders):
    """Render explicit case -> assignment selection and assignment-owned days."""
    st.markdown("---")
    st.markdown("### 👩‍🍼 多月嫂指派排班")
    st.caption("先選案件，再選正式服務指派。此處不支援同日分時段，也不提供人工輸入 actual_hours。")

    case_options = {
        f"{order['case_no']}｜{order.get('client_name', '')}｜{order.get('order_status', '')}": order["case_no"]
        for order in all_orders
        if order.get("case_no")
    }
    if not case_options:
        st.info("目前沒有可選案件。")
        return

    selected_case_label = st.selectbox(
        "1. 選擇案件",
        list(case_options),
        key="multi_caregiver_case_no",
    )
    case_no = case_options[selected_case_label]
    try:
        assignments = _multi_caregiver_request(
            f"/api/v1/cases/{case_no}/assignment-schedules"
        ).get("assignments", [])
    except (requests.RequestException, ValueError) as error:
        st.error(f"無法讀取案件的正式服務指派：{_multi_caregiver_error(error)}")
        return

    if not assignments:
        st.info("此案件目前沒有未取消的正式服務指派。")
        return

    assignment_options = {
        f"#{item['id']}｜{item.get('staff_name', '')}｜{item.get('assigned_start_date')} ～ {item.get('assigned_end_date')}": item["id"]
        for item in assignments
    }
    selected_assignment_label = st.selectbox(
        "2. 選擇月嫂服務區段",
        list(assignment_options),
        key=f"multi_caregiver_assignment_{case_no}",
    )
    assignment_id = assignment_options[selected_assignment_label]
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
        return

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
    st.caption("請假只影響這一筆正式指派；如需延長服務區段，請明確調整指派日期，系統不會自動跨入下一位月嫂區段。")

    day_options = {str(item["work_date"]): item for item in schedule_days}
    with st.form(f"multi_caregiver_day_adjustment_{assignment_id}"):
        selected_work_date = st.selectbox("調整日期", list(day_options))
        selected_day = day_options[selected_work_date]
        is_work_day = st.checkbox("計入服務時數", value=bool(selected_day.get("is_work_day")))
        is_double_pay = st.checkbox("雙倍薪服務日", value=bool(selected_day.get("is_double_pay")))
        notes = st.text_input("行政備註", value=selected_day.get("notes") or "", max_chars=255)
        submitted = st.form_submit_button("儲存此日調整")
    if submitted:
        try:
            _multi_caregiver_request(
                f"/api/v1/assignment-schedules/{assignment_id}/days/{selected_work_date}",
                method="PUT",
                payload={
                    "is_work_day": is_work_day,
                    "is_double_pay": is_double_pay,
                    "notes": notes or None,
                },
            )
            st.success("已儲存指派內單日調整。")
            st.rerun()
        except (requests.RequestException, ValueError) as error:
            st.error(f"無法儲存指派日調整：{_multi_caregiver_error(error)}")

def show():
    """服務人員行事曆與檔期調控獨立頁面入口 (CalendarUI)"""
    st.title("📅 服務人員行事曆與檔期調控")
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
        
        staff_col, year_col, month_col = st.columns(3)
        with staff_col:
            selected_staff_label = st.selectbox(
                "選擇要查看的服務人員/月嫂",
                list(staff_options.keys()),
                key="cal_staff_main",
            )
            cal_staff_id = staff_options[selected_staff_label]
        with year_col:
            cal_year = st.selectbox("選擇年份", [2025, 2026, 2027], index=1)
        with month_col:
            curr_month = datetime.today().month
            cal_month = st.selectbox("選擇月份", list(range(1, 13)), index=curr_month - 1)
            
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
            monthly_schedules = sched_data.get("schedule_map") or {}
        except Exception as err_sched:
            st.warning(f"⚠️ 月度排班資料 API 讀取失敗: {err_sched}")
            monthly_schedules = {}

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
            if h_date.year == cal_year and h_date.month == cal_month:
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


        _render_multi_caregiver_panel(all_orders)

        
        calc_res = None
        target_order = None
        preview_days_set = set()
        buffer_days_set = set()
        
        green_days_set = set()      # 🟢 綠底休假日期集合
        calc_red_days_set = set()   # 🔴 算術推進後的紅底工作日集合
        
        col_op1, col_op2 = st.columns([1, 2])
        with col_op1:
            action_mode = st.radio(
                "1. 執行操作",
                ["不連動，單純看行事曆", "訂單匹配", "出勤天數精算"],
                index=0
            )
            
        with col_op2:
            # 根據 1. 執行操作 動態過濾符合條件的訂單
            if action_mode == "訂單匹配":
                # 篩選洽談中且無硬衝突的案件
                filtered_orders = []
                for o in all_orders:
                    if o.get('staff_id') == cal_staff_id and o.get('order_status') == '洽談中':
                        st_d_check = safe_date(o['actual_start_date']) or safe_date(o['start_date'])
                        days_cnt_check = o['service_days'] or 20
                        ed_d_check = st_d_check + timedelta(days=days_cnt_check - 1) if st_d_check else None
                        
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
                        if not has_conflict:
                            filtered_orders.append(o)
            elif action_mode == "出勤天數精算":
                # 篩選訂單成立/服務中且確定 actual_start_date 的案件
                filtered_orders = [
                    o for o in all_orders 
                    if o.get('staff_id') == cal_staff_id and bool(o.get('actual_start_date'))
                ]
            else:
                filtered_orders = []
                
            order_menu_opts = {"無 (單純查看行事曆)": None}
            for o in filtered_orders:
                st_d_tmp = safe_date(o['actual_start_date']) or safe_date(o['start_date'])
                days_cnt_tmp = o['service_days'] or 20
                ed_d_tmp = st_d_tmp + timedelta(days=days_cnt_tmp - 1) if st_d_tmp else None
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
                
                potential_dates = [safe_date(d_item['date']) for d_item in init_calc.get('day_by_day', [])]
                date_labels_map = {f"{d.strftime('%Y-%m-%d')} ({['週一','週二','週三','週四','週五','週六','週日'][d.weekday()]})": d for d in potential_dates}
                
                persisted_rest_dates = set()
                raw_custom_json = target_order.get('custom_rest_dates')
                if raw_custom_json:
                    persisted_rest_dates, parse_error = _parse_stored_rest_dates(raw_custom_json)
                    if parse_error:
                        st.warning(parse_error)

                default_rest_dates = [
                    label
                    for label, d in date_labels_map.items()
                    if any(
                        safe_date(d_item['date']) == d and d_item['is_rest_day']
                        for d_item in init_calc.get('day_by_day', [])
                    )
                    or d in persisted_rest_dates
                ]
                
                selected_rest_labels = st.multiselect(
                    "🗓️ 行事曆單日排休調整 (🟢 綠底休假，可跨週自訂點選)",
                    list(date_labels_map.keys()),
                    default=default_rest_dates,
                    key="rest_dates_ms_page"
                )
                custom_leave_dates = {date_labels_map[k] for k in selected_rest_labels}
                
                all_rest_dt_list = [d.strftime("%Y-%m-%d") for d in (custom_leave_dates | custom_holiday_rest_dates)]
                if st.button("💾 儲存放假與動態順延 (寫入資料庫)", type="primary", key="save_rest_dates_btn"):
                    if not calc_assignment_id:
                        st.error("無法判定該案件的正式服務指派，請先完成正式媒合後再保存排休。")
                        return
                    try:
                        resp_save = requests.put(
                            f"{resolve_api_base_url()}/api/v1/assignment-schedules/{calc_assignment_id}/rest-dates",
                            headers=admin_headers,
                            json={"rest_dates": all_rest_dt_list},
                            timeout=10,
                        )
                        resp_save.raise_for_status()

                        save_res = resp_save.json().get("data") or {}
                        if save_res.get('success'):
                            st.success(f"✅ {save_res.get('message', '成功更新休假排休日期')}")
                            st.balloons()
                            st.rerun()
                        else:
                            st.error(f"❌ {save_res.get('message', '儲存放假失敗')}")
                    except Exception as err:
                        st.error(f"❌ 儲存放假失敗: {err}")
                
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
                    if item_date.year == cal_year and item_date.month == cal_month:
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
                                status_label = "<span class='status-label-yellow'>🟡 已簽約</span>"
                                client_text = f"<span class='client-text'><b>客戶: {day_info['client_name']}</b></span>"
                        elif day_info['status'] == 'green':
                            if not is_target_order_record:
                                bg_class = "status-green"
                                status_label = "<span class='status-label-green'>🟢 排定休假</span>"
                                client_text = f"<span class='client-text'><b>休假: {day_info['client_name']}</b></span>"
                        elif day_info['status'] == 'red':
                            if not is_target_order_record:
                                bg_class = "status-red"
                                status_label = "<span class='status-label-red'>🔴 正在服務中</span>"
                                client_text = f"<span class='client-text'><b>客戶: {day_info['client_name']}</b></span>"
                    
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
