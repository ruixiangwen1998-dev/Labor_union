# -*- coding: utf-8 -*-
"""Import client bank receipts into the case-based client ledger.

Receipt amounts are never used to define a case's receivable amount.  A new
client-payment snapshot is derived only from the order's service terms.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from typing import Any, Mapping

import pandas as pd
from dotenv import load_dotenv

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.append(ROOT)
load_dotenv(os.path.join(ROOT, ".env"))

from services.client_payment_transactions import calculate_client_payment_state
from services.client_payment_writer import build_client_summary_update
from services.db_service import get_connection
from services.client_receipt_reconciliation import reconcile_client_receipt
from services.client_subsidy_return_transactions import record_client_subsidy_return
from services.finance_identity_maps import load_finance_identity_maps
from services.finance_alert_wiring import maybe_alert_pending
from services.finance_import_staging import stage_finance_rows
from services.government_subsidy_reconciliation import reconcile_government_subsidy
from services.order_amount_calculator import calculate_order_amounts
from services.staff_actual_transfers import reconcile_staff_actual_transfer
from scripts.imports.finance_statement_normalizer import normalize_workbook


PAYMENT_STAGES = (
    ("deposit", "deposit_received"),
    ("first_payment", "first_payment_received"),
    ("second_payment", "second_payment_received"),
)


def allocate_receipt(receivables: dict, current_state: dict, amount) -> list[tuple[str, Decimal]]:
    """Allocate one receipt in collection-stage order without over-collection."""
    remaining = Decimal(str(amount))
    assert remaining > 0
    allocations = []
    for stage, received_key in PAYMENT_STAGES:
        stage_remaining = Decimal(str(receivables[stage])) - Decimal(str(current_state[received_key]))
        if stage_remaining <= 0:
            continue
        allocation = min(remaining, stage_remaining)
        allocations.append((stage, allocation))
        remaining -= allocation
        if remaining == 0:
            return allocations
    raise ValueError("receipt exceeds the remaining client receivable")


def decode_va_to_case_no(virtual_account):
    """Decode 99781699 + ROC year + sequence virtual account into case_no."""
    va_str = str(virtual_account).strip()
    if len(va_str) != 14 or not va_str.startswith("997816") or va_str[6:8] != "99":
        return None
    code_part = va_str[8:]
    if not code_part.isdigit():
        return None
    return f"{code_part[:3]}{int(code_part[3:]):06d}"


def normalize_header(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().lower().replace(" ", "")


def get_sheet_name(xl: pd.ExcelFile, expected: str) -> str:
    matches = {normalize_header(name): name for name in xl.sheet_names}
    try:
        return matches[normalize_header(expected)]
    except KeyError as exc:
        raise ValueError(f"找不到工作表：{expected}") from exc


def load_transactions(xl: pd.ExcelFile) -> pd.DataFrame:
    raw = xl.parse(get_sheet_name(xl, "銀行流水明細"), header=None)
    required = {"交易日期", "交易摘要", "支出", "收入", "虛擬帳號/對方帳號"}
    for row_index in range(min(10, len(raw))):
        headers = [str(value).strip() if pd.notna(value) else "" for value in raw.iloc[row_index]]
        if required.issubset(set(headers)):
            data = raw.iloc[row_index + 1:].copy()
            data.columns = headers
            return data[["交易日期", "交易摘要", "支出", "收入", "虛擬帳號/對方帳號"]]
    raise ValueError("找不到銀行流水明細的必要欄位")


def build_snapshot_plan(order: dict) -> dict | None:
    """Build a client ledger plan, or return None for a historical incomplete order."""
    if order.get("deposit_service_days") is None:
        return None
    service_start_date = order.get("actual_start_date") or order.get("start_date")
    if not service_start_date or not order.get("deposit_date"):
        return None
    return calculate_order_amounts(
        {
            "case_no": order["case_no"],
            "service_days": order.get("service_days"),
            "service_hours_per_day": order.get("service_hours_per_day"),
            "identity_status": order.get("identity_status"),
            "client_floor_fee": order.get("floor_fee", 0),
            "service_start_date": service_start_date,
            "actual_completion_date": order.get("actual_end_date"),
        },
        collection_schedule={
            "deposit_service_days": order["deposit_service_days"],
            "deposit_due_date": order["deposit_date"],
        },
    )


def create_client_payment_snapshot(cursor, plan: dict) -> int:
    """Persist the calculator's three-stage client ledger snapshot."""
    stages = {stage["stage"]: stage for stage in plan["client_ledger_plan"]["stages"]}
    cursor.execute(
        """INSERT INTO client_payments (
            case_no,
            deposit_receivable, deposit_due_date,
            first_payment_receivable, first_payment_due_date,
            second_payment_receivable, second_payment_due_date,
            amount_receivable, payment_status
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'待收訂金')""",
        (
            plan["case_no"],
            stages["deposit"]["receivable"], stages["deposit"]["due_date"],
            stages["first_payment"]["receivable"], stages["first_payment"]["due_date"],
            stages["second_payment"]["receivable"], stages["second_payment"]["due_date"],
            plan["client_ledger_plan"]["amount_receivable"],
        ),
    )
    return cursor.lastrowid


