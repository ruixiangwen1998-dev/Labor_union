import os
import json
import pymysql
import math
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

# 從專案根目錄的 .env 讀取資料庫連線設定 (若 .env 不存在或缺少某欄位，則回退為原本的預設值)
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

DB_CONFIG = {
    'host': os.getenv('DB_HOST', '127.0.0.1'),
    'port': int(os.getenv('DB_PORT', 3306)),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', '1234'),
    'database': os.getenv('DB_DATABASE', 'union_db'),
    'charset': 'utf8mb4'
}

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
            return None
    return val

def generate_virtual_account(case_no) -> str:
    """
    ponytail: 依據 9 碼的「查詢序號(案件編號)」(例如 115000001) 計算對應的 14 碼虛擬帳號。
    如果長度不符合 9 碼，則嘗試回退提取所有數字做基本填充處理。
    """
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
    # fallback
    digits = "".join(filter(str.isdigit, case_no_str))
    if len(digits) >= 3:
        year = digits[:3]
        try:
            seq = int(digits[3:]) if digits[3:] else 0
            return f"99781699{year}{seq:03d}"
        except ValueError:
            pass
    return ""

def get_connection():
    """建立並回傳資料庫連線"""
    return pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)


def _resolve_case_no(case_ref) -> str:
    """Normalize the canonical case number used by every order operation."""
    case_no = str(case_ref or "").strip()
    if not case_no:
        raise ValueError("case_no 不可為空")
    return case_no


def get_order_by_case_no(case_no: str) -> dict | None:
    """Fetch the unique order identified by case_no."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT o.case_no, o.client_id, o.staff_id, o.status, o.cancel_reason,
                       o.line_group_id, o.actual_start_date, o.actual_end_date,
                       o.contract_id, o.service_days, o.service_hours_per_day,
                       o.floor_fee, o.deposit_date,
                       o.start_date, o.end_date, o.custom_rest_dates,
                       o.created_at, o.updated_at,
                       c.name AS client_name, c.identity_status AS identity_status,
                       s.name AS staff_name
                FROM orders o
                JOIN clients c ON c.case_no = o.case_no
                LEFT JOIN staff s ON s.id = o.staff_id
                WHERE o.case_no = %s
            """, (_resolve_case_no(case_no),))
            return cursor.fetchone()
    finally:
        conn.close()

def get_table_data(table_name: str) -> list[dict]:
    """讀取指定原始資料表的內容"""
    allowed_tables = [
        'clients',
        'orders',
        'beclass_records',
        'matching_records',
        'holidays',
        'staff',
        'staff_bank_accounts',
        'case_staff_assignments',
        'client_payments',
        'client_payment_transactions',
        'actual_hours_adjustments',
        'staff_payments',
        'staff_payment_transactions',
        'payment_migration_reviews',
        'staff_schedule',
        'staff_regions',
        'staff_cooking_skills',
        'staff_weekly_rest',
        'staff_time_slots',
        'staff_transportation',
        'staff_holiday_availability',
        'staff_baby_types',
        'line_confirmation_requests',
        'staff_bookings',
    ]
    if table_name not in allowed_tables:
        raise ValueError(f"不允許查詢此資料表: {table_name}")

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            if table_name == 'orders':
                cursor.execute("""
                    SELECT o.case_no, o.client_id, o.staff_id, o.status, o.cancel_reason,
                           o.line_group_id, o.actual_start_date, o.actual_end_date,
                           o.contract_id, o.service_days, o.service_hours_per_day,
                           o.floor_fee, o.deposit_date, o.start_date, o.end_date,
                           o.custom_rest_dates, o.created_at, o.updated_at,
                           c.identity_status AS identity_status, s.name AS staff_name
                    FROM orders o
                    JOIN clients c ON c.case_no = o.case_no
                    LEFT JOIN staff s ON o.staff_id = s.id
                """)
            elif table_name == 'staff':
                cursor.execute("SELECT * FROM `staff`")
                rows = cursor.fetchall()

                cursor.execute("SELECT staff_id, region_name, custom_region_detail FROM staff_regions")
                region_rows = cursor.fetchall()
                region_map = {}
                for row in region_rows:
                    sid = row.get('staff_id')
                    if sid is None:
                        continue
                    region_map.setdefault(sid, [])
                    if row.get('region_name'):
                        region_map[sid].append(row.get('region_name'))
                    if row.get('custom_region_detail'):
                        region_map[sid].append(f"其他：{row['custom_region_detail']}")

                cursor.execute("SELECT staff_id, skill_name, custom_skill_detail FROM staff_cooking_skills")
                skill_rows = cursor.fetchall()
                skill_map = {}
                for row in skill_rows:
                    sid = row.get('staff_id')
                    if sid is None:
                        continue
                    skill_map.setdefault(sid, [])
                    if row.get('skill_name'):
                        skill_map[sid].append(row.get('skill_name'))
                    if row.get('custom_skill_detail'):
                        skill_map[sid].append(f"其他：{row['custom_skill_detail']}")

                cursor.execute("SELECT staff_id, rest_type FROM staff_weekly_rest")
                rest_rows = cursor.fetchall()
                rest_map = {}
                for row in rest_rows:
                    sid = row.get('staff_id')
                    if sid is None:
                        continue
                    rest_map.setdefault(sid, [])
                    rest_map[sid].append(row.get('rest_type'))

                cursor.execute("SELECT staff_id, slot_name, custom_slot_detail FROM staff_time_slots")
                slot_rows = cursor.fetchall()
                slot_map = {}
                for row in slot_rows:
                    sid = row.get('staff_id')
                    if sid is None:
                        continue
                    slot_map.setdefault(sid, [])
                    if row.get('slot_name'):
                        slot_map[sid].append(row.get('slot_name'))
                    if row.get('custom_slot_detail'):
                        slot_map[sid].append(f"其他：{row['custom_slot_detail']}")

                cursor.execute("SELECT staff_id, vehicle_type FROM staff_transportation")
                transport_rows = cursor.fetchall()
                transport_map = {}
                for row in transport_rows:
                    sid = row.get('staff_id')
                    if sid is None:
                        continue
                    transport_map.setdefault(sid, [])
                    if row.get('vehicle_type'):
                        transport_map[sid].append(row.get('vehicle_type'))

                cursor.execute("SELECT staff_id, holiday_name, custom_holiday_detail FROM staff_holiday_availability")
                holiday_rows = cursor.fetchall()
                holiday_map = {}
                for row in holiday_rows:
                    sid = row.get('staff_id')
                    if sid is None:
                        continue
                    holiday_map.setdefault(sid, [])
                    if row.get('holiday_name'):
                        holiday_map[sid].append(row.get('holiday_name'))
                    if row.get('custom_holiday_detail'):
                        holiday_map[sid].append(f"其他：{row['custom_holiday_detail']}")

                cursor.execute("SELECT staff_id, baby_type, custom_baby_detail FROM staff_baby_types")
                baby_rows = cursor.fetchall()
                baby_map = {}
                for row in baby_rows:
                    sid = row.get('staff_id')
                    if sid is None:
                        continue
                    baby_map.setdefault(sid, [])
                    if row.get('baby_type'):
                        baby_map[sid].append(row.get('baby_type'))
                    if row.get('custom_baby_detail'):
                        baby_map[sid].append(f"其他：{row['custom_baby_detail']}")

                cursor.execute("SELECT staff_id, bank_code, branch_code, account_no, is_primary FROM staff_bank_accounts")
                bank_rows = cursor.fetchall()
                bank_map = {}
                for row in bank_rows:
                    sid = row.get('staff_id')
                    if sid is None:
                        continue
                    bank_map.setdefault(sid, [])
                    bank_parts = []
                    if row.get('bank_code'):
                        bank_parts.append(row.get('bank_code'))
                    if row.get('branch_code'):
                        bank_parts.append(row.get('branch_code'))
                    if row.get('account_no'):
                        bank_parts.append(row.get('account_no'))
                    suffix = "/".join(bank_parts) if bank_parts else str(row.get('account_no') or "")
                    bank_label = f"{'主帳戶' if row.get('is_primary') else '次要帳戶'}: {suffix}" if suffix else ('主帳戶' if row.get('is_primary') else '次要帳戶')
                    bank_map[sid].append(bank_label)

                for row in rows:
                    sid = row.get('id')
                    if sid is None:
                        continue

                    regions = [item for item in region_map.get(sid, []) if item]
                    if regions:
                        row['service_regions'] = json.dumps(list(dict.fromkeys(regions)), ensure_ascii=False)
                    else:
                        row['service_regions'] = json.dumps([], ensure_ascii=False)

                    skills = [item for item in skill_map.get(sid, []) if item]
                    if skills:
                        row['special_skills'] = json.dumps(list(dict.fromkeys(skills)), ensure_ascii=False)
                    else:
                        row['special_skills'] = json.dumps([], ensure_ascii=False)

                    rest_days = []
                    rest_items = rest_map.get(sid, [])
                    for rest_type in rest_items:
                        if rest_type in ('週休一日', '週休1日'):
                            if 'Sunday' not in rest_days:
                                rest_days.append('Sunday')
                        elif rest_type in ('週休二日', '週休2日'):
                            for day in ('Saturday', 'Sunday'):
                                if day not in rest_days:
                                    rest_days.append(day)
                    row['weekly_rest_days'] = json.dumps(rest_days, ensure_ascii=False)

                    time_slots = [item for item in slot_map.get(sid, []) if item]
                    if time_slots:
                        row['service_time_slots'] = json.dumps(list(dict.fromkeys(time_slots)), ensure_ascii=False)
                    else:
                        row['service_time_slots'] = json.dumps([], ensure_ascii=False)

                    transports = [item for item in transport_map.get(sid, []) if item]
                    if transports:
                        row['transportation_preferences'] = json.dumps(list(dict.fromkeys(transports)), ensure_ascii=False)
                    else:
                        row['transportation_preferences'] = json.dumps([], ensure_ascii=False)

                    holiday_preferences = [item for item in holiday_map.get(sid, []) if item]
                    if holiday_preferences:
                        row['holiday_preferences'] = json.dumps(list(dict.fromkeys(holiday_preferences)), ensure_ascii=False)
                    else:
                        row['holiday_preferences'] = json.dumps([], ensure_ascii=False)

                    baby_preferences = [item for item in baby_map.get(sid, []) if item]
                    if baby_preferences:
                        row['baby_type_preferences'] = json.dumps(list(dict.fromkeys(baby_preferences)), ensure_ascii=False)
                    else:
                        row['baby_type_preferences'] = json.dumps([], ensure_ascii=False)

                    bank_accounts = [item for item in bank_map.get(sid, []) if item]
                    if bank_accounts:
                        row['bank_accounts'] = json.dumps(list(dict.fromkeys(bank_accounts)), ensure_ascii=False)
                    else:
                        row['bank_accounts'] = json.dumps([], ensure_ascii=False)

                return rows
            else:
                cursor.execute(f"SELECT * FROM `{table_name}`")
            return cursor.fetchall()
    finally:
        conn.close()


