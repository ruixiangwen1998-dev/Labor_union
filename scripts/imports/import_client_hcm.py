# -*- coding: utf-8 -*-
"""
File: scripts/imports/import_client_hcm.py
Description: 解析並清洗 HCM 月子平台 -市府 Excel 工作表，將乾淨數據寫入 clients 表，並同步初始化 orders 為「洽談中」。
ponytail: 去重與更新時排除 line_user_id 欄位，自動為新案件在 orders 建立「洽談中」紀錄。
"""
import sys
import os
import re
from datetime import date, datetime, timedelta

import pymysql
import pandas as pd
from dotenv import load_dotenv

# 確保中文輸出編碼正確
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# 讓 file_watcher.py 以子程序執行本檔時也能 import services 底下的模組
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.append(ROOT)

from services.client_import_validation import (
    EXCEL_TO_DB_COLUMN,
    fallback_case_key,
    validate_hcm_row,
)
from services.system_alert_service import (
    delete_system_alert,
    resolve_if_exists,
    upsert_system_alert,
)

# 從專案根目錄的 .env 讀取資料庫連線設定 (若 .env 不存在或缺少某欄位，則回退為原本的預設值)
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

# 資料庫連線配置
DB_CONFIG = {
    'host': os.getenv('DB_HOST', '127.0.0.1'),
    'port': int(os.getenv('DB_PORT', 3306)),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', '1234'),
    'database': os.getenv('DB_DATABASE', 'union_db'),
    'charset': 'utf8mb4'
}

# 欄位映射關係 (與舊 import_excel.py 一致，但移除 案件狀態 映射以免覆寫 status)
CLIENTS_FIELD_MAPPING = {
    '項次': 'seq_num',
    '不符合原因': 'reject_reason',
    '查詢序號(案件編號)': 'case_no',
    '報名時間(建檔)': 'created_at',
    'IP位址': 'ip_address',
    '姓名': 'name',
    '性別': 'gender',
    '行動電話': 'phone',
    '縣市': 'city',
    '地址': 'address',
    '身分資格': 'identity_status',
    '服務時間': 'service_time',
    '預產期/預計服務開始月份': 'due_month',
    '預計服務日期': 'service_start_date',
    '其他事項': 'notes',
    '希望服務天數': 'service_days',
    '居住型態': 'residence_type',
    '生產方式': 'delivery_type',
    '服務方式': 'service_type',
    '寶寶資訊': 'baby_info',
    'LINE ID': 'line_id',
    '管理者註記事項': 'admin_notes'
}

def clean_phone(phone_val):
    if pd.isna(phone_val) or not phone_val:
        return None
    phone = str(phone_val).replace(" ", "").replace("-", "").strip()
    phone = re.sub(r'(?<!^)\D', '', phone)
    if phone.startswith("+886"):
        phone = "0" + phone[4:]
    elif phone.startswith("886"):
        phone = "0" + phone[3:]
    if len(phone) == 9 and phone.startswith("9"):
        phone = "0" + phone
    return phone

def clean_city_and_address(city_val, address_val):
    city = str(city_val).strip() if pd.notna(city_val) else ""
    address = str(address_val).strip() if pd.notna(address_val) else ""
    city = city.replace("臺", "台")
    address = address.replace("臺", "台")
    
    if not city and len(address) >= 3:
        for possible_city in ["台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市", "基隆市", "新竹市", "嘉義市", "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣", "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣", "台東縣", "澎湖縣", "金門縣", "連江縣"]:
            if address.startswith(possible_city):
                city = possible_city
                break

    if city in ["台北", "新北", "桃園", "台中", "台南", "高雄"]:
        city = city + "市"
    elif city in ["新竹", "苗栗", "彰化", "南投", "雲林", "嘉義", "屏東", "宜蘭", "花蓮", "台東", "澎湖", "金門", "連江"]:
        city = city + "縣"
        
    return city, address

def clean_data(val, col_name):
    if pd.isna(val):
        return None
    if col_name in ['seq_num', 'service_days']:
        try:
            return int(val)
        except:
            return None
    return str(val).strip()


def _parse_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _load_holiday_dates(cursor):
    cursor.execute("SELECT holiday_date FROM holidays")
    return {
        parsed
        for row in cursor.fetchall()
        if (parsed := _parse_date(row[0])) is not None
    }


def _calculate_service_end_date(start_date, service_days, service_type, holiday_dates):
    if start_date is None or not service_days or service_days < 1:
        return None

    rest_weekdays = {
        "週休1日": {6},
        "週休2日": {5, 6},
        "連續服務": set(),
    }.get(service_type, set())

    current_date = start_date
    completed_days = 0
    while completed_days < service_days:
        if current_date.weekday() not in rest_weekdays and current_date not in holiday_dates:
            completed_days += 1
            if completed_days == service_days:
                return current_date
        current_date += timedelta(days=1)

    return None


def _result(inserted=0, skipped_existing=0, review_required=0, failed=0):
    return {
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "review_required": review_required,
        "failed": failed,
    }