def _order_for_snapshot(cursor, case_no: str) -> dict | None:
    cursor.execute(
        """SELECT o.case_no, o.service_days, o.service_hours_per_day, c.identity_status,
                  o.floor_fee, o.deposit_date, o.deposit_service_days,
                  o.start_date, o.actual_start_date, o.actual_end_date
           FROM orders o
           JOIN clients c ON c.id = o.client_id
           WHERE o.case_no = %s FOR UPDATE""",
        (case_no,),
    )
    return cursor.fetchone()


def _existing_receipt_transactions(cursor, client_payment_id: int) -> list[dict]:
    cursor.execute(
        """SELECT stage, transaction_type, transaction_status, amount, occurred_at, external_reference
           FROM client_payment_transactions
           WHERE client_payment_id = %s
             AND stage IN ('deposit', 'first_payment', 'second_payment')
           ORDER BY occurred_at, id FOR UPDATE""",
        (client_payment_id,),
    )
    return cursor.fetchall()


def _record_receipt_allocation(cursor, payment: dict, transaction: dict, stage: str, amount: Decimal):
    candidate = {
        "stage": stage,
        "transaction_type": "receipt",
        "transaction_status": "succeeded",
        "amount": amount,
        "occurred_at": transaction["date"],
        "external_reference": f"excel:{payment['case_no']}:{transaction['idx']}:{stage}",
    }
    cursor.execute(
        """INSERT INTO client_payment_transactions
           (client_payment_id, case_no, stage, transaction_type, transaction_status,
            amount, occurred_at, external_reference, notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (payment["id"], payment["case_no"], stage, "receipt", "succeeded", amount,
         transaction["date"], candidate["external_reference"], "銀行 Excel 匯入"),
    )
    return candidate


def _update_client_summary(cursor, payment: dict, transactions: list[dict], occurred_at):
    receivables = {
        "deposit": payment["deposit_receivable"],
        "first_payment": payment["first_payment_receivable"],
        "second_payment": payment["second_payment_receivable"],
    }
    update = build_client_summary_update(receivables, transactions, occurred_at)
    cursor.execute(
        """UPDATE client_payments SET
            deposit_received=%s, first_payment_received=%s, second_payment_received=%s,
            amount_received=%s, deposit_received_at=%s, first_payment_received_at=%s,
            second_payment_received_at=%s, second_payment_due_date=%s
           WHERE id=%s""",
        (update["deposit_received"], update["first_payment_received"], update["second_payment_received"],
         update["amount_received"], update["deposit_received_at"], update["first_payment_received_at"],
         update["second_payment_received_at"], update["second_payment_due_date"], payment["id"]),
    )
    if update["deposit_received_at"]:
        cursor.execute(
            """UPDATE orders SET status = '訂單成立'
               WHERE case_no = %s AND status = '洽談中'""",
            (payment["case_no"],),
        )
    return update


def _parse_bank_transactions(df_tx: pd.DataFrame) -> tuple[dict[str, list[dict]], int]:
    transactions_by_case: dict[str, list[dict]] = {}
    skipped = 0
    for idx, row in df_tx.iterrows():
        if pd.isna(row.get("交易日期")):
            continue
        income_value = row.get("收入")
        income = Decimal(str(income_value)) if pd.notna(income_value) and str(income_value).strip() else Decimal("0")
        if income <= 0:
            if pd.notna(row.get("支出")):
                skipped += 1
            continue
        case_no = decode_va_to_case_no(row.get("虛擬帳號/對方帳號"))
        if not case_no:
            skipped += 1
            continue
        transactions_by_case.setdefault(case_no, []).append({
            "amount": income,
            "date": pd.to_datetime(row["交易日期"]).strftime("%Y-%m-%d"),
            "idx": idx,
        })
    for transactions in transactions_by_case.values():
        transactions.sort(key=lambda transaction: transaction["date"])
    return transactions_by_case, skipped


def import_bank_transactions(df_tx: pd.DataFrame) -> dict[str, int]:
    """Import parsed bank receipts atomically and return imported/skipped counts."""
    transactions_by_case, skipped_transactions = _parse_bank_transactions(df_tx)
    imported_client_transactions = 0
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            for case_no, bank_transactions in transactions_by_case.items():
                order = _order_for_snapshot(cursor, case_no)
                if not order:
                    skipped_transactions += len(bank_transactions)
                    continue
                cursor.execute("SELECT * FROM client_payments WHERE case_no = %s FOR UPDATE", (case_no,))
                payment = cursor.fetchone()
                if not payment:
                    plan = build_snapshot_plan(order)
                    if plan is None:
                        skipped_transactions += len(bank_transactions)
                        continue
                    payment_id = create_client_payment_snapshot(cursor, plan)
                    cursor.execute("SELECT * FROM client_payments WHERE id = %s FOR UPDATE", (payment_id,))
                    payment = cursor.fetchone()

                existing = _existing_receipt_transactions(cursor, payment["id"])
                receivables = {
                    "deposit": payment["deposit_receivable"],
                    "first_payment": payment["first_payment_receivable"],
                    "second_payment": payment["second_payment_receivable"],
                }
                current_state = calculate_client_payment_state(receivables, existing)
                for transaction in bank_transactions:
                    reference_prefix = f"excel:{case_no}:{transaction['idx']}:"
                    if any(str(item.get("external_reference") or "").startswith(reference_prefix) for item in existing):
                        continue
                    try:
                        allocations = allocate_receipt(receivables, current_state, transaction["amount"])
                    except ValueError:
                        skipped_transactions += 1
                        continue
                    for stage, amount in allocations:
                        candidate = _record_receipt_allocation(cursor, payment, transaction, stage, amount)
                        existing.append(candidate)
                        current_state = calculate_client_payment_state(receivables, existing)
                        imported_client_transactions += 1
                    _update_client_summary(cursor, payment, existing, transaction["date"])
        conn.commit()
        return {"imported_client_transactions": imported_client_transactions, "skipped_transactions": skipped_transactions}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_STAFF_COMPONENTS = (
    ("regular_salary", "service_salary"),
    ("floor_fee", "floor_fee_amount"),
    ("adjustment", "adjustment_amount"),
)


def _identity_ids(value: Any) -> list[int] | None:
    """Decode the immutable classifier identity set without guessing."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, list):
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in value):
        return None
    return value