def get_table_columns(table_name: str) -> list[str]:
    """取得指定資料表欄位名稱（用於無資料時仍可顯示欄位資訊）"""
    allowed_tables = [
        'clients',
        'orders',
        'beclass_records',
        'matching_records',
        'holidays',
        'staff',
        'staff_bank_accounts',
        'case_staff_assignments',
        'client_payments',
        'client_payment_transactions',
        'actual_hours_adjustments',
        'staff_payments',
        'staff_payment_transactions',
        'payment_migration_reviews',
        'staff_schedule',
        'staff_regions',
        'staff_cooking_skills',
        'staff_weekly_rest',
        'staff_time_slots',
        'staff_transportation',
        'staff_holiday_availability',
        'staff_baby_types',
        'line_confirmation_requests',
        'staff_bookings',
    ]
    if table_name not in allowed_tables:
        raise ValueError(f"不允許查詢此資料表: {table_name}")

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            if table_name == 'staff':
                # staff 畫面會額外聚合顯示下列欄位，保留可視欄位一致性
                cursor.execute("SHOW COLUMNS FROM `staff`")
                rows = cursor.fetchall()
                base_cols = [row.get('Field') for row in rows if row.get('Field')]
                extra_cols = [
                    'service_regions',
                    'special_skills',
                    'weekly_rest_days',
                    'service_time_slots',
                    'transportation_preferences',
                    'holiday_preferences',
                    'baby_type_preferences',
                    'bank_accounts',
                ]
                return base_cols + [col for col in extra_cols if col not in base_cols]

            cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
            rows = cursor.fetchall()
            return [row.get('Field') for row in rows if row.get('Field')]
    finally:
        conn.close()

# 資料庫原始資料瀏覽頁面 (01_data_browser.py) 專用：可即時編輯資料表的主鍵欄位對照
# holidays 使用 holiday_date 作為主鍵，其餘資料表皆使用自增 id 作為主鍵
TABLE_PRIMARY_KEYS = {
    'clients': 'id',
    'staff': 'id',
    'orders': 'case_no',
    'beclass_records': 'id',
    'matching_records': 'id',
    'holidays': 'holiday_date',
    'staff_bank_accounts': 'id',
    'staff_regions': 'staff_id',
    'staff_cooking_skills': 'staff_id',
    'staff_weekly_rest': 'staff_id',
    'staff_time_slots': 'staff_id',
    'staff_transportation': 'staff_id',
    'staff_holiday_availability': 'staff_id',
    'staff_baby_types': 'staff_id',
    'line_confirmation_requests': 'id',
    'staff_bookings': 'id',
    'case_staff_assignments': 'id',
    'client_payments': 'id',
    'client_payment_transactions': 'id',
    'actual_hours_adjustments': 'id',
    'staff_payments': 'id',
    'staff_payment_transactions': 'id',
    'payment_migration_reviews': 'id',
    'staff_schedule': 'id',
}

_READONLY_SUBTABLES = {
    'staff_regions',
    'staff_cooking_skills',
    'staff_weekly_rest',
    'staff_time_slots',
    'staff_transportation',
    'staff_holiday_availability',
    'staff_baby_types',
}

