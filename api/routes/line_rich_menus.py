"""
================================================================================
檔案名稱: api/routes/line_rich_menus.py
功能說明: LINE 下方選單 API，提供圖片、預覽、單頁／雙頁群組發布、紀錄與失敗重試
================================================================================
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response

from api.dependencies.admin_auth import require_line_manager, require_line_viewer
from api.schemas.base import BaseResponse
from api.schemas.line_config import LineMenusConfig, RichMenuDefinition
from api.schemas.line_rich_menus import (
    RichMenuPublicationRetryRequest,
    RichMenuPublishRequest,
)
from line.worker import wake_worker
from services.json_config_service import read_config
from services.line_rich_menu_service import (
    RichMenuPublicationConflictError,
    RichMenuPublicationNotFoundError,
    create_publication_group_jobs,
    create_publication_job,
    get_publication,
    list_publications,
    retry_publication,
)
from services.media_storage_service import (
    MAX_UPLOAD_BYTES,
    MediaAssetNotFoundError,
    MediaValidationError,
    delete_media_asset,
    read_media_asset,
    render_rich_menu_image,
    store_uploaded_rich_menu_image,
)


router = APIRouter(
    prefix="/api/v1/line/rich-menus",
    tags=["LINE Rich Menu"],
    dependencies=[Depends(require_line_viewer)],
)


def _publication_error(exc: Exception) -> None:
    if isinstance(exc, (RichMenuPublicationNotFoundError, MediaAssetNotFoundError)):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, RichMenuPublicationConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, (MediaValidationError, ValueError)):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


def _menu(menu_id: str) -> RichMenuDefinition:
    config = read_config("line_menus", LineMenusConfig)
    menu = next((item for item in config.menus if item.id == menu_id), None)
    if not menu:
        raise HTTPException(status_code=404, detail="找不到 Rich Menu")
    return menu


@router.post("/preview", response_class=Response)
def preview_rich_menu(payload: RichMenuDefinition):
    try:
        if payload.appearance.image_mode == "uploaded" and payload.appearance.image_asset_id:
            _, image = read_media_asset(payload.appearance.image_asset_id)
        else:
            image = render_rich_menu_image(payload.model_dump(mode="json"))
    except (MediaValidationError, MediaAssetNotFoundError) as exc:
        _publication_error(exc)
    return Response(content=image, media_type="image/jpeg")


@router.post(
    "/{menu_id}/images",
    response_model=BaseResponse[dict],
    dependencies=[Depends(require_line_manager)],
)
async def upload_rich_menu_image(
    menu_id: str,
    request: Request,
    image: UploadFile = File(...),
):
    menu = _menu(menu_id)
    content = await image.read(MAX_UPLOAD_BYTES + 1)
    try:
        asset = store_uploaded_rich_menu_image(
            content,
            menu_id=menu_id,
            original_filename=image.filename or "rich-menu-image",
            expected_width=menu.size.width,
            expected_height=menu.size.height,
            created_by_admin_user_id=request.state.admin_principal.id,
        )
    except (MediaValidationError, MediaAssetNotFoundError) as exc:
        _publication_error(exc)
    request.state.audit_action = "line.rich_menu.image.upload"
    request.state.audit_resource_type = "media_asset"
    request.state.audit_resource_id = str(asset["id"])
    return BaseResponse(
        data={
            key: asset.get(key)
            for key in (
                "id",
                "original_filename",
                "mime_type",
                "file_size",
                "sha256",
                "width",
                "height",
                "created_at",
            )
        },
        message="Rich Menu 圖片已安全保存",
    )


@router.delete(
    "/images/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_line_manager)],
)
def remove_rich_menu_image(asset_id: int, request: Request):
    config = read_config("line_menus", LineMenusConfig)
    if any(menu.appearance.image_asset_id == asset_id for menu in config.menus):
        raise HTTPException(status_code=409, detail="此圖片仍被 Rich Menu 草稿引用")
    try:
        delete_media_asset(asset_id)
    except (MediaAssetNotFoundError, MediaValidationError) as exc:
        _publication_error(exc)
    request.state.audit_action = "line.rich_menu.image.delete"
    request.state.audit_resource_type = "media_asset"
    request.state.audit_resource_id = str(asset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/publications", response_model=BaseResponse[dict])
def publication_list(
    menu_id: str | None = None,
    publication_status: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    try:
        result = list_publications(
            menu_id=menu_id,
            status=publication_status,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        _publication_error(exc)
    return BaseResponse(data=result)


@router.get("/publications/{publication_id}", response_model=BaseResponse[dict])
def publication_detail(publication_id: int):
    try:
        result = get_publication(publication_id)
    except RichMenuPublicationNotFoundError as exc:
        _publication_error(exc)
    return BaseResponse(data=result)


@router.post(
    "/publications/{publication_id}/retry",
    response_model=BaseResponse[dict],
    dependencies=[Depends(require_line_manager)],
)
def publication_retry(
    publication_id: int,
    payload: RichMenuPublicationRetryRequest,
    request: Request,
):
    try:
        result = retry_publication(publication_id)
    except (RichMenuPublicationNotFoundError, RichMenuPublicationConflictError) as exc:
        _publication_error(exc)
    request.state.audit_action = "line.rich_menu.publication.retry"
    request.state.audit_resource_type = "line_rich_menu_publication"
    request.state.audit_resource_id = str(publication_id)
    request.state.audit_details = {"reason": payload.reason.strip()} if payload.reason.strip() else None
    wake_worker()
    return BaseResponse(data=result, message="發布工作已重新排入")


@router.post(
    "/groups/{group_id}/publish",
    response_model=BaseResponse[list[dict]],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_line_manager)],
)
def publish_rich_menu_group(
    group_id: str,
    payload: RichMenuPublishRequest,
    request: Request,
):
    try:
        result = create_publication_group_jobs(
            group_id,
            request.state.admin_principal.id,
        )
    except (
        RichMenuPublicationNotFoundError,
        RichMenuPublicationConflictError,
        MediaAssetNotFoundError,
    ) as exc:
        _publication_error(exc)
    request.state.audit_action = "line.rich_menu.group.publish"
    request.state.audit_resource_type = "line_rich_menu_group"
    request.state.audit_resource_id = group_id
    request.state.audit_details = {
        "reason": payload.reason.strip(),
        "publication_ids": [item["id"] for item in result],
    }
    wake_worker()
    return BaseResponse(data=result, message="雙頁 Rich Menu 已排入安全發布流程")


@router.post(
    "/{menu_id}/publish",
    response_model=BaseResponse[dict],
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_line_manager)],
)
def publish_rich_menu(
    menu_id: str,
    payload: RichMenuPublishRequest,
    request: Request,
):
    try:
        result = create_publication_job(menu_id, request.state.admin_principal.id)
    except (
        RichMenuPublicationNotFoundError,
        RichMenuPublicationConflictError,
        MediaAssetNotFoundError,
    ) as exc:
        _publication_error(exc)
    request.state.audit_action = "line.rich_menu.publish"
    request.state.audit_resource_type = "line_rich_menu_publication"
    request.state.audit_resource_id = str(result["id"])
    request.state.audit_details = {"reason": payload.reason.strip()} if payload.reason.strip() else None
    wake_worker()
    return BaseResponse(data=result, message="Rich Menu 發布工作已建立")