def _load_dispatch_row(cursor: Any, row_id: int) -> Mapping[str, Any]:
    cursor.execute(
        """SELECT id, classification_type, matched_identity_ids,
                  resolved_counterparty_account, classification_reason, debit
           FROM finance_import_rows WHERE id=%s FOR UPDATE""",
        (row_id,),
    )
    row = cursor.fetchone()
    if not isinstance(row, Mapping):
        raise RuntimeError("inserted finance import row was not found")
    return row


def _staff_transfer_candidates(
    cursor: Any,
    dispatch_row: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return exact, complete staff transfer plans; zero/many means review."""
    staff_ids = _identity_ids(dispatch_row.get("matched_identity_ids"))
    if staff_ids is None or len(staff_ids) != 1:
        return []
    try:
        debit = Decimal(str(dispatch_row.get("debit")))
    except Exception:
        return []
    if not debit.is_finite() or debit <= 0:
        return []

    classification_type = dispatch_row.get("classification_type")
    payment_phase = (
        "second_subsidy" if classification_type == "staff_legacy_subsidy" else None
    )
    if payment_phase is None and classification_type != "staff_salary":
        return []

    cursor.execute(
        """SELECT id, staff_id
           FROM staff_monthly_settlements
           WHERE staff_id=%s AND status IN ('finalized','partially_paid')
           ORDER BY settlement_month, id
           FOR UPDATE""",
        (staff_ids[0],),
    )
    settlements = cursor.fetchall()
    plans: list[dict[str, Any]] = []
    for settlement in settlements:
        if not isinstance(settlement, Mapping):
            raise TypeError("cursor must return mapping settlement rows")
        settlement_id = settlement.get("id")
        cursor.execute(
            """SELECT id AS settlement_detail_id, service_salary,
                      legacy_subsidy_payable, floor_fee_amount,
                      adjustment_amount, legacy_subsidy_status, review_required
               FROM staff_monthly_settlement_details
               WHERE settlement_id=%s AND staff_id=%s
               ORDER BY id
               FOR UPDATE""",
            (settlement_id, staff_ids[0]),
        )
        detail_rows = cursor.fetchall()
        if not detail_rows:
            continue

        details: dict[int, Mapping[str, Any]] = {}
        for row in detail_rows:
            if not isinstance(row, Mapping):
                raise TypeError("cursor must return mapping settlement detail rows")
            detail_id = row.get("settlement_detail_id")
            if isinstance(detail_id, bool) or not isinstance(detail_id, int):
                raise ValueError("settlement detail id must be an integer")
            details[detail_id] = row
        cursor.execute(
            """SELECT sta.settlement_detail_id, sta.component_type,
                      sta.allocated_amount, sat.transaction_type
               FROM staff_transfer_allocations sta
               JOIN staff_actual_transfers sat ON sat.id=sta.transfer_id
               WHERE sta.settlement_detail_id IN (
                   SELECT id FROM staff_monthly_settlement_details
                   WHERE settlement_id=%s
               )
                 AND sta.review_status='approved'
                 AND sat.transaction_status='succeeded'
               ORDER BY sta.id
               FOR UPDATE""",
            (settlement_id,),
        )
        paid: dict[tuple[int, str], Decimal] = {}
        for row in cursor.fetchall():
            if not isinstance(row, Mapping):
                raise TypeError("cursor must return mapping allocation rows")
            sign = Decimal("-1") if row.get("transaction_type") == "reversal" else Decimal("1")
            key = (row.get("settlement_detail_id"), row.get("component_type"))
            paid[key] = paid.get(key, Decimal("0")) + sign * Decimal(
                str(row.get("allocated_amount") or 0)
            )

        phase = payment_phase
        if phase is None:
            has_legacy = any(
                Decimal(str(detail.get("legacy_subsidy_payable") or 0)) > 0
                for detail in details.values()
            )
            phase = "first_salary" if has_legacy else "normal"

        components = (
            (("legacy_subsidy", "legacy_subsidy_payable"),)
            if phase == "second_subsidy"
            else _STAFF_COMPONENTS
        )
        allocations: list[dict[str, Any]] = []
        valid = True
        for detail_id, detail in details.items():
            if phase == "second_subsidy" and (
                detail.get("legacy_subsidy_status") != "confirmed"
                or bool(detail.get("review_required"))
            ):
                valid = False
                break
            for component_type, column in components:
                amount = Decimal(str(detail.get(column) or 0))
                remaining = amount - paid.get((detail_id, component_type), Decimal("0"))
                if remaining < 0:
                    valid = False
                    break
                if remaining > 0:
                    allocations.append(
                        {
                            "settlement_detail_id": detail_id,
                            "component_type": component_type,
                            "allocated_amount": remaining,
                            "allocation_method": "explicit",
                        }
                    )
            if not valid:
                break
        if valid and allocations and sum(
            (item["allocated_amount"] for item in allocations), Decimal("0")
        ) == debit:
            plans.append(
                {
                    "settlement_id": settlement_id,
                    "payment_phase": phase,
                    "allocations": allocations,
                }
            )
    return plans


def _dispatch_inserted_row(cursor: Any, staged_row: Mapping[str, Any]) -> dict[str, Any]:
    row_id = int(staged_row["row_id"])
    classification_type = staged_row.get("classification_type")
    if classification_type == "client_receipt":
        return reconcile_client_receipt(cursor, row_id)
    if classification_type == "client_subsidy_return":
        row = _load_dispatch_row(cursor, row_id)
        identities = _identity_ids(row.get("matched_identity_ids"))
        if identities is None or len(identities) != 1:
            return {"result": "pending", "reason": "matched_identity_not_unique"}
        return record_client_subsidy_return(cursor, identities[0], row_id)
    if classification_type == "government_subsidy":
        return reconcile_government_subsidy(cursor, row_id)
    if classification_type in {"staff_salary", "staff_legacy_subsidy"}:
        row = _load_dispatch_row(cursor, row_id)
        plans = _staff_transfer_candidates(cursor, row)
        if len(plans) != 1:
            return {"result": "pending", "reason": "staff_transfer_plan_not_unique"}
        plan = plans[0]
        return reconcile_staff_actual_transfer(
            cursor,
            row_id,
            plan["settlement_id"],
            plan["payment_phase"],
            plan["allocations"],
        )
    row = _load_dispatch_row(cursor, row_id)
    return {
        "result": "pending",
        "reason": row.get("classification_reason") or "non_business_review",
    }


def import_finance_workbook(excel_path: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Normalize, stage, and reconcile one workbook; optionally roll everything back."""
    if not isinstance(dry_run, bool):
        raise TypeError("dry_run must be a boolean")
    source_path = os.path.abspath(excel_path)
    normalized_result = normalize_workbook(source_path)
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            identity_maps = load_finance_identity_maps(cursor)
            staging = stage_finance_rows(cursor, normalized_result, identity_maps)
            inserted_rows = 0
            skipped_existing = 0
            pending_rows: list[int] = []
            reconciled_counts: dict[str, int] = {}
            row_results: list[dict[str, Any]] = []
            for staged_row in staging["staged_rows"]:
                classification_type = str(staged_row.get("classification_type"))
                row_manifest = {
                    "dedup_fingerprint": staged_row.get("dedup_fingerprint"),
                    "classification_type": classification_type,
                    "staging_result": staged_row.get("result"),
                    "dispatch_result": None,
                    "reason": None,
                }
                if staged_row.get("result") == "skipped_existing":
                    skipped_existing += 1
                    row_results.append(row_manifest)
                    continue
                inserted_rows += 1
                result = _dispatch_inserted_row(cursor, staged_row)
                row_id = int(staged_row["row_id"])
                row_manifest["dispatch_result"] = result.get("result")
                row_manifest["reason"] = result.get("reason")
                row_results.append(row_manifest)
                if result.get("result") in {"reconciled", "existing"}:
                    reconciled_counts[classification_type] = (
                        reconciled_counts.get(classification_type, 0) + 1
                    )
                else:
                    pending_rows.append(row_id)
                    maybe_alert_pending(
                        cursor,
                        classification_type=classification_type,
                        row_id=row_id,
                        batch_id=staging["batch_id"],
                        result=result,
                    )
            cursor.execute(
                """UPDATE finance_import_batches
                   SET status='completed', completed_at=CURRENT_TIMESTAMP,
                       failure_message=NULL
                   WHERE id=%s AND status='staged'""",
                (staging["batch_id"],),
            )
            if getattr(cursor, "rowcount", 1) != 1:
                raise RuntimeError("finance import batch completion failed")
        result_manifest = {
            "mode": "dry_run" if dry_run else "apply",
            "source_path": source_path,
            "format_manifest": {
                "format_id": normalized_result.get("format_id"),
                "sheet_name": normalized_result.get("sheet_name"),
                "header_row": normalized_result.get("header_row"),
                "normalized_row_count": len(normalized_result["normalized_rows"]),
            },
            "batch_id": None if dry_run else staging["batch_id"],
            "inserted_rows": inserted_rows,
            "skipped_existing": skipped_existing,
            "reconciled_counts": reconciled_counts,
            "pending_rows": [] if dry_run else pending_rows,
            "row_results": row_results,
            "transaction_outcome": "rolled_back" if dry_run else "committed",
        }
        if dry_run:
            connection.rollback()
        else:
            connection.commit()
        return result_manifest
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="Import finance Excel data")
    parser.add_argument("--check", action="store_true", help="Only verify readiness")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the complete import and reconciliation path, then roll back",
    )
    parser.add_argument(
        "--excel-path",
        default="document/資料庫、資料處理/帳務.xlsx",
    )
    args = parser.parse_args()
    if args.check:
        print("READY TO IMPORT")
        return 0
    if not os.path.exists(args.excel_path):
        raise FileNotFoundError(f"找不到帳務 Excel：{args.excel_path}")
    result = import_finance_workbook(args.excel_path, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    assert decode_va_to_case_no("99781699115001") == "115000001"
    raise SystemExit(main())
