from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from services.accounting_source_projection import load_case_accounting_source_with_cursor
from services.client_payment_transactions import calculate_client_payment_state
from services.client_subsidy_return_obligations import (
    activate_subsidy_return_obligation,
    calculate_subsidy_return_due_date,
)
from services.db_service import get_connection
from services.order_amount_calculator import calculate_order_amounts


def _activate_subsidy_return_obligation_after_full_receipt(cursor, payment):
    """Calculate and activate the obligation using canonical projected facts."""
    accounting_source = load_case_accounting_source_with_cursor(
        cursor, payment["case_no"]
    )
    order_info = accounting_source["order"]
    client_info = accounting_source["client"]
    actual_end_date = order_info.get("actual_end_date")
    if not actual_end_date:
        return None

    due_date = calculate_subsidy_return_due_date(actual_end_date)
    order_terms = {
        "case_no": payment["case_no"],
        "service_days": order_info.get("service_days"),
        "service_hours_per_day": order_info.get("service_hours_per_day"),
        "identity_status": client_info.get("identity_status"),
        "client_floor_fee": order_info.get("floor_fee") or 0,
        "service_start_date": (
            order_info.get("actual_start_date") or order_info.get("start_date")
        ),
        "actual_completion_date": actual_end_date,
    }
    calculated_plan = calculate_order_amounts(
        order_terms,
        [],
        accounting_source["collection_schedule"],
    )

    subsidy_return_amount = Decimal(
        str(calculated_plan["client_ledger_plan"]["subsidy_return_amount"])
    )
    if subsidy_return_amount <= 0:
        return None

    return activate_subsidy_return_obligation(
        cursor,
        payment["id"],
        subsidy_return_amount,
        due_date,
    )


def _persist_subsidy_return_review(cursor, client_payment_id, reason):
    cursor.execute(
        """UPDATE client_payments
           SET subsidy_return_review_status=%s,
               subsidy_return_review_reason=%s
           WHERE id=%s""",
        ("review_required", reason, client_payment_id),
    )


def _clear_subsidy_return_review(cursor, client_payment_id):
    cursor.execute(
        """UPDATE client_payments
           SET subsidy_return_review_status=NULL,
               subsidy_return_review_reason=NULL
           WHERE id=%s""",
        (client_payment_id,),
    )


_ACTIVE_STAGES = frozenset({"deposit", "first_payment", "second_payment"})
_TRANSACTION_TYPES = frozenset({"receipt", "reversal"})
_TRANSACTION_STATUSES = frozenset({"succeeded", "failed", "reversed"})


def build_client_summary_update(receivables, transactions, occurred_at):
    state = calculate_client_payment_state(receivables, transactions)
    settlement_dates = _settlement_dates(receivables, transactions, occurred_at)
    first_received_at = settlement_dates["first_payment_received_at"]
    second_due_date = None
    if Decimal(str(receivables["second_payment"])) > 0 and first_received_at:
        second_due_date = (_as_date(first_received_at) + timedelta(days=15)).isoformat()
    return state | settlement_dates | {"second_payment_due_date": second_due_date}


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise ValueError("occurred_at must be a date or ISO date string")


def _settlement_dates(receivables, transactions, fallback_occurred_at):
    received = {"deposit": Decimal("0"), "first_payment": Decimal("0"), "second_payment": Decimal("0")}
    dates = {stage: None for stage in received}
    ordered_transactions = sorted(
        enumerate(transactions),
        key=lambda item: (_as_date(item[1].get("occurred_at") or fallback_occurred_at), item[0]),
    )
    for _, transaction in ordered_transactions:
        if transaction.get("transaction_status") != "succeeded":
            continue
        stage = transaction.get("stage")
        if stage not in received:
            continue
        amount = Decimal(str(transaction["amount"]))
        transaction_type = transaction.get("transaction_type")
        received[stage] += amount if transaction_type == "receipt" else -amount
        receivable = Decimal(str(receivables[stage]))
        if receivable == 0:
            dates[stage] = None
        elif received[stage] == receivable:
            dates[stage] = _as_date(transaction.get("occurred_at") or fallback_occurred_at).isoformat()
        else:
            dates[stage] = None
    return {
        "deposit_received_at": dates["deposit"],
        "first_payment_received_at": dates["first_payment"],
        "second_payment_received_at": dates["second_payment"],
    }


