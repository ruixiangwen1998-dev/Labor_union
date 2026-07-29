"""
================================================================================
檔案名稱: ui/components/line_health_monitor.py
功能說明: LINE 管理中心主動監控總覽，顯示細分狀態、最後檢查時間與異常／恢復紀錄
================================================================================
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st


TAIPEI = ZoneInfo("Asia/Taipei")
STATUS_LABELS = {
    "healthy": "正常",
    "warning": "注意",
    "critical": "異常",
    "unknown": "尚待確認",
    "maintenance": "維護中",
}
STATUS_ICONS = {
    "healthy": "✅",
    "warning": "⚠️",
    "critical": "🔴",
    "unknown": "❔",
    "maintenance": "🛠️",
}


def _taipei_time(value: Any) -> str:
    if not value:
        return "尚未檢查"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")


def render_line_health_monitor(overview: dict[str, Any], events: list[dict[str, Any]]) -> None:
    overall = overview.get("overall_status", "unknown")
    checks = overview.get("checks") or {}
    generated_at = overview.get("generated_at")

    metric_columns = st.columns(4)
    metric_columns[0].metric("整體狀態", STATUS_LABELS.get(overall, overall))
    metric_columns[1].metric("自動發送", STATUS_LABELS.get((checks.get("worker") or {}).get("status", "unknown")))
    metric_columns[2].metric("資料連線", STATUS_LABELS.get((checks.get("database") or {}).get("status", "unknown")))
    abnormal_count = sum(1 for item in checks.values() if item.get("status") in {"warning", "critical"})
    metric_columns[3].metric("需要注意", abnormal_count)

    if overview.get("monitor_stale"):
        st.error("監控資料已超過 90 秒沒有更新，獨立監控程序可能已停止。")
    elif overall == "healthy":
        st.success("所有已啟用的 LINE 服務檢查目前正常。")
    elif overall == "warning":
        st.warning("部分服務需要注意，系統正在持續重新確認。")
    elif overall == "critical":
        st.error("偵測到重要服務異常，請查看下方項目。")
    else:
        st.info("監控程序正在建立第一次檢查結果。")

    st.caption(f"最後監控時間：{_taipei_time(generated_at)}。監控由獨立程序持續執行，本頁不需要固定重新整理。")
    if st.button("立即更新畫面", key="line_health_refresh"):
        st.rerun()

    rows = []
    for name, item in checks.items():
        status = item.get("status", "unknown")
        rows.append(
            {
                "服務": item.get("component") or name,
                "狀態": f"{STATUS_ICONS.get(status, '')} {STATUS_LABELS.get(status, status)}",
                "說明": item.get("message", ""),
                "回應時間": f"{item['response_ms']} ms" if item.get("response_ms") is not None else "-",
                "最後檢查": _taipei_time(item.get("checked_at") or item.get("last_checked_at")),
            }
        )
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    with st.expander("異常與恢復紀錄"):
        if not events:
            st.caption("目前沒有監控異常紀錄。")
        else:
            event_rows = [
                {
                    "服務": item.get("component") or "-",
                    "等級": "異常" if item.get("severity") == "critical" else "注意",
                    "狀態": "已恢復" if item.get("status") == "resolved" else "處理中",
                    "說明": item.get("description") or "",
                    "首次發現": _taipei_time(item.get("first_detected_at")),
                    "最近發現": _taipei_time(item.get("last_detected_at")),
                    "發生次數": item.get("occurrence_count") or 1,
                }
                for item in events
            ]
            st.dataframe(pd.DataFrame(event_rows), use_container_width=True, hide_index=True)
