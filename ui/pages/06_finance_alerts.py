"""異常警示中心：資料匯入異常 / 流程未完成 / 帳務異常 / 服務人員 / Line。

兩套底層機制並存：
- 帳務異常：沿用原「帳務警示中心」(B6)，走 /api/v1/finance-alerts，
  finance_alerts/finance_alert_events 不可變事件溯源，保稽核軌跡。
- 資料匯入異常／流程未完成：走新的 /api/v1/system-alerts，system_alerts 是
  「滾動更新」表——同一案件同一代碼只有一列，每次掃描直接覆蓋 details 內容，
  不需要不可竄改的稽核軌跡，單純提醒同事去處理。

「流程未完成」分頁內部依處理方式分三組（LINE-001／LINE-005 已於 2026-07-27
移至「Line」分頁；ORDER-003/004「待回覆」同日移至「服務人員→待回覆接案
意願」，皆不在此處）：
- 「訂單配對」(ORDER-001/002，需要人工多步驟處理，有深連結佇列「依序前往配對處理」)
- 「待補資料」(BECLASS-001，純資料有無判斷，補齊後重新掃描會自動解除，
  不需要手動認領/解除)
- 「補發送資訊」(DOC-SEND-001 履歷未發送，單一動作可解決，per-row「發送履歷」
  按鈕直接呼叫 /api/v1/orders/{case_no}/send-resume 完成動作)
- 「帳務逾期提醒」(RECEIVABLE-001 客戶訂金/期款逾期未收、PAYOUT-001 月嫂應付款
  逾期未匯、RETURN-001 補助款應退還客戶逾期未退——判斷邏輯直接用
  client_payments/staff_payments 既有的到期日欄位，今天超過到期日且金額未結清
  即列入，不額外設寬限天數；系統本身不執行金流動作，仍需搭配銀行對帳/實際匯款
  處理後，重新掃描才會自動解除)

「資料匯入異常」分頁目前全部 5 個代碼都已接上：
IMPORT-001（HCM 欄位驗證）、IMPORT-004（服務人員 BeClass 欄位驗證）、
IMPORT-005（客戶 BeClass 欄位驗證）都是匯入當下即時寫入；IMPORT-002
（scripts/file_watcher.py 偵測到匯入腳本執行失敗）是事件型，同檔案下次
處理成功會自動解除；IMPORT-003（BeClass↔HCM 對不起來、身分證字號重複但
姓名不同）是「重新掃描」觸發的掃描型。合約簽署 (DOC-SEND-002) 仍在
「尚未接上」清單裡。

「帳務異常」分頁背後的對帳/核銷邏輯（client_receipt_reconciliation.py 等）
其實一直都存在，2026-07-27 之前缺的只是「寫成一筆看得到的 finance_alerts」
這條線，已透過 services/finance_alert_wiring.py 在
scripts/imports/import_finance_excel.py 的匯入流程中補上（12 個 §3.1
核銷代碼），詳見系統異常警示中心規格書.md §1.1／§3.1。

「服務人員」分頁（2026-07-27 新增）內部分三組：
- 「行事曆」(SCHEDULE-001/003/005/006，純資料判斷，排除後重新掃描會自動解除)
- 「帳務拆分確認」(SCHEDULE-002 月嫂中途更換人員，case_staff_assignments
  沒有「已複核」欄位可判斷是否處理完成，因此是一次性提醒，不會自動解除，
  需要在此手動認領/解除)
- 「待回覆接案意願」(ORDER-003/004，原本在「流程未完成→待回覆」，
  2026-07-27 移過來並更名；per-row「重新找月嫂」按鈕可單筆直接跳轉回配對頁面)
SCHEDULE-004（月嫂技能/胎數不符）因客戶端沒有結構化需求欄位可比對，
暫時無法偵測，仍列在「尚未接上」清單。

「Line」分頁（2026-07-27 新增，同日再把 LINE-001／LINE-005 從「流程未完成→
待補資料」移過來集中管理）：LINE-001（客戶尚未綁定 LINE）、LINE-005（服務
人員尚未綁定 LINE）、LINE-002（群組任務已推播但完全沒收到該用戶任何回覆）、
LINE-004（同一個 line_user_id 同時綁定客戶與服務人員兩種身分），皆為純資料
判斷、排除後自動解除。DOC-SEND-002（合約簽署逾期）因 orders.contract_id
沒有發送時間/簽署狀態可判斷，暫時無法偵測。
"""

