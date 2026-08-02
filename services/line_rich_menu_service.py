"""
================================================================================
檔案名稱: services/line_rich_menu_service.py
功能說明: LINE 下方選單可靠發布服務，管理圖片、雙頁群組、Alias、發布重試與使用者綁定
================================================================================
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql
import requests

from api.schemas.line_config import LineMenusConfig, RichMenuDefinition
from services.db_service import get_connection
from services.json_config_service import config_revision, read_config
from services.line_task_service import enqueue_line_task
from services.media_storage_service import (
    MediaValidationError,
    get_media_asset,
    read_media_asset,
    store_generated_rich_menu_image,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_IDS_PATH = PROJECT_ROOT / "config" / "rich_menu_ids.json"
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}


class RichMenuPublicationNotFoundError(LookupError):
    pass


class RichMenuPublicationConflictError(RuntimeError):
    pass


class RichMenuPublishError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _decode_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def create_publication_job(menu_id: str, requested_by_admin_user_id: int | None) -> dict[str, Any]:
    config = read_config("line_menus", LineMenusConfig)
    menu = next((item for item in config.menus if item.id == menu_id), None)
    if not menu:
        raise RichMenuPublicationNotFoundError(f"找不到 Rich Menu {menu_id}")
    if not menu.enabled:
        raise RichMenuPublicationConflictError("停用中的 Rich Menu 不能發布")
    if menu.appearance.image_mode == "uploaded":
        if not menu.appearance.image_asset_id:
            raise RichMenuPublicationConflictError("上傳圖片模式尚未選擇圖片資產")
        asset = get_media_asset(menu.appearance.image_asset_id)
        if (asset.get("width"), asset.get("height")) != (
            menu.size.width,
            menu.size.height,
        ):
            raise RichMenuPublicationConflictError("圖片尺寸與 Rich Menu 尺寸不一致")

    revision = config_revision("line_menus")
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT id FROM line_rich_menu_publications
                WHERE menu_config_id=%s AND status IN ('pending','processing')
                LIMIT 1 FOR UPDATE
                """,
                (menu_id,),
            )
            active = cursor.fetchone()
            if active:
                raise RichMenuPublicationConflictError(
                    f"此選單已有發布工作 #{active['id']} 正在處理"
                )
            cursor.execute(
                """
                INSERT INTO line_rich_menu_publications (
                    menu_config_id, audience_role, config_revision, config_snapshot,
                    image_asset_id, requested_by_admin_user_id
                ) VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (
                    menu.id,
                    menu.audience_role,
                    revision,
                    json.dumps(menu.model_dump(mode="json"), ensure_ascii=False),
                    menu.appearance.image_asset_id,
                    requested_by_admin_user_id,
                ),
            )
            publication_id = int(cursor.lastrowid)
        conn.commit()
        return get_publication(publication_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_publication_group_jobs(
    group_id: str,
    requested_by_admin_user_id: int | None,
) -> list[dict[str, Any]]:
    """Queue every enabled menu in a tab group, publishing the entry menu last."""
    config = read_config("line_menus", LineMenusConfig)
    menus = [
        menu
        for menu in config.menus
        if menu.enabled and menu.menu_group_id == group_id
    ]
    if len(menus) < 2:
        raise RichMenuPublicationNotFoundError(f"找不到可發布的選單群組 {group_id}")
    entries = [menu for menu in menus if menu.is_group_entry]
    if len(entries) != 1:
        raise RichMenuPublicationConflictError("選單群組必須只有一個入口頁")
    ordered = sorted(menus, key=lambda menu: (menu.is_group_entry, menu.id))
    return [
        create_publication_job(menu.id, requested_by_admin_user_id)
        for menu in ordered
    ]


def get_publication(publication_id: int) -> dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT * FROM line_rich_menu_publications WHERE id=%s",
                (publication_id,),
            )
            item = cursor.fetchone()
        if not item:
            raise RichMenuPublicationNotFoundError(
                f"找不到 Rich Menu 發布工作 #{publication_id}"
            )
        item["config_snapshot"] = _decode_json(item.get("config_snapshot"))
        return item
    finally:
        conn.close()


def list_publications(
    *,
    menu_id: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    allowed_statuses = {"pending", "processing", "published", "failed"}
    if status and status not in allowed_statuses:
        raise ValueError("不支援的發布狀態")
    clauses = ["1=1"]
    params: list[Any] = []
    if menu_id:
        clauses.append("menu_config_id=%s")
        params.append(menu_id)
    if status:
        clauses.append("status=%s")
        params.append(status)
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    offset = (page - 1) * page_size
    where_sql = " AND ".join(clauses)
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                f"SELECT COUNT(1) AS total FROM line_rich_menu_publications WHERE {where_sql}",
                params,
            )
            total = int((cursor.fetchone() or {}).get("total") or 0)
            cursor.execute(
                f"""
                SELECT id, menu_config_id, audience_role, config_revision, status,
                       line_rich_menu_id, previous_line_rich_menu_id, image_asset_id,
                       retry_count, max_retries, is_current, error_code, error_message,
                       created_at, started_at, published_at, failed_at, updated_at
                FROM line_rich_menu_publications
                WHERE {where_sql}
                ORDER BY id DESC LIMIT %s OFFSET %s
                """,
                [*params, page_size, offset],
            )
            items = list(cursor.fetchall())
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }
    finally:
        conn.close()


def retry_publication(publication_id: int) -> dict[str, Any]:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT id,status FROM line_rich_menu_publications WHERE id=%s FOR UPDATE",
                (publication_id,),
            )
            item = cursor.fetchone()
            if not item:
                raise RichMenuPublicationNotFoundError(
                    f"找不到 Rich Menu 發布工作 #{publication_id}"
                )
            if item["status"] != "failed":
                raise RichMenuPublicationConflictError("只有失敗的發布工作可以重試")
            cursor.execute(
                """
                UPDATE line_rich_menu_publications
                SET status='pending', retry_count=0, next_retry_at=NULL,
                    processing_started_at=NULL, error_code=NULL, error_message=NULL,
                    failed_at=NULL
                WHERE id=%s
                """,
                (publication_id,),
            )
        conn.commit()
        return get_publication(publication_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_current_rich_menu_id(audience_role: str) -> str:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT line_rich_menu_id FROM line_rich_menu_publications
                WHERE audience_role=%s AND status='published' AND is_current=TRUE
                  AND (
                    JSON_EXTRACT(config_snapshot,'$.menu_group_id') IS NULL
                    OR JSON_UNQUOTE(JSON_EXTRACT(config_snapshot,'$.is_group_entry'))='true'
                  )
                ORDER BY published_at DESC, id DESC LIMIT 1
                """,
                (audience_role,),
            )
            row = cursor.fetchone()
        if not row:
            return ""
        return str(row.get("line_rich_menu_id") if isinstance(row, dict) else row[0] or "")
    except pymysql.MySQLError:
        return ""
    finally:
        conn.close()


