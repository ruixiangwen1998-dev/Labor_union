"""
================================================================================
檔案名稱: ui/pages/order/editor.py
功能說明: 單筆訂單 38 全欄位動態試算與資料維護組件 (EditOrderUI Core)
================================================================================
"""

import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from calendar import monthrange
import math
import importlib
import uuid
import requests
from ui.pages.shared import build_admin_headers, resolve_api_base_url


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


def safe_optional_date(val):
    """將可為空的資料庫日期轉為 Streamlit 可接受的日期或 None。"""
    if not val:
        return None
    return safe_date(val)


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "date"):
        return value.date()
    if isinstance(value, (str, bytes)):
        try:
            return datetime.strptime(str(value).split(" ")[0].strip(), "%Y-%m-%d").date()
        except:
            return None
    return None


def _month_index(date_value: datetime.date, offset: int) -> datetime.date:
    month_index = date_value.year * 12 + (date_value.month - 1) + offset
    year = month_index // 12
    month = month_index % 12 + 1
    return datetime(year=year, month=month, day=15).date()


def _derive_service_end_date(order: dict) -> datetime.date | None:
    actual_end = _parse_date(order.get("actual_end_date"))
    if actual_end:
        return actual_end

    actual_start = _parse_date(order.get("actual_start_date"))
    service_days = safe_int(order.get("service_days"))
    if not actual_start or not service_days:
        return None
    return actual_start + timedelta(days=max(service_days - 1, 0))


def _derive_staff_payment_date(order: dict) -> str:
    end_date = _derive_service_end_date(order)
    if not end_date:
        return ""

    identity_status = str(order.get("identity_status") or "").strip()
    month_delta = 2 if identity_status == "補助市民" else 1
    return _month_index(end_date, month_delta).isoformat()


def _derive_subsidy_refund_date(order: dict) -> str:
    end_date = _derive_service_end_date(order)
    identity_status = str(order.get("identity_status") or "").strip()
    if not end_date or identity_status == "非市民":
        return ""

    month_end_day = monthrange(end_date.year, end_date.month)[1]
    return (datetime(end_date.year, end_date.month, month_end_day).date() + timedelta(days=5)).isoformat()


def _generate_virtual_account(case_no) -> str:
    if not case_no:
        return ""
    case_no_str = str(case_no).strip()
    if len(case_no_str) == 9 and case_no_str.isdigit():
        year = case_no_str[:3]
        try:
            seq = int(case_no_str[3:])
            return f"99781699{year}{seq:03d}"
        except ValueError:
            pass
    digits = "".join(filter(str.isdigit, case_no_str))
    if len(digits) >= 3:
        year = digits[:3]
        try:
            seq = int(digits[3:]) if len(digits) > 3 else 0
            return f"99781699{year}{seq:03d}"
        except ValueError:
            pass
    return ""