from __future__ import annotations

import json
from typing import Any, Callable

import requests
import streamlit as st
from ui.pages.shared import build_admin_headers, resolve_api_base_url

from ui import nav_helper

title = "🚨 異常警示中心"
_STATUSES = ("", "open", "claimed", "resolved")

_ORDER_MATCH_CODES_WIRED = ("ORDER-001", "ORDER-002")
_ORDER_PENDING_REPLY_CODES_WIRED = ("ORDER-003", "ORDER-004")
_MISSING_DATA_CODES_WIRED = ("BECLASS-001",)
_DOC_SEND_CODES_WIRED = ("DOC-SEND-001",)
_OVERDUE_PAYMENT_CODES_WIRED = ("RECEIVABLE-001", "PAYOUT-001", "RETURN-001")
_IMPORT_CODES_WIRED = ("IMPORT-001", "IMPORT-002", "IMPORT-003", "IMPORT-004", "IMPORT-005")
_IMPORT_SCAN_CODES = ("IMPORT-003",)  # 這幾個代碼要靠「重新掃描」才會更新，其餘是匯入當下即時寫入
_LINE_TAB_CODES_WIRED = ("LINE-001", "LINE-002", "LINE-004", "LINE-005")
_SCHEDULE_AUTO_CODES_WIRED = ("SCHEDULE-001", "SCHEDULE-003", "SCHEDULE-005", "SCHEDULE-006")
_SCHEDULE_MANUAL_CODES_WIRED = ("SCHEDULE-002",)  # 沒有「已複核」欄位可判斷，一次性提醒，不自動解除
_PROCESS_CODES_PLANNED = (
    ("DOC-SEND-002", "合約已發送簽署邀請，逾期未完成簽署"),
    ("SCHEDULE-004", "月嫂技能/照顧胎數不符"),
)

_ALERT_CODE_LABELS = {
    "ORDER-001": "訂單未配對月嫂－資訊-1未發送",
    "ORDER-002": "訂單未配對月嫂－資訊-2未發送",
    "ORDER-003": "已發資訊-1但候選人未回覆",
    "ORDER-004": "已發資訊-2但候選人未回覆",
    "BECLASS-001": "客戶尚未填寫 BeClass 問卷",
    "LINE-001": "客戶尚未綁定 LINE",
    "LINE-005": "服務人員尚未綁定 LINE",
    "DOC-SEND-001": "履歷尚未發送給客戶",
    "IMPORT-001": "HCM 匯入欄位驗證失敗",
    "IMPORT-002": "File Watcher 匯入解析失敗",
    "IMPORT-003": "跨表整合去重/關聯衝突",
    "IMPORT-004": "服務人員匯入欄位驗證失敗",
    "IMPORT-005": "客戶 BeClass 匯入欄位驗證失敗",
    "RECEIVABLE-001": "客戶應收帳款逾期未收齊",
    "PAYOUT-001": "服務人員應付款逾期未匯",
    "RETURN-001": "補助款應退還客戶逾期未退",
    "LINE-002": "月嫂群組任務逾期未回覆",
    "LINE-004": "LINE 帳號重複綁定衝突",
    "SCHEDULE-001": "服務檔期跨國定假日，行政尚未決策放假與否",
    "SCHEDULE-002": "月嫂服務中途更換人員，需人工複核財務拆分",
    "SCHEDULE-003": "月嫂檔期重疊/雙重預約",
    "SCHEDULE-005": "國定假日休假偏好衝突",
    "SCHEDULE-006": "服務天數與實際排班天數不平衡",
    # 帳務金流核銷異常（3.1 節，finance_alerts）：由 services/finance_alert_wiring.py
    # 從既有對帳邏輯的 pending 結果轉寫而來，2026-07-27 接上。
    "case_not_found": "案件不存在",
    "case_not_unique": "案件不唯一/身分歧義",
    "client_receipt_single_overpay": "客戶入款金額超過剩餘應收",
    "client_payment_terms_changed": "訂單條款於收款計畫建立後變更",
    "shared_refund_account": "補助退款帳號多人共用",
    "subsidy_return_underpaid": "補助退款金額不足",
    "subsidy_return_overpaid": "補助退款溢退/超退",
    "multi_batch_same_amount_ambiguity": "政府補助同額多批次歧義",
    "staff_monthly_settlement_ambiguity": "服務人員月結單不唯一/歧義",
    "staff_payment_missing_reference": "服務人員薪資轉帳無明確參考碼",
    "staff_shared_bank_account": "服務人員領薪帳號多人共用",
    "staff_payment_amount_mismatch": "服務人員薪資轉帳金額不符",
}
_STATUS_LABELS = {
    "open": "🟡 待處理",
    "claimed": "🔵 已認領",
    "resolved": "✅ 已解決",
}
_EVENT_TYPE_LABELS = {
    "detected": "🔍 系統偵測到異常",
    "claimed": "🙋 已認領",
    "resolved": "✅ 已解除",
}
_SNAPSHOT_KEY_LABELS = {
    "case_no": "案件編號",
    "batch_ids": "匯入批次編號",
    "duplicate_count": "重複筆數",
}


