"""Regression checks for the business-alert and service-monitor table boundary."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_business_and_service_monitor_alerts_use_separate_tables():
    schema = (ROOT / "db/schema.sql").read_text(encoding="utf-8")
    monitor_service = (ROOT / "services/line_monitor_service.py").read_text(
        encoding="utf-8"
    )
    business_service = (ROOT / "services/system_alert_service.py").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS system_alerts" in schema
    assert "CREATE TABLE IF NOT EXISTS service_monitor_alerts" in schema
    assert "service_monitor_alerts" in monitor_service
    assert "INSERT INTO system_alerts" in business_service
    assert "INSERT INTO service_monitor_alerts" not in business_service


def test_split_migration_preserves_the_legacy_monitor_table():
    migration = (
        ROOT / "db/schema_parts/104_split_system_and_service_monitor_alerts.sql"
    ).read_text(encoding="utf-8")

    assert "RENAME TABLE system_alerts TO service_monitor_alerts" in migration
    assert "DROP TABLE" not in migration.upper()
    assert "CREATE TABLE IF NOT EXISTS system_alerts" in migration
    assert "CREATE TABLE IF NOT EXISTS service_monitor_alerts" in migration
