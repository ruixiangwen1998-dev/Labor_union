"""服務人員 BeClass 匯入欄位層級驗證規則，供
scripts/imports/import_staff_beclass.py 使用。

沿用 client_import_validation.py 的通用小工具（電話正規化、查無識別碼時的
替代鍵），縣市清單額外容忍「臺」的寫法，因為這支匯入腳本自己的
clean_city_and_address() 是正規化成「臺」而不是「台」。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pandas as pd

from services.client_import_validation import (
    VALID_CITIES,
    _is_blank,
    _normalize_phone_digits,
    fallback_case_key,
)

IDENTITY_CARD_PATTERN = re.compile(r"^[A-Za-z]\d{9}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BANK_BRANCH_PATTERN = re.compile(r"^\d{7}$")

VALID_CITIES_BOTH = set(VALID_CITIES) | {c.replace("台", "臺") for c in VALID_CITIES}

# Excel 欄位名稱 -> staff 資料表欄位名稱，驗證失敗時要把這個 DB 欄位存成 NULL。
# 姓名不在這份清單裡：staff.name 是 NOT NULL，缺姓名時整列直接不寫入，不是存 NULL。
# 銀行代碼/帳號也不在這份清單裡：那兩欄寫在 staff_bank_accounts，不是 staff 表本身的欄位。
EXCEL_TO_DB_COLUMN = {
    "IP位址": "ip_address",
    "行動電話": "phone",
    "EMAIL": "email",
    "縣市": "city",
}


def _has_resolvable_birthday(row: dict[str, Any]) -> bool:
    """比照 import_staff_beclass.py 的生日解析順序：先看合併欄位，再看拆開的年/月/日。"""
    combined = row.get("民國出生年月日")
    if not _is_blank(combined):
        return True
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


def validate_staff_row(row: dict[str, Any]) -> dict[str, str]:
    """檢查一列服務人員原始 Excel 資料，回傳 {欄位名稱: 錯誤說明}；乾淨則回傳空字典。"""
    errors: dict[str, str] = {}

    if _is_blank(row.get("姓名")):
        errors["姓名"] = "不可空值"

    identity_card = row.get("身分證字號")
    if _is_blank(identity_card):
        errors["身分證字號"] = "不可空值"
    elif not IDENTITY_CARD_PATTERN.match(str(identity_card).strip()):
        errors["身分證字號"] = f"格式需為1碼英文字母+9碼數字：{identity_card}"

    if _is_blank(row.get("IP位址")):
        errors["IP位址"] = "不可空值"

    if _is_blank(row.get("報名時間")):
        errors["報名時間"] = "不可空值"

    if not _has_resolvable_birthday(row):
        errors["民國出生年月日"] = "不可空值，且需能解析成合法日期"

    phone = row.get("行動電話")
    if _is_blank(phone):
        errors["行動電話"] = "不可空值，需為09開頭的10碼字串"
    else:
        phone_digits = _normalize_phone_digits(phone)
        if not re.match(r"^09\d{8}$", phone_digits):
            errors["行動電話"] = f"需要09開頭的10碼字串：{phone}"

    email = row.get("EMAIL")
    if not _is_blank(email) and not EMAIL_PATTERN.match(str(email).strip()):
        errors["EMAIL"] = f"格式不正確：{email}"

    city = row.get("縣市")
    if not _is_blank(city) and str(city).strip() not in VALID_CITIES_BOTH:
        errors["縣市"] = f"不在縣市清單中：{city}"

    bank_account = row.get("銀行帳號")
    if not _is_blank(bank_account):
        bank_branch = row.get("銀行代3碼+分行代號4碼")
        if _is_blank(bank_branch):
            bank_branch = row.get("銀行代碼3碼+分行代號4碼")
        if not _is_blank(bank_branch):
            digits = re.sub(r"\D", "", str(bank_branch))
            if not BANK_BRANCH_PATTERN.match(digits):
                errors["銀行代3碼+分行代號4碼"] = f"值需為7碼數字（3碼銀行代碼+4碼分行代號）：{bank_branch}"

    return errors


__all__ = ["validate_staff_row", "fallback_case_key", "EXCEL_TO_DB_COLUMN"]
