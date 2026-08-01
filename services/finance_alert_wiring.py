"""Wire finance-import reconciliation "pending" results into finance_alerts.

Each reconciliation function in services/{client_receipt_reconciliation,
client_subsidy_return_transactions, government_subsidy_reconciliation,
staff_actual_transfers}.py already classifies "needs human review" states
precisely and returns them as {"result": "pending", "reason": ...}. That
logic was never missing -- the one missing wire was that a pending result
never became a visible finance_alerts row, so it just sat silently in
finance_import_rows/client_payments with nobody able to see it.

This module is that wire. maybe_alert_pending() is called once per pending
dispatch result from scripts/imports/import_finance_excel.py. Per the
2026-07-27 決議 with the user: only reasons that represent a genuine business
anomaly (mapped onto the formal 系統異常警示中心規格書.md §3.1 codes) create
an alert; reasons that are pure format/dedup/workflow-gate noise (malformed
rows, wrong bank format, waiting on an earlier step) stay silent by design --
they are not something an administrator needs to act on.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from services.finance_alert_detection import create_or_get_finance_alert

# reason (as returned by the reconciliation function) -> §3.1 alert_code.
# Anything not listed here is a technical/workflow-gate skip, not an alert.

_CLIENT_RECEIPT_CODES = {
    "case_not_found": "case_not_found",
    "case_not_unique": "case_not_unique",
    "receipt_exceeds_remaining_receivable": "client_receipt_single_overpay",
    # 訂單條款在收款計畫已建立後又變更，與既有計畫衝突；§3.1 尚無對應代碼，
    # 沿用既有 CLIENT 命名慣例新開一個，待實際發生時再依真實案例調整措辭。
    "snapshot_review_required": "client_payment_terms_changed",
}

_SUBSIDY_RETURN_CODES = {
    "subsidy_return_review_required": "shared_refund_account",
    "matched_identity_not_unique": "case_not_unique",
    # amount_mismatch 依實際金額大於/小於應退餘額分成兩個代碼，見 _client_subsidy_return_alert()
}

_GOVERNMENT_SUBSIDY_CODES = {
    "exact approved batch candidate is not unique": "multi_batch_same_amount_ambiguity",
}
_GOVERNMENT_SUBSIDY_REASON_ZH = {
    "exact approved batch candidate is not unique": "同金額有多筆已核准且未撥款的補助批次，系統無法唯一對應這筆入款",
}

_STAFF_TRANSFER_CODES = {
    "staff_transfer_plan_not_unique": "staff_monthly_settlement_ambiguity",
    "resolved_counterparty_account_missing": "staff_payment_missing_reference",
    "matched_staff_identity_not_unique": "staff_shared_bank_account",
    "counterparty_account_owner_not_unique": "staff_shared_bank_account",
    "allocation_total_must_equal_bank_debit": "staff_payment_amount_mismatch",
    "component_balance_not_paid_exactly": "staff_payment_amount_mismatch",
    "settlement_total_would_be_invalid": "staff_payment_amount_mismatch",
    "existing_transfer_differs": "staff_payment_amount_mismatch",
    "settlement_paid_projection_mismatch": "staff_payment_amount_mismatch",
    "non_pending_staging_row_has_no_identical_transfer": "staff_payment_amount_mismatch",
}

_ALERT_CODE_LABELS = {
    "case_not_found": "案件不存在",
    "case_not_unique": "案件不唯一/身分歧義",
    "client_receipt_single_overpay": "客戶入款金額超過剩餘應收",
    "client_payment_terms_changed": "訂單條款於收款計畫建立後變更",
    "shared_refund_account": "補助退款帳號多人共用",
    "multi_batch_same_amount_ambiguity": "政府補助同額多批次歧義",
    "staff_monthly_settlement_ambiguity": "服務人員月結單不唯一/歧義",
    "staff_payment_missing_reference": "服務人員薪資轉帳無明確參考碼",
    "staff_shared_bank_account": "服務人員領薪帳號多人共用",
    "staff_payment_amount_mismatch": "服務人員薪資轉帳金額不符",
}


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except Exception:
        return None
    return amount if amount.is_finite() else None


def _finance_import_row(cursor: Any, row_id: int) -> Mapping[str, Any] | None:
    cursor.execute(
        "SELECT id, credit, debit, transaction_date, bank_references "
        "FROM finance_import_rows WHERE id=%s",
        (row_id,),
    )
    return cursor.fetchone()


def _subsidy_return_case_no(cursor: Any, row_id: int) -> str | None:
    """Resolve the single matched client_payment's case_no, if unambiguous.

    The reconciliation obligation dict doesn't carry case_no (it's a pure
    receivable-vs-refunded projection, tested elsewhere with exact-equality
    assertions we don't want to disturb), so look it up the same way dispatch
    already resolved matched_identity_ids for this row.
    """
    cursor.execute(
        "SELECT matched_identity_ids FROM finance_import_rows WHERE id=%s",
        (row_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    identities = row.get("matched_identity_ids")
    if isinstance(identities, str):
        try:
            identities = json.loads(identities)
        except json.JSONDecodeError:
            return None
    if not isinstance(identities, list) or len(identities) != 1:
        return None
    cursor.execute(
        "SELECT case_no FROM client_payments WHERE id=%s",
        (identities[0],),
    )
    payment = cursor.fetchone()
    return payment.get("case_no") if payment else None


def _client_payment_remaining(cursor: Any, case_no: str, stage_hint: str | None = None) -> Decimal | None:
    cursor.execute(
        """SELECT deposit_receivable, deposit_received,
                  first_payment_receivable, first_payment_received,
                  second_payment_receivable, second_payment_received
           FROM client_payments WHERE case_no=%s""",
        (case_no,),
    )
    payment = cursor.fetchone()
    if payment is None:
        return None
    remaining = Decimal("0")
    for stage in ("deposit", "first_payment", "second_payment"):
        receivable = _decimal_or_none(payment[f"{stage}_receivable"])
        received = _decimal_or_none(payment[f"{stage}_received"])
        if receivable is None or received is None:
            return None
        remaining += receivable - received
    return remaining


def _alert(
    cursor: Any,
    *,
    alert_code: str,
    source_domain: str,
    row_id: int,
    case_no: str | None,
    reason_detail: str,
    candidate_snapshot: dict[str, Any],
    expected_amount: Any = None,
    actual_amount: Any = None,
    difference_amount: Any = None,
) -> dict[str, Any]:
    label = _ALERT_CODE_LABELS.get(alert_code, alert_code)
    source_id = case_no or f"row-{row_id}"
    return create_or_get_finance_alert(
        cursor,
        alert_code=alert_code,
        source_domain=source_domain,
        source_type="finance_import_row",
        source_id=source_id,
        reason=f"{label}：{reason_detail}",
        candidate_snapshot=candidate_snapshot,
        finance_import_row_id=row_id,
        expected_amount=expected_amount,
        actual_amount=actual_amount,
        difference_amount=difference_amount,
        detected_at=datetime.now(timezone.utc),
    )


def _client_receipt_alert(cursor, row_id, batch_id, result) -> dict[str, Any] | None:
    reason = result.get("reason")
    alert_code = _CLIENT_RECEIPT_CODES.get(reason)
    if alert_code is None:
        return None
    case_no = result.get("case_no")
    expected = actual = difference = None
    if alert_code == "client_receipt_single_overpay" and case_no:
        raw = _finance_import_row(cursor, row_id)
        credit = _decimal_or_none(raw["credit"]) if raw else None
        remaining = _client_payment_remaining(cursor, case_no)
        if credit is not None and remaining is not None:
            expected, actual, difference = remaining, credit, credit - remaining
    return _alert(
        cursor,
        alert_code=alert_code,
        source_domain="CLIENT",
        row_id=row_id,
        case_no=case_no,
        reason_detail=f"finance_import_rows#{row_id}（原因：{reason}）",
        candidate_snapshot={"row_id": row_id, "batch_id": batch_id, "case_no": case_no, "reason": reason},
        expected_amount=expected,
        actual_amount=actual,
        difference_amount=difference,
    )


def _subsidy_return_alert(cursor, row_id, batch_id, result) -> dict[str, Any] | None:
    reason = result.get("reason")
    obligation = result.get("obligation") or {}
    case_no = _subsidy_return_case_no(cursor, row_id)
    if reason == "amount_mismatch":
        raw = _finance_import_row(cursor, row_id)
        debit = _decimal_or_none(raw["debit"]) if raw else None
        remaining = _decimal_or_none(obligation.get("subsidy_return_remaining"))
        if debit is None or remaining is None:
            return None
        alert_code = "subsidy_return_underpaid" if debit < remaining else "subsidy_return_overpaid"
        return _alert(
            cursor,
            alert_code=alert_code,
            source_domain="RETURN",
            row_id=row_id,
            case_no=case_no,
            reason_detail=f"應退餘額 {remaining}，實際退款 {debit}",
            candidate_snapshot={
                "row_id": row_id,
                "batch_id": batch_id,
                "obligation": {k: str(v) for k, v in obligation.items()},
            },
            expected_amount=remaining,
            actual_amount=debit,
            difference_amount=debit - remaining,
        )
    alert_code = _SUBSIDY_RETURN_CODES.get(reason)
    if alert_code is None:
        return None
    return _alert(
        cursor,
        alert_code=alert_code,
        source_domain="RETURN",
        row_id=row_id,
        case_no=case_no,
        reason_detail=f"finance_import_rows#{row_id}（原因：{reason}）",
        candidate_snapshot={"row_id": row_id, "batch_id": batch_id, "reason": reason},
    )


def _government_subsidy_alert(cursor, row_id, batch_id, result) -> dict[str, Any] | None:
    reason = result.get("reason")
    alert_code = _GOVERNMENT_SUBSIDY_CODES.get(reason)
    if alert_code is None:
        return None
    bank_amount = result.get("bank_amount")
    expected_amount = result.get("expected_amount")
    return _alert(
        cursor,
        alert_code=alert_code,
        source_domain="SUBSIDY",
        row_id=row_id,
        case_no=None,
        reason_detail=_GOVERNMENT_SUBSIDY_REASON_ZH.get(reason, reason),
        candidate_snapshot={"row_id": row_id, "batch_id": batch_id, "reason": reason},
        expected_amount=expected_amount,
        actual_amount=bank_amount,
        difference_amount=(
            bank_amount - expected_amount
            if bank_amount is not None and expected_amount is not None
            else None
        ),
    )


def _staff_transfer_alert(cursor, row_id, batch_id, result) -> dict[str, Any] | None:
    reason = result.get("reason")
    alert_code = _STAFF_TRANSFER_CODES.get(reason)
    if alert_code is None:
        return None
    settlement = result.get("settlement") or {}
    case_no = None  # staff settlements are keyed by staff_id/settlement_id, not case_no
    staff_id = settlement.get("staff_id")
    return _alert(
        cursor,
        alert_code=alert_code,
        source_domain="STAFF",
        row_id=row_id,
        case_no=case_no,
        reason_detail=f"staff_id={staff_id}（原因：{reason}）" if staff_id else f"finance_import_rows#{row_id}（原因：{reason}）",
        candidate_snapshot={
            "row_id": row_id,
            "batch_id": batch_id,
            "reason": reason,
            "settlement": {k: str(v) for k, v in settlement.items()},
        },
    )


_DISPATCH = {
    "client_receipt": _client_receipt_alert,
    "client_subsidy_return": _subsidy_return_alert,
    "government_subsidy": _government_subsidy_alert,
    "staff_salary": _staff_transfer_alert,
    "staff_legacy_subsidy": _staff_transfer_alert,
}


def maybe_alert_pending(
    cursor: Any,
    *,
    classification_type: str,
    row_id: int,
    batch_id: int,
    result: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Create/refresh a finance_alerts row for a pending dispatch result if it is
    a genuine business anomaly. Returns None for reasons deemed technical noise
    (nothing was written), or the create_or_get_finance_alert() result dict.
    """
    handler = _DISPATCH.get(classification_type)
    if handler is None:
        return None
    return handler(cursor, row_id, batch_id, result)
