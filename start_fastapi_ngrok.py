"""Start and supervise local FastAPI, LINE Worker, and ngrok processes."""

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent
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
    )


def run_line_worker() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "scripts.run_line_worker"],
        cwd=PROJECT_ROOT,
        shell=False,
    )


def run_monitor() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "scripts.run_service_monitor"],
        cwd=PROJECT_ROOT,
        shell=False,
    )


def run_streamlit() -> subprocess.Popen[bytes]:
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
    )


def _write_supervisor_heartbeat(processes: dict[str, subprocess.Popen]) -> None:
    state_dir = PROJECT_ROOT / ".monitor_state"
    state_dir.mkdir(exist_ok=True)
    target = state_dir / "supervisor-heartbeat.json"
    temporary = state_dir / "supervisor-heartbeat.tmp"
    payload = {
        "written_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "process_id": os.getpid(),
        "children": {
            name: {"pid": process.pid, "running": process.poll() is None}
            for name, process in processes.items()
        },
    }
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


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


def _wait_for_tunnel(ngrok_process: subprocess.Popen, timeout_seconds: int = 15) -> str | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if ngrok_process.poll() is not None:
            return None
        try:
            response = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=1)
            response.raise_for_status()
            for tunnel in response.json().get("tunnels", []):
                if tunnel.get("proto") == "https":
                    return tunnel.get("public_url")
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return None


def _print_urls(public_url: str) -> None:
    print("✨" * 25)
    print("🎉 啟動成功！請將以下完整網址設定到 LINE Developers：")
    print(f"👉 Webhook 網址: {public_url}/webhook/line")
    print("\n🎉 LIFF 測試表單網址：")
    print(f"👉 LIFF 網址: {public_url}/api/static/register.html")
    print("✨" * 25)
    print("\n💡 FastAPI、LINE Worker 或 ngrok 任一方停止時，其餘服務也會自動關閉。")
    print("💡 按 Ctrl+C 可正常關閉兩個服務。")


class ServiceFailure(RuntimeError):
    """A supervised service stopped or failed to become ready."""


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
    print("ngrok、FastAPI 與 LINE Worker 已自動關閉。")
    print("=" * 60)

    if not sys.stdin or not sys.stdin.isatty():
        print("[EXIT] 目前不是互動式終端，無法讀取 y/n，服務將直接關閉。")
        return False

    while True:
        try:
            answer = input("是否要重新啟動 ngrok、FastAPI 與 LINE Worker？(y/n): ").strip().lower()
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


def _run_supervised_session() -> None:
    print("=" * 60)
    print("🚀 正在啟動開發環境（FastAPI + LINE Worker + Monitor + Streamlit + ngrok）...")
    print("=" * 60)
    line_reviewer = DevLineConsoleReviewer()
    review_notifier = DevReviewNotificationServer(line_reviewer)
    starters = {
        "ngrok": run_ngrok,
        "FastAPI": run_fastapi,
        "LINE Worker": run_line_worker,
        "Monitor": run_monitor,
        "Streamlit": run_streamlit,
    }
    processes: dict[str, subprocess.Popen] = {}
    restart_counts = {name: 0 for name in starters}

    try:
        review_notifier.start()
        for name, starter in starters.items():
            processes[name] = starter()
            print(f"▶ {name} 已啟動（PID: {processes[name].pid}）")
        ngrok_process = processes["ngrok"]
        threading.Thread(
            target=_relay_output,
            args=(ngrok_process, "ngrok"),
            daemon=True,
        ).start()
        print("⏳ 正在等待 ngrok Tunnel 就緒...")

        public_url = _wait_for_tunnel(ngrok_process)
        if not public_url:
            exit_code = ngrok_process.poll()
            if exit_code is not None:
                raise ServiceFailure(f"ngrok 啟動失敗或已停止。\nExit Code：{exit_code}")
            else:
                raise ServiceFailure("ngrok 在 15 秒內未建立 Tunnel。\n請查看終端的 [ngrok] 日誌。")

        _print_urls(public_url)
        # Recover requests left pending before this development session once only.
        line_reviewer.recover_pending_once()

        while True:
            _write_supervisor_heartbeat(processes)
            for name, process in tuple(processes.items()):
                exit_code = process.poll()
                if exit_code is None:
                    continue
                restart_counts[name] += 1
                print(f"\n[ERROR] {name} 已停止，Exit Code: {exit_code}")
                if restart_counts[name] > 3:
                    raise ServiceFailure(
                        f"{name} 連續 3 次自動重啟仍失敗，請人工檢查後重新啟動。"
                    )
                time.sleep(1)
                processes[name] = starters[name]()
                print(
                    f"[RESTART] {name} 第 {restart_counts[name]}/3 次自動重啟完成"
                    f"（PID: {processes[name].pid}）。"
                )
                if name == "ngrok":
                    threading.Thread(
                        target=_relay_output,
                        args=(processes[name], "ngrok"),
                        daemon=True,
                    ).start()
            line_reviewer.tick()
            time.sleep(0.5)
    finally:
        review_notifier.stop()
        for name, process in reversed(tuple(processes.items())):
            _terminate_process_tree(process, name)


def main() -> int:
    while True:
        try:
            _run_supervised_session()
        except KeyboardInterrupt:
            print("\n[STOP] 收到 Ctrl+C，LINE Bot 開發環境已正常關閉。")
            return 0
        except (ServiceFailure, FileNotFoundError) as exc:
            message = str(exc)
            print(f"[ERROR] {message}")
        except Exception as exc:
            message = f"啟動器發生未預期錯誤：{exc}"
            print(f"[ERROR] {message}")

        if _ask_restart(message):
            print("[RESTART] 1 秒後重新建立 FastAPI、LINE Worker 與 ngrok...")
            time.sleep(1)
            continue

        print("[EXIT] 使用者選擇關閉，請於需要時手動重新啟動。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