def _validated_transaction(transaction):
    if not isinstance(transaction, dict):
        raise ValueError("transaction must be a dict")
    required = {
        "stage", "transaction_type", "transaction_status", "amount",
        "occurred_at", "external_reference", "finance_import_row_id",
    }
    missing = required - set(transaction)
    if missing:
        raise ValueError(f"transaction is missing fields: {', '.join(sorted(missing))}")

    stage = transaction["stage"]
    if stage not in _ACTIVE_STAGES:
        raise ValueError("subsidy refund/return and adjustment transactions are not enabled")
    if transaction["transaction_type"] not in _TRANSACTION_TYPES:
        raise ValueError("transaction_type must be receipt or reversal")
    if transaction["transaction_status"] not in _TRANSACTION_STATUSES:
        raise ValueError("invalid transaction_status")
    try:
        amount = Decimal(str(transaction["amount"]))
    except (InvalidOperation, ValueError):
        raise ValueError("transaction amount must be a positive finite number") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("transaction amount must be a positive finite number")

    occurred_at = _as_date(transaction["occurred_at"]).isoformat()
    reference = transaction["external_reference"]
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("external_reference must be a non-empty string")
    reference = reference.strip()
    finance_import_row_id = transaction["finance_import_row_id"]
    if finance_import_row_id is not None and (
        isinstance(finance_import_row_id, bool)
        or not isinstance(finance_import_row_id, int)
        or finance_import_row_id < 1
    ):
        raise ValueError("finance_import_row_id must be a positive integer or None")
    if reference.startswith("fp:") and finance_import_row_id is None:
        raise ValueError("bank reconciliation references require finance_import_row_id")
    notes = transaction.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("notes must be a string or None")

    return {
        "stage": stage,
        "transaction_type": transaction["transaction_type"],
        "transaction_status": transaction["transaction_status"],
        "amount": amount,
        "occurred_at": occurred_at,
        "external_reference": reference,
        "finance_import_row_id": finance_import_row_id,
        "notes": notes,
    }


def _same_transaction(existing, candidate, client_payment_id, case_no):
    try:
        existing_amount = Decimal(str(existing.get("amount")))
        existing_date = _as_date(existing.get("occurred_at")).isoformat()
    except (InvalidOperation, ValueError, TypeError):
        return False
    return (
        existing.get("client_payment_id") == client_payment_id
        and existing.get("case_no") == case_no
        and existing.get("stage") == candidate["stage"]
        and existing.get("transaction_type") == candidate["transaction_type"]
        and existing.get("transaction_status") == candidate["transaction_status"]
        and existing_amount == candidate["amount"]
        and existing_date == candidate["occurred_at"]
        and existing.get("external_reference") == candidate["external_reference"]
        and existing.get("finance_import_row_id") == candidate["finance_import_row_id"]
        and existing.get("notes") == candidate["notes"]
    )


