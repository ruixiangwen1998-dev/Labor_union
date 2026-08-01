"""Administrative API for caregiver availability-lock lifecycle actions."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.dependencies.admin_auth import require_system_admin
from api.schemas.base import BaseResponse
from services.admin_auth_service import AdminPrincipal
from services.caregiver_availability_lock_conversion_service import (
    convert_availability_lock_to_assignments,
)
from services.caregiver_availability_lock_release_service import (
    release_caregiver_availability_lock,
)
from services.caregiver_availability_lock_service import (
    acquire_caregiver_availability_lock,
)


router = APIRouter(
    prefix="/api/v1/orders",
    tags=["Caregiver availability locks"],
)


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AcquireAvailabilityLockRequest(_StrictRequest):
    event_key: str = Field(..., min_length=1)
    actor: str = Field(..., min_length=1)


class ReleaseAvailabilityLockRequest(AcquireAvailabilityLockRequest):
    reason: str = Field(..., min_length=1)


class AssignmentTerm(_StrictRequest):
    segment_id: int = Field(..., strict=True, gt=0)
    hourly_rate: Decimal = Field(..., ge=0)
    floor_fee_allocated: Decimal = Field(..., ge=0)

    @field_validator("hourly_rate", "floor_fee_allocated", mode="before")
    @classmethod
    def _reject_float_money(cls, value: Any) -> Any:
        if isinstance(value, (bool, float)):
            raise ValueError("money values must not be bool or float")
        return value


class ConvertAvailabilityLockRequest(ReleaseAvailabilityLockRequest):
    assignment_terms: list[AssignmentTerm] = Field(..., min_length=1, max_length=4)


def _service_response(result: dict[str, Any], message: str) -> BaseResponse[dict[str, Any]]:
    return BaseResponse(data=result, message=message)


def _service_error(exc: Exception, operation: str) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(
        status_code=500,
        detail=f"Unexpected error during availability lock {operation}",
    )


def _require_actor(principal: AdminPrincipal, actor: str) -> None:
    if str(principal.username or "").strip() != actor.strip():
        raise HTTPException(
            status_code=403,
            detail="actor does not match authenticated principal",
        )


@router.post(
    "/{case_no}/matching-plans/{plan_id}/availability-lock/acquire",
    response_model=BaseResponse[dict[str, Any]],
)
def acquire_availability_lock(
    request: AcquireAvailabilityLockRequest,
    case_no: str = Path(..., min_length=1),
    plan_id: int = Path(..., strict=True, gt=0),
    principal: AdminPrincipal = Depends(require_system_admin),
) -> BaseResponse[dict[str, Any]]:
    _require_actor(principal, request.actor)
    try:
        result = acquire_caregiver_availability_lock(
            case_no=case_no,
            plan_id=plan_id,
            event_key=request.event_key,
            actor=request.actor,
        )
        return _service_response(result, "Caregiver availability lock acquired")
    except Exception as exc:
        raise _service_error(exc, "acquisition") from exc


@router.post(
    "/{case_no}/matching-plans/{plan_id}/availability-locks/{lock_id}/release",
    response_model=BaseResponse[dict[str, Any]],
)
def release_availability_lock(
    request: ReleaseAvailabilityLockRequest,
    case_no: str = Path(..., min_length=1),
    plan_id: int = Path(..., strict=True, gt=0),
    lock_id: int = Path(..., strict=True, gt=0),
    principal: AdminPrincipal = Depends(require_system_admin),
) -> BaseResponse[dict[str, Any]]:
    _require_actor(principal, request.actor)
    try:
        result = release_caregiver_availability_lock(
            case_no=case_no,
            plan_id=plan_id,
            lock_id=lock_id,
            event_key=request.event_key,
            actor=request.actor,
            reason=request.reason,
        )
        return _service_response(result, "Caregiver availability lock released")
    except Exception as exc:
        raise _service_error(exc, "release") from exc


@router.post(
    "/{case_no}/availability-locks/{lock_id}/convert",
    response_model=BaseResponse[dict[str, Any]],
)
def convert_availability_lock(
    request: ConvertAvailabilityLockRequest,
    case_no: str = Path(..., min_length=1),
    lock_id: int = Path(..., strict=True, gt=0),
    principal: AdminPrincipal = Depends(require_system_admin),
) -> BaseResponse[dict[str, Any]]:
    _require_actor(principal, request.actor)
    try:
        result = convert_availability_lock_to_assignments(
            case_no=case_no,
            lock_id=lock_id,
            event_key=request.event_key,
            actor=request.actor,
            reason=request.reason,
            assignment_terms=[
                term.model_dump(mode="python") for term in request.assignment_terms
            ],
        )
        return _service_response(result, "Caregiver availability lock converted")
    except Exception as exc:
        raise _service_error(exc, "conversion") from exc
