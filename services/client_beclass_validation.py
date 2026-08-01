"""客戶 BeClass 匯入欄位層級驗證規則，供
scripts/imports/import_client_beclass.py 使用。

沿用 client_import_validation.py 的通用小工具（縣市清單、電話正規化、查無
識別碼時的替代鍵）。這支匯入腳本的 clean_city_and_address() 正規化成「台」，
跟 HCM 那支一致，不用像服務人員那支額外容忍「臺」。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from services.client_import_validation import (
    VALID_CITIES,
    _is_blank,
    _normalize_phone_digits,
    fallback_case_key,
)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BANK_BRANCH_PATTERN = re.compile(r"^\d{7}$")

# Excel 欄位名稱 -> beclass_records 資料表欄位名稱，驗證失敗時要把這個 DB 欄位存成 NULL。
# 查詢序號不在這份清單裡：那是查無資料時整列不寫入的硬性條件，不是存 NULL 就好。
EXCEL_TO_DB_COLUMN = {
    "報名時間": "created_at",
    "姓名": "name",
    "Email": "email",
    "行動電話": "phone",
    "縣市": "city",
}


def _has_resolvable_birth_date(row: dict[str, Any]) -> bool:
    year, month, day = row.get("出生年"), row.get("月"), row.get("日")
    if _is_blank(year) or _is_blank(month) or _is_blank(day):
        return False
    try:
        y, m, d = int(year), int(month), int(day)
        if y < 1900:
            y += 1911
        date(y, m, d)
        return True
    except (ValueError, TypeError):
        return False


def validate_client_beclass_row(row: dict[str, Any]) -> dict[str, str]:
    """檢查一列客戶 BeClass 原始 Excel 資料，回傳 {欄位名稱: 錯誤說明}；乾淨則回傳空字典。"""
    errors: dict[str, str] = {}

    if _is_blank(row.get("查詢序號")):
        errors["查詢序號"] = "不可空值"

    if _is_blank(row.get("姓名")):
        errors["姓名"] = "不可空值"

    if _is_blank(row.get("報名時間")):
        errors["報名時間"] = "不可空值"

    phone = row.get("行動電話")
    if _is_blank(phone):
        errors["行動電話"] = "不可空值，需為09開頭的10碼字串"
    else:
        phone_digits = _normalize_phone_digits(phone)
        if not re.match(r"^09\d{8}$", phone_digits):
            errors["行動電話"] = f"需要09開頭的10碼字串：{phone}"

    email = row.get("Email")
    if not _is_blank(email) and not EMAIL_PATTERN.match(str(email).strip()):
        errors["Email"] = f"格式不正確：{email}"

    city = row.get("縣市")
    if not _is_blank(city) and str(city).strip() not in VALID_CITIES:
        errors["縣市"] = f"不在縣市清單中：{city}"

    if not _has_resolvable_birth_date(row):
        errors["出生年"] = "出生年/月/日不可空值，且需能解析成合法日期"

    refund_account = row.get("銀行帳號")
    if not _is_blank(refund_account):
        refund_bank_code = row.get("補助款退款:銀行代號+分行代號")
        if not _is_blank(refund_bank_code):
            digits = re.sub(r"\D", "", str(refund_bank_code))
            if not BANK_BRANCH_PATTERN.match(digits):
                errors["補助款退款:銀行代號+分行代號"] = (
                    f"值需為7碼數字（3碼銀行代碼+4碼分行代號）：{refund_bank_code}"
                )

    return errors


__all__ = ["validate_client_beclass_row", "fallback_case_key", "EXCEL_TO_DB_COLUMN"]