def process_import(excel_path):
    if not os.path.exists(excel_path):
        print(f"錯誤：找不到 Excel 檔案：{excel_path}")
        return _result(review_required=1)
        
    print(f"解析 Excel 檔案：{excel_path} ...")
    xl = pd.ExcelFile(excel_path)
    
    # 尋找匹配的分頁 (不區分大小寫、去空白)
    target_sheet = None
    for name in xl.sheet_names:
        clean_name = name.replace(" ", "").lower()
        if "hcm" in clean_name or "市府" in clean_name:
            target_sheet = name
            break
            
    if not target_sheet:
        print("未找到包含 'HCM' 或 '市府' 關鍵字的工作表。跳過此檔案。")
        return _result(review_required=1)
        
    df = xl.parse(target_sheet)
    print(f"找到匹配工作表：'{target_sheet}'，共有 {len(df)} 筆資料，準備匯入...")
    
    try:
        conn = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        cursor = conn.cursor()
        # 強制指定 utf8mb4 字元編碼以防止 ENUM 狀態機寫入中文時遭到截斷
        cursor.execute("SET NAMES utf8mb4;")
        conn.commit()
    except Exception as e:
        print(f"資料庫連線失敗：{e}")
        return _result(failed=1)
        
    inserted = 0
    skipped_existing = 0
    review_required = 0
    
    try:
        for _, row in df.iterrows():
            raw_row = row.to_dict()
            errors = validate_hcm_row(raw_row)

            record = {}
            for excel_col, db_col in CLIENTS_FIELD_MAPPING.items():
                if excel_col in row:
                    record[db_col] = clean_data(row[excel_col], db_col)

            # 欄位清理
            if 'phone' in record:
                record['phone'] = clean_phone(record['phone'])
            if 'city' in record or 'address' in record:
                clean_c, clean_a = clean_city_and_address(record.get('city'), record.get('address'))
                record['city'] = clean_c
                record['address'] = clean_a

            # 驗證失敗的欄位一律存 NULL，避免髒資料進 DB，同時保留原因供異常警示使用
            for excel_col in errors:
                db_col = EXCEL_TO_DB_COLUMN.get(excel_col)
                if db_col and db_col in record:
                    record[db_col] = None

            name_for_alert = raw_row.get('姓名')
            phone_for_alert = raw_row.get('行動電話')

            case_no = record.get('case_no')
            if not case_no:
                review_required += 1
                if errors:
                    error_keys_joined = "、".join(errors.keys())
                    upsert_system_alert(
                        cursor,
                        alert_code="IMPORT-001",
                        source_domain="IMPORT",
                        case_key=fallback_case_key(name_for_alert, phone_for_alert),
                        reason=f"HCM 匯入資料異常（查無案號）：{error_keys_joined}",
                        details=errors,
                    )
                continue

            # 比對去重
            cursor.execute("SELECT id FROM clients WHERE case_no = %s", (case_no,))
            existing = cursor.fetchone()

            if existing:
                skipped_existing += 1
                continue

            cols = ", ".join([f"`{k}`" for k in record.keys()])
            places = ", ".join(["%s"] * len(record))
            sql = f"INSERT INTO clients ({cols}) VALUES ({places})"
            cursor.execute(sql, tuple(record.values()))
            client_id = cursor.lastrowid
            inserted += 1

            # 關聯訂單與生命週期狀態機初始化
            s_days = clean_data(record.get('service_days'), 'service_days') or 20
            s_time_raw = record.get('service_time') or "9"
            hrs_match = re.search(r'\d+', str(s_time_raw))
            s_hours = int(hrs_match.group(0)) if hrs_match else 9
            start_date = _parse_date(record.get('service_start_date'))
            holiday_dates = _load_holiday_dates(cursor) if start_date else set()
            end_date = _calculate_service_end_date(
                start_date,
                s_days,
                record.get('service_type') or "週休1日",
                holiday_dates,
            )

            cursor.execute("""
                INSERT INTO orders (
                    case_no, client_id, status, service_days,
                    service_hours_per_day, start_date, end_date
                )
                VALUES (%s, %s, '洽談中', %s, %s, %s, %s)
            """, (case_no, client_id, s_days, s_hours, start_date, end_date))

            # 有案號了：若先前用 error_姓名_電話 這個替代鍵記錄過，就把舊的清掉
            fallback_key = fallback_case_key(name_for_alert, phone_for_alert)
            if not fallback_key.startswith("error_row_"):
                delete_system_alert(cursor, alert_code="IMPORT-001", case_key=fallback_key)

            if errors:
                review_required += 1
                error_keys_joined = "、".join(errors.keys())
                upsert_system_alert(
                    cursor,
                    alert_code="IMPORT-001",
                    source_domain="IMPORT",
                    case_key=case_no,
                    reason=f"案件 {case_no} 匯入資料異常：{error_keys_joined}",
                    details=errors,
                )
            else:
                resolve_if_exists(
                    cursor,
                    alert_code="IMPORT-001",
                    case_key=case_no,
                    reason="系統重新匯入：欄位驗證通過，自動解除",
                )

        conn.commit()
        print(f"匯入成功：新增 {inserted} 筆，略過既有 {skipped_existing} 筆，待確認 {review_required} 筆。")
    except Exception as err:
        conn.rollback()
        import traceback
        traceback.print_exc()
        print(f"執行出錯已 Rollback：{err}")
        return _result(skipped_existing=skipped_existing, review_required=review_required, failed=1)
    finally:
        conn.close()
        
    return _result(inserted=inserted, skipped_existing=skipped_existing, review_required=review_required)

if __name__ == "__main__":
    # 提供預設本機路徑或接收命令列參數
    excel_arg = sys.argv[1] if len(sys.argv) > 1 else "document/資料庫、資料處理/假資料_模板.xlsx"
    process_import(excel_arg)
