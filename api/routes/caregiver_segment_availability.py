"""Caregiver segment availability search router."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.dependencies.admin_auth import require_system_admin
from api.schemas.base import BaseResponse
from services.admin_auth_service import AdminPrincipal
from services.caregiver_segment_availability_query_service import (
    search_segmented_caregiver_availability,
)


router = APIRouter(
    prefix="/api/v1/orders",
    tags=["Multi-caregiver schedules"],
)

_STRICT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _as_iso_date(value: Any, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    if not isinstance(value, str) or not _STRICT_DATE_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    return date.fromisoformat(value)


class CaregiverSegmentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    staff_id: int | None = Field(default=None, strict=True, gt=0)
    start_date: date | None = Field(default=None)
    end_date: date | None = Field(default=None)

    @field_validator("staff_id", mode="before")
    @classmethod
    def _validate_staff_id(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("staff_id must be a positive integer")
        return value

    @field_validator("start_date", mode="before")
    @classmethod
    def _validate_start_date(cls, value: date | str | None) -> date | None:
        if value is None:
            return value
        return _as_iso_date(value, "start_date")

    @field_validator("end_date", mode="before")
    @classmethod
    def _validate_end_date(cls, value: date | str | None) -> date | None:
        if value is None:
            return value
        return _as_iso_date(value, "end_date")


class CaregiverSegmentAvailabilitySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_count: Literal[2, 3, 4] = Field(...)
    segment_drafts: list[CaregiverSegmentDraft] = Field(...)
    as_of: date = Field(...)

    @field_validator("as_of", mode="before")
    @classmethod
    def _validate_as_of(cls, value: date | str) -> date:
        if isinstance(value, bool):
            raise ValueError("as_of must be YYYY-MM-DD")
        return _as_iso_date(value, "as_of")


class SingleCaregiverEligibilityRequest(BaseModel):
    """Internal product gate used before showing the 2–4 segment fallback."""

    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: date
    as_of: date

    @field_validator("start_date", mode="before")
    @classmethod
    def _validate_start_date(cls, value: date | str) -> date:
        return _as_iso_date(value, "start_date")

    @field_validator("end_date", mode="before")
    @classmethod
    def _validate_end_date(cls, value: date | str) -> date:
        return _as_iso_date(value, "end_date")

    @field_validator("as_of", mode="before")
    @classmethod
    def _validate_gate_as_of(cls, value: date | str) -> date:
        return _as_iso_date(value, "as_of")


class CaregiverSegmentCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_index: int
    staff_id: int
    start_date: date
    end_date: date


class CaregiverConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_index: int
    staff_id: int | None
    work_date: date
    reason_code: str


class CaregiverSegmentAvailabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_no: str
    planned_start_date: date
    planned_end_date: date
    feasibility: Literal["complete", "partial"]
    complete_combinations: list[list[CaregiverSegmentCandidate]]
    segment_candidates: list[CaregiverSegmentCandidate]
    conflicts: list[CaregiverConflict]


def _value_to_status(message: str) -> int:
    if message == "case not found":
        return 404
    if message == "case is not in negotiation stage":
        return 409
    return 422


def _response_from_service(
    service_result: dict[str, Any],
    message: str,
) -> BaseResponse[CaregiverSegmentAvailabilityResponse]:
    return BaseResponse(
        data=CaregiverSegmentAvailabilityResponse(**service_result),
        message=message,
    )


@router.post(
    "/{case_no}/caregiver-single-eligibility/check",
    response_model=BaseResponse[CaregiverSegmentAvailabilityResponse],
)
def check_single_caregiver_eligibility(
    request: SingleCaregiverEligibilityRequest,
    case_no: str = Path(..., min_length=1),
    principal: AdminPrincipal = Depends(require_system_admin),
) -> BaseResponse[CaregiverSegmentAvailabilityResponse]:
    """Check whether one caregiver covers the whole period before multi-segment UI."""
    del principal
    if request.start_date > request.end_date:
        raise HTTPException(status_code=422, detail="start_date cannot be after end_date")
    try:
        result = search_segmented_caregiver_availability(
            case_no=case_no,
            segment_count=1,
            segment_drafts=[
                {
                    "start_date": request.start_date.isoformat(),
                    "end_date": request.end_date.isoformat(),
                }
            ],
            as_of=request.as_of,
        )
        return _response_from_service(
            result,
            "Single-caregiver eligibility check completed",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=_value_to_status(str(exc)),
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected error during single-caregiver eligibility check",
        ) from exc


@router.post(
    "/{case_no}/caregiver-segment-availability/search",
    response_model=BaseResponse[CaregiverSegmentAvailabilityResponse],
)
def search_caregiver_segment_availability(
    request: CaregiverSegmentAvailabilitySearchRequest,
    case_no: str = Path(..., min_length=1),
    principal: AdminPrincipal = Depends(require_system_admin),
) -> BaseResponse[CaregiverSegmentAvailabilityResponse]:
    """Return availability candidates for multi-caregiver segmented search."""
    del principal
    try:
        service_result = search_segmented_caregiver_availability(
            case_no=case_no,
            segment_count=request.segment_count,
            segment_drafts=[draft.model_dump(mode="json", exclude_none=True) for draft in request.segment_drafts],
            as_of=request.as_of,
        )
        return _response_from_service(
            service_result,
            "Caregiver segment availability search completed",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=_value_to_status(str(exc)),
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Unexpected error during caregiver segment availability search",
        ) from exc
