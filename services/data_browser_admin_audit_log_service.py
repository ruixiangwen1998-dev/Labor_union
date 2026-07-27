"""
================================================================================
檔案名稱: services/data_browser_admin_audit_log_service.py
功能說明: Data Browser 微調異動紀錄至 audit_logs 服務 (DataBrowserAdminAuditLogService)
================================================================================
"""

import hashlib
import json
import uuid
from typing import Any, Dict, Optional

from services.db_service import get_connection

REQUIRED_AUDIT_COLUMNS = {
    "action",
    "table_name",
    "pk_value",
    "changed_fields",
    "actor",
    "role",
    "request_id",
    "before_hash",
    "after_hash",
    "occurred_at",
}


def _snapshot_to_json(snapshot: Optional[Dict[str, Any]]) -> str:
    if snapshot is None:
        return "null"
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)


def _snapshot_sha256(snapshot: Optional[Dict[str, Any]]) -> str:
    return hashlib.sha256(_snapshot_to_json(snapshot).encode("utf-8")).hexdigest()


def _assert_required_audit_schema(cursor) -> None:
    cursor.execute("SHOW COLUMNS FROM `audit_logs`")
    columns = {row.get("Field") for row in cursor.fetchall() or [] if row.get("Field")}
    missing = sorted(REQUIRED_AUDIT_COLUMNS - columns)
    if missing:
        raise RuntimeError(f"audit_logs table schema not prepared: missing columns {missing}")


def _split_audit_fields(
    table_name: str,
    pk_value: str,
    changed_fields: Dict[str, Any],
    actor: str,
    role: str,
    request_id: str,
    before_snapshot: Optional[Dict[str, Any]],
    after_snapshot: Optional[Dict[str, Any]],
):
    if not isinstance(changed_fields, dict):
        raise ValueError("changed_fields must be a dictionary.")

    before_hash = _snapshot_sha256(before_snapshot)
    after_hash = _snapshot_sha256(after_snapshot)

    insert_fields = (
        "action",
        "table_name",
        "pk_value",
        "changed_fields",
        "actor",
        "role",
        "request_id",
        "before_hash",
        "after_hash",
        "occurred_at",
    )
    values = ("%s", "%s", "%s", "%s", "%s", "%s", "%s", "%s", "%s", "NOW()")

    final_values = (
        "DATA_BROWSER_PATCH",
        table_name,
        str(pk_value),
        json.dumps(changed_fields, ensure_ascii=False, default=str),
        actor,
        role,
        request_id,
        before_hash,
        after_hash,
    )

    sql = f"INSERT INTO audit_logs ({', '.join(insert_fields)}) VALUES ({', '.join(values)})"
    return sql, tuple(final_values)


def record_data_browser_patch_audit(
    table_name: str,
    pk_value: str,
    changed_fields: Dict[str, Any],
    operator_id: str = "admin",
    actor: Optional[str] = None,
    role: str = "admin",
    before_snapshot: Optional[Dict[str, Any]] = None,
    after_snapshot: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
    cursor: Optional[Any] = None,
) -> bool:
    """寫入 Data Browser PATCH 異動紀錄"""
    actor_id = actor or operator_id
    event_id = request_id or str(uuid.uuid4())

    if cursor is not None:
        _assert_required_audit_schema(cursor)
        sql, final_values = _split_audit_fields(
            table_name=table_name,
            pk_value=str(pk_value),
            changed_fields=changed_fields,
            actor=actor_id,
            role=role,
            request_id=event_id,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
        cursor.execute(sql, final_values)
        return True

    conn = get_connection()
    try:
        with conn.cursor() as inner_cursor:
            _assert_required_audit_schema(inner_cursor)
            sql, final_values = _split_audit_fields(
                table_name=table_name,
                pk_value=str(pk_value),
                changed_fields=changed_fields,
                actor=actor_id,
                role=role,
                request_id=event_id,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
            )
            inner_cursor.execute(sql, final_values)
            conn.commit()
            return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
