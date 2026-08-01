"""Staff occupancy mutex helper for transaction-safe occupancy serialization."""

from __future__ import annotations

from typing import Any


def _validate_staff_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("staff_ids must contain positive integers")
    return value


def _normalise_row_id(row: Any) -> int:
    if isinstance(row, dict):
        if set(row.keys()) != {"id"}:
            raise ValueError("invalid staff row returned from cursor")
        value = row.get("id")
    else:
        raise ValueError("invalid staff row returned from cursor")

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("invalid staff id returned from cursor")
    return value


def lock_staff_occupancy_mutex(cursor: Any, staff_ids: list[Any]) -> list[int]:
    """Acquire FOR UPDATE locks on staff rows in ascending canonical order."""
    if not isinstance(staff_ids, list):
        raise ValueError("staff_ids must be a list")

    if not 1 <= len(staff_ids) <= 4:
        raise ValueError("staff_ids must have one to four ids")

    canonical = sorted({_validate_staff_id(item) for item in staff_ids})
    if len(canonical) != len(staff_ids):
        raise ValueError("staff_ids must not contain duplicates")

    placeholders = ", ".join(["%s"] * len(canonical))
    sql = (
        "SELECT id FROM staff WHERE id IN ("
        + placeholders
        + ") ORDER BY id FOR UPDATE"
    )
    cursor.execute(sql, tuple(canonical))
    rows = cursor.fetchall()

    if rows is None:
        rows = []
    locked_ids = [_normalise_row_id(row) for row in rows]

    if len(locked_ids) != len(canonical):
        raise ValueError("cannot lock all requested staff")

    normalized_locked = sorted(locked_ids)
    if len(set(normalized_locked)) != len(canonical) or normalized_locked != canonical:
        raise ValueError("locked staff ids do not match requested staff ids")

    return normalized_locked