def record_client_payment_transaction_with_cursor(cursor, client_payment_id, transaction):
    """Write one transaction using a caller-owned database transaction."""
    if isinstance(client_payment_id, bool) or not isinstance(client_payment_id, int) or client_payment_id < 1:
        raise ValueError("client_payment_id must be a positive integer")
    candidate = _validated_transaction(transaction)

    cursor.execute("SELECT * FROM client_payments WHERE id = %s FOR UPDATE", (client_payment_id,))
    payment = cursor.fetchone()
    if not payment:
        raise ValueError("client payment does not exist")

    cursor.execute(
        """SELECT id, client_payment_id, case_no, stage, transaction_type,
                  transaction_status, amount, occurred_at, external_reference,
                  finance_import_row_id, notes
           FROM client_payment_transactions
           WHERE external_reference = %s
           FOR UPDATE""",
        (candidate["external_reference"],),
    )
    conflicts = cursor.fetchall()
    existing = None
    if conflicts:
        if len(conflicts) != 1 or not _same_transaction(
            conflicts[0], candidate, client_payment_id, payment["case_no"]
        ):
            raise ValueError("external_reference conflicts with another transaction")
        existing = conflicts[0]

    cursor.execute(
        """SELECT id, stage, transaction_type, transaction_status, amount,
                  occurred_at, external_reference, finance_import_row_id
           FROM client_payment_transactions
           WHERE client_payment_id = %s
             AND stage IN ('deposit', 'first_payment', 'second_payment')
           ORDER BY occurred_at, id
           FOR UPDATE""",
        (client_payment_id,),
    )
    transactions = cursor.fetchall()
    receivables = {
        "deposit": payment["deposit_receivable"],
        "first_payment": payment["first_payment_receivable"],
        "second_payment": payment["second_payment_receivable"],
    }

    if existing is not None:
        if not any(row.get("id") == existing["id"] for row in transactions):
            raise ValueError("existing transaction is missing from the locked client ledger")
        update = build_client_summary_update(receivables, transactions, candidate["occurred_at"])
        return {"transaction_id": existing["id"], **update}

    prior_update = build_client_summary_update(receivables, transactions, candidate["occurred_at"])
    transactions.append(candidate)
    update = build_client_summary_update(receivables, transactions, candidate["occurred_at"])
    cursor.execute(
        """INSERT INTO client_payment_transactions
           (client_payment_id, case_no, stage, transaction_type,
            transaction_status, amount, occurred_at, external_reference,
            finance_import_row_id, notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            client_payment_id,
            payment["case_no"],
            candidate["stage"],
            candidate["transaction_type"],
            candidate["transaction_status"],
            candidate["amount"],
            candidate["occurred_at"],
            candidate["external_reference"],
            candidate["finance_import_row_id"],
            candidate["notes"],
        ),
    )
    transaction_id = cursor.lastrowid
    if candidate["transaction_status"] != "succeeded":
        return {"transaction_id": transaction_id, **prior_update}
    cursor.execute(
        """UPDATE client_payments SET deposit_received=%s, first_payment_received=%s,
           second_payment_received=%s, amount_received=%s,
           deposit_received_at=%s, first_payment_received_at=%s,
           second_payment_received_at=%s, second_payment_due_date=%s
           WHERE id=%s""",
        (
            update["deposit_received"],
            update["first_payment_received"],
            update["second_payment_received"],
            update["amount_received"],
            update["deposit_received_at"],
            update["first_payment_received_at"],
            update["second_payment_received_at"],
            update["second_payment_due_date"],
            client_payment_id,
        ),
    )
    if not prior_update["deposit_received_at"] and update["deposit_received_at"]:
        cursor.execute(
            """SELECT l.id
                 FROM caregiver_availability_locks l
                 JOIN caregiver_matching_plans p ON p.id = l.plan_id
                WHERE p.case_no = %s
                  AND l.status = 'active'
                  AND l.is_active = 1
                LIMIT 1
                FOR UPDATE""",
            (payment["case_no"],),
        )
        active_lock = cursor.fetchone()
        if active_lock is None:
            cursor.execute(
                """UPDATE orders
                   SET status = '訂單成立'
                   WHERE case_no = %s AND status = '洽談中'""",
                (payment["case_no"],),
            )
        else:
            update["awaiting_availability_lock_conversion"] = True

    total_receivable_val = payment.get("amount_receivable")
    if total_receivable_val is None:
        total_receivable_val = receivables["deposit"] + receivables["first_payment"] + receivables["second_payment"]
    amount_receivable = Decimal(str(total_receivable_val))
    amount_received = Decimal(str(update["amount_received"]))

    existing_return_receivable = payment.get("subsidy_return_receivable")
    if (
        candidate["transaction_type"] == "reversal"
        and existing_return_receivable is not None
        and Decimal(str(existing_return_receivable)) > 0
        and amount_received < amount_receivable
    ):
        reason = "client_receipt_reversal_below_receivable"
        _persist_subsidy_return_review(cursor, client_payment_id, reason)
        update["subsidy_return_review_status"] = "review_required"
        update["subsidy_return_review_reason"] = reason

    if (
        candidate["transaction_type"] == "receipt"
        and amount_received == amount_receivable
    ):
        activation_res = _activate_subsidy_return_obligation_after_full_receipt(
            cursor, payment
        )
        if activation_res:
            update["subsidy_return_activation"] = activation_res
            if activation_res["result"] in {"activated", "existing"}:
                _clear_subsidy_return_review(cursor, client_payment_id)
                update["subsidy_return_review_status"] = None
                update["subsidy_return_review_reason"] = None
            elif activation_res["result"] == "review_required":
                reason = "subsidy_return_obligation_requires_review"
                _persist_subsidy_return_review(cursor, client_payment_id, reason)
                update["subsidy_return_review_status"] = "review_required"
                update["subsidy_return_review_reason"] = reason

    return {"transaction_id": transaction_id, **update}


def record_client_payment_transaction(
    client_payment_id,
    stage,
    transaction_type,
    transaction_status,
    amount,
    occurred_at,
    external_reference,
    notes=None,
    finance_import_row_id=None,
):
    """Compatibility wrapper that owns the connection transaction."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            result = record_client_payment_transaction_with_cursor(
                cursor,
                client_payment_id,
                {
                    "stage": stage,
                    "transaction_type": transaction_type,
                    "transaction_status": transaction_status,
                    "amount": amount,
                    "occurred_at": occurred_at,
                    "external_reference": external_reference,
                    "finance_import_row_id": finance_import_row_id,
                    "notes": notes,
                },
            )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
