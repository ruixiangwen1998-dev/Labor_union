# -*- coding: utf-8 -*-
"""
File: scripts/file_watcher.py
Description: \u5730\u7aef\u6a94\u6848\u76e3\u63a7\u670d\u52d9\uff0c\u76e3\u63a7 downloads/ \u5404\u5c08\u5c6c\u5b50\u76ee\u9304\uff0c\u767c\u73fe .xlsx \u65b0\u589e\u6216\u8b8a\u66f4\u6642\u81ea\u52d5\u89f8\u767c\u5c0d\u61c9\u7684\u5fae\u532f\u5165\u8173\u672c\u3002
ponytail: \u4e0d\u4f7f\u7528\u4e8b\u4ef6\u961f\u5217\u6216\u4e26\u884c\u865f\uff0c\u76f4\u63a5\u5728 watchdog callback \u4e2d\u540c\u6b65\u57f7\u884c\u8173\u672c\uff1b\u5982\u679c\u672a\u4f86\u9700\u8981\u9023\u7e8c\u89f8\u767c\u6216\u4e26\u884c\u8655\u7406\uff0c\u518d\u5f15\u5165 Queue + Thread Pool\u3002
"""
import sys
import os
import time
import subprocess

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.append(ROOT)

from services.db_service import get_connection
from services.system_alert_service import resolve_if_exists, upsert_system_alert

# \u5b9a\u7fa9\u76e3\u63a7\u76ee\u9304 \u2192 \u5c0d\u61c9\u7684\u532f\u5165\u8173\u672c\u8def\u5f91
WATCH_CONFIG = {
    "downloads/hcm":             "scripts/imports/import_client_hcm.py",
    "downloads/client_beclass":  "scripts/imports/import_client_beclass.py",
    "downloads/staff_beclass":   "scripts/imports/import_staff_beclass.py",
    "downloads/bank":            "scripts/imports/import_finance_excel.py",
}

# \u51b7\u537b\u6642\u9593 (\u79d2)\uff1a\u540c\u4e00\u6a94\u6848\u5728\u591a\u5c11\u79d2\u5167\u91cd\u8907\u89f8\u767c\u5247\u50c5\u57f7\u884c\u4e00\u6b21
COOLDOWN_SECONDS = 5
_last_triggered = {}  # {filepath: last_trigger_timestamp}


class XlsxHandler(FileSystemEventHandler):
    def __init__(self, watch_dir, script_path):
        self.watch_dir = os.path.abspath(watch_dir)
        # ponytail: Use absolute paths to prevent CWD dependency issues
        self.script_path = os.path.abspath(script_path)

    def _trigger(self, event_path):
        # 1. 僅處理 .xlsx 檔案
        if not event_path.lower().endswith('.xlsx'):
            return
            
        # 2. 忽略 Excel 自動產生的隱藏暫存檔
        filename = os.path.basename(event_path)
        if filename.startswith('~$'):
            return

        # 3. 確保檔案已寫入完畢 (未被鎖定)
        # 嘗試以 "a" 模式開啟，若因正在寫入而被鎖定則進行延遲重試
        retry_count = 5
        for i in range(retry_count):
            try:
                with open(event_path, "a", encoding="utf-8"):
                    break
            except IOError:
                time.sleep(0.5)
        else:
            print(f"[警告] 檔案 {filename} 仍處於佔用或寫入狀態，跳過本次匯入")
            return

        # 4. 冷卻機制：避免同一檔案在短時間內重複觸發
        now = time.time()
        last = _last_triggered.get(event_path, 0)
        if now - last < COOLDOWN_SECONDS:
            return
        _last_triggered[event_path] = now

        print(f"\n[偵測到檔案變更] {event_path}")
        print(f"  觸發腳本: {self.script_path}")
        case_key = f"{os.path.basename(self.watch_dir)}/{filename}"
        try:
            result = subprocess.run(
                [sys.executable, self.script_path, event_path],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            if result.stdout:
                print(result.stdout.strip())
            if result.stderr:
                print(f"[警告] {result.stderr.strip()}")
            if result.returncode != 0:
                print(f"[錯誤] 腳本回傳非零返回碼: {result.returncode}")
                self._record_failure(
                    case_key, filename,
                    f"腳本回傳非零返回碼 {result.returncode}：{result.stderr.strip()[:500]}"
                )
            else:
                self._record_success(case_key)
        except Exception as e:
            print(f"[無法執行腳本] {e}")
            self._record_failure(case_key, filename, f"無法執行腳本：{e}")

    def _record_failure(self, case_key, filename, message):
        """IMPORT-002：記錄本次觸發的匯入腳本失敗，供異常警示中心讀取。"""
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                upsert_system_alert(
                    cursor,
                    alert_code="IMPORT-002",
                    source_domain="IMPORT",
                    case_key=case_key,
                    reason=f"檔案 {filename} 解析失敗：{message}",
                    details={"檔案": filename, "監控目錄": self.watch_dir, "訊息": message},
                )
            conn.commit()
        except Exception as exc:
            print(f"[警示寫入失敗] {exc}")
            conn.rollback()
        finally:
            conn.close()

    def _record_success(self, case_key):
        """同一檔案下次重新處理成功時，自動解除先前的 IMPORT-002 警示。"""
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                resolve_if_exists(
                    cursor,
                    alert_code="IMPORT-002",
                    case_key=case_key,
                    reason="重新處理成功，自動解除",
                )
            conn.commit()
        except Exception as exc:
            print(f"[警示解除失敗] {exc}")
            conn.rollback()
        finally:
            conn.close()

    def on_created(self, event):
        if not event.is_directory:
            self._trigger(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._trigger(event.src_path)


def main():
    # \u78ba\u4fdd\u6240\u6709\u76e3\u63a7\u76ee\u9304\u5b58\u5728
    for watch_dir in WATCH_CONFIG:
        os.makedirs(watch_dir, exist_ok=True)

    observer = Observer()
    for watch_dir, script_path in WATCH_CONFIG.items():
        abs_dir = os.path.abspath(watch_dir)
        handler = XlsxHandler(watch_dir, script_path)
        observer.schedule(handler, path=abs_dir, recursive=False)
        print(f"\u76e3\u63a7\u4e2d: {abs_dir}  \u2192  {script_path}")

    observer.start()
    print(f"\nFileWatcher \u5df2\u555f\u52d5\uff01\u76e3\u63a7 {len(WATCH_CONFIG)} \u500b\u76ee\u9304\u4e2d...(\u6309 Ctrl+C \u505c\u6b62)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nFileWatcher \u505c\u6b62\u4e2d...")
        observer.stop()
    observer.join()
    print("FileWatcher \u5df2\u505c\u6b62\u3002")


if __name__ == "__main__":
    main()