def import_legacy_rich_menu_ids() -> int:
    """Register existing JSON IDs once without republishing or contacting LINE."""
    try:
        legacy = json.loads(LEGACY_IDS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    config = read_config("line_menus", LineMenusConfig)
    revision = config_revision("line_menus")
    key_by_role = {
        "customer": "default_rich_menu_id",
        "staff": "staff_rich_menu_id",
        "union_staff": "union_staff_rich_menu_id",
    }
    imported = 0
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            for menu in config.menus:
                rich_menu_id = str(legacy.get(key_by_role[menu.audience_role]) or "").strip()
                if not rich_menu_id:
                    continue
                cursor.execute(
                    """
                    SELECT id FROM line_rich_menu_publications
                    WHERE audience_role=%s AND is_current=TRUE LIMIT 1 FOR UPDATE
                    """,
                    (menu.audience_role,),
                )
                if cursor.fetchone():
                    continue
                cursor.execute(
                    """
                    INSERT INTO line_rich_menu_publications (
                        menu_config_id, audience_role, config_revision, config_snapshot,
                        status, line_rich_menu_id, is_current, published_at
                    ) VALUES (%s,%s,%s,%s,'published',%s,TRUE,UTC_TIMESTAMP())
                    """,
                    (
                        menu.id,
                        menu.audience_role,
                        revision,
                        json.dumps(menu.model_dump(mode="json"), ensure_ascii=False),
                        rich_menu_id,
                    ),
                )
                imported += 1
        conn.commit()
        return imported
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recover_stale_publications() -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE line_rich_menu_publications
                SET status='pending', processing_started_at=NULL,
                    error_code='stale_recovered'
                WHERE status='processing'
                  AND processing_started_at < UTC_TIMESTAMP() - INTERVAL 10 MINUTE
                """
            )
        conn.commit()
    finally:
        conn.close()


def next_publication_run_at() -> datetime | None:
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT MIN(COALESCE(next_retry_at,created_at))
                FROM line_rich_menu_publications WHERE status='pending'
                """
            )
            row = cursor.fetchone()
        return next(iter(row.values()), None) if isinstance(row, dict) else row[0] if row else None
    finally:
        conn.close()


