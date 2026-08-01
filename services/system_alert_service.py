"""Mutable "process reminder" alerts, stored in system_alerts.

Unlike finance_alerts (immutable event-sourced, for audit-sensitive money
matters), system_alerts is a simple rolling-update table: one row per
(alert_code, case_key), whose `details` JSON gets overwritten on every
rescan. There is no append-only event history here on purpose -- these are
staff reminders ("go fix this case"), not records that need to survive
tamper-proof for an audit.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

_STATUSES = frozenset({"open", "claimed", "resolved"})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    details = row.get("details")
    if isinstance(details, str):
        try:
            row["details"] = json.loads(details)
        except json.JSONDecodeError:
            pass
    # UI 沿用 finance_alerts 慣用的 source_id 欄位名稱做顯示，這裡補一個別名。
    row.setdefault("source_id", row.get("case_key"))
    return row


def upsert_system_alert(
    cursor: Any,
    *,
    alert_code: str,
    source_domain: str,
    case_key: str,
    reason: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Create or roll-update the alert for (alert_code, case_key).

    Reopens a previously-resolved row if the problem has recurred. Leaves a
    `claimed` row's status alone (a human is already on it) but still
    refreshes `details`/`reason` underneath so they see current data.
    """
    details_json = json.dumps(details, ensure_ascii=False, sort_keys=True)
    cursor.execute(
        """SELECT id, status FROM system_alerts
           WHERE alert_code=%s AND case_key=%s
           FOR UPDATE""",
        (alert_code, case_key),
    )
    existing = cursor.fetchone()
    now = _now()
    if existing is None:
        cursor.execute(
            """INSERT INTO system_alerts
                   (alert_code, source_domain, case_key, reason, details, status)
               VALUES (%s, %s, %s, %s, %s, 'open')""",
            (alert_code, source_domain, case_key, reason, details_json),
        )
        alert_id = cursor.lastrowid
        result = "created"
    else:
        alert_id = existing["id"]
        new_status = "open" if existing["status"] == "resolved" else existing["status"]
        cursor.execute(
            """UPDATE system_alerts
               SET reason=%s, details=%s, status=%s, updated_at=%s
               WHERE id=%s""",
            (reason, details_json, new_status, now, alert_id),
        )
        result = "updated"
    cursor.execute("SELECT * FROM system_alerts WHERE id=%s", (alert_id,))
    return {"result": result, "alert": _decode_row(cursor.fetchone())}


def resolve_absent_alerts(
    cursor: Any,
    *,
    alert_code: str,
    still_open_case_keys: set[str],
    reason: str,
    operator: str = "system",
) -> int:
    """Resolve open (not claimed) rows for alert_code whose case_key cleared up."""
    cursor.execute(
        "SELECT id, case_key FROM system_alerts WHERE alert_code=%s AND status='open'",
        (alert_code,),
    )
    resolved = 0
    now = _now()
    for row in cursor.fetchall():
        if row["case_key"] in still_open_case_keys:
            continue
        cursor.execute(
            """UPDATE system_alerts
               SET status='resolved', resolved_by=%s, resolved_at=%s, resolution_reason=%s
               WHERE id=%s""",
            (operator, now, reason, row["id"]),
        )
        resolved += 1
    return resolved


def resolve_if_exists(
    cursor: Any,
    *,
    alert_code: str,
    case_key: str,
    reason: str,
    operator: str = "system",
) -> bool:
    """Resolve one specific (alert_code, case_key) row if it's currently open.

    For per-row callers (like a single import row that turned out clean) that
    can't compute a "still open" set the way a full-table rescan can.
    """
    cursor.execute(
        "SELECT id FROM system_alerts WHERE alert_code=%s AND case_key=%s AND status='open'",
        (alert_code, case_key),
    )
    row = cursor.fetchone()
    if row is None:
        return False
    cursor.execute(
        """UPDATE system_alerts
           SET status='resolved', resolved_by=%s, resolved_at=%s, resolution_reason=%s
           WHERE id=%s""",
        (operator, _now(), reason, row["id"]),
    )
    return True


def delete_system_alert(cursor: Any, *, alert_code: str, case_key: str) -> bool:
    """Remove a fallback-keyed row once it's superseded by a real case_no."""
    cursor.execute(
        "DELETE FROM system_alerts WHERE alert_code=%s AND case_key=%s",
        (alert_code, case_key),
    )
    return cursor.rowcount > 0


def list_system_alerts(
    cursor: Any,
    *,
    status: str | None = None,
    alert_code: str | None = None,
    source_domain: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        if status not in _STATUSES:
            raise ValueError("invalid system alert status")
        clauses.append("status=%s")
        params.append(status)
    if alert_code is not None:
        clauses.append("alert_code=%s")
        params.append(alert_code)
    if source_domain is not None:
        clauses.append("source_domain=%s")
        params.append(source_domain)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    cursor.execute(
        f"""SELECT * FROM system_alerts{where}
            ORDER BY updated_at DESC, id DESC
            LIMIT %s OFFSET %s""",
        tuple(params + [limit, offset]),
    )
    return [_decode_row(row) for row in cursor.fetchall()]


def get_system_alert(cursor: Any, alert_id: int) -> dict[str, Any] | None:
    cursor.execute("SELECT * FROM system_alerts WHERE id=%s", (alert_id,))
    row = cursor.fetchone()
    return _decode_row(row) if row else None


def claim_system_alert(cursor: Any, *, alert_id: int, operator: str) -> dict[str, Any]:
    cursor.execute("SELECT * FROM system_alerts WHERE id=%s FOR UPDATE", (alert_id,))
    alert = cursor.fetchone()
    if alert is None:
        raise ValueError("alert_id does not exist")
    if alert["status"] == "resolved":
        return {"result": "conflict", "alert": _decode_row(alert)}
    if alert["status"] == "claimed" and alert["claimed_by"] != operator:
        return {"result": "conflict", "alert": _decode_row(alert)}
    cursor.execute(
        "UPDATE system_alerts SET status='claimed', claimed_by=%s, claimed_at=%s WHERE id=%s",
        (operator, _now(), alert_id),
    )
    return {"result": "claimed", "alert": get_system_alert(cursor, alert_id)}


def resolve_system_alert(
    cursor: Any, *, alert_id: int, operator: str, reason: str
) -> dict[str, Any]:
    cursor.execute("SELECT * FROM system_alerts WHERE id=%s FOR UPDATE", (alert_id,))
    alert = cursor.fetchone()
    if alert is None:
        raise ValueError("alert_id does not exist")
    if alert["status"] == "claimed" and alert["claimed_by"] != operator:
        return {"result": "conflict", "alert": _decode_row(alert)}
    cursor.execute(
        """UPDATE system_alerts
           SET status='resolved', resolved_by=%s, resolved_at=%s, resolution_reason=%s
           WHERE id=%s""",
        (operator, _now(), reason, alert_id),
    )
    return {"result": "resolved", "alert": get_system_alert(cursor, alert_id)}