def render_editor(target_case_no, orders_data, payments_raw, key_prefix="v25"):

    """
    可重用的單筆訂單編輯器渲染函式 (EditOrderUI Core)。
    分頁一的手風琴展開面板可以直接內嵌呼叫同一套試算/編輯邏輯。

    參數:
      target_case_no: 欲編輯案件的正式案件編號 (必須已由呼叫端選定，此函式不再提供下拉選單)
      orders_data: db_service.get_order_details() 的完整結果
      payments_raw: 保留相容性的空白帳務資料；舊 payments 已停用
      key_prefix: Streamlit widget key 前綴，避免同頁面內多個展開面板的 key 互相衝突
    """
    assert isinstance(orders_data, list)
    target_order = next((o for o in orders_data if o['case_no'] == target_case_no), None)
    if not target_order:
        st.warning("找不到此訂單資料，可能已被刪除或狀態已變更，請重新整理頁面。")
        return

    st.write("🔒 **公式欄位安全鎖定**")
    is_unlocked = st.checkbox("🔓 強制解鎖自訂衍生公式欄位", value=False, key=f"{key_prefix}_unlock_toggle_{target_case_no}")
    try:
        admin_headers = build_admin_headers()
    except Exception as err:
        st.error(f"未完成管理員授權設定：{err}")
        return

    def api_request(path, *, method="GET", payload=None):
        response = requests.request(
            method,
            f"{resolve_api_base_url()}{path}",
            headers=admin_headers,
            json=payload,
            timeout=15,
        )
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text}
        if not response.ok:
            raise ValueError(f"HTTP {response.status_code}: {body.get('detail') or body.get('message') or body}")
        if not body.get("success", False):
            raise ValueError(body.get("error") or body.get("message") or "同步 API 請求失敗")
        return body.get("data") or {}

    curr_p = {}
    payment_error = None
    try:
        curr_p = api_request(f"/api/v1/client-payments/{target_case_no}")
    except (requests.RequestException, ValueError) as error:
        payment_error = str(error)
        curr_p = next((p for p in payments_raw if p.get('case_no') == target_order.get('case_no')), {})

    if payment_error and not curr_p:
        st.caption(f"帳務資料載入失敗（維持空白帳務欄位）：{payment_error}")

    # 若開啟解鎖，跳出警告 Alert (INV-EDIT-04)
    if is_unlocked:
        st.warning("⚠️ **警告：您已開啟強制解鎖自訂模式！** 手動覆寫總時數、完工日或期款金額後，系統原本的自動試算連動公式將部分失效，請務必確認與客戶合約金額相符後再行儲存。")
    else:
        st.caption("🔒 提示：衍生金額與完工日目前由系統自動連動試算 (唯讀鎖定)。如需特例強制修正，請點選右上角「🔓 強制解鎖」開關。")

    st.markdown("---")

    # =========================================================================
    # 區塊一：📌 案件基本與時程排定 (含預產期與休假方式)
    # =========================================================================
    with st.container(border=True):
        st.markdown(f"### 📌 一、案件基本與時程排定 (案件編號: `{target_case_no}`)")
        
        c1, c2, c3 = st.columns(3)
        with c1:
            w_client_name = st.text_input("客戶名稱", value=target_order['client_name'], key=f"{key_prefix}_client_{target_case_no}")
            w_due_date = st.date_input("預產期", value=safe_date(target_order.get('due_date')), key=f"{key_prefix}_due_{target_case_no}")
            
            client_identity_status = target_order.get('identity_status') or '未設定'
            st.text_input(
                "身分資格（唯讀）",
                value=client_identity_status,
                disabled=True,
                key=f"{key_prefix}_identity_{target_case_no}",
                help="身分資格由客戶主檔管理；訂單編輯不可修改。",
            )
        
        with c2:
            w_staff_name = st.text_input("服務人員", value=target_order.get('staff_name') or '尚未指派', disabled=True, key=f"{key_prefix}_staff_{target_case_no}")
            s_mode_opts = ["週休1日", "週休2日", "連續服務"]
            c_mode = target_order.get('service_mode', '週休1日')
            # 休假方式以客戶申請資料 clients.service_type 為準，僅供本頁計算。
            # 此欄位不屬於 orders，不能顯示成可編輯卻未被儲存的選單。
            w_service_mode = c_mode if c_mode in s_mode_opts else '週休1日'
            st.text_input("休假方式 (客戶申請)", value=w_service_mode, disabled=True, key=f"{key_prefix}_mode_{target_case_no}")
            w_start_date = st.date_input(
                "預期服務開始日",
                value=safe_date(target_order.get('start_date')),
                disabled=True,
                key=f"{key_prefix}_st_{target_case_no}",
            )
        
        with c3:
            w_act_start = st.date_input(
                "服務開始 (實際開工)",
                value=safe_optional_date(target_order.get('actual_start_date')),
                disabled=True,
                key=f"{key_prefix}_act_st_{target_case_no}",
            )
            w_service_days = st.number_input(
                "希望服務天數 (天)",
                value=max(1, safe_int(target_order.get('service_days', 20))),
                min_value=1,
                max_value=60,
                step=1,
                disabled=True,
                key=f"{key_prefix}_days_{target_case_no}",
            )
            
            # 只有已確認實際開始日才計算實際結束日，避免預期日期或今天被寫回。
            calc_act_end = None
            if w_act_start:
                try:
                    resp_calc = requests.post(
                        f"{resolve_api_base_url()}/api/v1/orders/calculate-schedule",
                        headers=admin_headers,
                        json={
                            "actual_start_date": str(w_act_start),
                            "target_service_days": w_service_days,
                            "service_mode": w_service_mode,
                        },
                        timeout=10,
                    )
                    resp_calc.raise_for_status()
                    calc_out = resp_calc.json().get("data") or {}
                    calc_act_end = safe_date(calc_out.get('actual_end_date')) or (w_act_start + timedelta(days=w_service_days-1))
                except Exception:
                    calc_act_end = w_act_start + timedelta(days=w_service_days-1)

            
            if not is_unlocked:
                end_text = calc_act_end.strftime('%Y-%m-%d') if calc_act_end else '尚未設定實際服務開始日'
                st.markdown(f"• ⚡ **服務結束 (🔒 自動精算)**: <b style='color:#2E7D32;'>{end_text}</b>", unsafe_allow_html=True)
                w_act_end = calc_act_end
            else:
                w_act_end = st.date_input(
                    "服務結束",
                    value=safe_optional_date(target_order.get('actual_end_date')) or calc_act_end,
                    disabled=True,
                    key=f"{key_prefix}_act_end_custom_{target_case_no}",
                )
    # =========================================================================
    # 區塊二：⏱️ 服務時數與請款天數統計區
    # =========================================================================
    with st.container(border=True):
        st.markdown("### ⏱️ 二、服務時數與請款天數統計區")
        
        hc1, hc2, hc3 = st.columns(3)
        with hc1:
            w_hours_per_day = st.number_input(
                "服務時段 (小時/天)",
                value=max(1, safe_int(target_order.get('service_hours_per_day', 9))),
                min_value=1,
                max_value=24,
                step=1,
                disabled=True,
                key=f"{key_prefix}_hrs_{target_case_no}",
            )
            calc_total_hours = w_service_days * w_hours_per_day
            display_total_h = calc_total_hours if not is_unlocked else safe_int(target_order.get('total_hours', calc_total_hours))
            w_total_hours = st.number_input("總時數 (小時)", value=display_total_h, disabled=not is_unlocked, key=f"{key_prefix}_total_h_{target_case_no}_{display_total_h}")
        
        with hc2:
            default_sub_hrs = 40 if client_identity_status == '一般市民' else 0
            w_subsidy_hours = st.number_input("補助時數 (小時)", value=safe_int(target_order.get('subsidy_hours', default_sub_hrs)), min_value=0, step=1, key=f"{key_prefix}_sub_h_{target_case_no}")
            calc_self_pay_hours = max(0, w_total_hours - w_subsidy_hours)
            display_self_h = calc_self_pay_hours if not is_unlocked else safe_int(target_order.get('self_pay_hours', calc_self_pay_hours))
            w_self_pay_hours = st.number_input("自費時數 (小時)", value=display_self_h, disabled=not is_unlocked, key=f"{key_prefix}_self_h_{target_case_no}_{display_self_h}")
            
        with hc3:
            w_claim_total_days = st.number_input("請款總日數 (天)", value=max(1, safe_int(target_order.get('claim_total_days', w_service_days))), min_value=1, step=1, key=f"{key_prefix}_claim_d_{target_case_no}")

    # =========================================================================
    # 區塊三：💰 費用與期款拆解試算區 (Formula Lock Guardrail)
    # =========================================================================
    with st.container(border=True):
        st.markdown("### 💰 三、費用與期款拆解試算區")
        
        mc1, mc2, mc3 = st.columns(3)
        with mc1:
            w_floor_fee = st.number_input(
                "樓層費用 (元)",
                value=safe_int(target_order.get('floor_fee', 0)),
                step=100,
                disabled=True,
                key=f"{key_prefix}_fl_{target_case_no}",
            )
            w_employer_rate = st.number_input("雇主單價 (元/天)", value=safe_int(target_order.get('employer_hourly_rate', 2000)), step=100, key=f"{key_prefix}_emp_rate_{target_case_no}")
            
            calc_base_pay = w_service_days * w_employer_rate
            calc_total_self_pay = calc_base_pay + w_floor_fee
            display_total_self = calc_total_self_pay if not is_unlocked else safe_int(target_order.get('total_employer_self_pay_payable', calc_total_self_pay))
            w_total_self_pay = st.number_input("雇主自費合計金額 (元)", value=display_total_self, disabled=not is_unlocked, step=100, key=f"{key_prefix}_total_self_{target_case_no}_{display_total_self}")

        with mc2:
            w_deposit_days = st.number_input("訂金天數", value=max(1, safe_int(target_order.get('deposit_days', 1))), min_value=1, step=1, key=f"{key_prefix}_dep_d_{target_case_no}")
            calc_deposit_amt = w_deposit_days * w_employer_rate
            display_dep_amt = safe_int(curr_p.get("deposit_receivable") if curr_p else calc_deposit_amt)
            w_deposit_amt = st.number_input("訂金 (元)", value=display_dep_amt, disabled=True, step=100, key=f"{key_prefix}_dep_amt_{target_case_no}_{display_dep_amt}")
            w_dep_due_date = st.date_input(
                "訂金應收日期",
                value=safe_optional_date(curr_p.get('deposit_due_date') or target_order.get('deposit_date')),
                disabled=True,
                key=f"{key_prefix}_dep_due_date_{target_case_no}",
                help="公會人員手動填寫；未填時維持空白。",
            )

        with mc3:
            half_days = safe_int(w_service_days / 2)
            w_first_pay_days = st.number_input("第一期款天數", value=safe_int(target_order.get('first_payment_days', half_days)), step=1, key=f"{key_prefix}_p1_days_{target_case_no}")
            calc_first_pay_amt = w_first_pay_days * w_employer_rate
            display_first_pay = safe_int(curr_p.get("first_payment_receivable") if curr_p else calc_first_pay_amt)
            w_first_pay_amt = st.number_input("第一期金額 (元)", value=display_first_pay, disabled=True, step=100, key=f"{key_prefix}_p1_amt_{target_case_no}_{display_first_pay}")
            w_first_pay_due_date = st.date_input(
                "第一期款應收日期",
                value=safe_optional_date(curr_p.get('first_payment_due_date') or target_order.get('first_payment_date')),
                disabled=True,
                key=f"{key_prefix}_p1_due_date_{target_case_no}",
            )

        st.markdown("---")
        m2_c1, m2_c2 = st.columns(2)
        with m2_c1:
            w_second_pay_days = st.number_input("第二期款天數", value=safe_int(target_order.get('second_payment_days', w_service_days - w_first_pay_days)), step=1, key=f"{key_prefix}_p2_days_{target_case_no}")
            calc_second_pay_amt = w_total_self_pay - (w_deposit_amt + w_floor_fee) - w_first_pay_amt
            display_second_pay = safe_int(curr_p.get("second_payment_receivable") if curr_p else calc_second_pay_amt)
            w_second_pay_amt = st.number_input("第二期金額 (元)", value=display_second_pay, disabled=True, step=100, key=f"{key_prefix}_p2_amt_{target_case_no}_{display_second_pay}")
        with m2_c2:
            w_second_pay_due_date = st.date_input(
                "第二期款應收日期",
                value=safe_optional_date(curr_p.get('second_payment_due_date') or target_order.get('second_payment_date')),
                disabled=True,
                key=f"{key_prefix}_p2_due_date_{target_case_no}",
            )

    # =========================================================================
    # 區塊四：💵 服務人員薪資與市府請款區
    # =========================================================================
    with st.container(border=True):
        st.markdown("### 💵 四、服務人員薪資與市府請款區")
        
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            w_caregiver_rate = st.number_input("服務單價 (元/天)", value=safe_int(target_order.get('caregiver_rate', 2000)), step=100, key=f"{key_prefix}_care_rate_{target_case_no}")
            w_salary_1_date = st.date_input("預計發薪日", value=safe_date(target_order.get('salary_payment_date_1')), key=f"{key_prefix}_p1_pay_date_{target_case_no}")
        with sc2:
            st.number_input("服務薪資 (元)", value=safe_int(target_order.get('service_salary')), disabled=True, step=100, key=f"{key_prefix}_service_salary_{target_case_no}")
            calc_sub_salary = safe_int(round((w_subsidy_hours / max(1, w_hours_per_day)) * w_caregiver_rate))
            display_sub_salary = calc_sub_salary if not is_unlocked else safe_int(target_order.get('subsidy_salary', calc_sub_salary))
            w_subsidy_salary = st.number_input("補助薪資 (元)", value=display_sub_salary, disabled=not is_unlocked, step=100, key=f"{key_prefix}_sub_sal_{target_case_no}_{display_sub_salary}")
        with sc3:
            w_govt_claim = st.date_input("市府請款 (請款送件日)", value=safe_date(target_order.get('govt_claim_date')), key=f"{key_prefix}_govt_date_{target_case_no}")

    # =========================================================================
    # 區塊五：📝 實收對帳、狀態與備註登錄區
    # =========================================================================
    with st.container(border=True):
        st.markdown("### 📝 五、實收對帳、狀態與備註登錄區")
        
        # ponytail: Show the 14-digit virtual account corresponding to the current case
        va_val = _generate_virtual_account(target_order.get('case_no'))

        if va_val:
            st.markdown(f"**🔗 專屬虛擬帳號**: `{va_val}`")

        rc1, rc2 = st.columns(2)
        with rc1:
            st.text_input(
                "服務人員付款日（衍生公式）",
                value=_derive_staff_payment_date(target_order),
                disabled=True,
                key=f"{key_prefix}_staff_payment_date_{target_case_no}",
                help="依服務結束日與身分資格自動推估，僅供參考，不可編輯。",
            )
            st.text_input(
                "補助退款日（衍生公式）",
                value=_derive_subsidy_refund_date(target_order),
                disabled=True,
                key=f"{key_prefix}_subsidy_refund_date_{target_case_no}",
                help="依服務結束日與身分資格自動推估，僅供參考，不可編輯。",
            )
            w_dep_rec = st.number_input("已收訂金 (元)", value=safe_int(curr_p.get('deposit_received')), disabled=True, step=100, key=f"{key_prefix}_dep_rec_{target_case_no}")
            w_dep_rec_date = st.date_input("訂金實收日期", value=safe_optional_date(curr_p.get('deposit_received_at')), disabled=True, key=f"{key_prefix}_dep_rec_date_{target_case_no}")
            w_p1_rec = st.number_input("已收第一期款 (元)", value=safe_int(curr_p.get('first_payment_received')), disabled=True, step=100, key=f"{key_prefix}_p1_rec_{target_case_no}")
            w_p1_rec_date = st.date_input("第一期款收取日期", value=safe_optional_date(curr_p.get('first_payment_received_at')), disabled=True, key=f"{key_prefix}_p1_rec_date_{target_case_no}")
            w_p2_rec = st.number_input("已收第二期款 (元)", value=safe_int(curr_p.get('second_payment_received')), disabled=True, step=100, key=f"{key_prefix}_p2_rec_{target_case_no}")
            w_p2_rec_date = st.date_input("第二期款收取日期", value=safe_optional_date(curr_p.get('second_payment_received_at')), disabled=True, key=f"{key_prefix}_p2_rec_date_{target_case_no}")
            
            status_list = ["洽談中", "訂單成立", "服務中", "訂單完成", "訂單取消"]
            c_status = target_order['order_status']
            st_idx = status_list.index(c_status) if c_status in status_list else 0
            w_order_status = st.selectbox("訂單成立狀態", status_list, index=st_idx, key=f"{key_prefix}_status_{target_case_no}")
        
        with rc2:
            stage_receivable_total = w_deposit_amt + w_floor_fee + w_first_pay_amt + w_second_pay_amt
            stage_received_total = w_dep_rec + w_p1_rec + w_p2_rec
            st.metric("應收總額", f"{stage_receivable_total:,.0f} 元")
            st.metric("實收總額", f"{stage_received_total:,.0f} 元")
            w_notes = st.text_area("備註 (注意事項/備忘)", value=target_order.get('notes') or "", key=f"{key_prefix}_notes_{target_case_no}")
            w_cancel_reason = ""
            if w_order_status == "訂單取消":
                w_cancel_reason = st.text_area("取消原因 (選取訂單取消時強制填寫)", value=target_order.get('cancel_reason') or "", key=f"{key_prefix}_cancel_rea_{target_case_no}")

    st.markdown("---")
    st.markdown("### 訂單狀態")

    status_changed = w_order_status != target_order["order_status"]
    if status_changed:
        st.warning("訂單狀態已變更，請先儲存狀態後再編輯其他基本資料。")
        cancel_actor = ""
        cancel_event_key_key = f"{key_prefix}_cancel_event_key_{target_case_no}"
        cancel_event_key = None
        if w_order_status == "訂單取消":
            cancel_actor = st.text_input(
                "取消操作識別（人員）",
                key=f"{key_prefix}_cancel_actor_{target_case_no}",
                help="取消操作必須留存可追溯的人員識別。",
            )
            cancel_event_key = st.session_state.get(cancel_event_key_key)
            if not isinstance(cancel_event_key, str) or not cancel_event_key.strip():
                cancel_event_key = f"cancel-{target_case_no}-{uuid.uuid4().hex[:12]}"
                st.session_state[cancel_event_key_key] = cancel_event_key
            st.text_input(
                "取消事件冪等鍵（自動產生）",
                value=cancel_event_key,
                disabled=True,
                key=f"{cancel_event_key_key}_display_{target_case_no}",
            )
        if st.button("更新訂單狀態", key=f"{key_prefix}_update_status_{target_case_no}"):
            if w_order_status == "訂單取消" and not w_cancel_reason.strip():
                st.error("訂單取消必須輸入取消原因。")
            elif w_order_status == "訂單取消" and not cancel_actor.strip():
                st.error("取消操作必須輸入操作識別。")
            else:
                try:
                    if w_order_status == "訂單取消":
                        api_request(
                            f"/api/v1/orders/{target_case_no}/cancel",
                            method="POST",
                            payload={
                                "event_key": cancel_event_key,
                                "actor": cancel_actor.strip(),
                                "cancel_reason": w_cancel_reason.strip(),
                            },
                        )
                        st.session_state.pop(cancel_event_key_key, None)
                    else:
                        api_request(
                            f"/api/v1/orders/{target_case_no}/status",
                            method="PUT",
                            payload={"status": w_order_status, "cancel_reason": w_cancel_reason.strip() or None},
                        )
                    st.success("訂單狀態更新完成，畫面將重新載入。")
                    st.rerun()
                except (requests.RequestException, ValueError) as error:
                    st.error(f"狀態更新失敗：{error}")
        return
    # 正式月嫂、assignment、服務區段與排班調整集中在「多月嫂排班」
    # 的「案件人力配置」分頁；編輯訂單不保留第二套入口。
    if st.button(
        "儲存訂單基本資料",
        type="primary",
        key=f"{key_prefix}_save_order_details_{target_case_no}",
    ):
        try:
            api_request(
                f"/api/v1/orders/{target_case_no}/full-details",
                method="PUT",
                payload={
                    "client_name": w_client_name.strip() or None,
                },
            )
            st.success("訂單基本資料已儲存；正式人力與排班未變更。")
            st.rerun()
        except (requests.RequestException, ValueError) as error:
            st.error(f"訂單基本資料儲存失敗：{error}")
