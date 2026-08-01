"""
================================================================================
檔案名稱: api/routes/line_system_config.py
功能說明: LINE 系統設定 API，管理訊息內容、自動通知、下方選單與 LIFF 頁面設定
================================================================================
"""

from __future__ import annotations

import threading
from typing import TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ValidationError

from api.schemas.line_config import (
    CustomerServiceConfig,
    LiffField,
    LiffPage,
    LiffSettingsConfig,
    LiffTheme,
    LineMenusConfig,
    MessageTemplate,
    MessageTemplateDraftPreviewRequest,
    MessageTemplatePreviewRequest,
    MessageTemplatesConfig,
    MessageSchedulesConfig,
    RichMenuDefinition,
)
from services.json_config_service import (
    config_revision,
    find_by_id,
    read_config,
    upsert_by_id,
    write_config,
)
from api.dependencies.admin_auth import require_line_manager, require_line_viewer
from line.worker import wake_worker
from services.line_rich_menu_service import (
    RichMenuPublicationConflictError,
    RichMenuPublicationNotFoundError,
    create_publication_job,
)
from services.line_liff_config_service import (
    get_liff_snapshot,
    list_liff_history,
    snapshot_liff_config,
    upgrade_liff_snapshot,
)


router = APIRouter(
    prefix="/api/config",
    tags=["System Config"],
    dependencies=[Depends(require_line_viewer)],
)
public_router = APIRouter(prefix="/api/config", tags=["Public LINE Config"])
T = TypeVar("T", bound=BaseModel)
MESSAGE_TEMPLATE_LOCK = threading.RLock()
MESSAGE_SCHEDULE_LOCK = threading.RLock()
LINE_MENU_LOCK = threading.RLock()
LIFF_CONFIG_LOCK = threading.RLock()


def _read(name: str, model: type[T]) -> T:
    try:
        return read_config(name, model)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Configuration {name} not found") from exc
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=500, detail=f"Invalid stored configuration: {exc}") from exc


def _save(name: str, value: BaseModel) -> None:
    try:
        write_config(name, value)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to save configuration: {exc}") from exc


def _normalize_revision(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:]
    return normalized.strip('"')


def _require_message_template_revision(if_match: str | None) -> None:
    """Reject stale UI drafts while preserving older clients without If-Match."""
    expected = _normalize_revision(if_match)
    if expected and expected != config_revision("message_templates"):
        raise HTTPException(
            status_code=409,
            detail="訊息範本已被其他人修改，請重新載入後再儲存",
        )


def _require_message_schedule_revision(if_match: str | None) -> None:
    expected = _normalize_revision(if_match)
    if expected and expected != config_revision("message_schedules"):
        raise HTTPException(
            status_code=409,
            detail="訊息排程已被其他人修改，請重新載入後再儲存",
        )


def _require_line_menu_revision(if_match: str | None) -> None:
    expected = _normalize_revision(if_match)
    if expected and expected != config_revision("line_menus"):
        raise HTTPException(
            status_code=409,
            detail="Rich Menu 已被其他人修改，請重新載入後再儲存",
        )


def _require_liff_revision(if_match: str | None) -> None:
    expected = _normalize_revision(if_match)
    if expected and expected != config_revision("liff"):
        raise HTTPException(
            status_code=409,
            detail="LIFF 設定已被其他人修改，請重新載入後再儲存",
        )


def _principal_name(request: Request) -> str:
    principal = getattr(request.state, "admin_principal", None)
    return str(getattr(principal, "username", None) or "system")


def _save_liff(payload: LiffSettingsConfig, request: Request, *, reason: str) -> None:
    snapshot_liff_config(actor=_principal_name(request), reason=reason)
    _save("liff", payload)


def _template_schedule_references(template_id: str) -> list[dict[str, int | str]]:
    schedules = _read("message_schedules", MessageSchedulesConfig)
    return [
        {"schedule_id": schedule.id, "schedule_name": schedule.name, "day": step.day}
        for schedule in schedules.schedules
        if schedule.enabled
        for step in schedule.steps
        if step.template_id == template_id
    ]