def _claim_publications(limit: int = 2) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT * FROM line_rich_menu_publications
                WHERE status='pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= UTC_TIMESTAMP())
                ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            items = list(cursor.fetchall())
            for item in items:
                cursor.execute(
                    """
                    UPDATE line_rich_menu_publications
                    SET status='processing', processing_started_at=UTC_TIMESTAMP(),
                        started_at=COALESCE(started_at,UTC_TIMESTAMP())
                    WHERE id=%s
                    """,
                    (item["id"],),
                )
        conn.commit()
        for item in items:
            item["config_snapshot"] = _decode_json(item["config_snapshot"])
        return items
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_line_action(action: dict[str, Any]) -> dict[str, str]:
    if action["type"] == "message":
        return {"type": "message", "text": action["text"]}
    if action["type"] == "postback":
        return {"type": "postback", "data": action["data"]}
    if action["type"] == "richmenuswitch":
        return {
            "type": "richmenuswitch",
            "richMenuAliasId": action["rich_menu_alias_id"],
            "data": action["data"],
        }
    if action.get("uri_source") == "liff":
        liff_id = os.getenv("LINE_LIFF_ID", "").strip()
        if not liff_id:
            raise RichMenuPublishError(
                "liff_not_configured",
                "LINE_LIFF_ID 尚未設定",
                retryable=False,
            )
        suffix = (action.get("uri") or "").strip()
        uri = f"https://liff.line.me/{liff_id}"
        if suffix.startswith(("?", "#")):
            uri += suffix
        return {"type": "uri", "uri": uri}
    return {"type": "uri", "uri": action["uri"]}


def build_line_menu(menu: dict[str, Any]) -> dict[str, Any]:
    validated = RichMenuDefinition.model_validate(menu)
    data = validated.model_dump(mode="json")
    return {
        "size": data["size"],
        "selected": data.get("selected", True),
        "name": data["name"],
        "chatBarText": data["chat_bar_text"],
        "areas": [
            {"bounds": item["bounds"], "action": build_line_action(item["action"])}
            for item in data["buttons"]
        ],
    }


def _line_request(method: str, url: str, **kwargs) -> requests.Response:
    try:
        response = requests.request(method, url, timeout=30, **kwargs)
    except requests.RequestException as exc:
        raise RichMenuPublishError("network_error", str(exc), retryable=True) from exc
    if not response.ok:
        raise RichMenuPublishError(
            f"http_{response.status_code}",
            response.text[:4000],
            retryable=response.status_code in RETRYABLE_HTTP,
        )
    return response


