"""
================================================================================
檔案名稱: services/line_liff_config_service.py
功能說明: LIFF 頁面設定版本服務，保存修改紀錄、補齊新版必要頁面並提供安全還原功能
================================================================================
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.json_config_service import config_revision, read_raw_config


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = PROJECT_ROOT / "config" / "liff_settings_history.json"
MAX_HISTORY = 20


def _read_history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    with HISTORY_PATH.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    revisions = payload.get("revisions", []) if isinstance(payload, dict) else []
    return revisions if isinstance(revisions, list) else []


def _write_history(revisions: list[dict[str, Any]]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".liff-history-",
        suffix=".tmp",
        dir=HISTORY_PATH.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {"version": 1, "revisions": revisions[:MAX_HISTORY]},
                stream,
                ensure_ascii=False,
                indent=2,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, HISTORY_PATH)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def snapshot_liff_config(*, actor: str, reason: str) -> str:
    """Save the current config before a mutation and return its revision."""
    revision = config_revision("liff")
    revisions = _read_history()
    if any(item.get("revision") == revision for item in revisions):
        return revision
    revisions.insert(
        0,
        {
            "revision": revision,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "reason": reason,
            "config": read_raw_config("liff"),
        },
    )
    _write_history(revisions)
    return revision


def list_liff_history() -> list[dict[str, Any]]:
    """Return snapshot metadata without exposing duplicate full configs."""
    return [
        {key: value for key, value in item.items() if key != "config"}
        for item in _read_history()
    ]


def get_liff_snapshot(revision: str) -> dict[str, Any] | None:
    return next(
        (
            item.get("config")
            for item in _read_history()
            if item.get("revision") == revision
        ),
        None,
    )


def upgrade_liff_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Add mandatory pages introduced after an older snapshot was created."""
    upgraded = json.loads(json.dumps(snapshot, ensure_ascii=False))
    current = read_raw_config("liff")
    upgraded_pages = upgraded.setdefault("pages", {})
    current_pages = current.get("pages", {})
    if "union_staff_binding" not in upgraded_pages:
        upgraded_pages["union_staff_binding"] = current_pages["union_staff_binding"]
    return upgraded