def _validate_scheduled_template_availability(config: MessageTemplatesConfig) -> None:
    available = {item.id for item in config.templates if item.enabled}
    schedules = _read("message_schedules", MessageSchedulesConfig)
    missing = sorted(
        {
            step.template_id
            for schedule in schedules.schedules
            if schedule.enabled
            for step in schedule.steps
            if step.template_id not in available
        }
    )
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"啟用中的排程仍引用下列缺少或停用的範本：{', '.join(missing)}",
        )


def _render_message_template(item: MessageTemplate, variables: dict[str, str]) -> dict:
    if item.message_type == "flex":
        return {"message_type": "flex", "content": item.content}
    rendered = str(item.content)
    for variable in item.variables:
        if variable.required and variable.name not in variables:
            raise HTTPException(status_code=422, detail=f"Missing variable: {variable.name}")
        rendered = rendered.replace(
            "{" + variable.name + "}", variables.get(variable.name, "")
        )
    return {"message_type": "text", "content": rendered}


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------
@router.get("/message-templates", response_model=MessageTemplatesConfig)
def get_message_templates():
    return _read("message_templates", MessageTemplatesConfig)


@router.get("/message-templates/state")
def get_message_templates_state():
    return {
        "revision": config_revision("message_templates"),
        "config": _read("message_templates", MessageTemplatesConfig),
    }


@router.post("/message-templates/preview")
def preview_message_template_draft(payload: MessageTemplateDraftPreviewRequest):
    return _render_message_template(payload.template, payload.variables)


