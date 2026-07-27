import re
import requests
import streamlit as st
import pandas as pd
from ui.pages import shared

title = "🔍 資料庫原始資料瀏覽"


# 可編輯欄位白名單 (僅本頁面即時編輯表格適用)：只有白名單內的欄位開放編輯，
# 其餘（含未來新增欄位）一律鎖定唯讀。對照依據:
# document/管理端UI/資料庫原始資料瀏覽_頁面欄位開放權限建議表.xlsx
EDITABLE_COLUMNS = {
    'clients': {
        'reject_reason', 'ip_address', 'name', 'gender', 'phone', 'city', 'address',
        'service_time', 'due_month', 'service_start_date', 'notes',
        'service_days', 'residence_type', 'delivery_type', 'service_type', 'baby_info',
        'line_id', 'admin_notes',
    },
    'staff': {
        'registered_at', 'ip_address', 'phone', 'tel', 'tel_ext', 'email', 'city',
        'zip_code', 'address', 'has_massage_cert', 'weekly_rest_days', 'service_regions',
        'special_skills', 'name', 'identity_card', 'birthday', 'care_babies',
    },
    'orders': {
        'line_group_id', 'contract_id',
    },
    'beclass_records': {
        'seq_num', 'email', 'tel', 'ext', 'city', 'zip_code', 'address',
        'refund_bank_code', 'refund_account_no', 'admin_notes',
    },
    # 全表建議唯讀：須透過「案件與配對中心」(02_orders.py) 的專屬按鈕操作
    'matching_records': set(),
    # 全表建議唯讀：已有專屬「國定假日管理面板」處理新增/更新/刪除
    'holidays': set(),
    'line_confirmation_requests': set(),
    'staff_bookings': set(),
    'staff_regions': set(),
    'staff_cooking_skills': set(),
    'staff_weekly_rest': set(),
    'staff_time_slots': set(),
    'staff_transportation': set(),
    'staff_holiday_availability': set(),
    'staff_baby_types': set(),
    'staff_bank_accounts': {
        'bank_code', 'branch_code', 'account_no', 'is_primary',
    },
}

# 限制輸入選項的欄位：改用下拉選單，不能自由輸入文字
COLUMN_VALID_OPTIONS = {
    'clients': {
        'gender': ['男', '女'],
        'delivery_type': ['自然產', '剖腹產'],
        'service_type': ['週休2日', '週休1日', '連續服務'],
    },
}

