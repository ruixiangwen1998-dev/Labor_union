"""Static safety checks for Windows administrator development launchers."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_bootstrap_uses_secure_random_key_and_only_writes_local_env():
    script = _read("scripts/bootstrap_admin_dev_env.ps1")

    assert "RandomNumberGenerator" in script
    assert 'Join-Path $root ".env"' in script
    assert ".env.example" not in script
    assert 'APP_ENV = "development"' in script
    assert 'ENABLE_ADMIN_AUTH = "false"' in script
    assert 'INTERNAL_API_KEY = ""' in script
    assert 'INTERNAL_API_KEY=$($desired[' not in script
    assert "INTERNAL_API_KEY=已更新（值不顯示）" in script


def test_bootstrap_batch_resolves_project_root_and_propagates_failure():
    script = _read("bootstrap_admin_dev_env.bat")

    assert 'cd /d "%~dp0"' in script
    assert '"%~dp0scripts\\bootstrap_admin_dev_env.ps1"' in script
    assert "if %ERRORLEVEL% neq 0 (" in script
    assert "exit /b %ERRORLEVEL%" in script


def test_dev_api_bootstraps_before_online_and_stops_on_failure():
    script = _read("dev_API.bat")

    bootstrap = script.index("bootstrap_admin_dev_env.ps1")
    failure_gate = script.index("if errorlevel 1")
    online = script.index('call "%~dp0online.bat"')

    assert bootstrap < failure_gate < online
    assert "exit /b 1" in script


def test_online_requires_persistent_internal_key_without_generating_one():
    script = _read("online.bat")

    assert '"^INTERNAL_API_KEY="' in script
    assert "INTERNAL_API_KEY is missing" in script
    assert "secrets.token_urlsafe" not in script


def test_start_reuses_env_key_or_generates_ephemeral_key():
    script = _read("start.bat")

    env_lookup = script.index('"^INTERNAL_API_KEY="')
    generated_key = script.index("secrets.token_urlsafe(32)")
    ready_gate = script.index(":internal_api_key_ready")

    assert env_lookup < generated_key < ready_gate
    assert "echo [Security] FastAPI and Streamlit share one internal API key" in script
