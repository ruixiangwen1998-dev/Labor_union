"""
================================================================================
檔案名稱: line/monitor.py
功能說明: 獨立主動監控程序，保存細部健康狀態並在服務監督器失聯時執行受控重啟
================================================================================
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from services.line_monitor_service import (
    get_latest_service_heartbeat,
    load_monitoring_config,
    record_supervisor_event,
    run_monitor_cycle,
)
from services.line_alert_notification_service import (
    process_due_alert_deliveries,
    process_snapshot_fallback_notifications,
    stage_monitor_alert_deliveries,
)
from services.runtime_supervision_service import (
    SingleInstanceLock,
    clear_intentional_shutdown,
    heartbeat_pid,
    intentional_shutdown_requested,
    mark_intentional_shutdown,
    spawn_independent,
    terminate_registered_process,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
_running = True
_received_signal: int | None = None
SUPERVISOR_RESTART_GRACE_SECONDS = 45
MAX_SUPERVISOR_RESTART_ATTEMPTS = 3
MANAGED_PROCESS_SIGNATURES = {
    "ngrok": ("ngrok", "http", "8000"),
    "fastapi": ("uvicorn", "api.main:app"),
    "streamlit": ("streamlit", "ui/app.py"),
}


def _stop(signum=None, _frame=None) -> None:
    global _received_signal, _running
    _received_signal = signum
    _running = False


def _confirm_intentional_shutdown() -> bool:
    """Only explicit operator confirmation may suppress peer recovery."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        answer = input(
            "\n[LINE Monitor][CONFIRM] 收到 Console Interrupt。"
            "若這是你主動停止，請輸入 y；其他情況將繼續監控 [y/N]："
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


def _print_signal_diagnostics(signum: int | None) -> None:
    signal_name = "unknown"
    if signum is not None:
        try:
            signal_name = signal.Signals(signum).name
        except ValueError:
            signal_name = str(signum)
    print(
        "[LINE Monitor][INTERRUPT] "
        f"signal={signal_name} time={datetime.now(timezone.utc).isoformat()} "
        f"pid={os.getpid()} parent_pid={os.getppid()}；來源尚未確認。"
    )


def _handle_stop_request(signum: int | None) -> tuple[bool, int]:
    """Return (continue_monitoring, exit_code) without mislabeling external stops."""
    _print_signal_diagnostics(signum)
    if signum == signal.SIGINT:
        if _confirm_intentional_shutdown():
            mark_intentional_shutdown("line_monitor")
            print("[LINE Monitor][STOP] 使用者已確認主動關閉 Monitor。")
            return False, 0
        print(
            "[LINE Monitor][RECOVERY] 中斷未經使用者確認，不寫入正常關閉標記；"
            "繼續執行監控。"
        )
        return True, 0
    print(
        "[LINE Monitor][ERROR] 收到外部終止訊號，不寫入正常關閉標記；"
        "交由服務監督器判定並恢復。",
        file=sys.stderr,
    )
    return False, 1


def _failure_popup_enabled() -> bool:
    return os.getenv("ENABLE_SERVER_FAILURE_POPUP", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _show_supervisor_failure_notice(message: str) -> None:
    print(f"[LINE Monitor] {message}", file=sys.stderr)
    if not _failure_popup_enabled() or os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "開發服務監督器異常",
            0x00000010 | 0x00040000,
        )
    except Exception as exc:
        print(f"[LINE Monitor] 無法顯示錯誤彈窗：{exc}", file=sys.stderr)


def _terminate_supervisor_and_managed_services(heartbeat: dict | None) -> None:
    details = (heartbeat or {}).get("details") or {}
    managed = details.get("managed_services") or {}
    for service_name, expected_fragments in MANAGED_PROCESS_SIGNATURES.items():
        try:
            pid = int(managed.get(service_name) or 0) or None
        except (TypeError, ValueError):
            pid = None
        stopped, message = terminate_registered_process(
            pid,
            expected_fragments=expected_fragments,
            include_children=True,
        )
        if pid:
            print(
                f"[LINE Monitor] 清理 {service_name} PID {pid}："
                f"{'成功' if stopped else '略過'}，{message}"
            )

    supervisor_pid = heartbeat_pid(heartbeat)
    stopped, message = terminate_registered_process(
        supervisor_pid,
        expected_fragments=("start_fastapi_ngrok.py",),
        include_children=False,
    )
    if supervisor_pid:
        print(
            f"[LINE Monitor] 清理服務監督器 PID {supervisor_pid}："
            f"{'成功' if stopped else '略過'}，{message}"
        )


