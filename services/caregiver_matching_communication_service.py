"""Matching-plan communication, willingness, resume, and cancellation events."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Mapping

from services.caregiver_segment_availability_query_service import (
    search_segmented_caregiver_availability,
)
from services.db_service import get_connection
from services.line_task_service import enqueue_line_task


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field} is too long")
    return normalized


def _close(resource: Any) -> None:
    closer = getattr(resource, "close", None)
    if callable(closer):
        try:
            closer()
        except BaseException:
            pass


def _event_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("matching event payload is invalid")
    return dict(value)


def _load_contact_state(cursor: Any, case_no: str, plan_id: int) -> dict[str, Any]:
    cursor.execute(
        """SELECT p.id, p.case_no, p.version, p.status, p.is_active,
                  o.status AS order_status, c.line_user_id AS client_line_user_id
             FROM caregiver_matching_plans p
             JOIN orders o ON o.case_no = p.case_no
             JOIN clients c ON c.id = o.client_id
            WHERE p.id = %s AND p.case_no = %s""",
        (plan_id, case_no),
    )
    plan = cursor.fetchone()
    if not isinstance(plan, Mapping):
        raise ValueError("matching plan not found")
    cursor.execute(
        """SELECT s.id AS segment_id, s.segment_order, s.staff_id,
                  s.assigned_start_date, s.assigned_end_date,
                  st.name AS staff_name, st.line_user_id AS staff_line_user_id
             FROM caregiver_matching_plan_segments s
             JOIN staff st ON st.id = s.staff_id
            WHERE s.plan_id = %s
            ORDER BY s.segment_order ASC""",
        (plan_id,),
    )
    segments = [dict(row) for row in (cursor.fetchall() or [])]
    if not 1 <= len(segments) <= 4:
        raise ValueError("matching plan segments are invalid")
    cursor.execute(
        """SELECT id, segment_id, event_type, event_key, actor, payload, occurred_at
             FROM caregiver_matching_plan_events
            WHERE plan_id = %s
            ORDER BY occurred_at ASC, id ASC""",
        (plan_id,),
    )
    events = [dict(row) for row in (cursor.fetchall() or [])]
    latest_willingness: dict[int, str] = {}
    info_sent: dict[int, set[str]] = {}
    resume_sent: set[int] = set()
    for event in events:
        segment_id = event.get("segment_id")
        if not isinstance(segment_id, int):
            continue
        payload = _event_payload(event.get("payload"))
        if event.get("event_type") == "willingness_changed":
            status = payload.get("willingness")
            if status not in {"pending", "willing", "unwilling"}:
                raise ValueError("matching willingness event is invalid")
            latest_willingness[segment_id] = status
        elif event.get("event_type") in {"info_1_sent", "info_2_sent"}:
            info_sent.setdefault(segment_id, set()).add(event["event_type"])
        elif event.get("event_type") == "resume_sent":
            resume_sent.add(segment_id)
    public_segments = []
    for segment in segments:
        segment_id = _positive_int(segment["segment_id"], "segment_id")
        public_segments.append(
            {
                **segment,
                "assigned_start_date": segment["assigned_start_date"].isoformat()
                if hasattr(segment["assigned_start_date"], "isoformat")
                else str(segment["assigned_start_date"]),
                "assigned_end_date": segment["assigned_end_date"].isoformat()
                if hasattr(segment["assigned_end_date"], "isoformat")
                else str(segment["assigned_end_date"]),
                "willingness": latest_willingness.get(segment_id, "pending"),
                "info_1_sent": "info_1_sent" in info_sent.get(segment_id, set()),
                "info_2_sent": "info_2_sent" in info_sent.get(segment_id, set()),
                "resume_sent": segment_id in resume_sent,
            }
        )
    return {
        "plan": dict(plan),
        "segments": public_segments,
        "all_willing": all(
            segment["willingness"] == "willing" for segment in public_segments
        ),
    }


def get_matching_plan_contact_state(case_no: Any, plan_id: Any) -> dict[str, Any]:
    case_no = _text(case_no, "case_no", 50)
    plan_id = _positive_int(plan_id, "plan_id")
    connection = cursor = None
    try:
        connection = get_connection()
        cursor = connection.cursor()
        return _load_contact_state(cursor, case_no, plan_id)
    finally:
        _close(cursor)
        _close(connection)


def get_active_matching_plan_state(case_no: Any) -> dict[str, Any]:
    """Reload the active negotiation plan and its deposit-lock lifecycle."""
    case_no = _text(case_no, "case_no", 50)
    connection = cursor = None
    try:
        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute(
            """SELECT id
                 FROM caregiver_matching_plans
                WHERE case_no = %s
                  AND is_active = 1
                  AND status IN ('proposed', 'accepted')
                ORDER BY version DESC
                LIMIT 1""",
            (case_no,),
        )
        active_plan = cursor.fetchone()
        if not isinstance(active_plan, Mapping):
            raise ValueError("active matching plan not found")
        plan_id = _positive_int(active_plan.get("id"), "plan_id")
        state = _load_contact_state(cursor, case_no, plan_id)
        cursor.execute(
            """SELECT id AS lock_id, plan_id, status, created_by, created_at
                 FROM caregiver_availability_locks
                WHERE plan_id = %s
                  AND status = 'active'
                  AND is_active = 1
                LIMIT 1""",
            (plan_id,),
        )
        lock = cursor.fetchone()
        state["availability_lock"] = dict(lock) if isinstance(lock, Mapping) else None
        cursor.execute(
            """SELECT deposit_receivable, deposit_received, deposit_received_at
                 FROM client_payments
                WHERE case_no = %s""",
            (case_no,),
        )
        payment = cursor.fetchone()
        state["deposit"] = dict(payment) if isinstance(payment, Mapping) else None
        return state
    finally:
        _close(cursor)
        _close(connection)


def _assert_latest_plan_availability(state: Mapping[str, Any]) -> None:
    plan = state["plan"]
    segments = state["segments"]
    result = search_segmented_caregiver_availability(
        case_no=plan["case_no"],
        segment_count=len(segments),
        segment_drafts=[
            {
                "staff_id": segment["staff_id"],
                "start_date": segment["assigned_start_date"],
                "end_date": segment["assigned_end_date"],
            }
            for segment in segments
        ],
        as_of=date.today().isoformat(),
    )
    conflicts = result.get("conflicts")
    if result.get("feasibility") != "complete" or not isinstance(conflicts, list):
        raise ValueError("matching plan is no longer fully available")
    if conflicts:
        details = ", ".join(
            f"staff={row.get('staff_id')} date={row.get('work_date')} reason={row.get('reason_code')}"
            for row in conflicts
        )
        raise ValueError("matching plan availability conflict: " + details)


def send_matching_plan_information(
    case_no: Any,
    plan_id: Any,
    segment_id: Any,
    info_type: Any,
    event_key: Any,
    actor: Any,
) -> dict[str, Any]:
    case_no = _text(case_no, "case_no", 50)
    plan_id = _positive_int(plan_id, "plan_id")
    segment_id = _positive_int(segment_id, "segment_id")
    if info_type not in {1, 2}:
        raise ValueError("info_type must be 1 or 2")
    event_key = _text(event_key, "event_key", 100)
    actor = _text(actor, "actor", 100)
    state = get_matching_plan_contact_state(case_no, plan_id)
    if state["plan"].get("status") != "proposed" or state["plan"].get("is_active") != 1:
        raise ValueError("matching plan is not an active proposed plan")
    _assert_latest_plan_availability(state)
    segment = next(
        (row for row in state["segments"] if row["segment_id"] == segment_id),
        None,
    )
    if segment is None:
        raise ValueError("segment does not belong to matching plan")
    recipient = segment.get("staff_line_user_id")
    if not isinstance(recipient, str) or not recipient.strip():
        raise ValueError("caregiver has no LINE delivery identity")

    connection = cursor = None
    try:
        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute(
            """SELECT id, plan_id, segment_id, event_type
                 FROM caregiver_matching_plan_events
                WHERE event_key = %s FOR UPDATE""",
            (event_key,),
        )
        existing = cursor.fetchone()
        event_type = f"info_{info_type}_sent"
        if existing is not None:
            if (
                existing.get("plan_id") == plan_id
                and existing.get("segment_id") == segment_id
                and existing.get("event_type") == event_type
            ):
                connection.rollback()
                return {"status": "idempotent_replay", "event_id": existing["id"]}
            raise ValueError("event_key belongs to a different matching event")
        cursor.execute(
            """SELECT p.status, p.is_active, o.status AS order_status
                 FROM caregiver_matching_plans p
                 JOIN orders o ON o.case_no = p.case_no
                WHERE p.id = %s AND p.case_no = %s FOR UPDATE""",
            (plan_id, case_no),
        )
        locked = cursor.fetchone()
        if (
            not isinstance(locked, Mapping)
            or locked.get("status") != "proposed"
            or locked.get("is_active") != 1
            or locked.get("order_status") != "洽談中"
        ):
            raise ValueError("matching plan is no longer sendable")
        message = (
            f"訂單資訊-{info_type}\n"
            f"服務區段：{segment['assigned_start_date']}～{segment['assigned_end_date']}"
        )
        task_id = enqueue_line_task(
            cursor,
            to_user_id=recipient.strip(),
            message_content=message,
            payload={
                "case_no": case_no,
                "plan_id": plan_id,
                "segment_id": segment_id,
                "info_type": info_type,
            },
            source_event_id=event_key,
            idempotency_key=event_key,
        )
        if task_id is None:
            raise ValueError("LINE information delivery task was not created")
        payload = {"line_task_id": task_id, "delivery_status": "queued"}
        cursor.execute(
            """INSERT INTO caregiver_matching_plan_events
                   (plan_id, segment_id, event_type, event_key, actor, payload)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                plan_id,
                segment_id,
                event_type,
                event_key,
                actor,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        event_id = _positive_int(cursor.lastrowid, "event_id")
        connection.commit()
        return {
            "status": "sent",
            "event_id": event_id,
            "line_task_id": task_id,
            "delivery_status": "queued",
        }
    except Exception:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        _close(cursor)
        _close(connection)


