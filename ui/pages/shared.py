"""共用 UI API 工具函式。"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

API_BASE_URL_ENV = "API_BASE_URL"
API_BASE_URL_DEFAULT = "http://localhost:8000"
INTERNAL_API_KEY_ENV = "INTERNAL_API_KEY"
ADMIN_ACCESS_TOKEN_KEY = "line_admin_access_token"
DEVELOPMENT_ENVIRONMENTS = {"development", "dev", "local", "test"}


def resolve_api_base_url() -> str:
    """在 runtime 解析 API_BASE_URL，支援執行過程中動態變更。"""
    value = (os.getenv(API_BASE_URL_ENV, API_BASE_URL_DEFAULT) or API_BASE_URL_DEFAULT).strip()
    return value.rstrip("/")


def admin_auth_is_bypassed() -> bool:
    """與 backend 相同：只有明確的開發環境設定可省略 Bearer session。"""
    app_env = (os.getenv("APP_ENV", "development") or "development").strip().lower()
    enabled = (os.getenv("ENABLE_ADMIN_AUTH", "true") or "true").strip().lower()
    return app_env in DEVELOPMENT_ENVIRONMENTS and enabled in {"0", "false", "no", "off"}


def resolve_internal_api_key() -> str:
    value = (os.getenv(INTERNAL_API_KEY_ENV, "") or "").strip()
    if not value:
        raise RuntimeError("缺少 INTERNAL_API_KEY，無法呼叫管理員 API。")
    return value


def resolve_admin_access_token() -> str:
    try:
        value = st.session_state.get(ADMIN_ACCESS_TOKEN_KEY)
    except Exception:
        value = None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("缺少有效的管理員 Session，請先登入管理後台。")
    return value.strip()


def build_admin_headers() -> dict[str, str]:
    headers = {"X-Internal-API-Key": resolve_internal_api_key()}
    if not admin_auth_is_bypassed():
        headers["Authorization"] = f"Bearer {resolve_admin_access_token()}"
    return headers