# 系統自動管理欄位，一律唯讀，不允許透過即時編輯表格寫入，避免破壞主鍵/去重與時間戳記追蹤
READONLY_SYSTEM_COLUMNS = {
    'id', 'db_created_at', 'db_updated_at', 'created_at', 'updated_at',
    'sent_at', 'replied_at',
}

def update_table_row(table_name: str, row_id, updates: dict, cursor=None) -> bool:
    """
    通用單列更新函式，供「資料庫原始資料瀏覽」頁面的即時編輯表格使用。
    row_id 為該資料表主鍵值 (多數表為 id，holidays 為 holiday_date)。
    updates 為 {欄位名: 新值} 的字典，僅允許白名單資料表與非唯讀欄位。
    """
    if table_name not in TABLE_PRIMARY_KEYS:
        raise ValueError(f"不允許編輯此資料表: {table_name}")
    if table_name in _READONLY_SUBTABLES:
        raise ValueError("此資料表為複合主鍵關聯表，目前僅支援瀏覽，不支援即時編輯。")

    pk_col = TABLE_PRIMARY_KEYS[table_name]

    # 過濾唯讀欄位與主鍵本身，避免被誤改
    safe_updates = {k: v for k, v in updates.items() if k not in READONLY_SYSTEM_COLUMNS and k != pk_col}
    if not safe_updates:
        return False

    set_clause = ", ".join(f"`{col}` = %s" for col in safe_updates.keys())
    values = list(safe_updates.values()) + [row_id]

    if cursor is not None:
        cursor.execute(
            f"UPDATE `{table_name}` SET {set_clause} WHERE `{pk_col}` = %s",
            values
        )
        return cursor.rowcount > 0

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE `{table_name}` SET {set_clause} WHERE `{pk_col}` = %s",
                values
            )
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

import json

def parse_beclass_survey_details(raw_val) -> dict:
    """自動解析 beclass_records.survey_details JSON 字串，提取 15 大照護細節 (INV-SVC-03)"""
    res = {
        "dietary_habits": "葷食、可以接受中藥補品：茶飲/藥飲/藥膳",
        "vegetarian_preference": "無法接受 (需確定為葷食月嫂)",
        "alcohol_ratio": "半酒",
        "cooking_oil_type": "苦茶油(前兩週)、麻油(後兩週)、一般食用油",
        "maternal_allergy": "無過敏體質",
        "special_care_notes": "依需求協助產婦與新生兒照顧",
        "meal_preferences": "清淡少鹽，口味不想重複",
        "cooking_tools": "炒菜鍋、大同電鍋、微波爐、烤箱、熱奶器、消毒鍋",
        "bath_water_prep": "中藥包煮沸",
        "breastfeeding_method": "母乳 + 配方奶混合哺育",
        "holiday_pricing_terms": "國定三節按合約規定支付 1 倍加班薪資",
        "multi_birth_count": "單胞胎",
        "stair_floor_fee_mode": "大樓電梯公寓 (無額外樓層費)",
        "parking_space_provided": "有提供專用轎車停車位",
        "other_babies_present": "無其他大寶同住"
    }
    if not raw_val:
        return res
        
    data = {}
    if isinstance(raw_val, dict):
        data = raw_val
    elif isinstance(raw_val, str):
        try:
            data = json.loads(raw_val)
        except Exception:
            return res

    # 精確對齊 SQL 實體 JSON 鍵值
    if "月子餐點調理喜好/飲食習慣：" in data:
        res["dietary_habits"] = str(data["月子餐點調理喜好/飲食習慣："])
    if "呈上題，若遇無法媒合到葷食服務人員時，是否可以接受蛋奶素服務人員？" in data:
        res["vegetarian_preference"] = str(data["呈上題，若遇無法媒合到葷食服務人員時，是否可以接受蛋奶素服務人員？"])
    if "2．餐飲含酒比例：" in data:
        res["alcohol_ratio"] = str(data["2．餐飲含酒比例："])
    if "3．料理用油：(可接受種類)" in data:
        res["cooking_oil_type"] = str(data["3．料理用油：(可接受種類)"])
    if "5媽咪有無過敏體質：" in data:
        res["maternal_allergy"] = str(data["5媽咪有無過敏體質："])
    if "特殊照護時應注意事項：" in data:
        res["special_care_notes"] = str(data["特殊照護時應注意事項："])
    if "餐點喜忌備註：" in data:
        res["meal_preferences"] = str(data["餐點喜忌備註："])
    if "烹煮工具" in data:
        res["cooking_tools"] = str(data["烹煮工具"])
    if "洗澡水準備：" in data:
        res["bath_water_prep"] = str(data["洗澡水準備："])
    if "哺乳方式：" in data:
        res["breastfeeding_method"] = str(data["哺乳方式："])
    if "特殊計費:甲方同意需另支付當日薪資1倍予乙方。" in data:
        res["holiday_pricing_terms"] = str(data["特殊計費:甲方同意需另支付當日薪資1倍予乙方。"])
    if "特殊計費:胎數" in data:
        res["multi_birth_count"] = str(data["特殊計費:胎數"])
    if "透天服務樓層方式(會加收樓層費)" in data:
        res["stair_floor_fee_mode"] = str(data["透天服務樓層方式(會加收樓層費)"])
    if "提供服務人員轎車停車位" in data:
        res["parking_space_provided"] = str(data["提供服務人員轎車停車位"])
    if "服務時間內是否有其他寶寶" in data:
        res["other_babies_present"] = str(data["服務時間內是否有其他寶寶"])
        
    return res

