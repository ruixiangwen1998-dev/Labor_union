"""HTTP projection for assignment leave-resolution domain failures."""

from __future__ import annotations

from fastapi import Request, status
from fastapi.responses import JSONResponse

from services.assignment_schedule_rest_date_service import (
    AssignmentLeaveResolutionDomainError,
)


_STATUS_BY_CATEGORY = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "validation_error": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "conflict": status.HTTP_409_CONFLICT,
    "locked": status.HTTP_409_CONFLICT,
}


async def assignment_leave_resolution_exception_handler(
    _request: Request,
    exc: AssignmentLeaveResolutionDomainError,
) -> JSONResponse:
    """Translate one typed domain failure without route-local reclassification."""
    payload = exc.as_dict()
    status_code = _STATUS_BY_CATEGORY.get(
        payload.get("category"),
        status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "message": str(payload.get("message") or "休假調整失敗"),
            "detail": payload,
        },
    )