def record_matching_plan_willingness(
    case_no: Any,
    plan_id: Any,
    segment_id: Any,
    willingness: Any,
    event_key: Any,
    actor: Any,
) -> dict[str, Any]:
    case_no = _text(case_no, "case_no", 50)
    plan_id = _positive_int(plan_id, "plan_id")
    segment_id = _positive_int(segment_id, "segment_id")
    if willingness not in {"pending", "willing", "unwilling"}:
        raise ValueError("willingness is invalid")
    event_key = _text(event_key, "event_key", 100)
    actor = _text(actor, "actor", 100)
    connection = cursor = None
    try:
        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute(
            """SELECT p.status, p.is_active, s.id AS segment_id
                 FROM caregiver_matching_plans p
                 JOIN caregiver_matching_plan_segments s ON s.plan_id = p.id
                WHERE p.id = %s AND p.case_no = %s AND s.id = %s
                FOR UPDATE""",
            (plan_id, case_no, segment_id),
        )
        row = cursor.fetchone()
        if (
            not isinstance(row, Mapping)
            or row.get("status") not in {"proposed", "accepted"}
            or row.get("segment_id") != segment_id
        ):
            raise ValueError("matching segment is not editable")
        cursor.execute(
            """SELECT id, plan_id, segment_id, event_type, payload
                 FROM caregiver_matching_plan_events
                WHERE event_key = %s FOR UPDATE""",
            (event_key,),
        )
        existing = cursor.fetchone()
        payload = {"willingness": willingness}
        if existing is not None:
            if (
                existing.get("plan_id") == plan_id
                and existing.get("segment_id") == segment_id
                and existing.get("event_type") == "willingness_changed"
                and _event_payload(existing.get("payload")) == payload
            ):
                connection.rollback()
                return {"status": "idempotent_replay", "event_id": existing["id"]}
            raise ValueError("event_key belongs to a different matching event")
        cursor.execute(
            """INSERT INTO caregiver_matching_plan_events
                   (plan_id, segment_id, event_type, event_key, actor, payload)
               VALUES (%s, %s, 'willingness_changed', %s, %s, %s)""",
            (
                plan_id,
                segment_id,
                event_key,
                actor,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        event_id = _positive_int(cursor.lastrowid, "event_id")
        connection.commit()
        return {"status": "recorded", "event_id": event_id, **payload}
    except Exception:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        _close(cursor)
        _close(connection)


def send_matching_plan_resumes(
    case_no: Any,
    plan_id: Any,
    note: Any,
    event_key: Any,
    actor: Any,
) -> dict[str, Any]:
    case_no = _text(case_no, "case_no", 50)
    plan_id = _positive_int(plan_id, "plan_id")
    note = _text(note, "note", 1000)
    event_key = _text(event_key, "event_key", 100)
    actor = _text(actor, "actor", 100)
    state = get_matching_plan_contact_state(case_no, plan_id)
    if not state["all_willing"]:
        missing = [
            {
                "segment_id": row["segment_id"],
                "staff_id": row["staff_id"],
                "willingness": row["willingness"],
            }
            for row in state["segments"]
            if row["willingness"] != "willing"
        ]
        raise ValueError("all caregivers must be willing before resume delivery: " + json.dumps(missing))
    recipient = state["plan"].get("client_line_user_id")
    if not isinstance(recipient, str) or not recipient.strip():
        raise ValueError("client has no LINE delivery identity")
    if len(state["segments"]) > 1 and "由多位月嫂共同完成" not in note:
        note = "本案預計由多位月嫂共同完成服務。" + note

    connection = cursor = None
    try:
        connection = get_connection()
        cursor = connection.cursor()
        derived_keys = [
            "resume-" + hashlib.sha256(
                f"{event_key}:{segment['segment_id']}".encode("utf-8")
            ).hexdigest()
            for segment in state["segments"]
        ]
        placeholders = ", ".join(["%s"] * len(derived_keys))
        cursor.execute(
            f"""SELECT id, event_key, plan_id, segment_id, event_type
                  FROM caregiver_matching_plan_events
                 WHERE event_key IN ({placeholders})
                 FOR UPDATE""",
            tuple(derived_keys),
        )
        existing = [dict(row) for row in (cursor.fetchall() or [])]
        if existing:
            if len(existing) == len(derived_keys) and all(
                row["plan_id"] == plan_id and row["event_type"] == "resume_sent"
                for row in existing
            ):
                connection.rollback()
                return {
                    "status": "idempotent_replay",
                    "event_ids": [row["id"] for row in existing],
                }
            raise ValueError("resume event_key has a partial or conflicting history")
        cursor.execute(
            """SELECT p.status, p.is_active, o.status AS order_status
                 FROM caregiver_matching_plans p
                 JOIN orders o ON o.case_no = p.case_no
                WHERE p.id = %s AND p.case_no = %s FOR UPDATE""",
            (plan_id, case_no),
        )
        locked = cursor.fetchone()
        if (
            not isinstance(locked, Mapping)
            or locked.get("status") not in {"proposed", "accepted"}
            or locked.get("order_status") != "洽談中"
        ):
            raise ValueError("matching plan is no longer eligible for resume delivery")
        event_ids, task_ids = [], []
        for segment, derived_key in zip(state["segments"], derived_keys):
            message = (
                f"月嫂履歷（第 {segment['segment_order']} 段）\n"
                f"服務區段：{segment['assigned_start_date']}～{segment['assigned_end_date']}\n"
                f"備註：{note}"
            )
            task_id = enqueue_line_task(
                cursor,
                to_user_id=recipient.strip(),
                message_content=message,
                payload={
                    "case_no": case_no,
                    "plan_id": plan_id,
                    "segment_id": segment["segment_id"],
                    "resume_note": note,
                },
                source_event_id=derived_key,
                idempotency_key=derived_key,
            )
            if task_id is None:
                raise ValueError("LINE resume delivery task was not created")
            payload = {
                "line_task_id": task_id,
                "delivery_status": "queued",
                "note": note,
            }
            cursor.execute(
                """INSERT INTO caregiver_matching_plan_events
                       (plan_id, segment_id, event_type, event_key, actor, payload)
                   VALUES (%s, %s, 'resume_sent', %s, %s, %s)""",
                (
                    plan_id,
                    segment["segment_id"],
                    derived_key,
                    actor,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            event_ids.append(_positive_int(cursor.lastrowid, "event_id"))
            task_ids.append(task_id)
        connection.commit()
        return {
            "status": "sent",
            "event_ids": event_ids,
            "line_task_ids": task_ids,
            "delivery_status": "queued",
            "note": note,
        }
    except Exception:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        _close(cursor)
        _close(connection)


def cancel_matching_plan(
    case_no: Any,
    plan_id: Any,
    event_key: Any,
    actor: Any,
    reason: Any,
) -> dict[str, Any]:
    case_no = _text(case_no, "case_no", 50)
    plan_id = _positive_int(plan_id, "plan_id")
    event_key = _text(event_key, "event_key", 100)
    actor = _text(actor, "actor", 100)
    reason = _text(reason, "reason", 255)
    connection = cursor = None
    try:
        connection = get_connection()
        cursor = connection.cursor()
        cursor.execute(
            """SELECT p.status, p.is_active, o.status AS order_status
                 FROM caregiver_matching_plans p
                 JOIN orders o ON o.case_no = p.case_no
                WHERE p.id = %s AND p.case_no = %s FOR UPDATE""",
            (plan_id, case_no),
        )
        plan = cursor.fetchone()
        if not isinstance(plan, Mapping):
            raise ValueError("matching plan not found")
        cursor.execute(
            """SELECT id, plan_id, segment_id, event_type, payload
                 FROM caregiver_matching_plan_events
                WHERE event_key = %s FOR UPDATE""",
            (event_key,),
        )
        existing = cursor.fetchone()
        if existing is not None:
            if (
                existing.get("plan_id") == plan_id
                and existing.get("segment_id") is None
                and existing.get("event_type") == "plan_cancelled"
                and _event_payload(existing.get("payload")).get("reason") == reason
            ):
                connection.rollback()
                return {"status": "idempotent_replay", "event_id": existing["id"]}
            raise ValueError("event_key belongs to a different matching event")
        if (
            plan.get("status") != "proposed"
            or plan.get("is_active") != 1
            or plan.get("order_status") != "洽談中"
        ):
            raise ValueError("only an active proposed negotiation plan can be cancelled")
        cursor.execute(
            """UPDATE caregiver_matching_plans
                  SET status = 'cancelled', is_active = NULL
                WHERE id = %s AND case_no = %s
                  AND status = 'proposed' AND is_active = 1""",
            (plan_id, case_no),
        )
        if cursor.rowcount != 1:
            raise ValueError("matching plan cancellation did not affect one row")
        cursor.execute(
            """INSERT INTO caregiver_matching_plan_events
                   (plan_id, segment_id, event_type, event_key, actor, payload)
               VALUES (%s, NULL, 'plan_cancelled', %s, %s, %s)""",
            (
                plan_id,
                event_key,
                actor,
                json.dumps({"reason": reason}, ensure_ascii=False, sort_keys=True),
            ),
        )
        event_id = _positive_int(cursor.lastrowid, "event_id")
        connection.commit()
        return {"status": "cancelled", "event_id": event_id}
    except Exception:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        _close(cursor)
        _close(connection)
