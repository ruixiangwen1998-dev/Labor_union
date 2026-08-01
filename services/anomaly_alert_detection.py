"""Detection for non-finance "process reminder" anomalies, stored in system_alerts.

Wired: ORDER-001~004 (the four mutually-exclusive stages of the 訂單配對
pipeline), BECLASS-001, LINE-001, LINE-005, DOC-SEND-001, IMPORT-003,
RECEIVABLE-001 (客戶訂金／第一期／第二期逾期未收), PAYOUT-001 (服務人員
應付款逾期未匯), RETURN-001 (補助款應退還客戶逾期未退). Each scan function
recomputes the full current state and rolling-updates system_alerts via
upsert_system_alert()/resolve_absent_alerts() -- unlike finance_alerts,
system_alerts allows in-place updates, so there is no immutability dance to
work around here.

RECEIVABLE-001/PAYOUT-001/RETURN-001 use the due-date columns already stored
per case (deposit_due_date 等) instead of an arbitrary global grace period --
once today passes that case's own due date and the amount isn't settled, it
is flagged immediately, matching the ORDER-/BECLASS- precedent of no buffer.

IMPORT-001 (HCM 匯入欄位驗證) is NOT scanned here -- it's written inline, row
by row, from scripts/imports/import_client_hcm.py at import time, since it's
triggered by a file arriving rather than a periodic full-table rescan.

LINE-002/LINE-004/SCHEDULE-001/003/005/006 follow the same auto-resolving
presence-check pattern as everything above. SCHEDULE-002 is the one
exception: case_staff_assignments has no "已複核" column, so there is no
data-driven signal that the finance split was actually reviewed -- per
2026-07-27 決議 it is a one-time reminder that a human must claim/resolve by
hand in the alert center; the scan never auto-resolves it, and deliberately
skips re-opening a case_key that a human already resolved.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from services.system_alert_service import resolve_absent_alerts, upsert_system_alert


def _scan_presence_check(
    cursor: Any,
    *,
    alert_code: str,
    source_domain: str,
    query_sql: str,
    reason_template: str,
    resolve_reason: str,
) -> dict[str, int]:
    """Shared shape for "case_nos where a data-presence condition currently holds".

    query_sql must return one row per affected case with a `case_no` column.
    """
    cursor.execute(query_sql)
    rows = cursor.fetchall()
    open_case_nos = {row["case_no"] for row in rows}
    created = updated = 0
    for row in rows:
        case_no = row["case_no"]
        result = upsert_system_alert(
            cursor,
            alert_code=alert_code,
            source_domain=source_domain,
            case_key=case_no,
            reason=reason_template.format(case_no=case_no),
            details={"case_no": case_no},
        )
        if result["result"] == "created":
            created += 1
        else:
            updated += 1
    resolved = resolve_absent_alerts(
        cursor,
        alert_code=alert_code,
        still_open_case_keys=open_case_nos,
        reason=resolve_reason,
    )
    return {"created": created, "updated": updated, "resolved": resolved}


_ORDER_PIPELINE_REASONS = {
    "ORDER-001": "案件 {case_no} 尚未發送訂單資訊-1給任何候選月嫂",
    "ORDER-002": "案件 {case_no} 已有候選月嫂願意接案，但尚未發送訂單資訊-2",
    "ORDER-003": "案件 {case_no} 已發送訂單資訊-1，候選月嫂尚未回覆意願",
    "ORDER-004": "案件 {case_no} 已發送訂單資訊-2，尚待後續回覆與定案",
}


def _fetch_unmatched_orders_with_matches(cursor: Any) -> dict[str, list[dict[str, Any]]]:
    cursor.execute(
        """SELECT o.case_no, m.id AS match_id, m.caregiver_accepted,
                  m.sent_info_1_at, m.sent_info_2_at
           FROM orders o
           LEFT JOIN matching_records m ON m.case_no = o.case_no
           WHERE o.status = '洽談中' AND o.staff_id IS NULL"""
    )
    by_case: dict[str, list[dict[str, Any]]] = {}
    for row in cursor.fetchall():
        matches = by_case.setdefault(row["case_no"], [])
        if row["match_id"] is not None:
            matches.append(row)
    return by_case


def _classify_order_matching_state(matches: list[dict[str, Any]]) -> str:
    """Classify one unmatched order's candidate pipeline into exactly one stage.

    Priority: no one has info-1 yet -> 001. Info-1 sent but nobody has
    replied yet (and nobody accepted) -> 003. Someone accepted but info-2
    hasn't reached them -> 002. Info-2 already sent to an accepted
    candidate -> 004 (waiting on the finalize step, no further reply field
    exists in the schema to track past this point).
    """
    info1_sent = [m for m in matches if m["sent_info_1_at"] is not None]
    if not info1_sent:
        return "ORDER-001"
    accepted = [m for m in info1_sent if m["caregiver_accepted"] == 1]
    if not accepted:
        pending = [m for m in info1_sent if m["caregiver_accepted"] is None]
        return "ORDER-003" if pending else "ORDER-001"
    info2_sent = [m for m in accepted if m["sent_info_2_at"] is not None]
    return "ORDER-004" if info2_sent else "ORDER-002"


def scan_order_matching_pipeline(cursor: Any) -> dict[str, dict[str, int]]:
    """ORDER-001~004: 訂單配對流程的四個互斥階段，未配對案件恰好落在其中一個。"""
    by_case = _fetch_unmatched_orders_with_matches(cursor)
    case_nos_by_code: dict[str, set[str]] = {code: set() for code in _ORDER_PIPELINE_REASONS}
    for case_no, matches in by_case.items():
        case_nos_by_code[_classify_order_matching_state(matches)].add(case_no)

    summary: dict[str, dict[str, int]] = {}
    for code, case_nos in case_nos_by_code.items():
        created = updated = 0
        for case_no in case_nos:
            result = upsert_system_alert(
                cursor,
                alert_code=code,
                source_domain="ORDER",
                case_key=case_no,
                reason=_ORDER_PIPELINE_REASONS[code].format(case_no=case_no),
                details={"case_no": case_no},
            )
            if result["result"] == "created":
                created += 1
            else:
                updated += 1
        resolved = resolve_absent_alerts(
            cursor,
            alert_code=code,
            still_open_case_keys=case_nos,
            reason="系統重新掃描：案件配對進度已變更，自動解除",
        )
        summary[code] = {"created": created, "updated": updated, "resolved": resolved}
    return summary


def scan_missing_beclass(cursor: Any) -> dict[str, int]:
    """BECLASS-001: 訂單已建立但客戶尚未填寫 BeClass 問卷 (無對應 beclass_records)。"""
    return _scan_presence_check(
        cursor,
        alert_code="BECLASS-001",
        source_domain="BECLASS",
        query_sql="""SELECT o.case_no
                      FROM orders o
                      LEFT JOIN beclass_records b ON b.query_no = o.case_no
                      WHERE b.id IS NULL""",
        reason_template="案件 {case_no} 客戶尚未填寫 BeClass 問卷",
        resolve_reason="系統重新掃描：BeClass 問卷已填寫，自動解除",
    )


def scan_client_missing_line(cursor: Any) -> dict[str, int]:
    """LINE-001: 案件的客戶尚未綁定 LINE (clients.line_user_id IS NULL)。"""
    return _scan_presence_check(
        cursor,
        alert_code="LINE-001",
        source_domain="LINE",
        query_sql="""SELECT o.case_no
                      FROM orders o
                      JOIN clients c ON c.case_no = o.case_no
                      WHERE c.line_user_id IS NULL""",
        reason_template="案件 {case_no} 的客戶尚未綁定 LINE",
        resolve_reason="系統重新掃描：客戶已綁定 LINE，自動解除",
    )


def scan_staff_missing_line(cursor: Any) -> dict[str, int]:
    """LINE-005: 案件已指派的月嫂尚未綁定 LINE (staff.line_user_id IS NULL)。"""
    return _scan_presence_check(
        cursor,
        alert_code="LINE-005",
        source_domain="LINE",
        query_sql="""SELECT o.case_no
                      FROM orders o
                      JOIN staff s ON s.id = o.staff_id
                      WHERE o.staff_id IS NOT NULL AND s.line_user_id IS NULL""",
        reason_template="案件 {case_no} 的服務人員尚未綁定 LINE",
        resolve_reason="系統重新掃描：服務人員已綁定 LINE，自動解除",
    )


def scan_resume_not_sent(cursor: Any) -> dict[str, int]:
    """DOC-SEND-001: 有候選月嫂已願意接案，但履歷尚未發送給客戶 (matching_records.sent_resume_at IS NULL)。"""
    return _scan_presence_check(
        cursor,
        alert_code="DOC-SEND-001",
        source_domain="DOC-SEND",
        query_sql="""SELECT DISTINCT o.case_no
                      FROM orders o
                      JOIN matching_records m ON m.case_no = o.case_no
                      WHERE o.staff_id IS NULL
                        AND m.caregiver_accepted = 1
                        AND m.sent_resume_at IS NULL""",
        reason_template="案件 {case_no} 已有候選月嫂願意接案，但履歷尚未發送給客戶",
        resolve_reason="系統重新掃描：履歷已發送或訂單已成立，自動解除",
    )


_RECEIVABLE_STAGES = (
    ("deposit", "訂金", "deposit_due_date", "deposit_receivable", "deposit_received"),
    ("first_payment", "第一期", "first_payment_due_date", "first_payment_receivable", "first_payment_received"),
    ("second_payment", "第二期", "second_payment_due_date", "second_payment_receivable", "second_payment_received"),
)


def scan_client_receivable_overdue(cursor: Any) -> dict[str, int]:
    """RECEIVABLE-001: 客戶訂金／第一期／第二期已過應收日期，但尚未收齊。

    一個案件可能同時有多個階段逾期，因此每個案件只開一筆警示，details 內列出
    所有目前逾期的階段；只要三階段全部收齊，該案件的警示才會被解除。
    """
    cursor.execute(
        """SELECT case_no,
                  deposit_due_date, deposit_receivable, deposit_received,
                  first_payment_due_date, first_payment_receivable, first_payment_received,
                  second_payment_due_date, second_payment_receivable, second_payment_received
           FROM client_payments
           WHERE (deposit_due_date IS NOT NULL AND deposit_due_date < CURDATE() AND deposit_received < deposit_receivable)
              OR (first_payment_due_date IS NOT NULL AND first_payment_due_date < CURDATE() AND first_payment_received < first_payment_receivable)
              OR (second_payment_due_date IS NOT NULL AND second_payment_due_date < CURDATE() AND second_payment_received < second_payment_receivable)"""
    )
    rows = cursor.fetchall()
    open_case_nos: set[str] = set()
    created = updated = 0
    for row in rows:
        overdue_stages = []
        for key, label, due_col, receivable_col, received_col in _RECEIVABLE_STAGES:
            if (
                row[due_col] is not None
                and row[due_col] < date.today()
                and row[received_col] < row[receivable_col]
            ):
                overdue_stages.append(
                    {
                        "階段": label,
                        "到期日": str(row[due_col]),
                        "應收": str(row[receivable_col]),
                        "已收": str(row[received_col]),
                    }
                )
        if not overdue_stages:
            continue
        case_no = row["case_no"]
        open_case_nos.add(case_no)
        stage_labels = "、".join(s["階段"] for s in overdue_stages)
        result = upsert_system_alert(
            cursor,
            alert_code="RECEIVABLE-001",
            source_domain="RECEIVABLE",
            case_key=case_no,
            reason=f"案件 {case_no} 的客戶{stage_labels}已過應收日期，尚未收齊",
            details={"case_no": case_no, "逾期階段": overdue_stages},
        )
        if result["result"] == "created":
            created += 1
        else:
            updated += 1
    resolved = resolve_absent_alerts(
        cursor,
        alert_code="RECEIVABLE-001",
        still_open_case_keys=open_case_nos,
        reason="系統重新掃描：應收款項已收齊或未逾期，自動解除",
    )
    return {"created": created, "updated": updated, "resolved": resolved}


def scan_staff_payout_overdue(cursor: Any) -> dict[str, int]:
    """PAYOUT-001: 服務人員應付款已過到期日，但尚未全額匯出。

    以 assignment_id 當識別碼（而非 case_no），因為同一案件可能由多位月嫂分段
    服務、各自對應一筆 staff_payments。
    """
    cursor.execute(
        """SELECT assignment_id, case_no, staff_id, due_date, total_payable, amount_paid, payment_status
           FROM staff_payments
           WHERE due_date IS NOT NULL AND due_date < CURDATE()
             AND amount_paid < total_payable
             AND payment_status NOT IN ('cancelled')"""
    )
    rows = cursor.fetchall()
    open_keys: set[str] = set()
    created = updated = 0
    for row in rows:
        case_key = f"{row['case_no']}#assign{row['assignment_id']}"
        open_keys.add(case_key)
        result = upsert_system_alert(
            cursor,
            alert_code="PAYOUT-001",
            source_domain="PAYOUT",
            case_key=case_key,
            reason=f"案件 {row['case_no']} 的服務人員應付款已過到期日（{row['due_date']}），尚未匯款完成",
            details={
                "case_no": row["case_no"],
                "staff_id": row["staff_id"],
                "到期日": str(row["due_date"]),
                "應付": str(row["total_payable"]),
                "已付": str(row["amount_paid"]),
                "狀態": row["payment_status"],
            },
        )
        if result["result"] == "created":
            created += 1
        else:
            updated += 1
    resolved = resolve_absent_alerts(
        cursor,
        alert_code="PAYOUT-001",
        still_open_case_keys=open_keys,
        reason="系統重新掃描：月嫂款項已匯出或未逾期，自動解除",
    )
    return {"created": created, "updated": updated, "resolved": resolved}


def scan_subsidy_return_overdue(cursor: Any) -> dict[str, int]:
    """RETURN-001: 工會應退還給客戶的補助款已過到期日，但尚未退齊。

    需人工覆核（subsidy_return_review_status='review_required'）的案件也一併
    列入提醒，但 reason／details 會註明卡在覆核中，避免跟單純逾期的案件混淆。
    """
    cursor.execute(
        """SELECT case_no, subsidy_return_due_date, subsidy_return_receivable,
                  subsidy_return_refunded, subsidy_return_review_status, subsidy_return_review_reason
           FROM client_payments
           WHERE subsidy_return_due_date IS NOT NULL
             AND subsidy_return_due_date < CURDATE()
             AND subsidy_return_refunded < subsidy_return_receivable"""
    )
    rows = cursor.fetchall()
    open_case_nos: set[str] = set()
    created = updated = 0
    for row in rows:
        case_no = row["case_no"]
        open_case_nos.add(case_no)
        needs_review = row["subsidy_return_review_status"] == "review_required"
        if needs_review:
            reason = (
                f"案件 {case_no} 的補助款應退還客戶已過到期日，"
                f"目前卡在人工覆核中（{row['subsidy_return_review_reason'] or '原因未填'}）"
            )
        else:
            reason = f"案件 {case_no} 的補助款應退還客戶已過到期日（{row['subsidy_return_due_date']}），尚未退齊"
        result = upsert_system_alert(
            cursor,
            alert_code="RETURN-001",
            source_domain="RETURN",
            case_key=case_no,
            reason=reason,
            details={
                "case_no": case_no,
                "到期日": str(row["subsidy_return_due_date"]),
                "應退": str(row["subsidy_return_receivable"]),
                "已退": str(row["subsidy_return_refunded"]),
                "需人工覆核": needs_review,
                "覆核原因": row["subsidy_return_review_reason"],
            },
        )
        if result["result"] == "created":
            created += 1
        else:
            updated += 1
    resolved = resolve_absent_alerts(
        cursor,
        alert_code="RETURN-001",
        still_open_case_keys=open_case_nos,
        reason="系統重新掃描：補助款已退齊或未逾期，自動解除",
    )
    return {"created": created, "updated": updated, "resolved": resolved}


def scan_beclass_hcm_mismatch(cursor: Any) -> dict[str, int]:
    """IMPORT-003(A): BeClass 問卷存在，但 HCM 名冊查無對應案件 (clients.case_no)。"""
    return _scan_presence_check(
        cursor,
        alert_code="IMPORT-003",
        source_domain="IMPORT",
        query_sql="""SELECT b.query_no AS case_no
                      FROM beclass_records b
                      LEFT JOIN clients c ON c.case_no = b.query_no
                      WHERE c.id IS NULL""",
        reason_template="案件 {case_no} 有 BeClass 問卷資料，但 HCM 名冊查無對應案件",
        resolve_reason="系統重新掃描：HCM 名冊已有對應案件，自動解除",
    )


def scan_line_identity_conflict(cursor: Any) -> dict[str, int]:
    """LINE-004: 同一個 line_user_id 同時綁定客戶與月嫂兩種不同身分。"""
    cursor.execute(
        """SELECT c.line_user_id, c.case_no AS client_case_no, c.name AS client_name,
                  s.id AS staff_id, s.name AS staff_name
           FROM clients c
           JOIN staff s ON s.line_user_id = c.line_user_id
           WHERE c.line_user_id IS NOT NULL AND c.line_user_id != ''"""
    )
    rows = cursor.fetchall()
    open_keys: set[str] = set()
    created = updated = 0
    for row in rows:
        case_key = row["line_user_id"]
        open_keys.add(case_key)
        result = upsert_system_alert(
            cursor,
            alert_code="LINE-004",
            source_domain="LINE",
            case_key=case_key,
            reason=(
                f"LINE 帳號 {case_key} 同時綁定客戶（{row['client_name']}／{row['client_case_no']}）"
                f"與服務人員（{row['staff_name']}）兩種身分"
            ),
            details={
                "line_user_id": case_key,
                "client_case_no": row["client_case_no"],
                "client_name": row["client_name"],
                "staff_id": row["staff_id"],
                "staff_name": row["staff_name"],
            },
        )
        if result["result"] == "created":
            created += 1
        else:
            updated += 1
    resolved = resolve_absent_alerts(
        cursor,
        alert_code="LINE-004",
        still_open_case_keys=open_keys,
        reason="系統重新掃描：該 LINE 帳號已不再同時綁定兩種身分，自動解除",
    )
    return {"created": created, "updated": updated, "resolved": resolved}


def scan_line_task_no_reply(cursor: Any) -> dict[str, int]:
    """LINE-002: 月嫂群組任務已推播，但發送後該用戶完全沒有任何 LINE 訊息回覆。"""
    cursor.execute(
        """SELECT lt.id, lt.to_user_id, lt.sent_at, lt.message_content
           FROM line_tasks lt
           WHERE lt.status = 'sent'
             AND lt.task_type = 'line_push'
             AND lt.sent_at IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM line_webhook_events lwe
                 WHERE lwe.source_user_id = lt.to_user_id
                   AND lwe.received_at > lt.sent_at
             )"""
    )
    rows = cursor.fetchall()
    open_keys: set[str] = set()
    created = updated = 0
    for row in rows:
        case_key = str(row["id"])
        open_keys.add(case_key)
        result = upsert_system_alert(
            cursor,
            alert_code="LINE-002",
            source_domain="LINE",
            case_key=case_key,
            reason=f"LINE 任務 #{row['id']}（發送對象 {row['to_user_id']}）已推播，但尚未收到任何回覆",
            details={
                "task_id": row["id"],
                "to_user_id": row["to_user_id"],
                "sent_at": str(row["sent_at"]),
                "message_content": row["message_content"],
            },
        )
        if result["result"] == "created":
            created += 1
        else:
            updated += 1
    resolved = resolve_absent_alerts(
        cursor,
        alert_code="LINE-002",
        still_open_case_keys=open_keys,
        reason="系統重新掃描：已收到該用戶的 LINE 回覆，自動解除",
    )
    return {"created": created, "updated": updated, "resolved": resolved}


def scan_staff_holiday_undecided(cursor: Any) -> dict[str, int]:
    """SCHEDULE-001: 服務期間內有國定假日，但該月嫂當天完全沒有排班決策紀錄。"""
    cursor.execute(
        """SELECT DISTINCT csa.staff_id, csa.case_no, h.holiday_date, h.holiday_name
           FROM case_staff_assignments csa
           JOIN holidays h
             ON h.holiday_date BETWEEN csa.assigned_start_date AND csa.assigned_end_date
           LEFT JOIN staff_schedule ss
             ON ss.staff_id = csa.staff_id AND ss.work_date = h.holiday_date
           WHERE csa.status IN ('planned', 'active')
             AND csa.assigned_start_date IS NOT NULL
             AND csa.assigned_end_date IS NOT NULL
             AND ss.id IS NULL"""
    )
    rows = cursor.fetchall()
    open_keys: set[str] = set()
    created = updated = 0
    for row in rows:
        case_key = f"{row['staff_id']}:{row['holiday_date']}"
        open_keys.add(case_key)
        result = upsert_system_alert(
            cursor,
            alert_code="SCHEDULE-001",
            source_domain="SCHEDULE",
            case_key=case_key,
            reason=(
                f"案件 {row['case_no']} 的服務期間內有國定假日「{row['holiday_name']}」"
                f"（{row['holiday_date']}），行政尚未決策該月嫂當天是否放假"
            ),
            details={
                "staff_id": row["staff_id"],
                "case_no": row["case_no"],
                "holiday_date": str(row["holiday_date"]),
                "holiday_name": row["holiday_name"],
            },
        )
        if result["result"] == "created":
            created += 1
        else:
            updated += 1
    resolved = resolve_absent_alerts(
        cursor,
        alert_code="SCHEDULE-001",
        still_open_case_keys=open_keys,
        reason="系統重新掃描：該月嫂當天已有排班決策紀錄，自動解除",
    )
    return {"created": created, "updated": updated, "resolved": resolved}


def scan_staff_replaced_assignments(cursor: Any) -> dict[str, int]:
    """SCHEDULE-002: 月嫂服務中途更換人員，需人工複核財務拆分。

    case_staff_assignments 沒有「已複核」欄位可供判斷是否處理完成，因此這是
    一次性提醒：只建立/更新警示，從不自動解除（沒有 resolve_absent_alerts）；
    人工在警示中心手動認領/解除後，重新掃描不會再把它撈回來（已解決的
    case_key 直接跳過，不會被 upsert 重新打開）。
    """
    cursor.execute(
        "SELECT case_key FROM system_alerts WHERE alert_code='SCHEDULE-002' AND status='resolved'"
    )
    already_resolved = {row["case_key"] for row in cursor.fetchall()}

    cursor.execute(
        """SELECT id, case_no, staff_id, assigned_start_date, assigned_end_date,
                  floor_fee_allocated, replacement_reason
           FROM case_staff_assignments
           WHERE status = 'replaced'"""
    )
    rows = cursor.fetchall()
    created = updated = 0
    for row in rows:
        case_key = str(row["id"])
        if case_key in already_resolved:
            continue
        result = upsert_system_alert(
            cursor,
            alert_code="SCHEDULE-002",
            source_domain="SCHEDULE",
            case_key=case_key,
            reason=(
                f"案件 {row['case_no']} 的月嫂服務中途更換人員"
                f"（原因：{row['replacement_reason'] or '未填寫'}），需人工複核財務拆分"
            ),
            details={
                "assignment_id": row["id"],
                "case_no": row["case_no"],
                "staff_id": row["staff_id"],
                "assigned_start_date": str(row["assigned_start_date"]),
                "assigned_end_date": str(row["assigned_end_date"]),
                "floor_fee_allocated": str(row["floor_fee_allocated"]),
                "replacement_reason": row["replacement_reason"],
            },
        )
        if result["result"] == "created":
            created += 1
        else:
            updated += 1
    return {"created": created, "updated": updated, "resolved": 0}


def scan_staff_schedule_overlap(cursor: Any) -> dict[str, int]:
    """SCHEDULE-003: 同一月嫂被指派給兩筆案件，服務期間完全重疊。"""
    cursor.execute(
        """SELECT a.id AS a_id, a.case_no AS a_case_no,
                  a.assigned_start_date AS a_start, a.assigned_end_date AS a_end,
                  b.id AS b_id, b.case_no AS b_case_no,
                  b.assigned_start_date AS b_start, b.assigned_end_date AS b_end,
                  a.staff_id
           FROM case_staff_assignments a
           JOIN case_staff_assignments b
             ON a.staff_id = b.staff_id AND a.id < b.id
           WHERE a.status IN ('planned', 'active') AND b.status IN ('planned', 'active')
             AND a.assigned_start_date IS NOT NULL AND a.assigned_end_date IS NOT NULL
             AND b.assigned_start_date IS NOT NULL AND b.assigned_end_date IS NOT NULL
             AND a.assigned_start_date <= b.assigned_end_date
             AND b.assigned_start_date <= a.assigned_end_date"""
    )
    rows = cursor.fetchall()
    open_keys: set[str] = set()
    created = updated = 0
    for row in rows:
        case_key = f"{row['a_id']}:{row['b_id']}"
        open_keys.add(case_key)
        result = upsert_system_alert(
            cursor,
            alert_code="SCHEDULE-003",
            source_domain="SCHEDULE",
            case_key=case_key,
            reason=(
                f"服務人員檔期重疊：案件 {row['a_case_no']}（{row['a_start']}~{row['a_end']}）"
                f"與案件 {row['b_case_no']}（{row['b_start']}~{row['b_end']}）由同一月嫂承接且日期重疊"
            ),
            details={
                "staff_id": row["staff_id"],
                "assignment_a": {"id": row["a_id"], "case_no": row["a_case_no"], "start": str(row["a_start"]), "end": str(row["a_end"])},
                "assignment_b": {"id": row["b_id"], "case_no": row["b_case_no"], "start": str(row["b_start"]), "end": str(row["b_end"])},
            },
        )
        if result["result"] == "created":
            created += 1
        else:
            updated += 1
    resolved = resolve_absent_alerts(
        cursor,
        alert_code="SCHEDULE-003",
        still_open_case_keys=open_keys,
        reason="系統重新掃描：檔期已不再重疊，自動解除",
    )
    return {"created": created, "updated": updated, "resolved": resolved}


def scan_staff_holiday_preference_conflict(cursor: Any) -> dict[str, int]:
    """SCHEDULE-005: 月嫂登記「國定假日必休」，但系統排班仍把當天排成上班日。

    只比對登記為「國定假日必休」（一律不上班）的月嫂；初一/端午/中秋等單一
    節日的個別偏好，因 holidays 表的假日名稱與此處命名不是嚴格一一對應，暫不
    納入比對範圍，避免文字比對誤判。
    """
    cursor.execute(
        """SELECT ss.staff_id, ss.case_no, ss.work_date, h.holiday_name
           FROM staff_schedule ss
           JOIN holidays h ON h.holiday_date = ss.work_date
           JOIN staff_holiday_availability sha
             ON sha.staff_id = ss.staff_id AND sha.holiday_name = '國定假日必休'
           WHERE ss.is_work_day = 1"""
    )
    rows = cursor.fetchall()
    open_keys: set[str] = set()
    created = updated = 0
    for row in rows:
        case_key = f"{row['staff_id']}:{row['work_date']}"
        open_keys.add(case_key)
        result = upsert_system_alert(
            cursor,
            alert_code="SCHEDULE-005",
            source_domain="SCHEDULE",
            case_key=case_key,
            reason=(
                f"案件 {row['case_no']} 的月嫂登記國定假日必休，"
                f"但 {row['work_date']}（{row['holiday_name']}）排班仍是上班日"
            ),
            details={
                "staff_id": row["staff_id"],
                "case_no": row["case_no"],
                "work_date": str(row["work_date"]),
                "holiday_name": row["holiday_name"],
            },
        )
        if result["result"] == "created":
            created += 1
        else:
            updated += 1
    resolved = resolve_absent_alerts(
        cursor,
        alert_code="SCHEDULE-005",
        still_open_case_keys=open_keys,
        reason="系統重新掃描：排班已調整為放假或月嫂已有替代人力，自動解除",
    )
    return {"created": created, "updated": updated, "resolved": resolved}


def scan_service_days_mismatch(cursor: Any) -> dict[str, int]:
    """SCHEDULE-006: 案件已完成，但合約服務天數與 staff_schedule 實際排班上班天數不一致。"""
    cursor.execute(
        """SELECT o.case_no, o.service_days,
                  (SELECT COUNT(*) FROM staff_schedule ss
                     WHERE ss.case_no = o.case_no AND ss.is_work_day = 1) AS actual_days
           FROM orders o
           WHERE o.status = '訂單完成'
           HAVING actual_days != o.service_days"""
    )
    rows = cursor.fetchall()
    open_keys: set[str] = set()
    created = updated = 0
    for row in rows:
        case_no = row["case_no"]
        open_keys.add(case_no)
        result = upsert_system_alert(
            cursor,
            alert_code="SCHEDULE-006",
            source_domain="SCHEDULE",
            case_key=case_no,
            reason=(
                f"案件 {case_no} 合約服務天數為 {row['service_days']} 天，"
                f"但實際排班上班天數為 {row['actual_days']} 天，兩者不一致"
            ),
            details={"case_no": case_no, "service_days": row["service_days"], "actual_days": row["actual_days"]},
        )
        if result["result"] == "created":
            created += 1
        else:
            updated += 1
    resolved = resolve_absent_alerts(
        cursor,
        alert_code="SCHEDULE-006",
        still_open_case_keys=open_keys,
        reason="系統重新掃描：服務天數與實際排班天數已一致，自動解除",
    )
    return {"created": created, "updated": updated, "resolved": resolved}


def run_process_alert_scan(cursor: Any) -> dict[str, dict[str, int]]:
    """Run every wired non-finance detector. Called by the UI's 重新掃描 action."""
    summary: dict[str, dict[str, int]] = dict(scan_order_matching_pipeline(cursor))
    summary["BECLASS-001"] = scan_missing_beclass(cursor)
    summary["LINE-001"] = scan_client_missing_line(cursor)
    summary["LINE-005"] = scan_staff_missing_line(cursor)
    summary["DOC-SEND-001"] = scan_resume_not_sent(cursor)
    summary["IMPORT-003"] = scan_beclass_hcm_mismatch(cursor)
    summary["RECEIVABLE-001"] = scan_client_receivable_overdue(cursor)
    summary["PAYOUT-001"] = scan_staff_payout_overdue(cursor)
    summary["RETURN-001"] = scan_subsidy_return_overdue(cursor)
    summary["LINE-002"] = scan_line_task_no_reply(cursor)
    summary["LINE-004"] = scan_line_identity_conflict(cursor)
    summary["SCHEDULE-001"] = scan_staff_holiday_undecided(cursor)
    summary["SCHEDULE-002"] = scan_staff_replaced_assignments(cursor)
    summary["SCHEDULE-003"] = scan_staff_schedule_overlap(cursor)
    summary["SCHEDULE-005"] = scan_staff_holiday_preference_conflict(cursor)
    summary["SCHEDULE-006"] = scan_service_days_mismatch(cursor)
    return summary