def _event_type_label(event_type: Any) -> str:
    return _EVENT_TYPE_LABELS.get(event_type, str(event_type or ""))


def _alert_code_label(code: Any) -> str:
    return _ALERT_CODE_LABELS.get(code, str(code or ""))


def _status_label(status: Any) -> str:
    return _STATUS_LABELS.get(status, str(status or ""))


def _format_datetime(value: Any) -> str:
    if not value:
        return "—"
    text = str(value).replace("T", " ")
    return text[:16] if len(text) >= 16 else text


def _request(
    prefix: str,
    path: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
) -> Any:
    base_url = resolve_api_base_url()
    request_headers = headers if headers is not None else build_admin_headers()
    response = requests.request(
        method,
        f"{base_url}{prefix}{path}",
        headers=request_headers,
        params=params,
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("success", False):
        raise ValueError(body.get("error") or body.get("message") or "警示 API 請求失敗")
    return body.get("data")


def _finance_api(path: str, **kwargs: Any) -> Any:
    """帳務異常：不可變事件溯源，見 services/finance_alert_detection.py。"""
    return _request("/api/v1/finance-alerts", path, **kwargs)


def _system_api(path: str, **kwargs: Any) -> Any:
    """資料匯入異常／流程未完成：滾動更新，見 services/system_alert_service.py。"""
    return _request("/api/v1/system-alerts", path, **kwargs)


def _send_resume(case_no: str) -> Any:
    """一鍵發送履歷：呼叫 /api/v1/orders/{case_no}/send-resume（不同於警示路由前綴）。"""
    response = requests.post(
        f"{resolve_api_base_url()}/api/v1/orders/{case_no}/send-resume",
        headers=build_admin_headers(),
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("success", False):
        raise ValueError(body.get("error") or body.get("message") or "發送履歷失敗")
    return body.get("data")


def _error_text(error: Exception) -> str:
    if isinstance(error, requests.HTTPError) and error.response is not None:
        try:
            detail = error.response.json().get("detail")
        except ValueError:
            detail = error.response.text
        return f"HTTP {error.response.status_code}: {detail}"
    return str(error)


def _snapshot(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _render_alert_detail(alert: dict[str, Any]) -> None:
    source_id = alert.get("source_id") or alert.get("case_key")
    st.subheader(f"{_alert_code_label(alert.get('alert_code'))}（案件 {source_id}）")
    st.caption(
        f"狀態：{_status_label(alert.get('status'))}　｜　"
        f"更新時間：{_format_datetime(alert.get('updated_at') or alert.get('created_at'))}"
    )
    st.write(alert.get("reason") or "未提供原因")

    has_amounts = any(
        alert.get(field) is not None
        for field in ("expected_amount", "actual_amount", "difference_amount")
    )
    with st.expander("🔧 技術細節（除錯/稽核用，一般處理不需要看）", expanded=False):
        source_bits = f"{alert.get('source_domain')}"
        if alert.get("source_type"):
            source_bits += f" / {alert.get('source_type')} / {source_id}"
        else:
            source_bits += f" / {source_id}"
        st.caption(f"警示編號：{alert.get('id')}　｜　代碼：`{alert.get('alert_code')}`　｜　來源：{source_bits}")

        if has_amounts:
            st.dataframe(
                [
                    {
                        "預期金額": alert.get("expected_amount"),
                        "實際金額": alert.get("actual_amount"),
                        "差額": alert.get("difference_amount"),
                        "匯入列": alert.get("finance_import_row_id"),
                        "匯入批次": alert.get("finance_import_batch_id"),
                    }
                ],
                hide_index=True,
                width="stretch",
            )

        content = alert.get("details")
        if content is None:
            content = _snapshot(alert.get("candidate_snapshot") or {})
        if isinstance(content, dict) and content:
            st.markdown("##### 相關資訊")
            for key, value in content.items():
                st.write(f"- {_SNAPSHOT_KEY_LABELS.get(key, key)}：{value}")

        st.markdown("##### 處理歷程")
        events = alert.get("events")
        if events is not None:
            if events:
                friendly_events = [
                    {
                        "事件": _event_type_label(event.get("event_type")),
                        "處理人": event.get("actor") or "系統",
                        "原因/備註": event.get("reason") or "—",
                        "時間": _format_datetime(event.get("occurred_at")),
                    }
                    for event in events
                ]
                st.dataframe(friendly_events, hide_index=True, width="stretch")
            else:
                st.info("尚無處理歷程。")
        else:
            # system_alerts 沒有獨立事件表，從欄位本身組出簡易時間軸。
            synthetic = [
                {
                    "事件": "🔍 系統偵測到異常",
                    "處理人": "系統",
                    "原因/備註": "—",
                    "時間": _format_datetime(alert.get("created_at")),
                }
            ]
            if alert.get("claimed_by"):
                synthetic.append(
                    {
                        "事件": "🙋 已認領",
                        "處理人": alert.get("claimed_by"),
                        "原因/備註": "—",
                        "時間": _format_datetime(alert.get("claimed_at")),
                    }
                )
            if alert.get("resolved_by"):
                synthetic.append(
                    {
                        "事件": "✅ 已解除",
                        "處理人": alert.get("resolved_by"),
                        "原因/備註": alert.get("resolution_reason") or "—",
                        "時間": _format_datetime(alert.get("resolved_at")),
                    }
                )
            st.dataframe(synthetic, hide_index=True, width="stretch")


def _render_actions(alert: dict[str, Any], *, api: Callable[..., Any]) -> None:
    alert_id = alert["id"]
    st.markdown("#### 人工處理")
    st.warning("解除警示不等於完成核銷，也不會建立或修改正式資料。")
    left, right = st.columns(2)
    with left:
        with st.form(f"alert_claim_{alert_id}"):
            operator = st.text_input("認領者", key=f"claim_operator_{alert_id}")
            claim = st.form_submit_button("認領警示")
        if claim:
            if not operator.strip():
                st.error("認領者不可空白。")
            else:
                try:
                    result = api(f"/{alert_id}/claim", method="POST", payload={"operator": operator.strip()})
                except (requests.RequestException, ValueError) as error:
                    st.error(f"認領失敗：{_error_text(error)}")
                else:
                    st.success(f"認領結果：{result.get('result')}")
                    st.rerun()
    with right:
        with st.form(f"alert_resolve_{alert_id}"):
            operator = st.text_input("處理者", key=f"resolve_operator_{alert_id}")
            reason = st.text_area("解除原因（必填）", key=f"resolve_reason_{alert_id}")
            resolve = st.form_submit_button("解除警示")
        if resolve:
            if not operator.strip() or not reason.strip():
                st.error("處理者與解除原因不可空白。")
            else:
                try:
                    result = api(
                        f"/{alert_id}/resolve",
                        method="POST",
                        payload={"operator": operator.strip(), "reason": reason.strip()},
                    )
                except (requests.RequestException, ValueError) as error:
                    st.error(f"解除失敗：{_error_text(error)}")
                else:
                    st.success(f"解除結果：{result.get('result')}")
                    st.rerun()


def _render_planned_roadmap(items: tuple[tuple[str, str], ...]) -> None:
    with st.expander(f"📋 尚未接上偵測邏輯的異常類型（{len(items)} 項，見規格書）", expanded=False):
        st.caption("以下項目已在《系統異常警示中心規格書》中定義，但目前沒有程式在偵測，不會出現在上方清單中。")
        st.table([{"代碼": code, "名稱": name} for code, name in items])


def _render_alert_list_and_detail(
    alerts: list[dict[str, Any]], *, key_prefix: str, api: Callable[..., Any]
) -> None:
    """Shared list + detail + claim/resolve UI, reused by the import/finance tabs."""
    display_rows = [
        {
            "案件編號": alert.get("source_id") or alert.get("case_key"),
            "異常類型": _alert_code_label(alert.get("alert_code")),
            "狀態": _status_label(alert.get("status")),
            "說明": alert.get("reason"),
            "建立時間": _format_datetime(alert.get("created_at")),
        }
        for alert in alerts
    ]
    st.dataframe(display_rows, hide_index=True, width="stretch")

    options = [None, *[alert["id"] for alert in alerts]]
    label_by_id = {
        alert["id"]: f"案件 {alert.get('source_id') or alert.get('case_key')}｜{_alert_code_label(alert.get('alert_code'))}"
        for alert in alerts
    }
    selected_id = st.selectbox(
        "選擇要檢視/處理的案件",
        options,
        format_func=lambda value: "請選擇案件" if value is None else label_by_id.get(value, f"案件 #{value}"),
        key=f"{key_prefix}_detail_picker",
    )
    if selected_id is None:
        return
    try:
        alert = api(f"/{selected_id}")
    except (requests.RequestException, ValueError) as error:
        st.error(f"無法讀取警示詳情：{_error_text(error)}")
        return
    _render_alert_detail(alert)
    _render_actions(alert, api=api)


def _render_import_tab() -> None:
    st.subheader("📥 資料匯入異常")
    st.caption("Excel／表單匯入的欄位格式錯誤、跨表整合去重衝突。")
    st.caption(
        f"其中 {'、'.join(_alert_code_label(c) for c in _IMPORT_SCAN_CODES)} "
        "是掃描型偵測，補齊資料後要按下方按鈕重新掃描才會更新。"
    )

    if st.button("🔄 重新掃描（跨表整合去重/關聯衝突）", key="import_rescan_btn"):
        try:
            summary = _system_api("/scan", method="POST")
        except (requests.RequestException, ValueError) as error:
            st.error(f"掃描失敗：{_error_text(error)}")
        else:
            relevant = {code: stats for code, stats in summary.items() if code in _IMPORT_SCAN_CODES}
            st.success(
                "掃描完成："
                + "；".join(
                    f"{_alert_code_label(code)} 新增{stats['created']}／更新{stats['updated']}／自動解除{stats['resolved']}"
                    for code, stats in relevant.items()
                )
            )
            st.rerun()

    alerts: list[dict[str, Any]] = []
    for code in _IMPORT_CODES_WIRED:
        params: dict[str, Any] = {"alert_code": code, "limit": 200, "offset": 0}
        try:
            alerts.extend(_system_api("", params=params) or [])
        except (requests.RequestException, ValueError) as error:
            st.error(f"無法讀取 {_alert_code_label(code)}：{_error_text(error)}")
            return
    alerts.sort(key=lambda alert: alert.get("updated_at") or alert.get("created_at") or "", reverse=True)

    if not alerts:
        st.info("目前沒有資料匯入類異常。")
    else:
        _render_alert_list_and_detail(alerts, key_prefix="import", api=_system_api)


def _render_process_tab() -> None:
    st.subheader("📋 流程未完成")
    st.caption("訂單配對、BeClass 問卷、資訊/履歷/合約發送等流程提醒。LINE 相關提醒已移至「Line」分頁。")

    if st.button("🔄 重新掃描（訂單配對／BeClass／帳務逾期提醒）", key="process_rescan_btn"):
        try:
            summary = _system_api("/scan", method="POST")
        except (requests.RequestException, ValueError) as error:
            st.error(f"掃描失敗：{_error_text(error)}")
        else:
            st.success(
                "掃描完成："
                + "；".join(
                    f"{_alert_code_label(code)} 新增{stats['created']}／更新{stats['updated']}／自動解除{stats['resolved']}"
                    for code, stats in summary.items()
                )
            )
            st.rerun()

    def _fetch(codes: tuple[str, ...], *, status: str | None) -> list[dict[str, Any]] | None:
        rows: list[dict[str, Any]] = []
        for code in codes:
            params: dict[str, Any] = {"alert_code": code, "limit": 200, "offset": 0}
            if status:
                params["status"] = status
            try:
                rows.extend(_system_api("", params=params) or [])
            except (requests.RequestException, ValueError) as error:
                st.error(f"無法讀取 {_alert_code_label(code)} 警示：{_error_text(error)}")
                return None
        rows.sort(key=lambda alert: alert.get("updated_at") or alert.get("created_at") or "", reverse=True)
        return rows

    # 各分類先用「open」狀態預抓一次筆數，只為了在下面的子分頁標籤上顯示數字，
    # 讓使用者不用點進去、不用往下滑就能看出哪個分類有事要處理。
    order_open_count = _fetch(_ORDER_MATCH_CODES_WIRED, status="open")
    missing_open_count = _fetch(_MISSING_DATA_CODES_WIRED, status="open")
    send_open_count = _fetch(_DOC_SEND_CODES_WIRED, status="open")
    overdue_open_count = _fetch(_OVERDUE_PAYMENT_CODES_WIRED, status="open")
    if None in (order_open_count, missing_open_count, send_open_count, overdue_open_count):
        return

    tab_order, tab_missing, tab_send, tab_overdue = st.tabs([
        f"🤝 訂單配對 ({len(order_open_count)})",
        f"📝 待補資料 ({len(missing_open_count)})",
        f"📤 補發送資訊 ({len(send_open_count)})",
        f"💸 帳務逾期提醒 ({len(overdue_open_count)})",
    ])

    with tab_order:
        st.caption("需要人工多步驟處理，點任一筆的「前往配對」即可依序跳轉處理全部案件。")
        order_alerts = order_open_count
        if not order_alerts:
            st.info("目前沒有訂單配對類異常，可先按上方按鈕重新掃描一次。")
        else:
            def _start_order_queue() -> None:
                queue_items = [{"case_no": alert.get("source_id") or alert.get("case_key")} for alert in order_alerts]
                nav_helper.navigate_to(
                    "多月嫂排班",
                    queue_items=queue_items,
                    queue_target_key="multi_caregiver_matching_case_picker",
                )

            header_cols = st.columns([1.6, 1.4, 2.8, 3.6])
            header_cols[0].markdown("**前往配對**")
            header_cols[1].markdown("**案件編號**")
            header_cols[2].markdown("**階段**")
            header_cols[3].markdown("**說明**")
            for alert in order_alerts:
                row_cols = st.columns([1.6, 1.4, 2.8, 3.6])
                if row_cols[0].button("🎯 前往配對", key=f"order_queue_{alert['id']}"):
                    _start_order_queue()
                row_cols[1].write(alert.get("source_id") or alert.get("case_key"))
                row_cols[2].write(_alert_code_label(alert.get("alert_code")))
                row_cols[3].write(alert.get("reason"))

    with tab_missing:
        st.caption("純粹是資料還沒補齊，補齊後按「重新掃描」會自動解除，不需要手動認領/處理。")
        missing_alerts = missing_open_count
        if not missing_alerts:
            st.info("目前沒有待補資料類異常。")
        else:
            display_rows = [
                {
                    "案件編號": alert.get("source_id") or alert.get("case_key"),
                    "缺少項目": _alert_code_label(alert.get("alert_code")),
                    "說明": alert.get("reason"),
                    "建立時間": _format_datetime(alert.get("created_at")),
                }
                for alert in missing_alerts
            ]
            st.dataframe(display_rows, hide_index=True, width="stretch")

    with tab_send:
        st.caption("單一動作可直接解決，按下按鈕即完成發送，不用跳轉頁面。")
        send_alerts = send_open_count
        if not send_alerts:
            st.info("目前沒有待補發送的資訊。")
        else:
            header_cols = st.columns([1.6, 1.4, 2.2, 3.6])
            header_cols[0].markdown("**發送資訊**")
            header_cols[1].markdown("**案件編號**")
            header_cols[2].markdown("**項目**")
            header_cols[3].markdown("**說明**")
            for alert in send_alerts:
                row_cols = st.columns([1.6, 1.4, 2.2, 3.6])
                if row_cols[0].button("📨 發送履歷", key=f"send_resume_{alert['id']}"):
                    try:
                        _send_resume(alert.get("source_id") or alert.get("case_key"))
                        _system_api("/scan", method="POST")  # 立即重新掃描，讓這筆從清單消失，不用使用者再多按一次
                    except (requests.RequestException, ValueError) as error:
                        st.error(f"發送失敗：{_error_text(error)}")
                    else:
                        st.success("履歷已發送。")
                        st.rerun()
                row_cols[1].write(alert.get("source_id") or alert.get("case_key"))
                row_cols[2].write(_alert_code_label(alert.get("alert_code")))
                row_cols[3].write(alert.get("reason"))

    with tab_overdue:
        st.caption("客戶訂金／期款、月嫂應付款、補助退還款已過各自到期日但尚未結清，需搭配銀行對帳/實際匯款處理。")
        overdue_alerts = overdue_open_count
        if not overdue_alerts:
            st.info("目前沒有帳務逾期類提醒。")
        else:
            display_rows = [
                {
                    "案件編號": alert.get("source_id") or alert.get("case_key"),
                    "項目": _alert_code_label(alert.get("alert_code")),
                    "說明": alert.get("reason"),
                    "建立時間": _format_datetime(alert.get("created_at")),
                }
                for alert in overdue_alerts
            ]
            st.dataframe(display_rows, hide_index=True, width="stretch")

    _render_planned_roadmap(_PROCESS_CODES_PLANNED)


def _render_line_tab() -> None:
    st.subheader("📱 Line")
    st.caption("客戶/服務人員尚未綁定 LINE、帳號身分衝突、群組任務逾期未回覆。純資料判斷，情況排除後重新掃描會自動解除。")

    if st.button("🔄 重新掃描（Line）", key="line_rescan_btn"):
        try:
            summary = _system_api("/scan", method="POST")
        except (requests.RequestException, ValueError) as error:
            st.error(f"掃描失敗：{_error_text(error)}")
        else:
            filtered = {code: stats for code, stats in summary.items() if code in _LINE_TAB_CODES_WIRED}
            st.success(
                "掃描完成："
                + "；".join(
                    f"{_alert_code_label(code)} 新增{stats['created']}／更新{stats['updated']}／自動解除{stats['resolved']}"
                    for code, stats in filtered.items()
                )
            )
            st.rerun()

    alerts: list[dict[str, Any]] = []
    for code in _LINE_TAB_CODES_WIRED:
        try:
            alerts.extend(_system_api("", params={"alert_code": code, "status": "open", "limit": 200, "offset": 0}) or [])
        except (requests.RequestException, ValueError) as error:
            st.error(f"無法讀取 {_alert_code_label(code)} 警示：{_error_text(error)}")
            return
    if not alerts:
        st.info("目前沒有 Line 相關異常。")
        return
    alerts.sort(key=lambda alert: alert.get("updated_at") or alert.get("created_at") or "", reverse=True)
    display_rows = [
        {
            "識別碼": alert.get("source_id") or alert.get("case_key"),
            "項目": _alert_code_label(alert.get("alert_code")),
            "說明": alert.get("reason"),
            "建立時間": _format_datetime(alert.get("created_at")),
        }
        for alert in alerts
    ]
    st.dataframe(display_rows, hide_index=True, width="stretch")


def _render_staff_tab() -> None:
    st.subheader("👤 服務人員")
    st.caption("月嫂排班/檔期類提醒，以及需要人工複核的財務拆分事項。")

    if st.button("🔄 重新掃描（服務人員）", key="staff_rescan_btn"):
        try:
            summary = _system_api("/scan", method="POST")
        except (requests.RequestException, ValueError) as error:
            st.error(f"掃描失敗：{_error_text(error)}")
        else:
            filtered = {
                code: stats
                for code, stats in summary.items()
                if code in _SCHEDULE_AUTO_CODES_WIRED + _SCHEDULE_MANUAL_CODES_WIRED + _ORDER_PENDING_REPLY_CODES_WIRED
            }
            st.success(
                "掃描完成："
                + "；".join(
                    f"{_alert_code_label(code)} 新增{stats['created']}／更新{stats['updated']}／自動解除{stats['resolved']}"
                    for code, stats in filtered.items()
                )
            )
            st.rerun()

    def _fetch_open(codes: tuple[str, ...]) -> list[dict[str, Any]] | None:
        rows: list[dict[str, Any]] = []
        for code in codes:
            try:
                rows.extend(_system_api("", params={"alert_code": code, "status": "open", "limit": 200, "offset": 0}) or [])
            except (requests.RequestException, ValueError) as error:
                st.error(f"無法讀取 {_alert_code_label(code)} 警示：{_error_text(error)}")
                return None
        rows.sort(key=lambda alert: alert.get("updated_at") or alert.get("created_at") or "", reverse=True)
        return rows

    calendar_alerts = _fetch_open(_SCHEDULE_AUTO_CODES_WIRED)
    split_alerts = _fetch_open(_SCHEDULE_MANUAL_CODES_WIRED)
    pending_reply_alerts = _fetch_open(_ORDER_PENDING_REPLY_CODES_WIRED)
    if calendar_alerts is None or split_alerts is None or pending_reply_alerts is None:
        return

    tab_calendar, tab_split, tab_pending_reply = st.tabs([
        f"📅 行事曆 ({len(calendar_alerts)})",
        f"💰 帳務拆分確認 ({len(split_alerts)})",
        f"⏳ 待回覆接案意願 ({len(pending_reply_alerts)})",
    ])

    with tab_calendar:
        st.caption("檔期重疊、國定假日排班決策/偏好衝突、服務天數不平衡。純資料判斷，排除後重新掃描會自動解除。")
        if not calendar_alerts:
            st.info("目前沒有行事曆類異常。")
        else:
            display_rows = [
                {
                    "識別碼": alert.get("source_id") or alert.get("case_key"),
                    "項目": _alert_code_label(alert.get("alert_code")),
                    "說明": alert.get("reason"),
                    "建立時間": _format_datetime(alert.get("created_at")),
                }
                for alert in calendar_alerts
            ]
            st.dataframe(display_rows, hide_index=True, width="stretch")

    with tab_split:
        st.caption("月嫂中途更換人員，需人工複核財務拆分是否已處理。這項沒有資料可判斷「是否已處理」，"
                   "確認完成後請手動認領/解除，重新掃描不會自動關閉。")
        if not split_alerts:
            st.info("目前沒有待複核的財務拆分事項。")
        else:
            _render_alert_list_and_detail(split_alerts, key_prefix="staff_split", api=_system_api)

    with tab_pending_reply:
        st.caption("訂單資訊已經發出去，正在等候選人回覆接案意願。可單筆直接跳轉回配對頁面（例如追加候選人）。")
        if not pending_reply_alerts:
            st.info("目前沒有待回覆中的配對案件。")
        else:
            header_cols = st.columns([1.6, 1.4, 2.2, 3.6])
            header_cols[0].markdown("**操作**")
            header_cols[1].markdown("**案件編號**")
            header_cols[2].markdown("**狀態**")
            header_cols[3].markdown("**說明**")
            for alert in pending_reply_alerts:
                row_cols = st.columns([1.6, 1.4, 2.2, 3.6])
                if row_cols[0].button("🔄 重新找月嫂", key=f"requeue_{alert['id']}"):
                    nav_helper.navigate_to(
                        "多月嫂排班",
                        queue_items=[{"case_no": alert.get("source_id") or alert.get("case_key")}],
                        queue_target_key="multi_caregiver_matching_case_picker",
                    )
                row_cols[1].write(alert.get("source_id") or alert.get("case_key"))
                row_cols[2].write(_alert_code_label(alert.get("alert_code")))
                row_cols[3].write(alert.get("reason"))


def _render_finance_tab() -> None:
    st.subheader("💰 帳務異常")
    st.caption("CLIENT、RETURN、SUBSIDY、STAFF 與 COMMON 警示的人工檢視入口。")

    filter_left, filter_right, filter_domain = st.columns(3)
    with filter_left:
        status = st.selectbox("狀態", _STATUSES, format_func=lambda value: value or "全部", key="finance_status_filter")
    with filter_right:
        alert_code = st.text_input("警示代碼", key="finance_alert_code_filter")
    with filter_domain:
        source_domain = st.text_input("來源領域", key="finance_source_domain_filter")
    limit = st.number_input("每頁筆數", min_value=1, max_value=200, value=50, key="finance_limit")

    params: dict[str, Any] = {"limit": int(limit), "offset": 0}
    if status:
        params["status"] = status
    if alert_code.strip():
        params["alert_code"] = alert_code.strip()
    if source_domain.strip():
        params["source_domain"] = source_domain.strip()

    try:
        alerts = _finance_api("", params=params)
    except (requests.RequestException, ValueError) as error:
        st.error(f"無法讀取帳務警示：{_error_text(error)}")
        return

    if not alerts:
        st.info("目前沒有符合條件的警示。")
        return

    _render_alert_list_and_detail(alerts, key_prefix="finance", api=_finance_api)


def show() -> None:
    st.title(title)
    st.caption("統一收攏所有需要行政人員人工檢視、認領、處理的異常情境，取代原本僅限帳務金流的帳務警示中心。")
    tab_import, tab_process, tab_finance, tab_staff, tab_line = st.tabs(
        ["📥 資料匯入異常", "📋 流程未完成", "💰 帳務異常", "👤 服務人員", "📱 Line"]
    )
    with tab_import:
        _render_import_tab()
    with tab_process:
        _render_process_tab()
    with tab_finance:
        _render_finance_tab()
    with tab_staff:
        _render_staff_tab()
    with tab_line:
        _render_line_tab()
