from __future__ import annotations

from pathlib import Path
from typing import Any
import ast
import re

import pytest

from services import staff_occupancy_mutex_service as service


class StaffOccupancyCursor:
    def __init__(
        self,
        rows: list[Any] | None = None,
        execute_exception: Exception | None = None,
        fetchall_exception: Exception | None = None,
    ):
        self.rows = rows or []
        self.execute_exception = execute_exception
        self.fetchall_exception = fetchall_exception
        self.execute_calls: list[tuple[str, tuple[int, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None):
        normalized = " ".join(sql.split())
        self.execute_calls.append((normalized, tuple(params) if params is not None else ()))
        if self.execute_exception is not None:
            raise self.execute_exception

    def fetchall(self):
        if self.fetchall_exception is not None:
            raise self.fetchall_exception
        return self.rows


@pytest.mark.parametrize(
    ("staff_ids", "rows"),
    [
        ([2, 1], [{"id": 1}, {"id": 2}]),
        ([1, 4, 3], [{"id": 3}, {"id": 1}, {"id": 4}]),
        ([3, 1, 2], [{"id": 1}, {"id": 2}, {"id": 3}]),
        ([4, 2, 1, 3], [{"id": 4}, {"id": 2}, {"id": 1}, {"id": 3}]),
    ],
)
def test_lock_staff_occupancy_mutex_sorts_canonical_and_executes_once(staff_ids, rows):
    cursor = StaffOccupancyCursor(rows=rows)

    locked = service.lock_staff_occupancy_mutex(cursor, staff_ids)

    assert locked == sorted(staff_ids)
    assert len(cursor.execute_calls) == 1
    statement, params = cursor.execute_calls[0]
    assert "SELECT id FROM staff WHERE id IN" in statement
    assert "ORDER BY id FOR UPDATE" in statement
    assert statement.count("%s") == len(staff_ids)
    assert list(params) == locked


@pytest.mark.parametrize(
    "staff_ids",
    [
        None,
        (),
        {1, 2},
        "1,2",
        1,
        {"a": 1},
    ],
)
def test_lock_staff_occupancy_mutex_rejects_non_list_staff_ids(staff_ids):
    cursor = StaffOccupancyCursor()
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, staff_ids)


@pytest.mark.parametrize(
    "staff_ids",
    [
        [],
        [1, 2, 3, 4, 5],
    ],
)
def test_lock_staff_occupancy_mutex_rejects_invalid_size(staff_ids):
    cursor = StaffOccupancyCursor()
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, staff_ids)


@pytest.mark.parametrize(
    "staff_ids",
    [
        [0],
        [-1],
        [1, 0, 2],
        [True],
        [False],
        [1, "2"],
        [1.5],
        [None],
    ],
)
def test_lock_staff_occupancy_mutex_rejects_non_positive_non_integer_ids(staff_ids):
    cursor = StaffOccupancyCursor()
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, staff_ids)


def test_lock_staff_occupancy_mutex_rejects_duplicate_staff_ids():
    cursor = StaffOccupancyCursor(rows=[{"id": 1}, {"id": 1}])
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, [1, 1])


def test_lock_staff_occupancy_mutex_rejects_missing_db_rows():
    cursor = StaffOccupancyCursor(rows=[{"id": 1}])
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, [1, 3])


def test_lock_staff_occupancy_mutex_rejects_extra_db_rows():
    cursor = StaffOccupancyCursor(rows=[{"id": 1}, {"id": 2}, {"id": 3}])
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, [1, 2])


def test_lock_staff_occupancy_mutex_rejects_duplicate_db_rows():
    cursor = StaffOccupancyCursor(rows=[{"id": 2}, {"id": 2}])
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, [2])


def test_lock_staff_occupancy_mutex_rejects_scalar_db_row():
    cursor = StaffOccupancyCursor(rows=[1, 2, 3])
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, [1, 2, 3])


def test_lock_staff_occupancy_mutex_rejects_tuple_db_row():
    cursor = StaffOccupancyCursor(rows=[(1,), (2,), (3,)])
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, [1, 2, 3])


def test_lock_staff_occupancy_mutex_rejects_tuple_with_unexpected_extra_column():
    cursor = StaffOccupancyCursor(rows=[(1, "unexpected-extra-column")])
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, [1])


def test_lock_staff_occupancy_mutex_rejects_row_with_extra_mapping_keys():
    cursor = StaffOccupancyCursor(rows=[{"id": 1, "name": "n1"}])
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, [1])


def test_lock_staff_occupancy_mutex_rejects_row_with_missing_mapping_key():
    cursor = StaffOccupancyCursor(rows=[{"name": "n1"}])
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, [1])


def test_lock_staff_occupancy_mutex_rejects_row_list_shape():
    cursor = StaffOccupancyCursor(rows=[[1], [2]])
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, [1, 2])


@pytest.mark.parametrize("invalid_id", [True, False, 0, -1, "1", 1.0, None])
def test_lock_staff_occupancy_mutex_rejects_invalid_dict_cursor_id(invalid_id):
    cursor = StaffOccupancyCursor(rows=[{"id": invalid_id}])
    with pytest.raises(ValueError):
        service.lock_staff_occupancy_mutex(cursor, [1])


@pytest.mark.parametrize(
    ("exception", "api"),
    [
        (RuntimeError("execute fail"), "execute"),
        (RuntimeError("fetchall fail"), "fetchall"),
    ],
)
def test_lock_staff_occupancy_mutex_propagates_cursor_exceptions(exception, api):
    cursor = StaffOccupancyCursor(
        rows=[{"id": 1}],
        execute_exception=exception if api == "execute" else None,
        fetchall_exception=exception if api == "fetchall" else None,
    )
    if api == "execute":
        with pytest.raises(RuntimeError, match="execute fail"):
            service.lock_staff_occupancy_mutex(cursor, [1])
        assert len(cursor.execute_calls) == 1
    else:
        with pytest.raises(RuntimeError, match="fetchall fail"):
            service.lock_staff_occupancy_mutex(cursor, [1])
        assert len(cursor.execute_calls) == 1


def test_lock_staff_occupancy_mutex_source_no_db_mutation_or_cursor_lifecycle():
    source = Path(service.__file__).resolve().read_text(encoding="utf-8")
    lowered = source.lower()
    for token in ("get_connection", ".commit(", ".rollback(", ".close("):
        assert token not in lowered

    for token in ("insert into", "delete from", "replace ", "drop ", "truncate "):
        assert token not in lowered

    tree = ast.parse(source)
    imported_modules = set()
    attribute_calls = set()
    direct_calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                attribute_calls.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                direct_calls.add(node.func.id)

    assert imported_modules.isdisjoint({"time", "datetime"})
    assert "cursor" not in attribute_calls
    assert attribute_calls.isdisjoint({"now", "utcnow", "today", "time"})
    assert direct_calls.isdisjoint({"now", "utcnow", "today"})

    assert not re.search(r"\binsert\b", lowered)
    assert not re.search(r"\bdelete\b", lowered)
    assert not re.search(r"\breplace\b", lowered)
    assert not re.search(r"\bdrop\b", lowered)
    assert not re.search(r"\btruncate\b", lowered)
    for token in (
        "assignment",
        "staff_assignments",
        "staff_schedule",
        "caregiver_availability_locks",
        "caregiver_availability_lock_days",
    ):
        assert token not in lowered