def _upsert_rich_menu_alias(
    alias_id: str,
    rich_menu_id: str,
    headers: dict[str, str],
) -> None:
    lookup_url = f"https://api.line.me/v2/bot/richmenu/alias/{alias_id}"
    try:
        response = requests.get(lookup_url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise RichMenuPublishError("network_error", str(exc), retryable=True) from exc
    if response.status_code == 404:
        _line_request(
            "POST",
            "https://api.line.me/v2/bot/richmenu/alias",
            headers=headers,
            json={"richMenuAliasId": alias_id, "richMenuId": rich_menu_id},
        )
        return
    if not response.ok:
        raise RichMenuPublishError(
            f"http_{response.status_code}",
            response.text[:4000],
            retryable=response.status_code in RETRYABLE_HTTP,
        )
    _line_request(
        "POST",
        lookup_url,
        headers=headers,
        json={"richMenuId": rich_menu_id},
    )


def _ensure_group_dependencies(item: dict[str, Any]) -> None:
    menu = item["config_snapshot"]
    group_id = menu.get("menu_group_id")
    if not group_id or not menu.get("is_group_entry"):
        return
    config = read_config("line_menus", LineMenusConfig)
    dependencies = [
        other.id
        for other in config.menus
        if other.enabled
        and other.menu_group_id == group_id
        and other.id != item["menu_config_id"]
    ]
    if not dependencies:
        return
    placeholders = ",".join(["%s"] * len(dependencies))
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(DISTINCT menu_config_id)
                FROM line_rich_menu_publications
                WHERE menu_config_id IN ({placeholders})
                  AND config_revision=%s AND status='published' AND is_current=TRUE
                """,
                [*dependencies, item["config_revision"]],
            )
            row = cursor.fetchone()
        count = int(next(iter(row.values()), 0) if isinstance(row, dict) else row[0] if row else 0)
    finally:
        conn.close()
    if count != len(dependencies):
        raise RichMenuPublishError(
            "group_dependency_pending",
            "同組的其他選單頁尚未完成發布",
            retryable=True,
        )


def _publish_to_line(item: dict[str, Any]) -> tuple[str, int]:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token or token == "mock_token" or token.startswith("your_"):
        raise RichMenuPublishError(
            "line_token_not_configured",
            "LINE_CHANNEL_ACCESS_TOKEN 尚未設定",
            retryable=False,
        )
    menu = item["config_snapshot"]
    _ensure_group_dependencies(item)
    appearance = menu.get("appearance", {})
    if appearance.get("image_mode", "generated") == "generated":
        if item.get("image_asset_id"):
            asset = get_media_asset(int(item["image_asset_id"]))
        else:
            asset = store_generated_rich_menu_image(
                menu,
                created_by_admin_user_id=item.get("requested_by_admin_user_id"),
            )
            conn = get_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE line_rich_menu_publications SET image_asset_id=%s WHERE id=%s",
                        (asset["id"], item["id"]),
                    )
                conn.commit()
            finally:
                conn.close()
            item["image_asset_id"] = asset["id"]
    else:
        asset = get_media_asset(int(appearance["image_asset_id"]))
    _, image_content = read_media_asset(int(asset["id"]))
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    created_id = ""
    try:
        created = _line_request(
            "POST",
            "https://api.line.me/v2/bot/richmenu",
            headers=headers,
            json=build_line_menu(menu),
        )
        created_id = created.json()["richMenuId"]
        _line_request(
            "POST",
            f"https://api-data.line.me/v2/bot/richmenu/{created_id}/content",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
            data=image_content,
        )
        alias_id = (menu.get("rich_menu_alias_id") or "").strip()
        if alias_id:
            _upsert_rich_menu_alias(alias_id, created_id, headers)
        if menu.get("set_as_default"):
            _line_request(
                "POST",
                f"https://api.line.me/v2/bot/user/all/richmenu/{created_id}",
                headers=headers,
            )
        return created_id, int(asset["id"])
    except Exception:
        if created_id:
            try:
                requests.delete(
                    f"https://api.line.me/v2/bot/richmenu/{created_id}",
                    headers=headers,
                    timeout=10,
                )
            except requests.RequestException:
                pass
        raise


def _write_legacy_id(audience_role: str, rich_menu_id: str) -> None:
    key = {
        "customer": "default_rich_menu_id",
        "staff": "staff_rich_menu_id",
        "union_staff": "union_staff_rich_menu_id",
    }[audience_role]
    try:
        existing = json.loads(LEGACY_IDS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    existing[key] = rich_menu_id
    LEGACY_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".rich-menu-ids-", suffix=".tmp", dir=LEGACY_IDS_PATH.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(existing, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, LEGACY_IDS_PATH)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _complete_publication(item: dict[str, Any], rich_menu_id: str, asset_id: int) -> None:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT line_rich_menu_id FROM line_rich_menu_publications
                WHERE menu_config_id=%s AND is_current=TRUE
                ORDER BY id DESC LIMIT 1 FOR UPDATE
                """,
                (item["menu_config_id"],),
            )
            previous = cursor.fetchone()
            previous_id = previous["line_rich_menu_id"] if previous else None
            cursor.execute(
                "UPDATE line_rich_menu_publications SET is_current=FALSE WHERE menu_config_id=%s",
                (item["menu_config_id"],),
            )
            cursor.execute(
                """
                UPDATE line_rich_menu_publications
                SET status='published', line_rich_menu_id=%s,
                    previous_line_rich_menu_id=%s, image_asset_id=%s,
                    is_current=TRUE, published_at=UTC_TIMESTAMP(),
                    processing_started_at=NULL, next_retry_at=NULL,
                    error_code=NULL, error_message=NULL
                WHERE id=%s
                """,
                (rich_menu_id, previous_id, asset_id, item["id"]),
            )
            snapshot = item.get("config_snapshot") or {}
            should_link_role = (
                not snapshot.get("menu_group_id") or snapshot.get("is_group_entry")
            )
            if item["audience_role"] in {"staff", "union_staff"} and should_link_role:
                cursor.execute(
                    """
                    SELECT line_user_id FROM line_users
                    WHERE role=%s AND status='active'
                    """,
                    (item["audience_role"],),
                )
                for user in cursor.fetchall():
                    user_id = user["line_user_id"]
                    enqueue_line_task(
                        cursor,
                        to_user_id=user_id,
                        task_type="rich_menu_link",
                        payload={"rich_menu_id": rich_menu_id},
                        idempotency_key=f"rich-menu-publication:{item['id']}:{user_id}",
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    snapshot = item.get("config_snapshot") or {}
    if snapshot.get("menu_group_id") and not snapshot.get("is_group_entry"):
        return
    try:
        _write_legacy_id(item["audience_role"], rich_menu_id)
    except OSError as exc:
        # MySQL is the authoritative runtime state; the JSON file is only a
        # temporary compatibility bridge for older code and deployments.
        print(f"[Rich Menu] Failed to update legacy id file: {exc}")


def _fail_publication(item: dict[str, Any], exc: Exception) -> None:
    retryable = isinstance(exc, RichMenuPublishError) and exc.retryable
    code = exc.code if isinstance(exc, RichMenuPublishError) else "publish_exception"
    retry_count = int(item.get("retry_count") or 0) + 1
    will_retry = retryable and retry_count <= int(item.get("max_retries") or 0)
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            if will_retry:
                delay = min(60 * (2 ** (retry_count - 1)), 3600)
                cursor.execute(
                    """
                    UPDATE line_rich_menu_publications
                    SET status='pending', retry_count=%s,
                        next_retry_at=DATE_ADD(UTC_TIMESTAMP(),INTERVAL %s SECOND),
                        processing_started_at=NULL, error_code=%s, error_message=%s
                    WHERE id=%s
                    """,
                    (retry_count, delay, code, str(exc)[:4000], item["id"]),
                )
            else:
                cursor.execute(
                    """
                    UPDATE line_rich_menu_publications
                    SET status='failed', retry_count=%s, failed_at=UTC_TIMESTAMP(),
                        processing_started_at=NULL, next_retry_at=NULL,
                        error_code=%s, error_message=%s
                    WHERE id=%s
                    """,
                    (retry_count, code, str(exc)[:4000], item["id"]),
                )
        conn.commit()
    finally:
        conn.close()


def process_due_publications() -> int:
    processed = 0
    while True:
        items = _claim_publications()
        if not items:
            return processed
        for item in items:
            try:
                rich_menu_id, asset_id = _publish_to_line(item)
                _complete_publication(item, rich_menu_id, asset_id)
            except Exception as exc:
                _fail_publication(item, exc)
            processed += 1
