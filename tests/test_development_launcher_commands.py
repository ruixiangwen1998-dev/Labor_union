"""Command contracts for the local development supervisor."""

from __future__ import annotations

import start_fastapi_ngrok as launcher


class _ProcessStub:
    pid = 12345


def _capture_command(monkeypatch, starter):
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _ProcessStub()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    starter()
    return captured


def test_line_worker_uses_project_module_entrypoint(monkeypatch):
    captured = _capture_command(monkeypatch, launcher.run_line_worker)

    assert captured["command"] == [
        launcher.sys.executable,
        "-m",
        "scripts.run_line_worker",
    ]
    assert captured["kwargs"]["cwd"] == launcher.PROJECT_ROOT


def test_runtime_monitor_uses_project_module_entrypoint(monkeypatch):
    captured = _capture_command(monkeypatch, launcher.run_monitor)

    assert captured["command"] == [
        launcher.sys.executable,
        "-m",
        "scripts.run_service_monitor",
    ]
    assert captured["kwargs"]["cwd"] == launcher.PROJECT_ROOT
