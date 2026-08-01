"""
================================================================================
檔案名稱: ui/pages/order/shared.py
功能說明: Order UI 子模組共用 Helper 與 HTTP API Client
================================================================================
"""

import math
import requests
from datetime import datetime, date, timedelta
from calendar import monthrange
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


def _month_index(date_value: date, offset: int) -> date:
    month_index = date_value.year * 12 + (date_value.month - 1) + offset
    year = month_index // 12
    month = month_index % 12 + 1
    return datetime(year=year, month=month, day=15).date()


def _derive_service_end_date(order: dict) -> date | None:
    actual_end = _parse_date(order.get("actual_end_date"))
    if actual_end:
        return actual_end

    actual_start = _parse_date(order.get("actual_start_date"))
    service_days = safe_int(order.get("service_days"))
    if not actual_start or not service_days:
        return None
    return actual_start + timedelta(days=max(service_days - 1, 0))


def _derive_staff_payment_date(order: dict) -> str:
    """依實際服務起日與身份類別，預估服務人員付款日。"""
    end_date = _derive_service_end_date(order)
    if not end_date:
        return ""

    identity_status = str(order.get("identity_status") or "").strip()
    month_delta = 2 if identity_status == "補助市民" else 1
    return _month_index(end_date, month_delta).isoformat()


def _derive_subsidy_refund_date(order: dict) -> str:
    """依實際服務起日與身份類別，預估補助退款日。"""
    end_date = _derive_service_end_date(order)
    identity_status = str(order.get("identity_status") or "").strip()
    if not end_date or identity_status == "非市民":
        return ""

    month_end_day = monthrange(end_date.year, end_date.month)[1]
    return (datetime(end_date.year, end_date.month, month_end_day).date() + timedelta(days=5)).isoformat()


def _payment_api_request(path, method="GET", payload=None):
    """Access payment ledgers only through FastAPI; never write summary columns directly."""
    base_url = resolve_api_base_url()
    response = requests.request(
        method,
        f"{base_url}/api/v1{path}",
        headers=build_admin_headers(),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("data")


def _finance_report_request(path, params=None, download=False):
    """Read finance reports exclusively through the FastAPI router."""
    base_url = resolve_api_base_url()
    response = requests.get(
        f"{base_url}/api/v1/finance-reports{path}",
        headers=build_admin_headers(),
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.content if download else (response.json().get("data") or {})
