"""Safe storage helpers for editable JSON configuration files."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

CONFIG_FILES = {
    "message_templates": "message_templates.json",
    "line_menus": "line_menu.json",
    "liff": "liff_settings.json",
    "customer_service": "customer_service.json",
    "message_schedules": "message_schedules.json",
    "line_alert_notifications": "line_alert_notifications.json",
}

T = TypeVar("T", bound=BaseModel)


def config_path(config_name: str) -> Path:
    try:
        filename = CONFIG_FILES[config_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported config: {config_name}") from exc
    return CONFIG_DIR / filename


def read_raw_config(config_name: str) -> dict[str, Any]:
    path = config_path(config_name)
    with path.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def read_config(config_name: str, model: type[T]) -> T:
    return model.model_validate(read_raw_config(config_name))


def config_revision(config_name: str) -> str:
    """Return a stable content revision used for optimistic UI updates."""
    return hashlib.sha256(config_path(config_name).read_bytes()).hexdigest()


def write_config(config_name: str, value: BaseModel | dict[str, Any]) -> None:
    """Atomically replace a config file after its caller validates the data."""
    path = config_path(config_name)
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.stem}-",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def upsert_by_id(items: list[T], new_item: T) -> list[T]:
    updated = list(items)
    for index, item in enumerate(updated):
        if getattr(item, "id") == getattr(new_item, "id"):
            updated[index] = new_item
            return updated
    updated.append(new_item)
    return updated


def find_by_id(items: list[T], item_id: str) -> T | None:
    return next((item for item in items if getattr(item, "id") == item_id), None)
