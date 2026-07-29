"""
================================================================================
檔案名稱: services/runtime_supervision_service.py
功能說明: 開發程序互相監控所需的單例鎖、心跳 PID 解析、安全終止與獨立重啟工具
================================================================================
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / ".monitor_state"


def _shutdown_marker(name: str) -> Path:
    return STATE_DIR / f"{name}.shutdown"


def mark_intentional_shutdown(name: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _shutdown_marker(name).write_text(str(os.getpid()), encoding="utf-8")


def clear_intentional_shutdown(name: str) -> None:
    try:
        _shutdown_marker(name).unlink()
    except FileNotFoundError:
        pass


def intentional_shutdown_requested(name: str) -> bool:
    return _shutdown_marker(name).exists()


class SingleInstanceLock:
    """Cross-process singleton guard for the monitor and service supervisor."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle: int | None = None
        self._file = None

    def acquire(self) -> bool:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            handle = kernel32.CreateMutexW(None, False, f"Local\\LaborUnion_{self.name}")
            if not handle:
                return False
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            self._handle = int(handle)
            return True

        import fcntl

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._file = (STATE_DIR / f"{self.name}.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            return False
        return True

    def release(self) -> None:
        if os.name == "nt" and self._handle is not None:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
        elif self._file is not None:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


def heartbeat_pid(heartbeat: dict[str, Any] | None) -> int | None:
    if not heartbeat:
        return None
    details = heartbeat.get("details") or heartbeat.get("details_json") or {}
    if isinstance(details, dict):
        try:
            pid = int(details.get("pid") or 0)
            if pid > 0:
                return pid
        except (TypeError, ValueError):
            pass
    instance_id = str(heartbeat.get("instance_id") or "")
    try:
        pid = int(instance_id.rsplit(":", 1)[-1])
        return pid if pid > 0 else None
    except (TypeError, ValueError):
        return None


def _command_matches(pid: int, expected_fragments: Sequence[str]) -> tuple[bool, str]:
    try:
        if os.name == "nt":
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\").CommandLine",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                check=False,
            )
            command = completed.stdout.strip().lower()
        else:
            proc_command = Path(f"/proc/{pid}/cmdline")
            if proc_command.exists():
                command = proc_command.read_bytes().replace(b"\x00", b" ").decode(
                    "utf-8", errors="replace"
                ).lower()
            else:
                completed = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                command = completed.stdout.strip().lower()
        if not command:
            return False, f"找不到 PID {pid} 或無法讀取命令列"
        if expected_fragments and not all(fragment.lower() in command for fragment in expected_fragments):
            return False, f"PID {pid} 的命令列不符合預期，已拒絕終止：{command}"
        return True, command
    except Exception as exc:
        return False, f"無法確認 PID {pid}：{exc}"


def terminate_registered_process(
    pid: int | None,
    *,
    expected_fragments: Sequence[str],
    include_children: bool,
) -> tuple[bool, str]:
    """Terminate only a PID whose command line matches the expected service."""
    if not pid:
        return True, "沒有已登記的程序"
    if pid == os.getpid():
        return False, "拒絕終止目前執行中的監控程序"
    matched, message = _command_matches(pid, expected_fragments)
    if not matched:
        return False, message
    try:
        if os.name == "nt":
            command = ["taskkill", "/PID", str(pid), "/F"]
            if include_children:
                command.insert(-1, "/T")
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            return completed.returncode == 0, completed.stdout.strip() or completed.stderr.strip()
        os.kill(pid, 15)
        return True, f"已送出 SIGTERM 給 PID {pid}"
    except Exception as exc:
        return False, f"終止 PID {pid} 失敗：{exc}"


def spawn_independent(command: Sequence[str], *, cwd: Path = PROJECT_ROOT) -> subprocess.Popen:
    """Launch a peer process in its own console/process group."""
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "shell": False,
        "env": os.environ.copy(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(list(command), **kwargs)