def get_order_details() -> list[dict]:
    """讀取 v_order_details 整合計算檢視表 (完全對齊 36 項業務與 15 大照護細節全圖譜)"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 嘗試讀取 beclass_records survey_details 進行關聯
            survey_map = {}
            try:
                cursor.execute("SELECT query_no, survey_details FROM beclass_records WHERE survey_details IS NOT NULL")
                b_rows = cursor.fetchall()
                for br in b_rows:
                    if br.get('query_no') and br.get('survey_details'):
                        survey_map[str(br['query_no']).strip()] = parse_beclass_survey_details(br['survey_details'])
            except Exception:
                pass

            cursor.execute("SELECT * FROM v_order_details")
            rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT csa.case_no,
                       GROUP_CONCAT(
                           s.name ORDER BY csa.assignment_sequence
                           SEPARATOR '、'
                       ) AS assignment_staff_names
                  FROM case_staff_assignments csa
                  JOIN staff s ON s.id = csa.staff_id
                 WHERE csa.status <> 'cancelled'
                 GROUP BY csa.case_no
                """
            )
            assignment_staff_names = {
                str(row["case_no"]): row.get("assignment_staff_names")
                for row in (cursor.fetchall() or [])
                if row.get("case_no")
            }
            for r in rows:
                assignment_names = assignment_staff_names.get(
                    str(r.get("case_no") or "")
                )
                if assignment_names:
                    r["staff_name"] = assignment_names
                r['notes'] = r.get('notes') or r.get('special_needs') or ""
                def to_str_date(val):
                    if not val:
                        return ""
                    if hasattr(val, "strftime"):
                        return val.strftime("%Y-%m-%d")
                    return str(val)

                r['due_date'] = to_str_date(r.get('due_date') or r.get('start_date'))
                # 實際服務日期不可用預期日期補值，否則尚未開工的訂單會被誤判。
                r['actual_start_date'] = to_str_date(r.get('actual_start_date'))
                r['actual_end_date'] = to_str_date(r.get('actual_end_date'))
                r['deposit_received_at'] = to_str_date(r.get('deposit_received_at') or r.get('deposit_date'))
                r['start_date'] = to_str_date(r.get('start_date'))
                r['end_date'] = to_str_date(r.get('end_date'))
                r['deposit_date'] = to_str_date(r.get('deposit_date'))
                r['govt_claim_date'] = to_str_date(r.get('govt_claim_date'))

                r['custom_leave_dates'] = r.get('custom_leave_dates') or ""
                r['service_mode'] = r.get('service_mode') or "週休1日"
                r['service_hours_per_day'] = safe_int(r.get('service_hours_per_day', 9))
                days = safe_int(r.get('service_days', 20))
                hrs = safe_int(r.get('service_hours_per_day', 9))
                r['total_hours'] = r.get('total_hours') or (days * hrs)
                r['subsidy_hours'] = r.get('subsidy_hours') or (40 if r.get('identity_status') != '一般身分' else 0)
                r['self_pay_hours'] = max(0, r['total_hours'] - r['subsidy_hours'])
                r['claim_total_days'] = days
                r['employer_hourly_rate'] = r.get('employer_hourly_rate') or 2000
                r['deposit_days'] = r.get('deposit_days') or 1
                r['first_payment_days'] = r.get('first_payment_days') or safe_int(days / 2)
                r['second_payment_days'] = r.get('second_payment_days') or (days - r['first_payment_days'])
                r['caregiver_rate'] = r.get('caregiver_rate') or 2000
                
                end_dt = safe_date(r['actual_end_date'])
                if end_dt:
                    m1 = end_dt.month % 12 + 1
                    y1 = end_dt.year + (1 if end_dt.month == 12 else 0)
                    default_pay1 = f"{y1:04d}-{m1:02d}-15"
                    
                    m2 = m1 % 12 + 1
                    y2 = y1 + (1 if m1 == 12 else 0)
                    default_pay2 = f"{y2:04d}-{m2:02d}-15"
                else:
                    default_pay1 = "2026-10-15"
                    default_pay2 = "2026-11-15"

                r['salary_payment_date_1'] = to_str_date(r.get('salary_payment_date_1') or default_pay1)
                r['salary_payment_date_2'] = to_str_date(r.get('salary_payment_date_2') or default_pay2)
                r['phone'] = r.get('phone') or r.get('client_phone') or "0912-345-678"
                r['address'] = r.get('address') or r.get('client_address') or "新竹市東區中央路 100 號"
                r['total_caregiver_salary'] = safe_int(r.get('service_salary', 0)) + safe_int(r.get('subsidy_salary', 0))

                # 注入解包出來的 15 大照護細節欄位
                c_details = survey_map.get(str(r.get('case_no') or '').strip(), {})
                for dk, dv in c_details.items():
                    r[dk] = dv

            return rows
    finally:
        conn.close()


def get_case_order_details() -> list[dict]:
    """Return order view rows identified by case_no."""
    return get_order_details()


def _sync_client_payment_due_dates_with_cursor(cursor, case_no: str | None = None) -> int:
    if case_no is not None:
        case_no = _resolve_case_no(case_no)
        cursor.execute(
            """
            UPDATE client_payments cp
            JOIN v_order_details od ON od.case_no = cp.case_no
            SET cp.deposit_due_date = COALESCE(cp.deposit_due_date, od.deposit_date),
                cp.first_payment_due_date = COALESCE(cp.first_payment_due_date, od.first_payment_date),
                cp.second_payment_due_date = COALESCE(cp.second_payment_due_date, od.second_payment_date)
            WHERE cp.case_no = %s
              AND (
                cp.deposit_due_date IS NULL
                OR cp.first_payment_due_date IS NULL
                OR cp.second_payment_due_date IS NULL
              )
            """,
            (case_no,),
        )
        return int(cursor.rowcount or 0)

    cursor.execute(
        """
        UPDATE client_payments cp
        JOIN v_order_details od ON od.case_no = cp.case_no
        SET cp.deposit_due_date = COALESCE(cp.deposit_due_date, od.deposit_date),
            cp.first_payment_due_date = COALESCE(cp.first_payment_due_date, od.first_payment_date),
            cp.second_payment_due_date = COALESCE(cp.second_payment_due_date, od.second_payment_date)
        WHERE (
            cp.deposit_due_date IS NULL
            OR cp.first_payment_due_date IS NULL
            OR cp.second_payment_due_date IS NULL
        )
        """,
    )
    return int(cursor.rowcount or 0)


def sync_client_payment_due_dates_for_case_no(case_no: str, cursor=None) -> int:
    case_no = _resolve_case_no(case_no)
    if cursor is not None:
        return _sync_client_payment_due_dates_with_cursor(cursor, case_no)

    conn = get_connection()
    try:
        with conn.cursor() as internal_cursor:
            updated = _sync_client_payment_due_dates_with_cursor(internal_cursor, case_no)
            conn.commit()
            return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _count_missing_client_payment_due_dates(cursor, case_no: str | None = None) -> int:
    if case_no is not None:
        case_no = _resolve_case_no(case_no)
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM client_payments cp
            JOIN v_order_details od ON od.case_no = cp.case_no
            WHERE cp.case_no = %s
              AND (
                cp.deposit_due_date IS NULL
                OR cp.first_payment_due_date IS NULL
                OR cp.second_payment_due_date IS NULL
              )
            """,
            (case_no,),
        )
    else:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM client_payments cp
            JOIN v_order_details od ON od.case_no = cp.case_no
            WHERE cp.deposit_due_date IS NULL
               OR cp.first_payment_due_date IS NULL
               OR cp.second_payment_due_date IS NULL
            """,
        )
    result = cursor.fetchone()
    return int(result[0] if isinstance(result, tuple) else result["COUNT(*)"] if result else 0)