# 格式檢核欄位 (建議表「說明備註」標註「檢核OO格式」者)：空值視為清空允許通過
_PHONE_FORMAT = (re.compile(r'^09\d{8}$'), '請輸入正確的行動電話格式 (例如 0912345678)')
_EMAIL_FORMAT = (re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$'), '請輸入正確的 Email 格式')
COLUMN_FORMAT_VALIDATORS = {
    'clients': {'phone': _PHONE_FORMAT},
    'staff': {'email': _EMAIL_FORMAT},
}

READ_ONLY_TABLES = {
    "staff_regions",
    "staff_cooking_skills",
    "staff_weekly_rest",
    "staff_time_slots",
    "staff_transportation",
    "staff_holiday_availability",
    "staff_baby_types",
    "line_confirmation_requests",
    "staff_bookings",
    "case_staff_assignments",
    "client_payments",
    "client_payment_transactions",
    "actual_hours_adjustments",
    "staff_payments",
    "staff_payment_transactions",
    "payment_migration_reviews",
    "staff_schedule",
}

# ADAD INV-UI-BROWSER-01: 資料庫全量欄位中文對照映射表
DB_COLUMN_LABEL_MAP = {
    # 通用/基礎欄位
    "id": "資料ID",
    "seq_num": "項次",
    "status": "狀態",
    "notes": "備註",
    "created_at": "報名/建檔時間",
    "updated_at": "最後更新時間",
    "db_created_at": "DB匯入時間",
    "db_updated_at": "DB更新時間",
    "admin_notes": "管理者註記",
    "ip_address": "IP位址",
    
    # 客戶 (clients)
    "case_no": "查詢序號(案件編號)",
    "reject_reason": "不符合原因",
    "name": "姓名",
    "gender": "性別",
    "phone": "行動電話",
    "tel": "市話",
    "city": "縣市",
    "address": "地址",
    "zip_code": "郵遞區號",
    "identity_status": "身分資格",
    "service_time": "服務時間",
    "due_month": "預產期月份",
    "service_start_date": "預計服務日期",
    "service_days": "希望服務天數",
    "residence_type": "居住型態",
    "delivery_type": "生產方式",
    "service_type": "服務方式",
    "baby_info": "寶寶資訊",
    "line_id": "LINE ID",
    "line_user_id": "LINE用戶ID",
    "email": "Email",
    "birth_date": "生日",
    
    # 服務人員 (staff)
    "registered_at": "報名時間",
    "identity_card": "身分證字號",
    "tel_ext": "分機",
    "birthday": "生日",
    "has_massage_cert": "嬰幼兒按摩證書",
    "weekly_rest_days": "固定休假偏好",
    "care_babies": "最大照顧寶寶數",
    "service_regions": "服務區域偏好",
    "special_skills": "特殊技能與標籤",
    "service_time_slots": "服務時段偏好",
    "transportation_preferences": "交通工具偏好",
    "holiday_preferences": "節日偏好",
    "baby_type_preferences": "可承接胎數",
    "bank_accounts": "服務人員銀行帳戶",
    
    # 服務人員銀行帳戶 (staff_bank_accounts)
    "bank_code": "銀行代碼(3碼)",
    "branch_code": "分行代碼(4碼)",
    "account_no": "銀行帳號",
    "is_primary": "是否為主要帳戶",

    # 服務人員關聯資料表
    "staff_id": "服務人員ID",
    "region_name": "服務區域",
    "custom_region_detail": "區域其他說明",
    "skill_name": "特殊技能/料理",
    "custom_skill_detail": "特殊技能其他說明",
    "rest_type": "固定休假類型",
    "custom_rest_detail": "固定休假其他說明",
    "slot_name": "服務時段",
    "custom_slot_detail": "時段其他說明",
    "vehicle_type": "交通工具",
    "holiday_name": "節日偏好",
    "custom_holiday_detail": "節日其他說明",
    "baby_type": "可承接胎數",
    "custom_baby_detail": "胎數其他說明",
    
    # 訂單 (orders)
    "client_id": "客戶ID",
    "staff_id": "服務人員ID",
    "cancel_reason": "取消原因",
    "line_group_id": "LINE群組ID",
    "actual_start_date": "實際服務開始日",
    "actual_end_date": "實際服務結束日",
    "contract_id": "線上契約ID",
    "service_hours_per_day": "每日服務時數",
    "floor_fee": "樓層費用",
    "deposit_date": "訂金收取日期",
    "start_date": "預計開始日",
    "end_date": "預計結束日",
    "custom_rest_dates": "自訂休假日期",
    "other_addition": "其他加價",
    "staff_name": "服務人員姓名",

    # 客戶帳務主檔與交易
    "legacy_payment_id": "舊系統付款ID",
    "client_payment_id": "客戶帳務主檔ID",
    "deposit_receivable": "訂金應收",
    "deposit_received": "訂金已收",
    "deposit_due_date": "訂金到期日",
    "deposit_received_at": "訂金全額核銷日",
    "first_payment_receivable": "第一期應收",
    "first_payment_received": "第一期已收",
    "first_payment_due_date": "第一期到期日",
    "first_payment_received_at": "第一期全額核銷日",
    "second_payment_receivable": "第二期應收",
    "second_payment_received": "第二期已收",
    "second_payment_due_date": "第二期到期日",
    "second_payment_received_at": "第二期全額核銷日",
    "amount_receivable": "應收總額",
    "amount_received": "實收總額",
    "subsidy_refund_receivable": "補助退還應收",
    "subsidy_refund_refunded": "補助退還已收",
    "subsidy_refund_due_date": "補助退還到期日",
    "subsidy_refund_at": "補助退還完成日",
    "subsidy_return_receivable": "補助返還應收",
    "subsidy_return_refunded": "補助返還已退",
    "subsidy_return_due_date": "補助返還到期日",
    "subsidy_return_at": "補助返還完成日",
    "subsidy_return_review_status": "補助返還人工覆核狀態",
    "subsidy_return_review_reason": "補助返還覆核原因",
    "stage": "款項階段",
    "transaction_type": "交易類型",
    "transaction_status": "交易狀態",
    "amount": "金額",
    "occurred_at": "發生日",
    "external_reference": "外部參考編號",
    "reversal_of_transaction_id": "沖正來源交易ID",

    # 案件月嫂服務指派
    "assignment_sequence": "指派順序",
    "assigned_start_date": "指派開始日",
    "assigned_end_date": "指派結束日",
    "planned_hours": "預計服務時數",
    "actual_hours": "實際服務時數",
    "floor_fee_allocated": "分攤樓層費",
    "replacement_reason": "更換原因",
    "replaced_assignment_id": "原指派ID",

    # 實際工時調整
    "previous_actual_hours": "異動前實際時數",
    "adjusted_actual_hours": "異動後實際時數",
    "adjustment_reason": "調整原因",
    "adjusted_by": "異動人員",
    "adjusted_at": "異動時間",

    # 月嫂付款主檔與交易
    "assignment_id": "服務指派ID",
    "service_hours": "服務時數",
    "service_salary": "服務總薪",
    "floor_fee_amount": "樓層費",
    "adjustment_amount": "人工調整金額",
    "total_payable": "應付總額",
    "due_date": "應付日期",
    "staff_payment_id": "月嫂付款ID",

    # 補助款待覆核
    "legacy_caregiver_fee": "歷史月嫂應付金額",
    "legacy_caregiver_paid_at": "歷史付款完成日",
    "review_status": "補助款覆核狀態",
    "resolved_at": "覆核完成時間",
    "resolution_notes": "覆核結果備註",

    # 排班明細
    "work_date": "服務日期",
    "is_work_day": "是否工作日",
    "is_double_pay": "是否雙倍薪資",
    

    
    # BeClass 報名記錄 (beclass_records)
    "query_no": "查詢序號",
    "ext": "分機",
    "refund_bank_code": "補助款退款:銀行代號+分行代號",
    "refund_account_no": "補助款退款:銀行帳號",
    "survey_details": "問卷詳細內容JSON",
    
    # 媒合記錄 (matching_records)
    "caregiver_accepted": "月嫂接受意願",
    "sent_at": "詢問發送時間",
    "replied_at": "月嫂回覆時間",
    "sent_info_1_at": "發送訂單資訊-1時間",
    "sent_info_2_at": "發送訂單資訊-2時間",
    
    # 國定假日 (holidays)
    "holiday_date": "假日日期",
    "holiday_name": "假日名稱",
    "is_double_pay_default": "預設雙倍薪資"
}

def format_col_header(col_name: str, mode: str) -> str:
    """ponytail: map column to friendly chinese label with original fallback"""
    zh_label = DB_COLUMN_LABEL_MAP.get(col_name)
    if not zh_label:
        return col_name
    if mode == "中文標籤 (含英文鍵名)":
        return f"{zh_label} ({col_name})"
    elif mode == "純中文標籤":
        return zh_label
    else:  # "原始英文鍵名"
        return col_name


def _admin_headers():
    return shared.build_admin_headers()


def _resolve_api_base_url() -> str:
    return shared.resolve_api_base_url()

def show():
    st.title("🔍 資料庫原始資料瀏覽")
    st.write("本頁面用於瀏覽系統中各資料表的原始狀態，已支援友善中文欄位顯示對照。")
    try:
        admin_headers = _admin_headers()
    except Exception as err:
        st.error(f"未完成管理員授權設定：{err}")
        return
    
    # 選擇要瀏覽的資料表
    table_options = {
        "服務人員/月嫂名冊 (staff)": "staff",
        "客戶名冊 (clients)": "clients",
        "LINE 確認請求紀錄 (line_confirmation_requests)": "line_confirmation_requests",
        "服務人員預約 (staff_bookings)": "staff_bookings",
        "個案服務人員指派 (case_staff_assignments)": "case_staff_assignments",
        "客戶帳務主檔 (client_payments)": "client_payments",
        "客戶帳務交易明細 (client_payment_transactions)": "client_payment_transactions",
        "實際工時調整 (actual_hours_adjustments)": "actual_hours_adjustments",
        "月嫂付款主檔 (staff_payments)": "staff_payments",
        "月嫂付款交易明細 (staff_payment_transactions)": "staff_payment_transactions",
        "補助款審核紀錄 (payment_migration_reviews)": "payment_migration_reviews",
        "服務排班明細 (staff_schedule)": "staff_schedule",
        "訂單資料 (orders)": "orders",
        "客戶BeClass表單 (beclass_records)": "beclass_records",
        "媒合意願記錄 (matching_records)": "matching_records",
        "國定假日設定 (holidays)": "holidays",
        "服務人員銀行帳戶 (staff_bank_accounts)": "staff_bank_accounts"
    }
    
    col_sel1, col_sel2 = st.columns([2, 1])
    with col_sel1:
        default_table_label = "服務人員/月嫂名冊 (staff)"
        selected_label = st.selectbox(
            "選擇要瀏覽的資料表",
            list(table_options.keys()),
            index=list(table_options.keys()).index(default_table_label)
        )
        table_name = table_options[selected_label]
    with col_sel2:
        header_mode = st.selectbox(
            "欄位顯示模式",
            ["中文標籤 (含英文鍵名)", "純中文標籤", "原始英文鍵名"],
            index=0
        )
    
    # 方案 C：如果選擇國定假日，提供新增/更新/刪除的互動管理功能
    if table_name == "holidays":
        st.markdown("### 📅 國定假日管理面板 (方案 A+C)")
        col_add, col_del = st.columns(2)
        
        with col_add:
            st.write("➕ 新增 / 更新假日")
            h_date = st.date_input("假日日期", key="h_date")
            h_name = st.text_input("假日名稱", placeholder="例如: 中秋節", key="h_name")
            h_double = st.checkbox("預設雙倍薪資", value=True, key="h_double")
            if st.button("確認儲存假日"):
                if not h_name.strip():
                    st.error("請輸入假日名稱")
                else:
                    try:
                        resp_save = requests.post(
                            f"{_resolve_api_base_url()}/api/v1/holidays",
                            headers=admin_headers,
                            json={"holiday_date": str(h_date), "holiday_name": h_name.strip(), "is_double_pay": h_double},
                            timeout=10,
                        )
                        resp_save.raise_for_status()
                        st.success(f"成功儲存假日: {h_name} ({h_date})")
                        st.rerun()
                    except Exception as err:
                        st.error(f"儲存失敗: {err}")
                        
        with col_del:
            st.write("❌ 刪除假日")
            try:
                resp_h_list = requests.get(
                    f"{_resolve_api_base_url()}/api/v1/holidays",
                    headers=admin_headers,
                    timeout=10,
                )
                resp_h_list.raise_for_status()
                current_holidays = resp_h_list.json().get("data") or []
                if not current_holidays:
                    st.info("目前無國定假日可刪除。")
                else:
                    del_options = {f"{h['holiday_date']} - {h['holiday_name']}": h['holiday_date'] for h in current_holidays}
                    selected_del = st.selectbox("選擇欲刪除之假日", list(del_options.keys()))
                    del_date = del_options[selected_del]
                    if st.button("確認刪除此假日"):
                        try:
                            resp_del = requests.delete(
                                f"{_resolve_api_base_url()}/api/v1/holidays/{del_date}",
                                headers=admin_headers,
                                timeout=10,
                            )
                            resp_del.raise_for_status()
                            st.success("假日已刪除")
                            st.rerun()
                        except Exception as err:
                            st.error(f"刪除失敗: {err}")
            except Exception as err:
                st.error(f"讀取假日出錯: {err}")
        st.markdown("---")

    try:
        resp_admin = requests.get(
            f"{_resolve_api_base_url()}/api/v1/admin/data-browser/{table_name}",
            headers=admin_headers,
            timeout=15,
        )
        resp_admin.raise_for_status()
        admin_payload = resp_admin.json()
        admin_data = admin_payload.get("data") or {}

        raw_data = admin_data.get("rows", [])
        table_columns = admin_data.get("columns", [])
        pk_col = admin_data.get("primary_key", "id")
        editable_cols = set(admin_data.get("editable_columns") or [])
        is_read_only = admin_data.get("read_only", False)
        valid_options = admin_data.get("valid_options") or {}

        if not raw_data:
            st.info(f"資料表 `{table_name}` 目前沒有任何數據，已為你顯示欄位清單。")
            if not table_columns:
                st.info("目前無法取得欄位資訊。")
                return
            df = pd.DataFrame(columns=table_columns)
        else:
            df = pd.DataFrame(raw_data)

        if df.empty:
            filtered_df = df.copy()

            rename_map = {col: format_col_header(col, header_mode) for col in filtered_df.columns}
            display_df = filtered_df.rename(columns=rename_map)

            column_config = {}
            for original_col, display_col in rename_map.items():
                if original_col in valid_options:
                    column_config[display_col] = st.column_config.SelectboxColumn(
                        options=valid_options[original_col],
                        required=False,
                    )
                elif original_col not in editable_cols:
                    column_config[display_col] = st.column_config.Column(disabled=True)

            st.write(f"共 0 筆資料 (總共 0 筆)")
            st.caption("💡 該表格目前為空，為避免混淆仍展示欄位名稱。")
            st.data_editor(
                display_df,
                width='stretch',
                num_rows="fixed",
                column_config=column_config,
                disabled=[rename_map[pk_col]] if pk_col in rename_map else False,
                key=f"editor_empty_{table_name}",
            )

            if is_read_only:
                st.info("此資料表目前為唯讀保護模式，僅供瀏覽，不開放即時寫入。")
            return

        search_query = st.text_input("🔍 搜尋表格內容", "")
        if search_query:
            mask = df.astype(str).apply(lambda x: x.str.contains(search_query, case=False)).any(axis=1)
            filtered_df = df[mask].copy()
        else:
            filtered_df = df.copy()

        rename_map = {col: format_col_header(col, header_mode) for col in filtered_df.columns}
        display_df = filtered_df.rename(columns=rename_map)
        reverse_rename_map = {v: k for k, v in rename_map.items()}
        format_validators = COLUMN_FORMAT_VALIDATORS.get(table_name, {})

        column_config = {}
        for original_col, display_col in rename_map.items():
            if original_col in valid_options:
                column_config[display_col] = st.column_config.SelectboxColumn(
                    options=valid_options[original_col],
                    required=False,
                )
            elif original_col not in editable_cols:
                column_config[display_col] = st.column_config.Column(disabled=True)

        st.write(f"共 {len(filtered_df)} 筆資料 (總共 {len(df)} 筆)")
        st.caption("💡 可直接在表格中點選儲存格修改內容（灰色欄位為系統/關聯欄位，唯讀鎖定；下拉選單欄位僅能從清單中選擇），修改完成後請務必點擊下方「💾 儲存變更」按鈕才會寫入資料庫。")

        edited_display_df = st.data_editor(
            display_df,
            width='stretch',
            num_rows="fixed",
            column_config=column_config,
            disabled=[rename_map[pk_col]] if pk_col in rename_map else False,
            key=f"editor_{table_name}",
        )

        if is_read_only:
            st.info("此資料表目前為唯讀保護模式，僅供瀏覽，不開放即時寫入。")
        elif st.button("💾 儲存變更", type="primary"):
            edited_df = edited_display_df.rename(columns=reverse_rename_map)
            original_df = filtered_df.set_index(pk_col, drop=False)
            edited_df = edited_df.set_index(pk_col, drop=False)

            updated_rows = 0
            errors = []
            for row_id, edited_row in edited_df.iterrows():
                if row_id not in original_df.index:
                    continue
                original_row = original_df.loc[row_id]
                changed_fields = {}
                for col in edited_df.columns:
                    if col == pk_col or col not in editable_cols:
                        continue
                    old_val = original_row.get(col)
                    new_val = edited_row.get(col)
                    old_str = "" if pd.isna(old_val) else str(old_val)
                    new_str = "" if pd.isna(new_val) else str(new_val)
                    if old_str != new_str:
                        changed_fields[col] = None if pd.isna(new_val) else new_val

                format_err = None
                for col, val in changed_fields.items():
                    if col in format_validators and val:
                        pattern, err_msg = format_validators[col]
                        if not pattern.match(str(val)):
                            format_err = f"第 {row_id} 筆欄位 {col} 格式錯誤: {err_msg}"
                            break

                if format_err:
                    errors.append(format_err)
                elif changed_fields:
                    try:
                        patch_resp = requests.patch(
                            f"{_resolve_api_base_url()}/api/v1/admin/data-browser/{table_name}/{row_id}",
                            headers=admin_headers,
                            json={"updates": changed_fields},
                            timeout=15,
                        )
                        patch_resp.raise_for_status()
                        updated_rows += 1
                    except Exception as row_err:
                        errors.append(f"第 {row_id} 筆更新失敗: {row_err}")

            if errors:
                for err_msg in errors:
                    st.error(err_msg)
            if updated_rows > 0:
                st.success(f"✅ 已成功經由 Admin API 儲存 {updated_rows} 筆變更資料並寫入稽核日誌！")
                st.rerun()
            elif not errors:
                st.info("目前沒有偵測到任何欄位變動。")

    except Exception as e:
        st.error(f"❌ 讀取資料庫中繼資料 API 出錯: {e}")
