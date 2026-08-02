"""
================================================================================
檔案名稱: start_fastapi_ngrok.py
功能說明: 開發服務監督器，管理 FastAPI、ngrok、Streamlit，並與獨立 Monitor 互相監控及重啟
================================================================================
"""

from __future__ import annotations

import os
import json
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import requests
from dotenv import load_dotenv

from services.line_monitor_service import get_latest_service_heartbeat
from services.runtime_supervision_service import (
    SingleInstanceLock,
    clear_intentional_shutdown,
    heartbeat_pid,
    intentional_shutdown_requested,
    mark_intentional_shutdown,
    spawn_independent,
    terminate_registered_process,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent
MONITOR_SNAPSHOT_PATH = PROJECT_ROOT / ".monitor_state" / "line_health.json"
SERVICE_CHECK_INTERVAL_SECONDS = 2.0
SERVICE_HEALTH_FAILURE_THRESHOLD = 3
SERVICE_RESTART_DELAYS_SECONDS = (1, 3, 10)
os.chdir(PROJECT_ROOT)
load_dotenv(PROJECT_ROOT / ".env")


def _prepare_development_review_auth() -> None:
    """Create a process-local internal key for the dev reviewer when absent."""
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    if app_env not in {"development", "dev", "local", "test"}:
        return
    if not os.getenv("INTERNAL_API_KEY", "").strip():
        os.environ["INTERNAL_API_KEY"] = secrets.token_urlsafe(32)
        print("[REVIEW] 已建立本次開發程序專用的內部API金鑰。")


_prepare_development_review_auth()


def _managed_child_creation_kwargs() -> dict[str, object]:
    """Isolate child services from console interrupts sent to the supervisor."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _resolve_ngrok() -> str:
    executable = shutil.which("ngrok")
    if executable:
        return executable
    local_executable = PROJECT_ROOT / ".venv" / "Scripts" / "ngrok.exe"
    if local_executable.exists():
        return str(local_executable)
    raise FileNotFoundError("找不到 ngrok，請先安裝並確認 ngrok 可從終端執行。")


def run_ngrok() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [_resolve_ngrok(), "http", "8000", "--log=stdout"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        shell=False,
        **_managed_child_creation_kwargs(),
    )


def run_fastapi() -> subprocess.Popen[bytes]:
    # 使用啟動本檔案的同一個 Python，避免 uv 選到另一套 Python 環境。
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "api.main:app",
            "--reload",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ],
        cwd=PROJECT_ROOT,
        shell=False,
        **_managed_child_creation_kwargs(),
    )


def run_monitor() -> subprocess.Popen[bytes]:
    """Restart Monitor as a peer process in its own console/process group."""
    return spawn_independent(
        [sys.executable, "-m", "line.monitor"],
        cwd=PROJECT_ROOT,
    )


def run_streamlit() -> subprocess.Popen[bytes]:
    """Start the Streamlit management UI under the same supervisor."""
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "ui/app.py",
            "--server.address",
            "127.0.0.1",
            "--server.port",
            "8501",
        ],
        cwd=PROJECT_ROOT,
        shell=False,
        **_managed_child_creation_kwargs(),
    )


def _relay_output(process: subprocess.Popen[str], prefix: str) -> None:
    if process.stdout is None:
        return
    for line in process.stdout:
        clean_line = line.rstrip()
        if clean_line:
            print(f"[{prefix}] {clean_line}")


def _terminate_process_tree(process: subprocess.Popen, service_name: str) -> None:
    if process.poll() is not None:
        return
    print(f"[SHUTDOWN] 正在關閉 {service_name}（PID: {process.pid}）...")
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _print_urls(public_url: str) -> None:
    print("✨" * 25)
    print("🎉 啟動成功！請將以下完整網址設定到 LINE Developers：")
    print(f"👉 Webhook 網址: {public_url}/webhook/line")
    print("\n🎉 LIFF 測試表單網址：")
    print(f"👉 LIFF 網址: {public_url}/api/static/register.html")
    print("✨" * 25)
    print("\n💡 FastAPI、ngrok 與 Streamlit 由服務監督器管理。")
    print("💡 LINE Monitor 是同層獨立程序，兩邊透過心跳互相監控與恢復。")
    print("💡 任一服務異常時會個別嘗試重啟，連續三次失敗後才要求人工處理。")
    print("💡 在本視窗按 Ctrl+C 只會正常關閉 FastAPI、ngrok 與 Streamlit；Monitor 為獨立視窗。")


class ServiceFailure(RuntimeError):
    """A supervised service stopped or failed to become ready."""


@dataclass
class ManagedService:
    """Runtime state for one child process managed by the dev supervisor."""

    name: str
    display_name: str
    starter: Callable[[], subprocess.Popen]
    ready_timeout_seconds: int
    process: subprocess.Popen | None = None
    started_at: float = 0.0
    consecutive_health_failures: int = 0
    metadata: dict[str, str] = field(default_factory=dict)


def _service_process_status(service: ManagedService) -> tuple[bool, str]:
    if service.process is None:
        return False, "程序尚未啟動"
    exit_code = service.process.poll()
    if exit_code is not None:
        return False, f"程序已停止（Exit Code: {exit_code}）"
    return True, "程序執行中"


def _request_health(url: str, expected_text: str | None = None) -> tuple[bool, str]:
    try:
        response = requests.get(url, timeout=3)
        response.raise_for_status()
        if expected_text is not None and expected_text not in response.text:
            return False, f"{url} 回應內容不符合預期"
        return True, f"HTTP {response.status_code}"
    except requests.RequestException as exc:
        return False, str(exc)


def _ngrok_health(service: ManagedService) -> tuple[bool, str]:
    running, message = _service_process_status(service)
    if not running:
        return running, message
    try:
        response = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=3)
        response.raise_for_status()
        for tunnel in response.json().get("tunnels", []):
            public_url = str(tunnel.get("public_url") or "")
            if tunnel.get("proto") == "https" and public_url.startswith("https://"):
                service.metadata["public_url"] = public_url.rstrip("/")
                return True, f"Tunnel 已建立：{public_url}"
        return False, "ngrok 程序仍在，但沒有可用的 HTTPS Tunnel"
    except (requests.RequestException, ValueError) as exc:
        return False, f"無法讀取 ngrok Tunnel：{exc}"


def _fastapi_health(service: ManagedService) -> tuple[bool, str]:
    running, message = _service_process_status(service)
    return (running, message) if not running else _request_health("http://127.0.0.1:8000/health")


def _streamlit_health(service: ManagedService) -> tuple[bool, str]:
    running, message = _service_process_status(service)
    return (
        (running, message)
        if not running
        else _request_health("http://127.0.0.1:8501/_stcore/health", "ok")
    )


def _monitor_health(service: ManagedService) -> tuple[bool, str]:
    if intentional_shutdown_requested("line_monitor"):
        return True, "Monitor 已由開發者正常關閉，不執行自動重啟"
    if service.process is not None:
        running, message = _service_process_status(service)
        if not running:
            return running, message
    try:
        modified_at = MONITOR_SNAPSHOT_PATH.stat().st_mtime
        if modified_at < service.started_at - 1:
            return False, "Monitor 已啟動，但尚未產生本次程序的健康快照"
        snapshot = json.loads(MONITOR_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        generated_at = datetime.fromisoformat(str(snapshot["generated_at"]).replace("Z", "+00:00"))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - generated_at).total_seconds()
        if age_seconds > 60:
            return False, f"Monitor 程序仍在，但健康快照已停止更新 {int(age_seconds)} 秒"
        return True, f"健康快照於 {int(max(0, age_seconds))} 秒前更新"
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError, OSError) as exc:
        return False, f"Monitor 健康快照無法讀取：{exc}"


SERVICE_HEALTH_CHECKS: dict[str, Callable[[ManagedService], tuple[bool, str]]] = {
    "ngrok": _ngrok_health,
    "fastapi": _fastapi_health,
    "streamlit": _streamlit_health,
    "monitor": _monitor_health,
}


def _record_supervisor_event(
    service: ManagedService,
    state: str,
    description: str,
    *,
    attempt: int | None = None,
    severity: str = "warning",
) -> None:
    """Save a supervisor transition without allowing DB trouble to block recovery."""
    try:
        from services.line_monitor_service import record_supervisor_event

        details = {"pid": service.process.pid if service.process else None}
        if attempt is not None:
            details["restart_attempt"] = attempt
        record_supervisor_event(
            service.display_name,
            state,
            description,
            severity=severity,
            details=details,
        )
    except Exception as exc:
        print(f"[SUPERVISOR] 無法將 {service.display_name} 事件寫入 DB：{exc}")


def _start_service(service: ManagedService) -> None:
    service.process = service.starter()
    service.started_at = time.time()
    service.consecutive_health_failures = 0
    print(f"▶ {service.display_name} 已啟動（PID: {service.process.pid}）")
    if service.name == "ngrok":
        threading.Thread(
            target=_relay_output,
            args=(service.process, "ngrok"),
            daemon=True,
        ).start()


def _wait_until_service_ready(service: ManagedService) -> tuple[bool, str]:
    deadline = time.monotonic() + service.ready_timeout_seconds
    last_message = "尚未執行健康檢查"
    while time.monotonic() < deadline:
        healthy, last_message = SERVICE_HEALTH_CHECKS[service.name](service)
        if healthy:
            return True, last_message
        if service.process is not None and service.process.poll() is not None:
            return False, last_message
        time.sleep(0.5)
    return False, last_message


def _restart_service(service: ManagedService, reason: str) -> None:
    """Restart only the failed service; escalate after three unsuccessful tries."""
    _record_supervisor_event(service, "unavailable", reason)
    last_error = reason
    for attempt, delay_seconds in enumerate(SERVICE_RESTART_DELAYS_SECONDS, start=1):
        if service.process is not None:
            _terminate_process_tree(service.process, service.display_name)
        print(
            f"[RESTART] {service.display_name} 將於 {delay_seconds} 秒後進行"
            f"第 {attempt}/3 次自動重啟。"
        )
        time.sleep(delay_seconds)
        try:
            _start_service(service)
            ready, ready_message = _wait_until_service_ready(service)
        except Exception as exc:
            ready, ready_message = False, str(exc)
        if ready:
            service.consecutive_health_failures = 0
            _record_supervisor_event(
                service,
                "recovered",
                f"{service.display_name} 已於第 {attempt} 次自動重啟後恢復。",
                attempt=attempt,
            )
            print(f"[RECOVERED] {service.display_name} 已恢復：{ready_message}")
            return
        last_error = ready_message
        _record_supervisor_event(
            service,
            "restart_failed",
            f"{service.display_name} 第 {attempt} 次自動重啟失敗：{ready_message}",
            attempt=attempt,
            severity="critical" if attempt == 3 else "warning",
        )
    raise ServiceFailure(
        f"{service.display_name} 連續三次自動重啟仍無法恢復。\n最後原因：{last_error}"
    )


def _restart_monitor_peer(service: ManagedService, reason: str) -> None:
    """Stop the heartbeat-registered Monitor and relaunch one independent peer."""
    try:
        heartbeat = get_latest_service_heartbeat("line_monitor")
    except Exception as exc:
        heartbeat = None
        print(f"[SUPERVISOR] 無法取得 Monitor PID：{exc}")
    pid = heartbeat_pid(heartbeat)
    stopped, stop_message = terminate_registered_process(
        pid,
        expected_fragments=("-m", "line.monitor"),
        include_children=False,
    )
    print(f"[SUPERVISOR] Monitor 舊程序處理結果：{stop_message}")
    if not stopped and pid:
        print("[SUPERVISOR] 將由 Monitor 單例鎖阻止重複程序。")
    clear_intentional_shutdown("line_monitor")
    service.process = None
    _restart_service(service, reason)


class DevLineConsoleReviewer:
    """Non-blocking y/n reviewer for all LINE confirmation requests."""

    def __init__(self) -> None:
        app_env = os.getenv("APP_ENV", "development").strip().lower()
        flag = os.getenv("ENABLE_LINE_REVIEW_CONSOLE", "true").strip().lower()
        self.enabled = (
            app_env in {"development", "dev", "local", "test"}
            and flag in {"1", "true", "yes", "on"}
        )
        self.api_key = os.getenv("INTERNAL_API_KEY", "").strip()
        self.current: dict | None = None
        self.notifications: queue.Queue[dict] = queue.Queue()
        self._warned = False
        self._recovered_pending = False

        if self.enabled and os.name != "nt":
            print("[REVIEW] 終端 y/n 審核目前只支援 Windows，已停用。")
            self.enabled = False
        if self.enabled and not self.api_key:
            print("[REVIEW] 缺少 INTERNAL_API_KEY，LINE 終端審核已停用。")
            self.enabled = False

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Internal-API-Key": self.api_key}

    def enqueue(self, notification: dict) -> None:
        if self.enabled:
            self.notifications.put(notification)

    def recover_pending_once(self) -> None:
        """One startup recovery scan; normal operation is push-only."""
        if not self.enabled or self._recovered_pending:
            return
        self._recovered_pending = True
        try:
            response = requests.get(
                "http://127.0.0.1:8000/api/line/staff/review-requests",
                headers=self.headers,
                timeout=2,
            )
            response.raise_for_status()
            requests_data = response.json().get("data", [])
            self._warned = False
        except (requests.RequestException, ValueError) as exc:
            if not self._warned:
                print(f"[REVIEW] 暫時無法取得 LINE 待審資料：{exc}")
                self._warned = True
            return

        if not requests_data:
            return
        for request_item in requests_data:
            self.notifications.put({
                "type": request_item.get("type"),
                "request_id": str(request_item.get("request_id")),
            })

    def _load_notified_request(self, notification: dict) -> None:
        request_type = notification.get("type")
        request_id = str(notification.get("request_id"))
        try:
            response = requests.get(
                "http://127.0.0.1:8000/api/line/staff/review-requests",
                params={"request_type": request_type},
                headers=self.headers,
                timeout=2,
            )
            response.raise_for_status()
            requests_data = response.json().get("data", [])
            self._warned = False
        except (requests.RequestException, ValueError) as exc:
            print(f"[REVIEW] 無法載入待審資料：{exc}")
            return

        self.current = next(
            (item for item in requests_data if str(item.get("request_id")) == request_id),
            None,
        )
        if self.current is None:
            return
        details = self.current.get("details") or {}
        print("\n" + "=" * 60)
        request_type = self.current.get("type", "")
        if request_type == "staff_verification":
            print("[Staff Review] 收到月嫂身分申請")
            print(f"申請編號：{self.current.get('request_id', '')}")
            print(f"LINE User ID：{details.get('line_user_id', '')}")
            print("是否核准月嫂身分？(y/n): ", end="", flush=True)
        else:
            print("[Rebind Review] 收到舊客戶重新綁定申請")
            print(f"申請編號：{self.current.get('request_id', '')}")
            print(f"客戶名稱：{details.get('client_name', '')}")
            print(f"舊 LINE ID：{details.get('old_line_user_id', '')}")
            print(f"新 LINE ID：{details.get('new_line_user_id', '')}")
            print("是否核准重新綁定？(y/n): ", end="", flush=True)

    def _submit(self, action: str) -> None:
        if not self.current:
            return
        request_id = self.current.get("request_id")
        request_type = self.current.get("type")
        try:
            response = requests.post(
                f"http://127.0.0.1:8000/api/line/staff/review-requests/{request_type}/{request_id}/{action}",
                headers=self.headers,
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()
            print(f"[REVIEW] {result.get('message', '審核已完成')}")
        except (requests.RequestException, ValueError) as exc:
            print(f"[REVIEW] 審核提交失敗，申請仍保留待審：{exc}")
        finally:
            self.current = None

    def tick(self) -> None:
        if not self.enabled:
            return
        if self.current is None:
            try:
                notification = self.notifications.get_nowait()
            except queue.Empty:
                return
            self._load_notified_request(notification)
            return

        import msvcrt

        if not msvcrt.kbhit():
            return
        answer = msvcrt.getwch().lower()
        if answer == "y":
            print("y")
            self._submit("approve")
        elif answer == "n":
            print("n")
            self._submit("reject")
        elif answer not in {"\r", "\n"}:
            print("\n請輸入 y（核准）或 n（拒絕）: ", end="", flush=True)


class DevReviewNotificationServer:
    """Loopback-only one-shot notification receiver for the dev supervisor."""

    def __init__(self, reviewer: DevLineConsoleReviewer) -> None:
        self.reviewer = reviewer
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.reviewer.enabled:
            return
        reviewer = self.reviewer
        api_key = self.reviewer.api_key

        class NotificationHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/notify":
                    self.send_error(404)
                    return
                received_key = self.headers.get("X-Internal-API-Key", "")
                if not secrets.compare_digest(received_key, api_key):
                    self.send_error(401)
                    return
                try:
                    content_length = min(int(self.headers.get("Content-Length", "0")), 4096)
                    payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                    if payload.get("type") not in {"staff_verification", "client_rebind"}:
                        raise ValueError("unsupported request type")
                    reviewer.enqueue(payload)
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                self.send_response(204)
                self.end_headers()

            def log_message(self, _format: str, *_args) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), NotificationHandler)
        port = self.server.server_address[1]
        os.environ["DEV_REVIEW_NOTIFY_URL"] = f"http://127.0.0.1:{port}/notify"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print(f"[REVIEW] 一次性通知入口已啟動（127.0.0.1:{port}）。")

    def stop(self) -> None:
        os.environ.pop("DEV_REVIEW_NOTIFY_URL", None)
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


def _failure_popup_enabled() -> bool:
    value = os.getenv("ENABLE_SERVER_FAILURE_POPUP", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _ask_console_restart(message: str) -> bool:
    """Ask an interactive developer terminal whether both services should restart."""
    print("\n" + "=" * 60)
    print(f"[SERVER ERROR] {message}")
    print("ngrok、FastAPI 與 Streamlit 已安全關閉；獨立 Monitor 仍持續運作。")
    print("=" * 60)

    if not sys.stdin or not sys.stdin.isatty():
        print("[EXIT] 目前不是互動式終端，無法讀取 y/n，服務將直接關閉。")
        return False

    while True:
        try:
            answer = input("是否要重新啟動全部開發服務？(y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[EXIT] 已取消重新啟動。")
            return False
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("請輸入 y（重新啟動）或 n（關閉）。")


def _ask_restart(message: str) -> bool:
    """Show a blocking Windows dialog. Return True for restart."""
    if not _failure_popup_enabled():
        return _ask_console_restart(message)

    prompt = f"{message}\n\n服務已安全關閉。是否重新啟動 LINE Bot？"
    try:
        import tkinter as tk

        selection = {"restart": False}
        window = tk.Tk()
        window.title("LINE Bot 伺服器異常")
        window.resizable(False, False)
        window.attributes("-topmost", True)

        body = tk.Frame(window, padx=28, pady=22)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text="LINE Bot 伺服器異常",
            font=("Microsoft JhengHei UI", 15, "bold"),
            fg="#B91C1C",
        ).pack(anchor="w")
        tk.Label(
            body,
            text=prompt,
            font=("Microsoft JhengHei UI", 10),
            justify="left",
            wraplength=430,
            pady=18,
        ).pack(anchor="w")

        buttons = tk.Frame(body)
        buttons.pack(fill="x")

        def choose_restart() -> None:
            selection["restart"] = True
            window.destroy()

        def choose_close() -> None:
            window.destroy()

        tk.Button(
            buttons,
            text="重新啟動",
            width=14,
            command=choose_restart,
            bg="#2563EB",
            fg="white",
        ).pack(side="left", padx=(0, 12))
        tk.Button(
            buttons,
            text="關閉",
            width=14,
            command=choose_close,
        ).pack(side="right")

        window.protocol("WM_DELETE_WINDOW", choose_close)
        window.update_idletasks()
        x = (window.winfo_screenwidth() - window.winfo_width()) // 2
        y = (window.winfo_screenheight() - window.winfo_height()) // 2
        window.geometry(f"+{x}+{y}")
        window.focus_force()
        window.mainloop()
        return selection["restart"]
    except Exception as exc:
        print(f"[WARNING] 自訂錯誤視窗無法顯示：{exc}")
        if os.name != "nt":
            return False
        # Windows 內建備援視窗：Retry=重新啟動、Cancel=關閉。
        import ctypes

        retry = ctypes.windll.user32.MessageBoxW(
            None,
            prompt,
            "LINE Bot 伺服器異常",
            0x00000005 | 0x00000010 | 0x00040000,
        )
        return retry == 4


def _record_supervisor_heartbeat(services: dict[str, ManagedService]) -> None:
    try:
        from services.line_monitor_service import record_service_heartbeat

        record_service_heartbeat(
            "development_supervisor",
            f"{os.environ.get('COMPUTERNAME', 'local')}:{os.getpid()}",
            details={
                "pid": os.getpid(),
                "managed_services": {
                    name: item.process.pid if item.process and item.process.poll() is None else None
                    for name, item in services.items()
                }
            },
        )
    except Exception as exc:
        print(f"[SUPERVISOR] 無法更新監督器心跳：{exc}")


def _run_supervised_session() -> None:
    print("=" * 60)
    print("🚀 正在啟動服務監督器（FastAPI + ngrok + Streamlit）...")
    print("=" * 60)

    os.environ["ENABLE_DEVELOPMENT_SUPERVISOR_CHECK"] = "true"
    services = {
        "ngrok": ManagedService("ngrok", "ngrok", run_ngrok, 20),
        "fastapi": ManagedService("fastapi", "FastAPI", run_fastapi, 30),
        "streamlit": ManagedService("streamlit", "Streamlit", run_streamlit, 40),
    }
    monitor_peer = ManagedService("monitor", "LINE 主動監控", run_monitor, 75)
    line_reviewer = DevLineConsoleReviewer()
    review_notifier = DevReviewNotificationServer(line_reviewer)
    active_public_url = ""

    try:
        # Must start before FastAPI so the child process inherits the callback URL.
        review_notifier.start()
        for service_name in ("ngrok", "fastapi", "streamlit"):
            service = services[service_name]
            try:
                _start_service(service)
                ready, message = _wait_until_service_ready(service)
            except Exception as exc:
                ready, message = False, str(exc)
            if not ready:
                print(f"[WARNING] {service.display_name} 首次啟動未就緒：{message}")
                _restart_service(service, f"首次啟動未就緒：{message}")
            else:
                print(f"[READY] {service.display_name}：{message}")

            if service_name == "ngrok":
                active_public_url = service.metadata.get("public_url", "")
                if not active_public_url:
                    raise ServiceFailure("ngrok 已啟動，但無法取得公開 HTTPS 網址。")
                # Children launched afterwards inherit the currently active dev URL.
                os.environ["BASE_URL"] = active_public_url

        monitor_ready, monitor_message = _wait_until_service_ready(monitor_peer)
        if not monitor_ready:
            print(f"[WARNING] LINE 主動監控尚未就緒：{monitor_message}")
            _restart_monitor_peer(monitor_peer, monitor_message)
        else:
            print(f"[READY] LINE 主動監控：{monitor_message}")

        _print_urls(active_public_url)
        # Recover requests left pending before this development session once only.
        line_reviewer.recover_pending_once()
        last_heartbeat_at = 0.0

        while True:
            for service_name in ("ngrok", "fastapi", "streamlit"):
                service = services[service_name]
                healthy, message = SERVICE_HEALTH_CHECKS[service_name](service)
                process_stopped = service.process is None or service.process.poll() is not None
                if healthy:
                    service.consecutive_health_failures = 0
                    continue
                service.consecutive_health_failures += 1
                failure_count = service.consecutive_health_failures
                print(
                    f"[HEALTH] {service.display_name} 檢查失敗 "
                    f"({failure_count}/{SERVICE_HEALTH_FAILURE_THRESHOLD})：{message}"
                )
                if not process_stopped and failure_count < SERVICE_HEALTH_FAILURE_THRESHOLD:
                    continue

                previous_public_url = active_public_url
                _restart_service(service, message)
                if service_name == "ngrok":
                    active_public_url = service.metadata.get("public_url", "")
                    if active_public_url:
                        os.environ["BASE_URL"] = active_public_url
                        _print_urls(active_public_url)
                    if active_public_url and active_public_url != previous_public_url:
                        print(
                            "[DEPENDENCY] ngrok 公開網址已變更，將重啟 FastAPI 與 Monitor "
                            "以載入新的 BASE_URL。"
                        )
                        _restart_service(services["fastapi"], "ngrok 公開網址已變更")
                        _restart_monitor_peer(monitor_peer, "ngrok 公開網址已變更")

            monitor_healthy, monitor_message = _monitor_health(monitor_peer)
            monitor_stopped = (
                monitor_peer.process is not None and monitor_peer.process.poll() is not None
            )
            if monitor_healthy:
                monitor_peer.consecutive_health_failures = 0
            else:
                monitor_peer.consecutive_health_failures += 1
                failure_count = monitor_peer.consecutive_health_failures
                print(
                    "[HEALTH] LINE 主動監控檢查失敗 "
                    f"({failure_count}/{SERVICE_HEALTH_FAILURE_THRESHOLD})：{monitor_message}"
                )
                if monitor_stopped or failure_count >= SERVICE_HEALTH_FAILURE_THRESHOLD:
                    _restart_monitor_peer(monitor_peer, monitor_message)

            line_reviewer.tick()
            if time.monotonic() - last_heartbeat_at >= 15:
                _record_supervisor_heartbeat(services)
                last_heartbeat_at = time.monotonic()
            time.sleep(SERVICE_CHECK_INTERVAL_SECONDS)
    finally:
        review_notifier.stop()
        for service_name in ("streamlit", "fastapi", "ngrok"):
            service = services[service_name]
            if service.process is not None:
                _terminate_process_tree(service.process, service.display_name)


def _confirm_intentional_shutdown() -> bool:
    """Require an explicit operator confirmation before suppressing peer recovery."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        answer = input(
            "\n[CONFIRM] Supervisor 收到 Console Interrupt。"
            "若這是你主動停止，請輸入 y；其他情況將自動恢復服務 [y/N]："
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


def _print_interrupt_diagnostics() -> None:
    """Log facts without claiming that a physical Ctrl+C keypress occurred."""
    print(
        "[INTERRUPT] Supervisor 收到 Windows Console Interrupt；"
        "來源可能是鍵盤、終端、父程序或程序控制事件，尚未確認。"
    )
    print(
        "[INTERRUPT] "
        f"time={datetime.now(timezone.utc).isoformat()} "
        f"pid={os.getpid()} parent_pid={os.getppid()}"
    )


def _main_loop() -> int:
    while True:
        try:
            _run_supervised_session()
            return 0
        except KeyboardInterrupt:
            _print_interrupt_diagnostics()
            if _confirm_intentional_shutdown():
                mark_intentional_shutdown("development_supervisor")
                print("[STOP] 使用者已確認主動關閉，LINE Bot 開發環境已正常停止。")
                return 0
            print(
                "[RECOVERY] 此次中斷未經使用者確認，不寫入正常關閉標記；"
                "1 秒後重新建立受管服務。"
            )
            time.sleep(1)
            continue
        except (ServiceFailure, FileNotFoundError) as exc:
            message = str(exc)
            print(f"[ERROR] {message}")
        except Exception as exc:
            message = f"啟動器發生未預期錯誤：{exc}"
            print(f"[ERROR] {message}")

        if _ask_restart(message):
            print("[RESTART] 使用者選擇重新啟動，1 秒後重新建立全部開發服務...")
            time.sleep(1)
            continue

        print("[EXIT] 使用者選擇關閉，請於需要時手動重新啟動。")
        mark_intentional_shutdown("development_supervisor")
        return 0


def main() -> int:
    singleton = SingleInstanceLock("development_supervisor")
    if not singleton.acquire():
        print("[SUPERVISOR] 已有一個服務監督器正在執行，本次不重複啟動。")
        return 0
    clear_intentional_shutdown("development_supervisor")
    try:
        return _main_loop()
    finally:
        singleton.release()


if __name__ == "__main__":
    raise SystemExit(main())