def backfill_client_payment_due_dates(case_no: str | None = None) -> dict:
    conn = get_connection()
    try:
        target_case_no = _resolve_case_no(case_no) if case_no else None
        with conn.cursor() as cursor:
            before = _count_missing_client_payment_due_dates(cursor, target_case_no)
            updated = _sync_client_payment_due_dates_with_cursor(cursor, target_case_no)
            after = _count_missing_client_payment_due_dates(cursor, target_case_no)
            conn.commit()
            return {
                "target_case_no": target_case_no,
                "updated_rows": updated,
                "remaining_missing_rows": after,
                "updated_before": before,
            }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def create_order(case_no: str, service_days: int, service_hours_per_day: int,
                 floor_fee: float = 0.0,
                 deposit_date = None, start_date = None, end_date = None, 
                 status: str = '洽談中') -> str:
    """依正式 case_no 建立或取得唯一訂單，並同步建立帳務記錄。"""
    case_no = _resolve_case_no(case_no)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, name FROM clients WHERE case_no = %s", (case_no,))
            client = cursor.fetchone()
            if not client:
                raise ValueError(f"找不到 case_no={case_no} 的客戶")

            cursor.execute("""
                INSERT INTO orders (
                    case_no, client_id, status, service_days, service_hours_per_day,
                    floor_fee, deposit_date, start_date, end_date
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE case_no = VALUES(case_no)
            """, (
                case_no, client['id'], status, service_days, service_hours_per_day,
                floor_fee, deposit_date, start_date, end_date
            ))

            cursor.execute("""
                INSERT IGNORE INTO client_payments (case_no, payment_status)
                VALUES (%s, '待收訂金')
            """, (case_no,))

            sync_client_payment_due_dates_for_case_no(case_no, cursor=cursor)
            
            conn.commit()
            return case_no
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def assign_staff_to_order(case_no: str, staff_id: int) -> bool:
    """為訂單指派服務人員，將狀態改為「訂單成立」並自動產生預設的每日排班"""
    case_no = _resolve_case_no(case_no)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 1. 更新 orders 資料表的 staff_id 與 status
            cursor.execute("""
                UPDATE orders 
                SET staff_id = %s, status = '訂單成立' 
                WHERE case_no = %s
            """, (staff_id, case_no))
            
            # 2. 獲取訂單的開始日期與服務天數，以進行自動排班
            cursor.execute("SELECT start_date, service_days FROM orders WHERE case_no = %s", (case_no,))
            order_info = cursor.fetchone()
            
            conn.commit()
            
            # 3. 呼叫 generate_default_schedule 產生排班 (若 start_date 存在)
            if order_info and order_info['start_date'] and order_info['service_days']:
                generate_default_schedule(case_no, staff_id, order_info['start_date'], order_info['service_days'])
                
            return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def update_order_status(case_no: str, status: str, cancel_reason: str = None) -> bool:
    """更新訂單狀態與取消原因"""
    case_no = _resolve_case_no(case_no)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE orders 
                SET status = %s, cancel_reason = %s 
                WHERE case_no = %s
            """, (status, cancel_reason, case_no))
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def update_order_full_details(case_no: str, data: dict) -> bool:
    """更新不影響正式 assignment／schedule 的訂單基本資料。"""
    case_no = _resolve_case_no(case_no)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 若 clients 姓名有修改，同步更新客戶主表 name
            if data.get('client_name'):
                cursor.execute("""
                    UPDATE clients c
                    SET c.name = %s
                    WHERE c.case_no = %s
                """, (data.get('client_name'), case_no))

            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()



def add_or_update_holiday(holiday_date, holiday_name: str, is_double_pay_default: bool = False) -> bool:
    """新增或更新國定假日"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO holidays (holiday_date, holiday_name, is_double_pay_default)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE holiday_name = %s, is_double_pay_default = %s
            """, (holiday_date, holiday_name, is_double_pay_default, holiday_name, is_double_pay_default))
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def delete_holiday(holiday_date) -> bool:
    """刪除特定國定假日"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM holidays WHERE holiday_date = %s", (holiday_date,))
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_staff_monthly_schedule(staff_id: int, year: int, month: int) -> dict[int, dict]:
    """
    獲取月嫂在指定年月的每日檔期狀態。
    導入 ADR-v3-01 預產期 7 天緩衝鎖定與服務中解鎖機制。
    回傳字典: { date_day(int): { 'status': 'white'/'yellow'/'red', 'client_name': '...', 'case_no': ..., 'is_work_day': bool } }
    """
    from datetime import date, datetime, timedelta
    conn = get_connection()
    schedule_map = {}
    
    def parse_dt(val):
        if not val:
            return None
        if isinstance(val, datetime):
            return val.date()
        if hasattr(val, "date"):
            return val
        if isinstance(val, (str, bytes)):
            try:
                return datetime.strptime(str(val).split(" ")[0].strip(), "%Y-%m-%d").date()
            except:
                return None
        return val

    try:
        with conn.cursor() as cursor:
            # 1. 讀取月嫂所有有效訂單 (洽談中/訂單成立/服務中/訂單完成) 進行動態天數與緩衝期計算
            cursor.execute("""
                SELECT o.case_no, o.status AS order_status, o.start_date, o.end_date,
                       o.actual_start_date, o.service_days, c.name AS client_name
                FROM orders o
                JOIN clients c ON o.client_id = c.id
                WHERE o.staff_id = %s AND o.status != '訂單取消'
            """, (staff_id,))
            orders = cursor.fetchall()
            
            for o in orders:
                st_date = parse_dt(o['actual_start_date']) or parse_dt(o['start_date'])
                if not st_date:
                    continue
                days_cnt = o['service_days'] or 20
                ed_date = st_date + timedelta(days=days_cnt - 1)
                
                status = o['order_status']
                
                # 計算該訂單影響的日期區間
                # 洽談中/訂單成立：服務期間 + 結束後 7 天 (黃底鎖定)
                # 服務中/訂單完成：服務期間 (紅底)，後續 7 天自動解鎖
                main_color = 'red' if status in ['服務中', '訂單完成'] else 'yellow'
                
                # A. 服務期間區間
                curr = st_date
                while curr <= ed_date:
                    if curr.year == year and curr.month == month:
                        schedule_map[curr.day] = {
                            'status': main_color,
                            'client_name': o['client_name'],
                            'case_no': o['case_no'],
                            'is_work_day': True,
                            'is_double_pay': False
                        }
                    curr += timedelta(days=1)
                    
                # B. 預排階段 (洽談中/訂單成立) 額外計算結束後 7 天緩衝鎖定 (黃底)
                if status in ['洽談中', '訂單成立']:
                    buffer_start = ed_date + timedelta(days=1)
                    buffer_end = ed_date + timedelta(days=7)
                    curr = buffer_start
                    while curr <= buffer_end:
                        if curr.year == year and curr.month == month:
                            # 若當天尚未被其他權重更高的排班設定，寫入緩衝鎖定
                            if curr.day not in schedule_map:
                                schedule_map[curr.day] = {
                                    'status': 'yellow',
                                    'client_name': f"{o['client_name']} (預留備用期)",
                                    'case_no': o['case_no'],
                                    'is_work_day': False,
                                    'is_double_pay': False
                                }
                        curr += timedelta(days=1)
                        
            # 2. 疊加 staff_schedule 明細對特定日期個體設定進行覆蓋
            cursor.execute("""
                SELECT s.*, c.name AS client_name, o.status AS order_status, o.custom_rest_dates
                FROM staff_schedule s
                JOIN orders o ON s.case_no = o.case_no
                JOIN clients c ON o.client_id = c.id
                WHERE s.staff_id = %s AND YEAR(s.work_date) = %s AND MONTH(s.work_date) = %s
            """, (staff_id, year, month))
            rows = cursor.fetchall()
            
            today_date = date.today()
            for r in rows:
                w_date = parse_dt(r['work_date'])
                if not w_date:
                    continue
                day = w_date.day
                
                # 判斷是否為排定休假 (is_work_day == False 標示為綠底 🟢)
                if r['order_status'] == '訂單取消':
                    status = 'white'
                elif not r['is_work_day']:
                    status = 'green'  # 🟢 綠底休假/請假
                elif w_date <= today_date or r['order_status'] in ['服務中', '訂單完成']:
                    status = 'red'
                else:
                    status = 'yellow'
                
                schedule_map[day] = {
                    'status': status,
                    'client_name': r['client_name'],
                    'case_no': r['case_no'],
                    'is_work_day': bool(r['is_work_day']),
                    'is_double_pay': bool(r['is_double_pay']),
                    'schedule_id': r['id']
                }
            return schedule_map
    finally:
        conn.close()