@router.put("/message-templates", response_model=MessageTemplatesConfig, dependencies=[Depends(require_line_manager)])
def replace_message_templates(
    payload: MessageTemplatesConfig,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with MESSAGE_TEMPLATE_LOCK:
        _require_message_template_revision(if_match)
        _validate_scheduled_template_availability(payload)
        _save("message_templates", payload)
    request.state.audit_action = "line.message_templates.replace"
    request.state.audit_resource_type = "line_message_templates"
    request.state.audit_details = {"template_count": len(payload.templates)}
    return payload


@router.post(
    "/message-templates",
    response_model=MessageTemplate,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_line_manager)],
)
def create_message_template(
    payload: MessageTemplate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with MESSAGE_TEMPLATE_LOCK:
        _require_message_template_revision(if_match)
        config = _read("message_templates", MessageTemplatesConfig)
        if find_by_id(config.templates, payload.id):
            raise HTTPException(status_code=409, detail="Template id already exists")
        config.templates.append(payload)
        validated = MessageTemplatesConfig.model_validate(config)
        _save("message_templates", validated)
    request.state.audit_action = "line.message_template.create"
    request.state.audit_resource_type = "line_message_template"
    request.state.audit_resource_id = payload.id
    request.state.audit_details = {
        "name": payload.name,
        "category": payload.category,
        "message_type": payload.message_type,
        "enabled": payload.enabled,
    }
    return payload


@router.get("/message-templates/{template_id}", response_model=MessageTemplate)
def get_message_template(template_id: str):
    config = _read("message_templates", MessageTemplatesConfig)
    item = find_by_id(config.templates, template_id)
    if not item:
        raise HTTPException(status_code=404, detail="Template not found")
    return item


@router.put("/message-templates/{template_id}", response_model=MessageTemplate, dependencies=[Depends(require_line_manager)])
def update_message_template(
    template_id: str,
    payload: MessageTemplate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    if payload.id != template_id:
        raise HTTPException(status_code=400, detail="Path id and payload id must match")
    with MESSAGE_TEMPLATE_LOCK:
        _require_message_template_revision(if_match)
        config = _read("message_templates", MessageTemplatesConfig)
        if not find_by_id(config.templates, template_id):
            raise HTTPException(status_code=404, detail="Template not found")
        if not payload.enabled:
            references = _template_schedule_references(template_id)
            if references:
                labels = ", ".join(
                    f"{item['schedule_name']} D+{item['day']}" for item in references
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"此範本仍被啟用中的排程引用，無法停用：{labels}",
                )
        config.templates = upsert_by_id(config.templates, payload)
        _save("message_templates", MessageTemplatesConfig.model_validate(config))
    request.state.audit_action = "line.message_template.update"
    request.state.audit_resource_type = "line_message_template"
    request.state.audit_resource_id = template_id
    request.state.audit_details = {
        "name": payload.name,
        "category": payload.category,
        "message_type": payload.message_type,
        "enabled": payload.enabled,
    }
    return payload


@router.delete("/message-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_line_manager)])
def delete_message_template(
    template_id: str,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with MESSAGE_TEMPLATE_LOCK:
        _require_message_template_revision(if_match)
        config = _read("message_templates", MessageTemplatesConfig)
        original_count = len(config.templates)
        config.templates = [item for item in config.templates if item.id != template_id]
        if len(config.templates) == original_count:
            raise HTTPException(status_code=404, detail="Template not found")
        references = _template_schedule_references(template_id)
        if references:
            labels = ", ".join(
                f"{item['schedule_name']} D+{item['day']}" for item in references
            )
            raise HTTPException(
                status_code=409,
                detail=f"此範本仍被啟用中的排程引用，無法刪除：{labels}",
            )
        _save("message_templates", MessageTemplatesConfig.model_validate(config))
    request.state.audit_action = "line.message_template.delete"
    request.state.audit_resource_type = "line_message_template"
    request.state.audit_resource_id = template_id
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/message-templates/{template_id}/preview", dependencies=[Depends(require_line_manager)])
def preview_message_template(template_id: str, payload: MessageTemplatePreviewRequest):
    item = get_message_template(template_id)
    return _render_message_template(item, payload.variables)


# ---------------------------------------------------------------------------
# Scheduled messages
# ---------------------------------------------------------------------------
@router.get("/message-schedules", response_model=MessageSchedulesConfig)
def get_message_schedules():
    return _read("message_schedules", MessageSchedulesConfig)


@router.get("/message-schedules/state")
def get_message_schedules_state():
    return {
        "revision": config_revision("message_schedules"),
        "config": _read("message_schedules", MessageSchedulesConfig),
    }


@router.put("/message-schedules", response_model=MessageSchedulesConfig, dependencies=[Depends(require_line_manager)])
def replace_message_schedules(
    payload: MessageSchedulesConfig,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with MESSAGE_SCHEDULE_LOCK:
        _require_message_schedule_revision(if_match)
        templates = _read("message_templates", MessageTemplatesConfig)
        enabled_template_ids = {item.id for item in templates.templates if item.enabled}
        missing = sorted({step.template_id for item in payload.schedules for step in item.steps} - enabled_template_ids)
        if missing:
            raise HTTPException(status_code=422, detail=f"Unknown or disabled templates: {', '.join(missing)}")
        _save("message_schedules", payload)
    request.state.audit_action = "line.message_schedules.replace"
    request.state.audit_resource_type = "line_message_schedules"
    request.state.audit_details = {
        "timezone": payload.timezone,
        "schedule_count": len(payload.schedules),
        "step_count": sum(len(item.steps) for item in payload.schedules),
    }
    return payload


# ---------------------------------------------------------------------------
# Rich menus
# ---------------------------------------------------------------------------
@router.get("/line-menus", response_model=LineMenusConfig)
def get_line_menus():
    return _read("line_menus", LineMenusConfig)


@router.get("/line-menus/state")
def get_line_menus_state():
    return {
        "revision": config_revision("line_menus"),
        "config": _read("line_menus", LineMenusConfig),
    }


@router.put("/line-menus", response_model=LineMenusConfig, dependencies=[Depends(require_line_manager)])
def replace_line_menus(
    payload: LineMenusConfig,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with LINE_MENU_LOCK:
        _require_line_menu_revision(if_match)
        _save("line_menus", payload)
    request.state.audit_action = "line.rich_menus.replace"
    request.state.audit_resource_type = "line_rich_menu_config"
    request.state.audit_details = {"menu_count": len(payload.menus)}
    return payload


@router.post("/line-menus", response_model=RichMenuDefinition, status_code=201, dependencies=[Depends(require_line_manager)])
def create_line_menu(
    payload: RichMenuDefinition,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with LINE_MENU_LOCK:
        _require_line_menu_revision(if_match)
        config = _read("line_menus", LineMenusConfig)
        if find_by_id(config.menus, payload.id):
            raise HTTPException(status_code=409, detail="Menu id already exists")
        config.menus.append(payload)
        _save("line_menus", LineMenusConfig.model_validate(config))
    request.state.audit_action = "line.rich_menu.create"
    request.state.audit_resource_type = "line_rich_menu_config"
    request.state.audit_resource_id = payload.id
    return payload


@router.get("/line-menus/{menu_id}", response_model=RichMenuDefinition)
def get_line_menu(menu_id: str):
    config = _read("line_menus", LineMenusConfig)
    item = find_by_id(config.menus, menu_id)
    if not item:
        raise HTTPException(status_code=404, detail="Menu not found")
    return item


@router.put("/line-menus/{menu_id}", response_model=RichMenuDefinition, dependencies=[Depends(require_line_manager)])
def update_line_menu(
    menu_id: str,
    payload: RichMenuDefinition,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    if payload.id != menu_id:
        raise HTTPException(status_code=400, detail="Path id and payload id must match")
    with LINE_MENU_LOCK:
        _require_line_menu_revision(if_match)
        config = _read("line_menus", LineMenusConfig)
        if not find_by_id(config.menus, menu_id):
            raise HTTPException(status_code=404, detail="Menu not found")
        config.menus = upsert_by_id(config.menus, payload)
        _save("line_menus", LineMenusConfig.model_validate(config))
    request.state.audit_action = "line.rich_menu.update"
    request.state.audit_resource_type = "line_rich_menu_config"
    request.state.audit_resource_id = menu_id
    return payload


@router.delete("/line-menus/{menu_id}", status_code=204, dependencies=[Depends(require_line_manager)])
def delete_line_menu(
    menu_id: str,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with LINE_MENU_LOCK:
        _require_line_menu_revision(if_match)
        config = _read("line_menus", LineMenusConfig)
        item = find_by_id(config.menus, menu_id)
        if not item:
            raise HTTPException(status_code=404, detail="Menu not found")
        if item.set_as_default:
            raise HTTPException(status_code=409, detail="Default menu cannot be deleted")
        config.menus = [menu for menu in config.menus if menu.id != menu_id]
        _save("line_menus", LineMenusConfig.model_validate(config))
    request.state.audit_action = "line.rich_menu.delete"
    request.state.audit_resource_type = "line_rich_menu_config"
    request.state.audit_resource_id = menu_id
    return Response(status_code=204)


@router.post("/line-menus/{menu_id}/preview", dependencies=[Depends(require_line_manager)])
def preview_line_menu(menu_id: str):
    return {"status": "valid", "menu": get_line_menu(menu_id)}


@router.post("/line-menus/{menu_id}/publish", status_code=202, dependencies=[Depends(require_line_manager)])
def publish_line_menu(menu_id: str, request: Request):
    principal = request.state.admin_principal
    try:
        publication = create_publication_job(menu_id, principal.id)
    except RichMenuPublicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RichMenuPublicationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    request.state.audit_action = "line.rich_menu.publish"
    request.state.audit_resource_type = "line_rich_menu_publication"
    request.state.audit_resource_id = str(publication["id"])
    wake_worker()
    return {"status": "accepted", "menu_id": menu_id, "publication_id": publication["id"]}


# ---------------------------------------------------------------------------
# LIFF settings and dynamic fields
# ---------------------------------------------------------------------------
@public_router.get("/liff", response_model=LiffSettingsConfig)
def get_liff_config(response: Response):
    revision = config_revision("liff")
    response.headers["ETag"] = f'"{revision}"'
    response.headers["Cache-Control"] = "no-cache"
    return _read("liff", LiffSettingsConfig)


@public_router.get("/liff/runtime")
def get_liff_runtime(response: Response, page: str | None = None):
    config = _read("liff", LiffSettingsConfig)
    revision = config_revision("liff")
    response.headers["ETag"] = f'"{revision}"'
    response.headers["Cache-Control"] = "no-cache"
    if page:
        selected = config.pages.get(page)
        if not selected or not selected.enabled:
            raise HTTPException(status_code=404, detail="LIFF page not found or disabled")
        runtime_page = selected.model_copy(deep=True)
        runtime_page.fields = sorted(
            [field for field in runtime_page.fields if field.enabled],
            key=lambda field: field.order,
        )
        runtime_page.actions = sorted(
            [action for action in runtime_page.actions if action.enabled],
            key=lambda action: action.order,
        )
        return {
            "revision": revision,
            "theme": config.theme,
            "page_id": page,
            "page": runtime_page,
        }
    return {"revision": revision, "config": config}


@router.get("/liff/state")
def get_liff_state():
    return {
        "revision": config_revision("liff"),
        "config": _read("liff", LiffSettingsConfig),
    }


@router.post("/liff/validate")
def validate_liff_config(payload: LiffSettingsConfig):
    return {"status": "valid", "config": payload}


@router.get("/liff/history")
def get_liff_history():
    return {"items": list_liff_history()}


class LiffRollbackRequest(BaseModel):
    reason: str = ""


@router.post("/liff/rollback/{revision}", dependencies=[Depends(require_line_manager)])
def rollback_liff_config(
    revision: str,
    payload: LiffRollbackRequest,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with LIFF_CONFIG_LOCK:
        _require_liff_revision(if_match)
        snapshot = get_liff_snapshot(revision)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="找不到指定的 LIFF 設定版本")
        restored = LiffSettingsConfig.model_validate(upgrade_liff_snapshot(snapshot))
        _save_liff(
            restored,
            request,
            reason=payload.reason.strip() or f"rollback to {revision[:12]}",
        )
    request.state.audit_action = "line.liff.rollback"
    request.state.audit_resource_type = "line_liff_config"
    request.state.audit_resource_id = revision
    request.state.audit_details = {"reason": payload.reason.strip()}
    return {"revision": config_revision("liff"), "config": restored}


@router.put("/liff", response_model=LiffSettingsConfig, dependencies=[Depends(require_line_manager)])
def replace_liff_config(
    payload: LiffSettingsConfig,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with LIFF_CONFIG_LOCK:
        _require_liff_revision(if_match)
        _save_liff(payload, request, reason="replace configuration")
    request.state.audit_action = "line.liff.replace"
    request.state.audit_resource_type = "line_liff_config"
    request.state.audit_details = {
        "version": payload.version,
        "pages": sorted(payload.pages),
    }
    return payload


@router.put("/liff/theme", response_model=LiffTheme, dependencies=[Depends(require_line_manager)])
def update_liff_theme(
    payload: LiffTheme,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with LIFF_CONFIG_LOCK:
        _require_liff_revision(if_match)
        config = _read("liff", LiffSettingsConfig)
        config.theme = payload
        _save_liff(config, request, reason="update theme")
    request.state.audit_action = "line.liff.theme.update"
    request.state.audit_resource_type = "line_liff_config"
    return payload


@router.put("/liff/pages/{page_id}", response_model=LiffPage, dependencies=[Depends(require_line_manager)])
def update_liff_page(
    page_id: str,
    payload: LiffPage,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with LIFF_CONFIG_LOCK:
        _require_liff_revision(if_match)
        config = _read("liff", LiffSettingsConfig)
        if page_id not in config.pages:
            raise HTTPException(status_code=404, detail="LIFF page not found")
        config.pages[page_id] = payload
        validated = LiffSettingsConfig.model_validate(config)
        _save_liff(validated, request, reason=f"update page {page_id}")
    request.state.audit_action = "line.liff.page.update"
    request.state.audit_resource_type = "line_liff_page"
    request.state.audit_resource_id = page_id
    return payload


@router.post("/liff/pages/{page_id}/fields", response_model=LiffField, status_code=201, dependencies=[Depends(require_line_manager)])
def create_liff_field(
    page_id: str,
    payload: LiffField,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    if payload.system_field:
        raise HTTPException(status_code=400, detail="Cannot create a new system field")
    with LIFF_CONFIG_LOCK:
        _require_liff_revision(if_match)
        config = _read("liff", LiffSettingsConfig)
        page = config.pages.get(page_id)
        if not page:
            raise HTTPException(status_code=404, detail="LIFF page not found")
        if find_by_id(page.fields, payload.id):
            raise HTTPException(status_code=409, detail="Field id already exists")
        page.fields.append(payload)
        page.fields.sort(key=lambda field: field.order)
        validated = LiffSettingsConfig.model_validate(config)
        _save_liff(validated, request, reason=f"create field {page_id}.{payload.id}")
    request.state.audit_action = "line.liff.field.create"
    request.state.audit_resource_type = "line_liff_field"
    request.state.audit_resource_id = f"{page_id}.{payload.id}"
    return payload


@router.put("/liff/pages/{page_id}/fields/{field_id}", response_model=LiffField, dependencies=[Depends(require_line_manager)])
def update_liff_field(
    page_id: str,
    field_id: str,
    payload: LiffField,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    if payload.id != field_id:
        raise HTTPException(status_code=400, detail="Path id and payload id must match")
    with LIFF_CONFIG_LOCK:
        _require_liff_revision(if_match)
        config = _read("liff", LiffSettingsConfig)
        page = config.pages.get(page_id)
        existing = find_by_id(page.fields, field_id) if page else None
        if not existing:
            raise HTTPException(status_code=404, detail="LIFF field not found")
        if existing.system_field and (
            not payload.system_field
            or payload.type != existing.type
            or not payload.enabled
            or not payload.required
        ):
            raise HTTPException(status_code=409, detail="System field contract cannot be changed")
        page.fields = upsert_by_id(page.fields, payload)
        page.fields.sort(key=lambda field: field.order)
        validated = LiffSettingsConfig.model_validate(config)
        _save_liff(validated, request, reason=f"update field {page_id}.{field_id}")
    request.state.audit_action = "line.liff.field.update"
    request.state.audit_resource_type = "line_liff_field"
    request.state.audit_resource_id = f"{page_id}.{field_id}"
    return payload


@router.delete("/liff/pages/{page_id}/fields/{field_id}", status_code=204, dependencies=[Depends(require_line_manager)])
def delete_liff_field(
    page_id: str,
    field_id: str,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    with LIFF_CONFIG_LOCK:
        _require_liff_revision(if_match)
        config = _read("liff", LiffSettingsConfig)
        page = config.pages.get(page_id)
        field = find_by_id(page.fields, field_id) if page else None
        if not field:
            raise HTTPException(status_code=404, detail="LIFF field not found")
        if field.system_field:
            raise HTTPException(status_code=409, detail="System field cannot be deleted")
        page.fields = [item for item in page.fields if item.id != field_id]
        validated = LiffSettingsConfig.model_validate(config)
        _save_liff(validated, request, reason=f"delete field {page_id}.{field_id}")
    request.state.audit_action = "line.liff.field.delete"
    request.state.audit_resource_type = "line_liff_field"
    request.state.audit_resource_id = f"{page_id}.{field_id}"
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Customer service static settings
# ---------------------------------------------------------------------------
@router.get("/customer-service", response_model=CustomerServiceConfig)
def get_customer_service_config():
    return _read("customer_service", CustomerServiceConfig)


@router.put("/customer-service", response_model=CustomerServiceConfig, dependencies=[Depends(require_line_manager)])
def update_customer_service_config(payload: CustomerServiceConfig):
    _save("customer_service", payload)
    return payload
