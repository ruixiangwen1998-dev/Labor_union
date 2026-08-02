"""Regression tests for development supervisor process and interrupt isolation."""

from __future__ import annotations

import subprocess
import signal

import start_fastapi_ngrok as launcher
import line.monitor as monitor


class _ProcessStub:
    pid = 12345


def test_managed_services_use_a_separate_windows_process_group(monkeypatch):
    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return _ProcessStub()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher, "_resolve_ngrok", lambda: "ngrok.exe")

    launcher.run_ngrok()
    launcher.run_fastapi()
    launcher.run_streamlit()

    assert len(calls) == 3
    if launcher.os.name == "nt":
        assert all(
            kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
            for _, kwargs in calls
        )
    else:
        assert all(kwargs["start_new_session"] is True for _, kwargs in calls)


def test_streamlit_keeps_default_browser_launch_behavior(monkeypatch):
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return _ProcessStub()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    launcher.run_streamlit()

    command = captured["command"]
    assert "--server.headless" not in command


def test_unconfirmed_console_interrupt_restarts_without_shutdown_marker(monkeypatch):
    attempts = 0
    marked = []

    def run_session():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "_run_supervised_session", run_session)
    monkeypatch.setattr(launcher, "_confirm_intentional_shutdown", lambda: False)
    monkeypatch.setattr(launcher, "_print_interrupt_diagnostics", lambda: None)
    monkeypatch.setattr(launcher, "mark_intentional_shutdown", marked.append)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    assert launcher._main_loop() == 0
    assert attempts == 2
    assert marked == []


def test_confirmed_console_interrupt_marks_intentional_shutdown(monkeypatch):
    marked = []

    def interrupt_session():
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "_run_supervised_session", interrupt_session)
    monkeypatch.setattr(launcher, "_confirm_intentional_shutdown", lambda: True)
    monkeypatch.setattr(launcher, "_print_interrupt_diagnostics", lambda: None)
    monkeypatch.setattr(launcher, "mark_intentional_shutdown", marked.append)

    assert launcher._main_loop() == 0
    assert marked == ["development_supervisor"]


def test_noninteractive_interrupt_cannot_claim_intentional_shutdown(monkeypatch):
    class _NonInteractiveInput:
        def isatty(self):
            return False

    monkeypatch.setattr(launcher.sys, "stdin", _NonInteractiveInput())

    assert launcher._confirm_intentional_shutdown() is False


def test_new_supervisor_session_consumes_stale_monitor_shutdown_marker(monkeypatch):
    service = launcher.ManagedService("monitor", "LINE 主動監控", launcher.run_monitor, 20)
    cleared = []
    restarted = []

    monkeypatch.setattr(launcher, "intentional_shutdown_requested", lambda _name: True)
    monkeypatch.setattr(launcher, "clear_intentional_shutdown", cleared.append)
    monkeypatch.setattr(
        launcher,
        "_restart_monitor_peer",
        lambda target, reason: restarted.append((target, reason)),
    )
    monkeypatch.setattr(
        launcher,
        "_wait_until_service_ready",
        lambda _service: (_ for _ in ()).throw(AssertionError("不應等待 stale marker")),
    )

    launcher._ensure_monitor_peer(service)

    assert cleared == ["line_monitor"]
    assert restarted and restarted[0][0] is service
    assert service.started_at > 0


def test_missing_monitor_is_started_after_short_discovery_window(monkeypatch):
    service = launcher.ManagedService("monitor", "LINE 主動監控", launcher.run_monitor, 20)
    restarted = []

    monkeypatch.setattr(launcher, "intentional_shutdown_requested", lambda _name: False)
    monkeypatch.setattr(launcher, "clear_intentional_shutdown", lambda _name: None)
    monkeypatch.setattr(
        launcher,
        "_wait_until_service_ready",
        lambda _service: (False, "尚未產生本次程序的健康快照"),
    )
    monkeypatch.setattr(
        launcher,
        "_restart_monitor_peer",
        lambda target, reason: restarted.append((target, reason)),
    )

    launcher._ensure_monitor_peer(service)

    assert restarted == [(service, "尚未產生本次程序的健康快照")]
    assert service.ready_timeout_seconds == launcher.MONITOR_STARTUP_DISCOVERY_SECONDS


def test_monitor_unconfirmed_sigint_continues_without_shutdown_marker(monkeypatch):
    marked = []
    monkeypatch.setattr(monitor, "_confirm_intentional_shutdown", lambda: False)
    monkeypatch.setattr(monitor, "_print_signal_diagnostics", lambda _signum: None)
    monkeypatch.setattr(monitor, "mark_intentional_shutdown", marked.append)

    assert monitor._handle_stop_request(signal.SIGINT) == (True, 0)
    assert marked == []


def test_monitor_confirmed_sigint_marks_intentional_shutdown(monkeypatch):
    marked = []
    monkeypatch.setattr(monitor, "_confirm_intentional_shutdown", lambda: True)
    monkeypatch.setattr(monitor, "_print_signal_diagnostics", lambda _signum: None)
    monkeypatch.setattr(monitor, "mark_intentional_shutdown", marked.append)

    assert monitor._handle_stop_request(signal.SIGINT) == (False, 0)
    assert marked == ["line_monitor"]


def test_monitor_sigterm_is_external_failure_without_shutdown_marker(monkeypatch):
    marked = []
    monkeypatch.setattr(monitor, "_print_signal_diagnostics", lambda _signum: None)
    monkeypatch.setattr(monitor, "mark_intentional_shutdown", marked.append)

    assert monitor._handle_stop_request(signal.SIGTERM) == (False, 1)
    assert marked == []