def save_order_rest_dates(case_no: str, rest_dates_list: list) -> dict:
    """
    持久化儲存訂單放假日期清單，並依據休假天數自動順延服務結束日 end_date。
    確保出勤工作服務天數 100% 足額達 service_days (N 天)。
    """
    import json
    from datetime import datetime, timedelta, date

    def parse_date(val):
        if not val:
            return None
        if isinstance(val, date):
            return val
        if isinstance(val, datetime):
            return val.date()
        try:
            return datetime.strptime(str(val).split(" ")[0].strip(), "%Y-%m-%d").date()
        except:
            return None

    case_no = _resolve_case_no(case_no)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 1. 讀取訂單資訊
            cursor.execute("""
                SELECT case_no, staff_id, start_date, service_days
                FROM orders 
                WHERE case_no = %s
            """, (case_no,))
            order = cursor.fetchone()
            if not order:
                return {'success': False, 'message': '找不到指定的訂單。'}

            start_d = parse_date(order['start_date'])
            if not start_d:
                return {'success': False, 'message': '該訂單尚未設定有效的開始服務日期。'}

            target_work_days = order['service_days'] or 20
            staff_id = order['staff_id']

            # 整理放假日期集合 (ISO 格式)
            clean_rest_set = set()
            for rd in rest_dates_list:
                dt = parse_date(rd)
                if dt:
                    clean_rest_set.add(dt.strftime("%Y-%m-%d"))

            # 2. 自動推算順延後的完工日 end_date
            curr_date = start_d
            work_days_count = 0
            new_end_date = start_d
            schedule_entries = []

            while work_days_count < target_work_days:
                date_str = curr_date.strftime("%Y-%m-%d")
                is_work = (date_str not in clean_rest_set)

                if is_work:
                    work_days_count += 1
                
                new_end_date = curr_date
                schedule_entries.append((date_str, is_work))
                curr_date += timedelta(days=1)

            # 3. 持久化更新 orders 表的 custom_rest_dates 與 end_date/actual_end_date
            json_rest_str = json.dumps(sorted(list(clean_rest_set)), ensure_ascii=False)
            cursor.execute("""
                UPDATE orders 
                SET custom_rest_dates = %s, end_date = %s, actual_end_date = %s 
                WHERE case_no = %s
            """, (json_rest_str, new_end_date, new_end_date, case_no))

            # 4. 若已被指派月嫂，覆寫更新 staff_schedule 表
            if staff_id:
                cursor.execute("DELETE FROM staff_schedule WHERE case_no = %s", (case_no,))
                for date_str, is_work in schedule_entries:
                    note = '正常服務出勤日' if is_work else '行政專員排定放假(動態順延)'
                    cursor.execute("""
                        INSERT INTO staff_schedule (case_no, staff_id, work_date, is_work_day, is_double_pay, notes)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE is_work_day = VALUES(is_work_day), notes = VALUES(notes)
                    """, (case_no, staff_id, date_str, is_work, False, note))

            conn.commit()
            return {
                'success': True,
                'message': f'成功儲存放假！完工日已動態順延至 {new_end_date.strftime("%Y-%m-%d")} (足額服務 {target_work_days} 天)。',
                'new_end_date': new_end_date.strftime("%Y-%m-%d"),
                'rest_count': len(clean_rest_set)
            }
    except Exception as e:
        conn.rollback()
        return {'success': False, 'message': f'儲存放假失敗: {str(e)}'}
    finally:
        conn.close()

