"""Service operations for the new assignment-based payment ledgers."""

from __future__ import annotations

from decimal import Decimal

from services.assignment_payroll_reconciliation_service import (
    reconcile_assignment_payroll_with_cursor,
)
from services.db_service import get_connection
from services.payment_rules import evaluate_payment_boundary


def calculate_staff_payable(service_hours, hourly_rate, floor_fee_amount, adjustment_amount=0) -> dict:
    hours = Decimal(str(service_hours))
    rate = Decimal(str(hourly_rate))
    floor_fee = Decimal(str(floor_fee_amount))
    adjustment = Decimal(str(adjustment_amount))
    if hours < 0 or rate < 0 or floor_fee < 0:
        raise ValueError("service hours, rate and floor fee cannot be negative")
    service_salary = hours * rate
    total_payable = service_salary + floor_fee + adjustment
    if total_payable < 0:
        raise ValueError("total payable cannot be negative")
    return {
        "service_hours": float(hours), "hourly_rate": float(rate),
        "service_salary": float(service_salary), "floor_fee_amount": float(floor_fee),
        "adjustment_amount": float(adjustment), "total_payable": float(total_payable),
    }


def create_case_staff_assignment(case_no, staff_id, assignment_sequence, hours, hourly_rate, floor_fee=0,
                                 start_date=None, end_date=None, status="planned", replacement_reason=None) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT service_days * service_hours_per_day AS total_hours, floor_fee FROM orders WHERE case_no = %s FOR UPDATE", (case_no,))
            order = cursor.fetchone()
            if not order:
                raise ValueError("case_no does not exist")
            cursor.execute("SELECT staff_id, COALESCE(actual_hours, planned_hours, 0) AS hours, floor_fee_allocated AS floor_fee FROM case_staff_assignments WHERE case_no = %s AND status <> 'cancelled' FOR UPDATE", (case_no,))
            assignments = cursor.fetchall()
            assignments.append({"staff_id": staff_id, "hours": hours, "floor_fee": floor_fee})
            result = evaluate_payment_boundary("assignment_allocation", order_hours=order["total_hours"], floor_fee=order["floor_fee"], assignments=assignments, finalized=False)
            if not result["valid"]:
                raise ValueError(result["error"])
            cursor.execute("""INSERT INTO case_staff_assignments
                (case_no, staff_id, assignment_sequence, assigned_start_date, assigned_end_date,
                 planned_hours, hourly_rate, floor_fee_allocated, status, replacement_reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (case_no, staff_id, assignment_sequence, start_date, end_date, hours, hourly_rate, floor_fee, status, replacement_reason))
            assignment_id = cursor.lastrowid
        conn.commit()
        return assignment_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_staff_payment(assignment_id, due_date=None, adjustment_amount=0) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT case_no FROM case_staff_assignments
                   WHERE id = %s FOR UPDATE""",
                (assignment_id,),
            )
            assignment = cursor.fetchone()
            if not assignment:
                raise ValueError("assignment does not exist")
            case_no = assignment["case_no"]
            if not isinstance(case_no, str) or not case_no.strip():
                raise ValueError("assignment requires a case_no")
            case_no = case_no.strip()

            reconciliation = reconcile_assignment_payroll_with_cursor(
                cursor, case_no
            )
            if not reconciliation["can_create_staff_payments"]:
                raise ValueError("assignment payroll reconciliation failed")

            reconciled_assignment = next(
                (
                    item
                    for item in reconciliation["assignments"]
                    if item["assignment_id"] == assignment_id
                ),
                None,
            )
            if reconciled_assignment is None:
                raise ValueError("assignment is not payable")

            payable = calculate_staff_payable(
                reconciled_assignment["actual_hours"]
                + reconciled_assignment["double_pay_hours"],
                reconciled_assignment["hourly_rate"],
                reconciled_assignment["floor_fee_allocated"],
                adjustment_amount,
            )
            cursor.execute("""INSERT INTO staff_payments
                (assignment_id, case_no, staff_id, service_hours, hourly_rate, service_salary,
                 floor_fee_amount, adjustment_amount, total_payable, due_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    assignment_id,
                    case_no,
                    reconciled_assignment["staff_id"],
                    payable["service_hours"],
                    payable["hourly_rate"],
                    payable["service_salary"],
                    payable["floor_fee_amount"],
                    payable["adjustment_amount"],
                    payable["total_payable"],
                    due_date,
                ))
            payment_id = cursor.lastrowid
        conn.commit()
        return payment_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