def _restart_development_supervisor(attempt: int) -> None:
    try:
        heartbeat = get_latest_service_heartbeat("development_supervisor")
    except Exception as exc:
        heartbeat = None
        print(f"[LINE Monitor] 無法取得服務監督器 PID：{exc}", file=sys.stderr)
    _terminate_supervisor_and_managed_services(heartbeat)
    clear_intentional_shutdown("development_supervisor")
    process = spawn_independent(
        [sys.executable, str(PROJECT_ROOT / "start_fastapi_ngrok.py")],
        cwd=PROJECT_ROOT,
    )
    try:
        record_supervisor_event(
            "開發啟動監督器",
            "restart_requested",
            f"Monitor 已執行第 {attempt}/3 次服務監督器重啟。",
            severity=(
                "critical" if attempt == MAX_SUPERVISOR_RESTART_ATTEMPTS else "warning"
            ),
            details={"restart_attempt": attempt, "new_pid": process.pid},
        )
    except Exception as exc:
        print(f"[LINE Monitor] 無法寫入監督器重啟事件：{exc}", file=sys.stderr)
    print(
        f"[LINE Monitor] 已執行第 {attempt}/3 次服務監督器重啟，"
        f"新 PID：{process.pid}"
    )


def _main_loop() -> int:
    global _received_signal, _running
    _running = True
    _received_signal = None
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    print("[LINE Monitor] 主動監控程序已啟動")
    last_run = {}
    previous_overall = None
    restart_attempts = 0
    next_restart_at = 0.0
    final_failure_notified = False
    while _running:
        try:
            snapshot, last_run = run_monitor_cycle(last_run)
            try:
                staged = stage_monitor_alert_deliveries()
                delivered = process_due_alert_deliveries()
                if staged or delivered:
                    print(
                        f"[LINE Monitor] 異常通知：新增 {staged} 筆，處理 {delivered} 筆"
                    )
            except Exception as notification_exc:
                print(
                    f"[LINE Monitor] DB 通知派送暫時不可用，改用本機快取：{notification_exc}",
                    file=sys.stderr,
                )
                try:
                    process_snapshot_fallback_notifications(snapshot)
                except Exception as fallback_exc:
                    print(
                        f"[LINE Monitor] 本機快取通知也失敗：{fallback_exc}",
                        file=sys.stderr,
                    )
            if snapshot["overall_status"] != previous_overall:
                print(f"[LINE Monitor] {snapshot['generated_at']} overall={snapshot['overall_status']}")
                previous_overall = snapshot["overall_status"]
            supervisor = (snapshot.get("checks") or {}).get("development_supervisor") or {}
            supervisor_status = supervisor.get("status")
            if intentional_shutdown_requested("development_supervisor"):
                restart_attempts = 0
                next_restart_at = 0.0
                final_failure_notified = False
            elif supervisor_status == "healthy":
                if restart_attempts:
                    try:
                        record_supervisor_event(
                            "開發啟動監督器",
                            "recovered",
                            "服務監督器已恢復心跳。",
                            details={"restart_attempts": restart_attempts},
                        )
                    except Exception as exc:
                        print(
                            f"[LINE Monitor] 無法寫入監督器恢復事件：{exc}",
                            file=sys.stderr,
                        )
                restart_attempts = 0
                next_restart_at = 0.0
                final_failure_notified = False
            elif (
                supervisor_status == "critical"
                and restart_attempts < MAX_SUPERVISOR_RESTART_ATTEMPTS
                and time.monotonic() >= next_restart_at
            ):
                restart_attempts += 1
                _restart_development_supervisor(restart_attempts)
                next_restart_at = time.monotonic() + SUPERVISOR_RESTART_GRACE_SECONDS
            elif (
                supervisor_status == "critical"
                and restart_attempts >= MAX_SUPERVISOR_RESTART_ATTEMPTS
                and time.monotonic() >= next_restart_at
                and not final_failure_notified
            ):
                final_failure_notified = True
                _show_supervisor_failure_notice(
                    "服務監督器連續三次重啟仍未恢復，請查看 Monitor 與服務終端後手動處理。"
                )
        except Exception as exc:
            print(f"[LINE Monitor] 監控循環錯誤：{exc}", file=sys.stderr)
        interval = max(5, int(load_monitoring_config().get("monitor_interval_seconds", 15)))
        for _ in range(interval):
            if not _running:
                break
            time.sleep(1)
    continue_monitoring, exit_code = _handle_stop_request(_received_signal)
    if continue_monitoring:
        return _main_loop()
    print("[LINE Monitor] 主動監控程序已停止")
    return exit_code


def main() -> int:
    singleton = SingleInstanceLock("line_monitor")
    if not singleton.acquire():
        print("[LINE Monitor] 已有一個主動監控程序正在執行，本次不重複啟動。")
        return 0
    clear_intentional_shutdown("line_monitor")
    try:
        return _main_loop()
    finally:
        singleton.release()


if __name__ == "__main__":
    raise SystemExit(main())