def generate_default_schedule(case_no: str, staff_id: int, start_date, service_days: int) -> bool:
    """根據開始日期與服務天數，自動生成一筆預設排班。預設排除週日放假，且將國定假日標記為放假/待確認"""
    from datetime import datetime, timedelta
    case_no = _resolve_case_no(case_no)
    conn = get_connection()
    try:
        # 1. 取得該月嫂與國定假日配置
        with conn.cursor() as cursor:
            cursor.execute("SELECT holiday_date FROM holidays")
            holiday_set = {h['holiday_date'] for h in cursor.fetchall()}
            
            # 取得月嫂固定休假偏好 (如 JSON)
            cursor.execute("SELECT weekly_rest_days FROM staff WHERE id = %s", (staff_id,))
            staff_row = cursor.fetchone()
            # 預設固定休星期日
            rest_days = [6] # python weekday: Monday=0, Sunday=6
            if staff_row and staff_row['weekly_rest_days']:
                try:
                    import json
                    rest_names = json.loads(staff_row['weekly_rest_days'])
                    name_map = {'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3, 'Friday': 4, 'Saturday': 5, 'Sunday': 6}
                    rest_days = [name_map[n] for n in rest_names if n in name_map]
                except:
                    pass
            
            # 2. 開始排班 (排滿指定的服務天數)
            curr_date = start_date
            if isinstance(curr_date, str):
                curr_date = datetime.strptime(curr_date, "%Y-%m-%d").date()
                
            work_days_count = 0
            # 限制防死循環 (最多排一年)
            # ponytail: protect loop with maximum 365 iterations
            for _ in range(365):
                if work_days_count >= service_days:
                    break
                    
                is_work = True
                is_holiday = curr_date in holiday_set
                is_weekend = curr_date.weekday() in rest_days
                
                # 如果是假日或月嫂固定休假，預設不工作 (is_work_day = FALSE)，且不計入服務天數
                if is_holiday or is_weekend:
                    is_work = False
                
                # 寫入 staff_schedule
                cursor.execute("""
                    INSERT INTO staff_schedule (case_no, staff_id, work_date, is_work_day, is_double_pay, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE is_work_day = %s
                """, (
                    case_no, staff_id, curr_date, is_work, False,
                    "國定假日預設放假" if is_holiday else ("週休預設放假" if is_weekend else None),
                    is_work
                ))
                
                if is_work:
                    work_days_count += 1
                curr_date += timedelta(days=1)
                
            # 更新訂單的預計服務結束日 與 實際服務結束日 (為排班的最後一天)
            last_date = curr_date - timedelta(days=1)
            cursor.execute("UPDATE orders SET end_date = %s, actual_end_date = %s WHERE case_no = %s", (last_date, last_date, case_no))
            
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def update_schedule_day(case_no: str | None, staff_id: int, work_date, is_work_day: bool, is_double_pay: bool, notes: str = None) -> bool:
    """手動新增/修改單日排班；case_no 為 None 時釋放該日檔期。"""
    if case_no is not None:
        case_no = _resolve_case_no(case_no)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            if case_no is None:
                # 釋放檔期，直接刪除排班記錄
                cursor.execute("""
                    DELETE FROM staff_schedule 
                    WHERE staff_id = %s AND work_date = %s
                """, (staff_id, work_date))
            else:
                cursor.execute("""
                    INSERT INTO staff_schedule (case_no, staff_id, work_date, is_work_day, is_double_pay, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        case_no = VALUES(case_no),
                        is_work_day = VALUES(is_work_day),
                        is_double_pay = VALUES(is_double_pay), notes = VALUES(notes)
                """, (
                    case_no, staff_id, work_date, is_work_day, is_double_pay, notes
                ))
            
            # 動態重新計算服務天數，並修正訂單的結束日期
            if case_no is not None:
                cursor.execute("""
                    SELECT COUNT(*) AS total_work_days, MAX(work_date) AS last_date
                    FROM staff_schedule 
                    WHERE case_no = %s AND staff_id = %s AND is_work_day = TRUE
                """, (case_no, staff_id))
                summary = cursor.fetchone()
                if summary and summary['last_date']:
                    # 更新訂單結束日
                    cursor.execute("UPDATE orders SET end_date = %s WHERE case_no = %s", (summary['last_date'], case_no))
                
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_order_matches(case_no: str) -> list[dict]:
    """獲取特定訂單的所有媒合意願記錄與發送狀態"""
    case_no = _resolve_case_no(case_no)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT m.*, s.name AS staff_name, s.phone AS staff_phone
                FROM matching_records m
                JOIN staff s ON m.staff_id = s.id
                WHERE m.case_no = %s
                ORDER BY m.sent_at DESC
            """, (case_no,))
            return cursor.fetchall()
    finally:
        conn.close()

def create_or_get_match_record(case_no: str, staff_id: int) -> int:
    """獲取或建立一筆媒合意願紀錄"""
    case_no = _resolve_case_no(case_no)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM matching_records WHERE case_no = %s AND staff_id = %s", (case_no, staff_id))
            r = cursor.fetchone()
            if r:
                return r['id']
            cursor.execute("""
                INSERT INTO matching_records (case_no, staff_id)
                VALUES (%s, %s)
            """, (case_no, staff_id))
            conn.commit()
            return cursor.lastrowid
    finally:
        conn.close()

def calculate_attendance_schedule(
    actual_start_date, 
    target_service_days: int, 
    service_mode: str = '週休1日', 
    custom_rest_weekdays: list = None,
    custom_leave_dates: set = None,
    custom_holiday_rest_dates: set = None,
    monthly_salary_base: float = 0.0
) -> dict:
    """
    出勤天數精算核心算法 (ADR-v4-01, ADR-v4-02, ADR-v5-01, ADR-v6-01 & ADR-v7-01):
    根據確定開始日、目標服務天數 N、單日動態請假與國定假日單日自主出勤勾選集合，
    自動順延計算最終完工日、個體國定假日出勤狀態與週報拆解統計。
    """
    from datetime import datetime, timedelta
    
    def parse_d(val):
        if not val:
            return None
        if isinstance(val, datetime):
            return val.date()
        if hasattr(val, "date"):
            return val
        if isinstance(val, (str, bytes)):
            try:
                return datetime.strptime(str(val).split(" ")[0].strip(), "%Y-%m-%d").date()
            except:
                return None
        return val

    st_d = parse_d(actual_start_date)
    if not st_d:
        return {}
        
    N = int(target_service_days or 20)
    
    leave_dates = set(custom_leave_dates) if custom_leave_dates is not None else set()
    
    # 讀取國定假日對照表
    conn = get_connection()
    holiday_map = {}
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT holiday_date, holiday_name FROM holidays")
            for h in cursor.fetchall():
                hd = parse_d(h['holiday_date'])
                if hd:
                    holiday_map[hd] = h['holiday_name']
    finally:
        conn.close()

    # 若未自訂國定假日放假集合，預設所有國定假日全放假 (休假順延 1 天)
    if custom_holiday_rest_dates is None:
        holiday_rest_dates = set(holiday_map.keys())
    else:
        holiday_rest_dates = set(custom_holiday_rest_dates)

    # 判定每週預設排休星期的集合 (ADR-v18-04)
    if custom_rest_weekdays is not None:
        rest_weekdays = set(custom_rest_weekdays)
    else:
        if service_mode == '週休1日':
            rest_weekdays = {6}       # 預設週日 (Sunday == 6)
        elif service_mode == '週休2日':
            rest_weekdays = {5, 6}    # 預設週六、週日 (Saturday == 5, Sunday == 6)
        else:
            rest_weekdays = set()

    # 迴圈計算出勤天數直至滿 N 個工作日
    curr = st_d
    worked_days_count = 0
    day_by_day = []
    national_holidays_found = []
    
    while worked_days_count < N:
        is_holiday = curr in holiday_map
        h_name = holiday_map.get(curr, None)
        
        # 判定今天是否為休假/請假:
        # 1. 符合每週預設排休 (例如週休二日之六日)
        # 2. 或單日排休選單點選 (leave_dates)
        # 3. 或國定假日選單勾選放假
        is_weekday_rest = curr.weekday() in rest_weekdays
        
        if is_holiday:
            is_rest = is_weekday_rest or (curr in leave_dates) or (curr in holiday_rest_dates)
            national_holidays_found.append({
                'date': curr, 
                'name': h_name, 
                'is_worked': not is_rest
            })
        else:
            is_rest = is_weekday_rest or (curr in leave_dates)

        if not is_rest:
            worked_days_count += 1
            is_work = True
        else:
            is_work = False
            
        day_by_day.append({
            'date': curr,
            'day_num': len(day_by_day) + 1,
            'is_work_day': is_work,
            'is_rest_day': is_rest,
            'holiday_name': h_name
        })
        
        if worked_days_count < N:
            curr += timedelta(days=1)

    actual_end_date = curr
    total_calendar_days = len(day_by_day)
    rest_days_count = total_calendar_days - N
    
    total_estimated_salary = monthly_salary_base
    
    # 週報拆解統計 (每 7 天為 1 週)
    weekly_stats = []
    for i in range(0, total_calendar_days, 7):
        chunk = day_by_day[i:i+7]
        w_idx = i // 7 + 1
        w_work = sum(1 for d in chunk if d['is_work_day'])
        w_rest = sum(1 for d in chunk if d['is_rest_day'])
        w_holidays = sum(1 for d in chunk if d['holiday_name'])
        weekly_stats.append({
            'week_num': w_idx,
            'start_date': chunk[0]['date'],
            'end_date': chunk[-1]['date'],
            'work_days': w_work,
            'rest_days': w_rest,
            'holiday_days': w_holidays
        })

    return {
        'actual_start_date': st_d,
        'actual_end_date': actual_end_date,
        'target_service_days': N,
        'total_calendar_days': total_calendar_days,
        'actual_work_days_count': N,
        'rest_days_count': rest_days_count,
        'national_holidays_found': national_holidays_found,
        'total_estimated_salary': total_estimated_salary,
        'weekly_stats': weekly_stats,
        'day_by_day': day_by_day
    }

def update_matching_info_sent(match_id: int, form_type: int) -> bool:
    """更新特定媒合紀錄的表單發送時間 (form_type: 1 或 2)"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            if form_type == 1:
                cursor.execute("""
                    UPDATE matching_records 
                    SET sent_info_1_at = NOW() 
                    WHERE id = %s
                """, (match_id,))
            elif form_type == 2:
                cursor.execute("""
                    UPDATE matching_records 
                    SET sent_info_2_at = NOW() 
                    WHERE id = %s
                """, (match_id,))
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def mark_resume_sent(match_id: int) -> bool:
    """記錄履歷已發送給客戶的時間。"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                UPDATE matching_records
                SET sent_resume_at = NOW()
                WHERE id = %s
            """, (match_id,))
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def mark_resume_sent_for_case(case_no: str) -> int | None:
    """找出該案件中「已被接受但履歷尚未發送」的候選人紀錄並標記發送，回傳該紀錄 id；找不到則回傳 None。"""
    case_no = _resolve_case_no(case_no)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id FROM matching_records
                WHERE case_no = %s AND caregiver_accepted = 1 AND sent_resume_at IS NULL
                ORDER BY id
                LIMIT 1
            """, (case_no,))
            row = cursor.fetchone()
            if row is None:
                return None
            match_id = row['id']
            cursor.execute("""
                UPDATE matching_records
                SET sent_resume_at = NOW()
                WHERE id = %s
            """, (match_id,))
            conn.commit()
            return match_id
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def reply_matching_inquiry(match_id: int, accepted) -> bool:
    """更新月嫂意願回覆狀態 (支援 True=願意, False=拒絕, None=待回覆)"""
    conn = get_connection()
    try:
        val = 1 if accepted is True else (0 if accepted is False else None)
        replied_time = "NOW()" if accepted is not None else "NULL"
        with conn.cursor() as cursor:
            cursor.execute(f"""
                UPDATE matching_records 
                SET caregiver_accepted = %s, replied_at = {replied_time} 
                WHERE id = %s
            """, (val, match_id))
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def parse_client_district(city: str, address: str) -> str:
    """ponytail: extract administrative district from client city and address"""
    full_str = f"{city or ''} {address or ''}"
    districts = ["香山區", "東區", "北區", "竹北市", "竹東鎮", "新埔鎮", "關西鎮", "湖口鄉", "新豐鄉", "芎林鄉", "橫山鄉", "北埔鄉", "寶山鄉", "峨眉鄉", "尖石鄉", "五峰鄉", "頭份市", "竹南鎮"]
    for d in districts:
        if d in full_str:
            return d
    if city:
        return city
    return ""

def get_recommended_staff_for_order(
    case_no: str,
    filter_region: bool = True,
    filter_schedule: bool = True,
    filter_babies: bool = True,
    filter_time: bool = True
) -> list[dict]:
    """
    ADAD INV-SVC-05: 智慧粗篩比對月嫂推薦引擎 (支援 7 天預留備用期持久化掃描與 city/address 區域比對)
    """
    case_no = _resolve_case_no(case_no)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT o.case_no, o.start_date, o.end_date, o.actual_start_date, o.service_days, o.service_hours_per_day,
                       c.id AS client_id, c.name AS client_name, c.city, c.address, c.baby_info, c.service_time
                FROM orders o
                JOIN clients c ON o.client_id = c.id
                WHERE o.case_no = %s
            """, (case_no,))
            order_info = cursor.fetchone()
            if not order_info:
                return []

            o_st = order_info.get('actual_start_date') or order_info.get('start_date')
            o_ed = order_info.get('end_date')
            client_district = parse_client_district(order_info.get('city'), order_info.get('address'))
            client_baby = str(order_info.get('baby_info') or '')
            is_twins = "雙胞胎" in client_baby or "2" in client_baby

            cursor.execute("SELECT * FROM staff WHERE status = 'active'")
            staff_list = cursor.fetchall()

            cursor.execute("SELECT staff_id, region_name FROM staff_regions")
            region_rows = cursor.fetchall()
            staff_region_map = {}
            for r in region_rows:
                sid = r['staff_id']
                staff_region_map.setdefault(sid, set()).add(r['region_name'])

            cursor.execute("""
                SELECT case_no, staff_id, start_date, end_date, actual_start_date
                FROM orders 
                WHERE staff_id IS NOT NULL AND status NOT IN ('訂單取消') AND case_no != %s
            """, (case_no,))
            existing_orders = cursor.fetchall()

            recommendations = []

            for s in staff_list:
                sid = s['id']
                reasons = []
                reject_reasons = []
                score = 100

                # 1. 服務區域比對
                s_regions = staff_region_map.get(sid, set())
                if s.get('service_regions'):
                    try:
                        import json
                        sr_list = json.loads(s['service_regions']) if isinstance(s['service_regions'], str) else s['service_regions']
                        s_regions.update(sr_list)
                    except:
                        pass

                region_matched = True
                if client_district and s_regions:
                    if not any(client_district in r or r in client_district for r in s_regions):
                        region_matched = False
                        reject_reasons.append(f"區域不符 ({client_district})")
                        score -= 40
                    else:
                        reasons.append(f"符合區域 ({client_district})")
                else:
                    reasons.append("區域可承接")

                # 2. 檔期衝突掃描 (包含 7 天預留備用期持久化計算)
                schedule_conflict = False
                if o_st:
                    o_end_date = o_ed or (o_st + timedelta(days=safe_int(order_info.get('service_days', 20))))
                    for eo in existing_orders:
                        if eo['staff_id'] == sid:
                            eo_st = eo.get('actual_start_date') or eo.get('start_date')
                            eo_ed = eo.get('end_date') or (eo_st + timedelta(days=20)) if eo_st else None
                            if eo_st and eo_ed:
                                # 包含 7 天預留備用期！
                                eo_buffered_end = eo_ed + timedelta(days=7)
                                if (o_st <= eo_buffered_end) and (o_end_date >= eo_st):
                                    schedule_conflict = True
                                    reject_reasons.append(f"檔期衝突(含7天備用期至{eo_buffered_end.strftime('%m/%d')})")
                                    score -= 50
                                    break
                if not schedule_conflict:
                    reasons.append("檔期無衝突")

                # 3. 照顧胎數比對
                care_babies = safe_int(s.get('care_babies', 1))
                if is_twins and care_babies < 2:
                    reject_reasons.append("不承接雙胞胎")
                    score -= 30
                else:
                    reasons.append("胎數符合")

                # 4. 可選條件過濾執行
                is_eligible = True
                if filter_region and not region_matched:
                    is_eligible = False
                if filter_schedule and schedule_conflict:
                    is_eligible = False
                if filter_babies and is_twins and care_babies < 2:
                    is_eligible = False

                if is_eligible:
                    status_prefix = "🟢 100% 匹配" if score >= 90 else ("🟡 部分匹配" if score >= 60 else "⚠️ 條件較不符")
                    reason_str = " | ".join(reasons)
                    if reject_reasons:
                        reason_str += f" (警示: {', '.join(reject_reasons)})"
                    
                    display_label = f"{s['name']} ({s.get('phone', '')}) - {status_prefix} [{reason_str}]"
                    
                    recommendations.append({
                        'staff_id': sid,
                        'name': s['name'],
                        'phone': s.get('phone'),
                        'line_user_id': s.get('line_user_id'),
                        'score': score,
                        'display_label': display_label,
                        'is_perfect': score >= 90,
                        'reasons': reasons,
                        'reject_reasons': reject_reasons
                    })

            recommendations.sort(key=lambda x: x['score'], reverse=True)
            return recommendations
    finally:
        conn.close()
