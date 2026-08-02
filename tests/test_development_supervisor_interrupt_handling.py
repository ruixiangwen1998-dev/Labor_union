"""Regression tests for development supervisor process and interrupt isolation."""

from __future__ import annotations

import subprocess

import start_fastapi_ngrok as launcher


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
