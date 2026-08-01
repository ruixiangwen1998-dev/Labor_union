from pathlib import Path

import pytest

from scripts import init_db
from scripts.init_db import load_schema_parts


ROOT = Path(__file__).resolve().parents[1]


class RecordingCursor:
    def __init__(self, fail_on=None):
        self.executed = []
        self.fail_on = fail_on

    def execute(self, statement):
        self.executed.append(statement)
        if self.fail_on and self.fail_on in statement:
            raise ValueError("forced failure")


def test_loads_schema_parts_in_filename_order(tmp_path):
    parts = tmp_path / "schema_parts"
    parts.mkdir()
    (parts / "20_second.sql").write_text("CREATE TABLE second (id INT);", encoding="utf-8")
    (parts / "10_first.sql").write_text(
        "-- comment\nCREATE TABLE first (id INT);\nALTER TABLE first ADD value INT;",
        encoding="utf-8",
    )
    cursor = RecordingCursor()

    loaded = load_schema_parts(cursor, parts)

    assert loaded == ["10_first.sql", "20_second.sql"]
    assert cursor.executed == [
        "CREATE TABLE first (id INT)",
        "ALTER TABLE first ADD value INT",
        "CREATE TABLE second (id INT)",
    ]


def test_stops_at_first_failing_part_and_reports_filename(tmp_path):
    parts = tmp_path / "schema_parts"
    parts.mkdir()
    (parts / "10_ok.sql").write_text("CREATE TABLE ok_table (id INT);", encoding="utf-8")
    (parts / "20_bad.sql").write_text("CREATE TABLE bad_table (id INT);", encoding="utf-8")
    (parts / "30_never.sql").write_text("CREATE TABLE never_table (id INT);", encoding="utf-8")
    cursor = RecordingCursor(fail_on="bad_table")

    with pytest.raises(RuntimeError, match="20_bad.sql"):
        load_schema_parts(cursor, parts)

    assert any("ok_table" in statement for statement in cursor.executed)
    assert any("bad_table" in statement for statement in cursor.executed)
    assert not any("never_table" in statement for statement in cursor.executed)


def test_empty_schema_parts_directory_is_valid(tmp_path):
    parts = tmp_path / "schema_parts"
    parts.mkdir()

    assert load_schema_parts(RecordingCursor(), parts) == []


def test_multi_caregiver_check_constraint_joins_table_metadata():
    migration = (
        ROOT / "db" / "schema_parts" / "95_multi_caregiver_schedule.sql"
    ).read_text(encoding="utf-8")

    assert "JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS t" in migration
    assert "AND t.TABLE_NAME = 'staff_schedule_assignment_reviews'" in migration
    assert "FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS\n    WHERE" not in migration


class MainConnection:
    def __init__(self):
        self.cursor_instance = RecordingCursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        connection = self

        class CursorContext:
            def __enter__(self):
                return connection.cursor_instance

            def __exit__(self, exc_type, exc, traceback):
                return False

        return CursorContext()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_main_rolls_back_closes_and_propagates_schema_part_failure(monkeypatch, capsys):
    connection = MainConnection()
    monkeypatch.setattr(init_db.pymysql, "connect", lambda **kwargs: connection)

    def fail_schema_part(cursor, schema_parts_dir):
        raise RuntimeError("載入 schema part 失敗：60_broken.sql: forced failure")

    monkeypatch.setattr(init_db, "load_schema_parts", fail_schema_part)

    with pytest.raises(RuntimeError, match="60_broken.sql"):
        init_db.main()

    output = capsys.readouterr().out
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert connection.closed is True
    assert "60_broken.sql" in output
    assert "Schema 更新成功" not in output
