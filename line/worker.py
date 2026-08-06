"""
================================================================================
檔案名稱: line/worker.py
功能說明: LINE 背景任務 Worker，負責心跳、排程喚醒、文字／複合訊息發送、重試與執行紀錄
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import pymysql
import requests

from services.db_service import get_connection as get_db_connection
from services.line_monitor_service import monitor_instance_id, record_service_heartbeat
from services.line_order_group_service import (
    build_invite_flex_message,
    expire_stale_invite_tasks,
    finalize_invite_task,
)
from services.line_rich_menu_service import (
    import_legacy_rich_menu_ids,
    next_publication_run_at,
    process_due_publications,
    recover_stale_publications,
)


_wakeup_event = asyncio.Event()
_worker_task: asyncio.Task[None] | None = None
_worker_instance_id = monitor_instance_id()
_last_worker_cycle_at: datetime | None = None
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def wake_worker() -> None:
    _wakeup_event.set()


def _recover_stale_tasks() -> None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE line_tasks
                SET status='pending', processing_started_at=NULL,
                    error_code='stale_recovered'
                WHERE status='processing'
                  AND processing_started_at < UTC_TIMESTAMP() - INTERVAL 10 MINUTE
                """
            )
            conn.commit()
    finally:
        conn.close()


def _claim_due_tasks(limit: int = 10) -> list[dict[str, Any]]:
    conn = get_db_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT * FROM line_tasks
                WHERE status='pending'
                  AND scheduled_at <= UTC_TIMESTAMP()
                  AND (next_retry_at IS NULL OR next_retry_at <= UTC_TIMESTAMP())
                ORDER BY scheduled_at, id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            tasks = list(cursor.fetchall())
            if tasks:
                for task in tasks:
                    if not task.get("line_request_id"):
                        task["line_request_id"] = str(uuid.uuid4())
                ids = [task["id"] for task in tasks]
                placeholders = ",".join(["%s"] * len(ids))
                cursor.execute(
                    f"UPDATE line_tasks SET status='processing', processing_started_at=UTC_TIMESTAMP() WHERE id IN ({placeholders})",
                    ids,
                )
                for task in tasks:
                    cursor.execute(
                        "UPDATE line_tasks SET line_request_id=COALESCE(line_request_id,%s) WHERE id=%s",
                        (task["line_request_id"], task["id"]),
                    )
        conn.commit()
        return tasks
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _next_run_at() -> datetime | None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT MIN(GREATEST(scheduled_at, COALESCE(next_retry_at, scheduled_at)))
                FROM line_tasks WHERE status='pending'
                """
            )
            row = cursor.fetchone()
            return next(iter(row.values()), None) if isinstance(row, dict) else row[0] if row else None
    finally:
        conn.close()


def _line_headers(task: dict[str, Any]) -> dict[str, str]:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "mock_token")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Line-Retry-Key": task["line_request_id"],
    }


def _push_text(task: dict[str, Any], text: str) -> tuple[bool, bool, str, str]:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "mock_token")
    if not token or token == "mock_token":
        print(f"[LINE Mock] Task #{task['id']}: {task['task_type']}")
        return True, False, "", ""
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/push",
            json={"to": task["to_user_id"], "messages": [{"type": "text", "text": text}]},
            headers=_line_headers(task),
            timeout=10,
        )
    except requests.RequestException as exc:
        return False, True, "network_error", str(exc)
    if response.status_code == 200:
        return True, False, "", ""
    return False, response.status_code in RETRYABLE_HTTP, f"http_{response.status_code}", response.text


def _push_messages(
    task: dict[str, Any],
    messages: list[dict[str, Any]],
) -> tuple[bool, bool, str, str]:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "mock_token")
    if not token or token == "mock_token":
        print(f"[LINE Mock] Task #{task['id']}: {task['task_type']}")
        return True, False, "", ""
    if not messages or len(messages) > 5:
        return False, False, "invalid_messages", "LINE messages must contain 1 to 5 items"
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/push",
            json={"to": task["to_user_id"], "messages": messages},
            headers=_line_headers(task),
            timeout=10,
        )
    except requests.RequestException as exc:
        return False, True, "network_error", str(exc)
    if response.status_code == 200:
        return True, False, "", ""
    return False, response.status_code in RETRYABLE_HTTP, f"http_{response.status_code}", response.text


def _rag_answer(user_text: str) -> str:
    fallback = "很抱歉，我不太懂您的意思，已經幫您轉交給行政專員為您人工處理。"
    try:
        import chromadb

        client = chromadb.PersistentClient(path="./db/chroma_data")
        collection = client.get_or_create_collection("union_faq")
        results = collection.query(query_texts=[user_text], n_results=1)
        if results and results.get("distances") and results["distances"][0]:
            if results["distances"][0][0] < 1.0:
                return results["metadatas"][0][0].get("answer", fallback)
    except Exception as exc:
        print(f"[LINE Worker] RAG query failed: {exc}")
    return fallback


def _menu_action(task: dict[str, Any], link: bool) -> tuple[bool, bool, str, str]:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "mock_token")
    payload = json.loads(task.get("payload_json") or "{}")
    if not token or token == "mock_token":
        return True, False, "", ""
    url = f"https://api.line.me/v2/bot/user/{task['to_user_id']}/richmenu"
    menu_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        if link:
            menu_id = payload.get("rich_menu_id")
            if not menu_id:
                return False, False, "menu_not_set", "Rich Menu ID is missing"
            response = requests.post(f"{url}/{menu_id}", headers=menu_headers, timeout=10)
        else:
            response = requests.delete(url, headers=menu_headers, timeout=10)
    except requests.RequestException as exc:
        return False, True, "network_error", str(exc)
    if response.status_code == 200:
        followup = payload.get("success_message")
        return _push_text(task, followup) if followup else (True, False, "", "")
    return False, response.status_code in RETRYABLE_HTTP, f"http_{response.status_code}", response.text


def _execute_task(task: dict[str, Any]) -> tuple[bool, bool, str, str]:
    task_type = task["task_type"]
    if task_type == "line_push":
        return _push_text(task, task.get("message_content") or "")
    if task_type == "line_push_messages":
        payload = json.loads(task.get("payload_json") or "{}")
        return _push_messages(task, payload.get("messages") or [])
    if task_type == "order_group_invite":
        return _push_messages(task, build_invite_flex_message(task))
    if task_type == "rag_reply":
        payload = json.loads(task.get("payload_json") or "{}")
        return _push_text(task, _rag_answer(payload.get("user_text", "")))
    if task_type == "rich_menu_link":
        return _menu_action(task, True)
    if task_type == "rich_menu_unlink":
        return _menu_action(task, False)
    return False, False, "unknown_task_type", task_type


def _start_task_attempt(task: dict[str, Any]) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 AS attempt_no FROM line_task_attempts WHERE task_id=%s",
                (task["id"],),
            )
            row = cursor.fetchone()
            attempt_no = int(row.get("attempt_no", 1) if isinstance(row, dict) else row[0])
            cursor.execute(
                """
                INSERT INTO line_task_attempts (
                    task_id, attempt_no, outcome, line_request_id
                ) VALUES (%s,%s,'running',%s)
                """,
                (task["id"], attempt_no, task.get("line_request_id")),
            )
            attempt_id = int(cursor.lastrowid)
        conn.commit()
        return attempt_id
    finally:
        conn.close()


def _finish_task_attempt(
    attempt_id: int,
    result: tuple[bool, bool, str, str],
    final_status: str,
) -> None:
    _, retryable, code, message = result
    outcome = {
        "sent": "sent",
        "pending": "retry_scheduled",
        "failed": "failed",
    }[final_status]
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE line_task_attempts
                SET outcome=%s, retryable=%s, error_code=%s,
                    error_message=%s, finished_at=UTC_TIMESTAMP()
                WHERE id=%s
                """,
                (
                    outcome,
                    retryable,
                    code or None,
                    message[:4000] if message else None,
                    attempt_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _finish_task(task: dict[str, Any], result: tuple[bool, bool, str, str]) -> str:
    success, retryable, code, message = result
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if success:
                cursor.execute(
                    "UPDATE line_tasks SET status='sent', sent_at=UTC_TIMESTAMP(), processing_started_at=NULL, error_code=NULL, error_message=NULL WHERE id=%s",
                    (task["id"],),
                )
                final_status = "sent"
            elif retryable and task["retry_count"] < task["max_retries"]:
                retry_count = task["retry_count"] + 1
                delay_seconds = min(60 * (2 ** (retry_count - 1)), 3600)
                cursor.execute(
                    """
                    UPDATE line_tasks SET status='pending', retry_count=%s,
                        next_retry_at=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND),
                        processing_started_at=NULL, error_code=%s, error_message=%s
                    WHERE id=%s
                    """,
                    (retry_count, delay_seconds, code, message[:4000], task["id"]),
                )
                final_status = "pending"
            else:
                cursor.execute(
                    """
                    UPDATE line_tasks SET status='failed', failed_at=UTC_TIMESTAMP(),
                        processing_started_at=NULL, error_code=%s, error_message=%s
                    WHERE id=%s
                    """,
                    (code, message[:4000], task["id"]),
                )
                final_status = "failed"
            finalize_invite_task(cursor, task, final_status)
            conn.commit()
            return final_status
    finally:
        conn.close()


async def process_due_tasks() -> None:
    while True:
        tasks = await asyncio.to_thread(_claim_due_tasks)
        if not tasks:
            return
        for task in tasks:
            attempt_id = await asyncio.to_thread(_start_task_attempt, task)
            try:
                result = await asyncio.to_thread(_execute_task, task)
            except Exception as exc:
                result = (False, True, "worker_exception", str(exc))
            final_status = await asyncio.to_thread(_finish_task, task, result)
            await asyncio.to_thread(
                _finish_task_attempt, attempt_id, result, final_status
            )


async def worker_loop() -> None:
    global _last_worker_cycle_at
    print("[LINE Worker] Reliable worker started")
    imported = await asyncio.to_thread(import_legacy_rich_menu_ids)
    if imported:
        print(f"[LINE Worker] Imported {imported} legacy Rich Menu ID(s)")
    await asyncio.to_thread(_recover_stale_tasks)
    expired = await asyncio.to_thread(expire_stale_invite_tasks)
    if expired:
        print(f"[LINE Worker] Expired and redacted {expired} stale group invite task(s)")
    await asyncio.to_thread(recover_stale_publications)
    while True:
        try:
            _last_worker_cycle_at = _utc_now_naive()
            await asyncio.to_thread(expire_stale_invite_tasks)
            await process_due_tasks()
            await asyncio.to_thread(process_due_publications)
            _wakeup_event.clear()
            next_at = await asyncio.to_thread(_next_run_at)
            next_publication_at = await asyncio.to_thread(next_publication_run_at)
            if next_at is None or (
                next_publication_at is not None and next_publication_at < next_at
            ):
                next_at = next_publication_at
            if _wakeup_event.is_set():
                continue
            # Notification is primary; a low-frequency scan recovers a task if
            # its wake-up signal was lost while a process was restarting.
            timeout = 15.0 if next_at is None else min(
                15.0,
                max(0.0, (next_at - _utc_now_naive()).total_seconds()),
            )
            try:
                await asyncio.wait_for(_wakeup_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[LINE Worker] Worker loop error: {exc}")
            await asyncio.sleep(5)


async def _worker_heartbeat_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(
                record_service_heartbeat,
                "line_worker",
                _worker_instance_id,
                details={
                    "task_running": True,
                    "last_cycle_at": _last_worker_cycle_at.isoformat()
                    if _last_worker_cycle_at
                    else None,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[LINE Worker] Heartbeat update failed: {exc}")
        await asyncio.sleep(15)


async def _worker_service() -> None:
    heartbeat_task = asyncio.create_task(
        _worker_heartbeat_loop(), name="line-worker-heartbeat"
    )
    try:
        await worker_loop()
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


def start_worker() -> asyncio.Task[None]:
    global _worker_task
    _worker_task = asyncio.create_task(_worker_service(), name="line-task-worker")
    return _worker_task


def worker_is_running() -> bool:
    return _worker_task is not None and not _worker_task.done()


async def stop_worker(task: asyncio.Task[None]) -> None:
    global _worker_task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if _worker_task is task:
            _worker_task = None
