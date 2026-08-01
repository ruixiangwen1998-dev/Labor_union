import pytest
import scripts.reset_fake_database as r


def test_preview_validates_v3_without_reset(monkeypatch):
    for variable in ("APP_ENV", "ENV", "FLASK_ENV"):
        monkeypatch.delenv(variable, raising=False)
        monkeypatch.setenv(variable, "test")

    report=r.reset(False)
    assert report["status"]=="preview" and report["snapshot_checksum"]


def test_production_and_wrong_confirmation_rejected(monkeypatch):
    with pytest.raises(r.FakeDatabaseResetError):r.validate_target(environment={"APP_ENV":"production"})
    with pytest.raises(r.FakeDatabaseResetError):r.reset(True,"wrong")
