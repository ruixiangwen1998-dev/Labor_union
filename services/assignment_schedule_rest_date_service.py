"""
================================================================================
檔案名稱: services/assignment_schedule_rest_date_service.py
功能說明: 以 assignment_id 為唯一權屬進行月嫂排休與順延完工日保存服務 (AssignmentScheduleRestDateService)
================================================================================
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import math
import json
from types import MappingProxyType
import re
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_CEILING, InvalidOperation
from typing import Any, Dict, List, Iterable, Mapping

from services.db_service import calculate_attendance_schedule, get_connection
from services.multi_caregiver_assignment_rules import (
    validate_non_overlapping_assignment_interval,
)
from services.multi_caregiver_schedule_read import (
    AssignmentScheduleConflictSnapshotDomainError,
    get_assignment_schedule,
    get_case_schedule_conflict_snapshot,
    get_case_schedule_conflict_snapshot_with_cursor,
)
from services.staff_occupancy_mutex_service import lock_staff_occupancy_mutex


def _validation_error_code(field_name: str) -> str:
    if not isinstance(field_name, str) or not field_name.strip():
        return "invalid_value"
    normalised = re.sub(r"[^a-z0-9]+", "_", field_name.lower().strip())
    return f"invalid_{normalised.strip('_')}"


class AssignmentScheduleRestDateValidationError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]+", code):
            raise ValueError("invalid validation error code")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("invalid validation error message")
        if details is not None:
            if not isinstance(details, Mapping):
                raise ValueError("invalid validation error details")
            _assert_domain_error_value(details)
            frozen_details = MappingProxyType(
                {
                    str(key): _snapshot_domain_error_value(value)
                    for key, value in dict(details).items()
                }
            )
        else:
            frozen_details = None
        self.code = code
        self.message = message
        self.details = frozen_details
        super().__init__(message)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": None
            if self.details is None
            else _snapshot_domain_error_value(self.details),
        }


def _as_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssignmentScheduleRestDateValidationError(
            code=_validation_error_code(field_name),
            message=f"{field_name} must be a positive integer",
            details={"field": field_name},
        )
    if value <= 0:
        raise AssignmentScheduleRestDateValidationError(
            code=_validation_error_code(field_name),
            message=f"{field_name} must be a positive integer",
            details={"field": field_name},
        )
    return value


def _as_date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise AssignmentScheduleRestDateValidationError(
                code=_validation_error_code(field_name),
                message=f"{field_name} must be YYYY-MM-DD",
                details={"field": field_name},
            )
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except Exception as exc:
            raise AssignmentScheduleRestDateValidationError(
                code=_validation_error_code(field_name),
                message=f"{field_name} must be YYYY-MM-DD",
                details={"field": field_name},
            ) from exc
        if value != parsed.isoformat():
            raise AssignmentScheduleRestDateValidationError(
                code=_validation_error_code(field_name),
                message=f"{field_name} must be YYYY-MM-DD",
                details={"field": field_name},
            )
        return parsed
    raise AssignmentScheduleRestDateValidationError(
        code=_validation_error_code(field_name),
        message=f"{field_name} must be YYYY-MM-DD",
        details={"field": field_name},
    )


def _as_rest_date_string(value: Any, field_name: str) -> str:
    if value is None or not isinstance(value, str):
        raise AssignmentScheduleRestDateValidationError(
            code=_validation_error_code(field_name),
            message=f"{field_name} must be a YYYY-MM-DD string",
            details={"field": field_name},
        )
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise AssignmentScheduleRestDateValidationError(
            code=_validation_error_code(field_name),
            message=f"{field_name} must be a YYYY-MM-DD string",
            details={"field": field_name},
        )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except Exception as exc:
        raise AssignmentScheduleRestDateValidationError(
            code=_validation_error_code(field_name),
            message=f"{field_name} must be a YYYY-MM-DD string",
            details={"field": field_name},
        ) from exc
    if value != parsed.isoformat():
        raise AssignmentScheduleRestDateValidationError(
            code=_validation_error_code(field_name),
            message=f"{field_name} must be a YYYY-MM-DD string",
            details={"field": field_name},
        )
    return value


def canonicalize_assignment_leave_resolution_batch_apply_envelope(
    request: Mapping[str, Any],
) -> Dict[str, Any]:
    """Purely validate and canonicalize a batch-apply request envelope."""
    expected_request_keys = {
        "contract_version", "case_no", "original_assignment_id", "items",
        "preview_fingerprint", "batch_key", "actor", "reason",
    }

    def invalid(field: str) -> None:
        raise AssignmentLeaveResolutionApplicationError(
            code="invalid_batch_apply_envelope",
            reason="batch apply envelope is invalid",
            details={"field": field},
        )

    if not isinstance(request, Mapping) or set(request) != expected_request_keys:
        invalid("request")
    if request["contract_version"] != "assignment-leave-substitution-batch-apply/v1":
        invalid("contract_version")

    def canonical_text(field: str, maximum: int) -> str:
        value = request[field]
        if not isinstance(value, str) or not value.strip():
            invalid(field)
        normalized = value.strip()
        if len(normalized) > maximum:
            invalid(field)
        return normalized

    def positive_int(value: Any, field: str) -> int:
        try:
            return _as_positive_int(value, field)
        except AssignmentScheduleRestDateValidationError:
            invalid(field)
            raise AssertionError("unreachable")

    def exact_date(value: Any) -> str:
        try:
            return _as_rest_date_string(value, "work_date")
        except AssignmentScheduleRestDateValidationError:
            invalid("work_date")
            raise AssertionError("unreachable")

    case_no = canonical_text("case_no", 50)
    batch_key = canonical_text("batch_key", 100)
    actor = canonical_text("actor", 100)
    reason = canonical_text("reason", 255)
    original_assignment_id = positive_int(request["original_assignment_id"], "original_assignment_id")
    fingerprint = request["preview_fingerprint"]
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        invalid("preview_fingerprint")
    raw_items = request["items"]
    if not isinstance(raw_items, list) or not raw_items:
        invalid("items")
    required_item_keys = {
        "original_schedule_id", "work_date", "resolution_type", "substitute_staff_id"
    }
    items: list[dict[str, Any]] = []
    schedule_ids: set[int] = set()
    work_dates: set[str] = set()
    for raw_item in raw_items:
        if (
            not isinstance(raw_item, Mapping)
            or not required_item_keys.issubset(raw_item)
            or set(raw_item) - (required_item_keys | {"is_double_pay"})
        ):
            invalid("items")
        schedule_id = positive_int(raw_item["original_schedule_id"], "original_schedule_id")
        work_date = exact_date(raw_item["work_date"])
        if schedule_id in schedule_ids or work_date in work_dates:
            invalid("items")
        schedule_ids.add(schedule_id)
        work_dates.add(work_date)
        resolution = raw_item["resolution_type"]
        substitute = raw_item["substitute_staff_id"]
        if resolution == "substitute":
            substitute = positive_int(substitute, "substitute_staff_id")
        elif resolution == "defer_following_assignments":
            if substitute is not None:
                invalid("substitute_staff_id")
        else:
            invalid("resolution_type")
        is_double_pay = raw_item.get("is_double_pay", False)
        if type(is_double_pay) is not bool:
            invalid("is_double_pay")
        if resolution == "defer_following_assignments" and is_double_pay:
            invalid("is_double_pay")
        items.append(
            {
                "original_schedule_id": schedule_id,
                "work_date": work_date,
                "resolution_type": resolution,
                "substitute_staff_id": substitute,
                "is_double_pay": is_double_pay,
            }
        )
    items.sort(key=lambda item: (item["work_date"], item["original_schedule_id"]))
    preview_request = {
        "contract_version": "assignment-leave-substitution-batch-preview/v1",
        "case_no": case_no,
        "original_assignment_id": original_assignment_id,
        "items": deepcopy(items),
    }
    replay_identity_seed = {
        "batch_key": batch_key,
        "request_snapshot": deepcopy(preview_request),
        "preview_fingerprint": fingerprint,
    }
    return {
        "preview_request": preview_request,
        "requested_preview_fingerprint": fingerprint,
        "batch_key": batch_key,
        "actor": actor,
        "reason": reason,
        "replay_identity_seed": replay_identity_seed,
    }


def read_assignment_leave_resolution_batch_replay_snapshot(
    cursor: Any, batch_key: str, lock_rows: bool
) -> Dict[str, Any]:
    """Read one persisted batch identity without deciding replay semantics."""
    def application_error(field: str) -> None:
        raise AssignmentLeaveResolutionApplicationError(
            code="invalid_batch_replay_read_request",
            reason="batch replay read request is invalid",
            details={"field": field},
        )

    def data_integrity_error() -> None:
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_replay_snapshot",
            reason="batch replay snapshot is invalid",
            details={"source": "batch_replay_snapshot"},
        )

    def infrastructure_error(cause: BaseException) -> None:
        raise AssignmentLeaveResolutionInfrastructureError(
            code="batch_replay_snapshot_read_unavailable",
            reason="batch replay snapshot read is unavailable",
            details={"operation": "batch_replay_snapshot_read"},
            cause=cause,
        ) from cause

    if cursor is None or not all(callable(getattr(cursor, name, None)) for name in ("execute", "fetchone", "fetchall")):
        application_error("cursor")
    if not isinstance(batch_key, str) or not batch_key or batch_key != batch_key.strip():
        application_error("batch_key")
    if type(lock_rows) is not bool:
        application_error("lock_rows")

    def json_object(value: Any, field: str) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value, parse_float=Decimal)
            except (TypeError, ValueError, json.JSONDecodeError):
                data_integrity_error()
        if not isinstance(value, Mapping):
            data_integrity_error()

        def canonical(item: Any) -> Any:
            if item is None or isinstance(item, (bool, str, int)):
                return item
            if isinstance(item, float):
                if not math.isfinite(item):
                    data_integrity_error()
                item = Decimal(str(item))
            if isinstance(item, Decimal):
                if not item.is_finite():
                    data_integrity_error()
                if item == item.to_integral_value():
                    return int(item)
                rendered = format(item.normalize(), "f")
                if "." in rendered:
                    rendered = rendered.rstrip("0").rstrip(".")
                return rendered or "0"
            if isinstance(item, Mapping):
                if any(not isinstance(key, str) for key in item):
                    data_integrity_error()
                return {key: canonical(item[key]) for key in sorted(item)}
            if isinstance(item, list):
                return [canonical(value) for value in item]
            data_integrity_error()

        result = canonical(value)
        if not isinstance(result, dict):
            data_integrity_error()
        return result

    def canonical_request_date(value: Any) -> str:
        try:
            return _as_rest_date_string(value, "request_snapshot.work_date")
        except AssignmentScheduleRestDateValidationError:
            data_integrity_error()

    def canonical_child_date(value: Any) -> str:
        if isinstance(value, datetime):
            data_integrity_error()
        if isinstance(value, date):
            return value.isoformat()
        try:
            return _as_rest_date_string(value, "work_date")
        except AssignmentScheduleRestDateValidationError:
            data_integrity_error()

    def positive_request_id(value: Any) -> int:
        try:
            return _as_positive_int(value, "substitute_staff_id")
        except AssignmentScheduleRestDateValidationError:
            data_integrity_error()

    suffix = " FOR UPDATE" if lock_rows else ""
    try:
        cursor.execute("SELECT batch_key, case_no, preview_fingerprint, item_count, actor, reason, request_snapshot FROM assignment_schedule_leave_substitution_batches WHERE batch_key = %s" + suffix, (batch_key,))
        raw_header = cursor.fetchone()
    except Exception as exc:
        infrastructure_error(exc)
    if raw_header is None:
        return {"state": "absent", "header": None, "children": []}
    header_fields = {"batch_key", "case_no", "preview_fingerprint", "item_count", "actor", "reason", "request_snapshot"}
    if not isinstance(raw_header, Mapping) or set(raw_header) != header_fields:
        data_integrity_error()
    if raw_header["batch_key"] != batch_key:
        data_integrity_error()
    item_count = raw_header["item_count"]
    if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 1:
        data_integrity_error()
    for field in ("case_no", "actor", "reason"):
        if not isinstance(raw_header[field], str) or not raw_header[field].strip() or raw_header[field] != raw_header[field].strip():
            data_integrity_error()
    if not isinstance(raw_header["preview_fingerprint"], str) or re.fullmatch(r"[0-9a-f]{64}", raw_header["preview_fingerprint"]) is None:
        data_integrity_error()
    request_snapshot = json_object(raw_header["request_snapshot"], "request_snapshot")
    if set(request_snapshot) != {"contract_version", "case_no", "original_assignment_id", "items"} or request_snapshot["contract_version"] != "assignment-leave-substitution-batch-preview/v1" or request_snapshot["case_no"] != raw_header["case_no"] or isinstance(request_snapshot["original_assignment_id"], bool) or not isinstance(request_snapshot["original_assignment_id"], int) or request_snapshot["original_assignment_id"] <= 0 or not isinstance(request_snapshot["items"], list) or not request_snapshot["items"] or len(request_snapshot["items"]) != item_count:
        data_integrity_error()
    required_item_keys = {
        "original_schedule_id",
        "work_date",
        "resolution_type",
        "substitute_staff_id",
    }
    seen_dates, seen_schedules, item_order = set(), set(), []
    for item in request_snapshot["items"]:
        if (
            not isinstance(item, Mapping)
            or not required_item_keys.issubset(item)
            or set(item) - (required_item_keys | {"is_double_pay"})
        ):
            data_integrity_error()
        schedule_id = item["original_schedule_id"]
        if isinstance(schedule_id, bool) or not isinstance(schedule_id, int) or schedule_id <= 0:
            data_integrity_error()
        work_date = canonical_request_date(item["work_date"])
        if work_date in seen_dates or schedule_id in seen_schedules:
            data_integrity_error()
        seen_dates.add(work_date); seen_schedules.add(schedule_id); item_order.append((work_date, schedule_id))
        if item["resolution_type"] == "substitute":
            positive_request_id(item["substitute_staff_id"])
        elif item["resolution_type"] == "defer_following_assignments":
            if item["substitute_staff_id"] is not None:
                data_integrity_error()
        else:
            data_integrity_error()
        is_double_pay = item.get("is_double_pay", False)
        if type(is_double_pay) is not bool:
            data_integrity_error()
        if item["resolution_type"] == "defer_following_assignments" and is_double_pay:
            data_integrity_error()
    if item_order != sorted(item_order):
        data_integrity_error()
    try:
        cursor.execute("SELECT batch_key, batch_item_index, case_no, original_assignment_id, original_schedule_id, work_date, resolution_type, substitute_assignment_id, event_key, actor, reason, schedule_snapshot, payroll_snapshot FROM assignment_schedule_leave_substitution_events WHERE batch_key = %s ORDER BY batch_item_index ASC" + suffix, (batch_key,))
        raw_children = cursor.fetchall()
    except Exception as exc:
        infrastructure_error(exc)
    child_fields = {"batch_key", "batch_item_index", "case_no", "original_assignment_id", "original_schedule_id", "work_date", "resolution_type", "substitute_assignment_id", "event_key", "actor", "reason", "schedule_snapshot", "payroll_snapshot"}
    children = []
    for raw in list(raw_children or []):
        if not isinstance(raw, Mapping) or set(raw) != child_fields:
            data_integrity_error()
        row = dict(raw)
        if row["batch_key"] != batch_key:
            data_integrity_error()
        ordinal = row["batch_item_index"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            data_integrity_error()
        for field in ("original_assignment_id", "original_schedule_id"):
            if isinstance(row[field], bool) or not isinstance(row[field], int) or row[field] <= 0:
                data_integrity_error()
        if row["case_no"] != raw_header["case_no"] or not isinstance(row["case_no"], str):
            data_integrity_error()
        if row["original_assignment_id"] != request_snapshot["original_assignment_id"]:
            data_integrity_error()
        if row["resolution_type"] not in {"leave_only", "defer_following_assignments", "substitute"}:
            data_integrity_error()
        if row["resolution_type"] == "substitute":
            if isinstance(row["substitute_assignment_id"], bool) or not isinstance(row["substitute_assignment_id"], int) or row["substitute_assignment_id"] <= 0 or row["substitute_assignment_id"] == row["original_assignment_id"]:
                data_integrity_error()
        elif row["substitute_assignment_id"] is not None:
            data_integrity_error()
        for field in ("event_key", "actor", "reason"):
            if not isinstance(row[field], str) or not row[field].strip() or row[field] != row[field].strip():
                data_integrity_error()
        row["work_date"] = canonical_child_date(row["work_date"])
        row["schedule_snapshot"] = json_object(row["schedule_snapshot"], "schedule_snapshot")
        row["payroll_snapshot"] = json_object(row["payroll_snapshot"], "payroll_snapshot")
        children.append(row)
    children.sort(key=lambda row: row["batch_item_index"])
    if len(children) != item_count or [row["batch_item_index"] for row in children] != list(range(item_count)):
        data_integrity_error()
    header = {"batch_key": batch_key, "contract_version": request_snapshot["contract_version"], "case_no": raw_header["case_no"], "original_assignment_id": request_snapshot["original_assignment_id"], "request_snapshot": request_snapshot, "preview_fingerprint": raw_header["preview_fingerprint"], "item_count": item_count, "actor": raw_header["actor"], "reason": raw_header["reason"]}
    return {"state": "present", "header": header, "children": children}


def decide_assignment_leave_resolution_batch_replay(
    replay_snapshot: Mapping[str, Any], requested_identity: Mapping[str, Any]
) -> Dict[str, Any]:
    """Purely decide whether a persisted batch is the same transport retry."""
    requested_fields = {"batch_key", "request_snapshot", "preview_fingerprint"}
    header_fields = {
        "batch_key", "contract_version", "case_no", "original_assignment_id",
        "request_snapshot", "preview_fingerprint", "item_count", "actor", "reason",
    }
    child_fields = {
        "batch_key", "batch_item_index", "case_no", "original_assignment_id",
        "original_schedule_id", "work_date", "resolution_type",
        "substitute_assignment_id", "event_key", "actor", "reason",
        "schedule_snapshot", "payroll_snapshot",
    }

    def application_error(code: str, reason: str, details: Mapping[str, Any]) -> None:
        raise AssignmentLeaveResolutionApplicationError(
            code=code, reason=reason, details=details
        )

    def data_integrity_error(code: str, reason: str) -> None:
        raise AssignmentLeaveResolutionDataIntegrityError(
            code=code, reason=reason, details={"source": "batch_replay_snapshot"}
        )

    def exact_date(value: Any, invalid: Any) -> str:
        try:
            return _as_rest_date_string(value, "work_date")
        except AssignmentScheduleRestDateValidationError:
            invalid()
            raise AssertionError("unreachable")

    def validate_preview_request(value: Any, invalid: Any) -> None:
        expected_preview_fields = {
            "contract_version", "case_no", "original_assignment_id", "items"
        }
        required_item_fields = {
            "original_schedule_id", "work_date", "resolution_type",
            "substitute_staff_id",
        }
        if not isinstance(value, Mapping) or set(value) != expected_preview_fields:
            invalid()
        if value["contract_version"] != "assignment-leave-substitution-batch-preview/v1":
            invalid()
        if not isinstance(value["case_no"], str) or not value["case_no"] or value["case_no"] != value["case_no"].strip():
            invalid()
        assignment_id = value["original_assignment_id"]
        if isinstance(assignment_id, bool) or not isinstance(assignment_id, int) or assignment_id <= 0:
            invalid()
        items = value["items"]
        if not isinstance(items, list) or not items:
            invalid()
        dates, schedule_ids, canonical_order = set(), set(), []
        for item in items:
            if (
                not isinstance(item, Mapping)
                or not required_item_fields.issubset(item)
                or set(item) - (required_item_fields | {"is_double_pay"})
            ):
                invalid()
            schedule_id = item["original_schedule_id"]
            if isinstance(schedule_id, bool) or not isinstance(schedule_id, int) or schedule_id <= 0:
                invalid()
            work_date = exact_date(item["work_date"], invalid)
            if schedule_id in schedule_ids or work_date in dates:
                invalid()
            schedule_ids.add(schedule_id)
            dates.add(work_date)
            canonical_order.append((work_date, schedule_id))
            if item["resolution_type"] == "substitute":
                substitute = item["substitute_staff_id"]
                if isinstance(substitute, bool) or not isinstance(substitute, int) or substitute <= 0:
                    invalid()
            elif item["resolution_type"] == "defer_following_assignments":
                if item["substitute_staff_id"] is not None:
                    invalid()
            else:
                invalid()
            is_double_pay = item.get("is_double_pay", False)
            if type(is_double_pay) is not bool:
                invalid()
            if item["resolution_type"] == "defer_following_assignments" and is_double_pay:
                invalid()
        if canonical_order != sorted(canonical_order):
            invalid()

    def validate_canonical_json_object(value: Any, invalid: Any) -> None:
        if not isinstance(value, Mapping):
            invalid()

        def validate_json(item: Any) -> None:
            if item is None or isinstance(item, (bool, str)):
                return
            if isinstance(item, int) and not isinstance(item, bool):
                return
            if isinstance(item, Mapping):
                if any(not isinstance(key, str) for key in item) or list(item) != sorted(item):
                    invalid()
                for nested in item.values():
                    validate_json(nested)
                return
            if isinstance(item, list):
                for nested in item:
                    validate_json(nested)
                return
            invalid()

        validate_json(value)

    if not isinstance(requested_identity, Mapping) or set(requested_identity) != requested_fields:
        application_error(
            "invalid_batch_replay_identity",
            "batch replay identity is invalid",
            {"field": "requested_identity"},
        )
    batch_key = requested_identity["batch_key"]
    if not isinstance(batch_key, str) or not batch_key or batch_key != batch_key.strip():
        application_error(
            "invalid_batch_replay_identity",
            "batch replay identity is invalid",
            {"field": "batch_key"},
        )
    if not isinstance(requested_identity["request_snapshot"], Mapping):
        application_error(
            "invalid_batch_replay_identity",
            "batch replay identity is invalid",
            {"field": "request_snapshot"},
        )
    if not isinstance(requested_identity["preview_fingerprint"], str):
        application_error(
            "invalid_batch_replay_identity",
            "batch replay identity is invalid",
            {"field": "preview_fingerprint"},
        )
    if re.fullmatch(r"[0-9a-f]{64}", requested_identity["preview_fingerprint"]) is None:
        application_error(
            "invalid_batch_replay_identity",
            "batch replay identity is invalid",
            {"field": "preview_fingerprint"},
        )
    validate_preview_request(
        requested_identity["request_snapshot"],
        lambda: application_error(
            "invalid_batch_replay_identity",
            "batch replay identity is invalid",
            {"field": "request_snapshot"},
        ),
    )

    if not isinstance(replay_snapshot, Mapping) or set(replay_snapshot) != {"state", "header", "children"}:
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
    if replay_snapshot["state"] == "absent":
        if replay_snapshot["header"] is not None or replay_snapshot["children"] != []:
            data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
        return {"status": "absent", "replay_result": None}
    if replay_snapshot["state"] != "present":
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")

    header = replay_snapshot["header"]
    children = replay_snapshot["children"]
    if not isinstance(header, Mapping) or set(header) != header_fields:
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
    if not isinstance(children, list):
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
    if header["batch_key"] != batch_key or not isinstance(header["request_snapshot"], Mapping) or not isinstance(header["preview_fingerprint"], str):
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
    if not isinstance(header["case_no"], str) or not header["case_no"] or header["case_no"] != header["case_no"].strip():
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
    if isinstance(header["original_assignment_id"], bool) or not isinstance(header["original_assignment_id"], int) or header["original_assignment_id"] <= 0:
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", header["preview_fingerprint"]) is None:
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
    if not all(isinstance(header[field], str) and header[field] and header[field] == header[field].strip() for field in ("actor", "reason")):
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
    validate_preview_request(
        header["request_snapshot"],
        lambda: data_integrity_error(
            "invalid_batch_replay_snapshot", "batch replay snapshot is invalid"
        ),
    )
    if header["contract_version"] != "assignment-leave-substitution-batch-preview/v1" or header["contract_version"] != header["request_snapshot"]["contract_version"]:
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
    if header["request_snapshot"]["case_no"] != header["case_no"] or header["request_snapshot"]["original_assignment_id"] != header["original_assignment_id"]:
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
    if isinstance(header["item_count"], bool) or not isinstance(header["item_count"], int) or header["item_count"] < 1:
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
    if len(header["request_snapshot"]["items"]) != header["item_count"]:
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")

    event_copies = []
    for child in children:
        if not isinstance(child, Mapping) or set(child) != child_fields:
            data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
        ordinal = child["batch_item_index"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
        if child["batch_key"] != header["batch_key"] or child["case_no"] != header["case_no"] or child["original_assignment_id"] != header["original_assignment_id"]:
            data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
        if isinstance(child["original_schedule_id"], bool) or not isinstance(child["original_schedule_id"], int) or child["original_schedule_id"] <= 0:
            data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
        exact_date(child["work_date"], lambda: data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid"))
        if child["resolution_type"] == "substitute":
            substitute = child["substitute_assignment_id"]
            if isinstance(substitute, bool) or not isinstance(substitute, int) or substitute <= 0 or substitute == child["original_assignment_id"]:
                data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
        elif child["resolution_type"] in {"leave_only", "defer_following_assignments"}:
            if child["substitute_assignment_id"] is not None:
                data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
        else:
            data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
        if not all(isinstance(child[field], str) and child[field] and child[field] == child[field].strip() for field in ("event_key", "actor", "reason")):
            data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")
        validate_canonical_json_object(child["schedule_snapshot"], lambda: data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid"))
        validate_canonical_json_object(child["payroll_snapshot"], lambda: data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid"))
        event_copies.append(_snapshot_domain_error_value(child))
    event_copies.sort(key=lambda child: child["batch_item_index"])
    if len(event_copies) != header["item_count"] or [
        child["batch_item_index"] for child in event_copies
    ] != list(range(header["item_count"])):
        data_integrity_error("invalid_batch_replay_snapshot", "batch replay snapshot is invalid")

    mismatched_fields = []
    for field in ("request_snapshot", "preview_fingerprint"):
        if header[field] != requested_identity[field]:
            mismatched_fields.append(field)
    if mismatched_fields:
        application_error(
            "batch_key_request_identity_conflict",
            "batch request identity differs",
            {"batch_key": batch_key, "mismatched_fields": sorted(mismatched_fields)},
        )
    return {
        "status": "idempotent_replay",
        "replay_result": {
            "status": "idempotent_replay",
            "batch": _snapshot_domain_error_value(header),
            "events": event_copies,
        },
    }


def _as_positive_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise AssignmentScheduleRestDateValidationError(
            code=_validation_error_code(field_name),
            message=f"{field_name} must be a positive decimal",
            details={"field": field_name},
        )
    try:
        normalised = Decimal(str(value))
    except (TypeError, InvalidOperation, ValueError) as exc:
        raise AssignmentScheduleRestDateValidationError(
            code=_validation_error_code(field_name),
            message=f"{field_name} must be a positive decimal",
            details={"field": field_name},
        ) from exc
    if not normalised.is_finite() or normalised <= 0:
        raise AssignmentScheduleRestDateValidationError(
            code=_validation_error_code(field_name),
            message=f"{field_name} must be a positive decimal",
            details={"field": field_name},
        )
    return normalised


def _normalise_rest_dates(rest_dates: Any) -> List[str]:
    if rest_dates is None:
        raise AssignmentScheduleRestDateValidationError(
            code=_validation_error_code("rest_dates"),
            message="rest_dates must be an array",
            details={"field": "rest_dates"},
        )
    if not isinstance(rest_dates, list):
        raise AssignmentScheduleRestDateValidationError(
            code=_validation_error_code("rest_dates"),
            message="rest_dates must be an array",
            details={"field": "rest_dates"},
        )
    if len(rest_dates) == 0:
        return []
    parsed: list[date] = []
    for item in rest_dates:
        parsed.append(_as_date(_as_rest_date_string(item, "rest_date"), "rest_date"))
    unique_dates = sorted(set(parsed))
    return [item.isoformat() for item in unique_dates]


def _snapshot_failure(status: str, message: str, error_code: str) -> Dict[str, Any]:
    return {
        "success": False,
        "status": status,
        "error_code": error_code,
        "message": message,
    }


_ASSIGNMENT_LEAVE_RESOLUTION_DOMAIN_ERROR_CATEGORIES = frozenset(
    {
        "not_found",
        "validation_error",
        "conflict",
        "locked",
        "stale_preview",
        "event_key_identity_conflict",
    }
)


class _FrozenDomainErrorList(tuple):
    """Immutable, copy-safe list container for domain error details."""

    def append(self, *args, **kwargs) -> None:
        raise TypeError("assignment leave resolution domain error details are immutable")

    def extend(self, *args, **kwargs) -> None:
        raise TypeError("assignment leave resolution domain error details are immutable")

    def insert(self, *args, **kwargs) -> None:
        raise TypeError("assignment leave resolution domain error details are immutable")

    def pop(self, *args, **kwargs):  # noqa: ANN001
        raise TypeError("assignment leave resolution domain error details are immutable")

    def remove(self, *args, **kwargs) -> None:
        raise TypeError("assignment leave resolution domain error details are immutable")

    def clear(self, *args, **kwargs) -> None:
        raise TypeError("assignment leave resolution domain error details are immutable")


def _assert_domain_error_value(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("invalid details")
            _assert_domain_error_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_domain_error_value(item)
        return
    if value is None:
        return
    if isinstance(value, bool):
        return
    if isinstance(value, str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("invalid details")
        return
    raise ValueError("invalid details")


def _freeze_domain_error_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {k: _freeze_domain_error_value(v) for k, v in dict(value).items()}
        )
    if isinstance(value, list):
        return _FrozenDomainErrorList(_freeze_domain_error_value(item) for item in value)
    if isinstance(value, tuple):
        return _FrozenDomainErrorList(_freeze_domain_error_value(item) for item in value)
    return deepcopy(value)


def _snapshot_domain_error_value(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {k: _snapshot_domain_error_value(v) for k, v in dict(value).items()}
    if isinstance(value, Mapping):
        return {k: _snapshot_domain_error_value(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_snapshot_domain_error_value(item) for item in value]
    if isinstance(value, list):
        return [_snapshot_domain_error_value(item) for item in value]
    return deepcopy(value)


class _AssignmentLeaveResolutionTypedError(Exception):
    """Private common implementation for non-business leave-resolution failures."""

    __slots__ = ("kind", "code", "reason", "details")

    def __init__(
        self,
        *,
        kind: str,
        code: str,
        reason: str,
        details: Mapping[str, Any],
    ) -> None:
        if kind not in {"application", "data_integrity", "infrastructure"}:
            raise ValueError("invalid typed error kind")
        if not isinstance(code, str) or re.fullmatch(r"[a-z0-9_]+", code) is None:
            raise ValueError("invalid typed error code")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("invalid typed error reason")
        if not isinstance(details, Mapping):
            raise ValueError("invalid typed error details")
        _assert_domain_error_value(details)
        self.kind = kind
        self.code = code
        self.reason = reason
        self.details = _freeze_domain_error_value(details)
        super().__init__(f"assignment leave resolution {kind} error: {code}")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "code": self.code,
            "reason": self.reason,
            "details": _snapshot_domain_error_value(self.details),
        }


class AssignmentLeaveResolutionApplicationError(_AssignmentLeaveResolutionTypedError):
    """Transport/application command failure; never a normal business result."""

    def __init__(self, *, code: str, reason: str, details: Mapping[str, Any]) -> None:
        super().__init__(
            kind="application", code=code, reason=reason, details=details
        )


class AssignmentLeaveResolutionDataIntegrityError(_AssignmentLeaveResolutionTypedError):
    """Persisted or dependency contract failure; never downgrade to a 4xx result."""

    def __init__(self, *, code: str, reason: str, details: Mapping[str, Any]) -> None:
        super().__init__(
            kind="data_integrity", code=code, reason=reason, details=details
        )


class AssignmentLeaveResolutionInfrastructureError(_AssignmentLeaveResolutionTypedError):
    """Infrastructure failure retaining its original cause outside response payloads."""

    def __init__(
        self,
        *,
        code: str,
        reason: str,
        details: Mapping[str, Any],
        cause: BaseException,
    ) -> None:
        if not isinstance(cause, BaseException):
            raise ValueError("invalid infrastructure error cause")
        super().__init__(
            kind="infrastructure", code=code, reason=reason, details=details
        )
        self.__cause__ = cause


@dataclass(frozen=True)
class AssignmentLeaveResolutionDomainError(Exception):
    category: str
    code: str
    reason: str
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.category not in _ASSIGNMENT_LEAVE_RESOLUTION_DOMAIN_ERROR_CATEGORIES:
            raise ValueError("invalid category")
        if not isinstance(self.code, str) or not self.code or " " in self.code:
            raise ValueError("invalid code")
        if not re.fullmatch(r"[a-z0-9_]+", self.code):
            raise ValueError("invalid code")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("invalid reason")
        if self.details is None:
            object.__setattr__(
                self,
                "args",
                (
                    f"assignment leave resolution domain error "
                    f"[category={self.category}, code={self.code}]: {self.reason}",
                ),
            )
            return
        if not isinstance(self.details, Mapping):
            raise ValueError("invalid details")
        _assert_domain_error_value(self.details)
        object.__setattr__(self, "details", _freeze_domain_error_value(self.details))
        object.__setattr__(
            self,
            "args",
            (
                f"assignment leave resolution domain error "
                f"[category={self.category}, code={self.code}]: {self.reason}",
            ),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "code": self.code,
            "reason": self.reason,
            "details": None if self.details is None else _snapshot_domain_error_value(
                self.details
            ),
        }


def preview_assignment_leave_resolution(
    case_no: str,
    original_assignment_id: int,
    original_schedule_id: int,
    work_date: str,
    resolution_type: str,
    substitute_staff_id: int | None = None,
) -> Dict[str, Any]:
    """Build a read-only, server-validated leave resolution plan."""
    from services.assignment_schedule_leave_resolution_preview import (
        compute_assignment_leave_resolution_preview_from_snapshot,
    )

    def _raise_domain_error(
        message: str,
        *,
        category: str,
        code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        raise AssignmentLeaveResolutionDomainError(
            category=category,
            code=code,
            reason=message,
            details=dict(details) if details is not None else None,
        )

    def _raise_by_validation_error(error: AssignmentScheduleRestDateValidationError) -> None:
        details = error.as_dict().get("details")
        _raise_domain_error(
            error.message,
            category="validation_error",
            code=error.code,
            details=dict(details) if details is not None else None,
        )

    try:
        if not isinstance(case_no, str) or not case_no.strip():
            _raise_domain_error(
                "case_no must be a non-empty string",
                category="validation_error",
                code="invalid_case_no",
                details={"field": "case_no"},
            )
        canonical_case_no = case_no.strip()
        assignment_id = _as_positive_int(original_assignment_id, "original_assignment_id")
        schedule_id = _as_positive_int(original_schedule_id, "original_schedule_id")
        leave_date = _as_date(_as_rest_date_string(work_date, "work_date"), "work_date")
        if resolution_type not in {"defer_following_assignments", "substitute"}:
            _raise_domain_error(
                "resolution_type must be defer_following_assignments or substitute",
                category="validation_error",
                code="invalid_resolution_type",
                details={"field": "resolution_type"},
            )
        if resolution_type == "substitute":
            substitute_id = _as_positive_int(substitute_staff_id, "substitute_staff_id")
        elif substitute_staff_id is not None:
            _raise_domain_error(
                "substitute_staff_id must be null when deferring assignments",
                category="validation_error",
                code="invalid_substitute_staff_id",
                details={"field": "substitute_staff_id"},
            )
        else:
            substitute_id = None

        original_snapshot = get_assignment_schedule(assignment_id)
        original = dict(original_snapshot.get("assignment") or {})
        if original.get("id") != assignment_id or original.get("case_no") != canonical_case_no:
            _raise_domain_error(
                "original assignment ownership mismatch",
                category="validation_error",
                code="assignment_case_mismatch",
                details={"field": "case_no"},
            )
        original_staff_id = _as_positive_int(original.get("staff_id"), "original staff_id")
        matching_schedule = [
            row
            for row in original_snapshot.get("schedule_days") or []
            if row.get("id") == schedule_id
        ]
        if len(matching_schedule) != 1:
            _raise_domain_error(
                "original_schedule_id does not belong to original_assignment_id",
                category="not_found",
                code="original_schedule_not_found",
                details={"field": "original_schedule_id"},
            )
        original_day = matching_schedule[0]
        if (
            original_day.get("assignment_id") != assignment_id
            or original_day.get("case_no") != canonical_case_no
            or original_day.get("staff_id") != original_staff_id
            or _as_date(original_day.get("work_date"), "schedule work_date") != leave_date
        ):
            _raise_domain_error(
                "original schedule ownership mismatch",
                category="validation_error",
                code="schedule_ownership_mismatch",
                details={"field": "original_schedule_id"},
            )
        if not bool(original_day.get("is_work_day")):
            _raise_domain_error(
                "original schedule day is not a work day",
                category="validation_error",
                code="schedule_not_work_day",
                details={"field": "work_date"},
            )

        current_assignments_seed = [
            dict(row) for row in original_snapshot.get("case_assignments") or []
        ]
        seed_staff_ids = {
            _as_positive_int(row.get("staff_id"), "assignment staff_id")
            for row in current_assignments_seed
            if row.get("status") != "cancelled"
        }
        seed_staff_ids.add(original_staff_id)
        if substitute_id is not None:
            seed_staff_ids.add(substitute_id)
        seed_dates = [
            _as_date(original.get("assigned_start_date"), "assigned_start_date"),
            _as_date(original.get("assigned_end_date"), "assigned_end_date"),
            leave_date,
        ]
        conflict_snapshot = get_case_schedule_conflict_snapshot(
            canonical_case_no,
            sorted(seed_staff_ids),
            min(seed_dates).isoformat(),
            (max(seed_dates) + timedelta(days=1)).isoformat(),
        )
        request = {
            "case_no": canonical_case_no,
            "original_assignment_id": assignment_id,
            "original_schedule_id": schedule_id,
            "work_date": leave_date.isoformat(),
            "resolution_type": resolution_type,
            "substitute_staff_id": substitute_id,
        }
        preview = compute_assignment_leave_resolution_preview_from_snapshot(
            request,
            original_snapshot,
            conflict_snapshot,
        )
    except AssignmentScheduleRestDateValidationError as exc:
        _raise_by_validation_error(exc)
    except AssignmentScheduleConflictSnapshotDomainError as exc:
        if exc.code == "case_not_found":
            _raise_domain_error(
                "case does not exist",
                category="not_found",
                code=exc.code,
                details=exc.as_dict()["details"],
            )
        if exc.code == "assignment_identity_changed_during_snapshot":
            _raise_domain_error(
                "assignment identity changed while reading conflict snapshot",
                category="conflict",
                code=exc.code,
                details=exc.as_dict()["details"],
            )
        raise

    return preview


def preview_assignment_leave_resolution_batch(
    request: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build a read-only, server-validated batch leave resolution plan."""
    from services.assignment_schedule_leave_resolution_preview import (
        compute_assignment_leave_resolution_batch_preview_from_snapshot,
    )

    def _raise_domain_error(
        message: str,
        *,
        category: str,
        code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        raise AssignmentLeaveResolutionDomainError(
            category=category,
            code=code,
            reason=message,
            details=dict(details) if details is not None else None,
        )

    def _raise_by_validation_error(error: AssignmentScheduleRestDateValidationError) -> None:
        details = error.as_dict().get("details")
        _raise_domain_error(
            error.message,
            category="validation_error",
            code=error.code,
            details=dict(details) if details is not None else None,
        )

    try:
        if not isinstance(request, Mapping):
            _raise_domain_error(
                "request must be a mapping",
                category="validation_error",
                code=_validation_error_code("request"),
                details={"field": "request"},
            )
        if set(request) != {
            "contract_version",
            "case_no",
            "original_assignment_id",
            "items",
        }:
            _raise_domain_error(
                "request contains unsupported or missing fields",
                category="validation_error",
                code=_validation_error_code("request"),
                details={"field": "request"},
            )
        contract_version = request.get("contract_version")
        if contract_version != "assignment-leave-substitution-batch-preview/v1":
            _raise_domain_error(
                "contract_version must be assignment-leave-substitution-batch-preview/v1",
                category="validation_error",
                code="invalid_contract_version",
                details={"field": "contract_version"},
            )
        canonical_case_no = request.get("case_no")
        if not isinstance(canonical_case_no, str) or not canonical_case_no.strip():
            _raise_domain_error(
                "case_no must be a non-empty string",
                category="validation_error",
                code="invalid_case_no",
                details={"field": "case_no"},
            )
        canonical_case_no = canonical_case_no.strip()
        original_assignment_id = _as_positive_int(
            request.get("original_assignment_id"), "original_assignment_id"
        )
        items = request.get("items")
        if not isinstance(items, list):
            _raise_domain_error(
                "items must be a list",
                category="validation_error",
                code="invalid_items",
                details={"field": "items"},
            )
        if len(items) == 0:
            _raise_domain_error(
                "items must be a non-empty list",
                category="validation_error",
                code="invalid_items",
                details={"field": "items"},
            )

        snapshot_staff_ids: list[int] = []
        work_dates: list[date] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                _raise_domain_error(
                    f"items[{index}] must be a mapping",
                    category="validation_error",
                    code="invalid_items",
                    details={"field": f"items[{index}]"},
                )
            _as_positive_int(item.get("original_schedule_id"), "original_schedule_id")
            work_dates.append(
                _as_date(
                    _as_rest_date_string(item.get("work_date"), f"items[{index}].work_date"),
                    f"items[{index}].work_date",
                )
            )
            resolution_type = item.get("resolution_type")
            if resolution_type not in {"defer_following_assignments", "substitute"}:
                _raise_domain_error(
                    "resolution_type must be defer_following_assignments or substitute",
                    category="validation_error",
                    code="invalid_resolution_type",
                    details={"field": f"items[{index}].resolution_type"},
                )
            if resolution_type == "substitute":
                substitute_staff_id = _as_positive_int(
                    item.get("substitute_staff_id"),
                    f"items[{index}].substitute_staff_id",
                )
                snapshot_staff_ids.append(substitute_staff_id)
            elif item.get("substitute_staff_id") is not None:
                _raise_domain_error(
                    "substitute_staff_id must be null when deferring assignments",
                    category="validation_error",
                    code="invalid_substitute_staff_id",
                    details={"field": f"items[{index}].substitute_staff_id"},
                )

        range_start = min(work_dates).isoformat()
        range_end = (max(work_dates) + timedelta(days=1)).isoformat()
        conflict_snapshot = get_case_schedule_conflict_snapshot(
            canonical_case_no,
            snapshot_staff_ids,
            range_start,
            range_end,
        )

        original_assignment = None
        for row in conflict_snapshot.get("assignments", []):
            if (
                isinstance(row, Mapping)
                and row.get("id") == original_assignment_id
                and row.get("case_no") == canonical_case_no
            ):
                original_assignment = row
                break
        if original_assignment is None:
            _raise_domain_error(
                "original assignment does not exist",
                category="not_found",
                code="original_assignment_not_found",
                details={
                    "case_no": canonical_case_no,
                    "original_assignment_id": original_assignment_id,
                },
            )

        original_schedule_rows = [
            row
            for row in (conflict_snapshot.get("assignment_schedule_days") or [])
            if isinstance(row, Mapping) and row.get("assignment_id") == original_assignment_id
        ]
        original_assignment_snapshot = {
            "assignment": original_assignment,
            "schedule_days": original_schedule_rows,
        }
        return compute_assignment_leave_resolution_batch_preview_from_snapshot(
            {
                "contract_version": "assignment-leave-substitution-batch-preview/v1",
                "case_no": canonical_case_no,
                "original_assignment_id": original_assignment_id,
                "items": list(items),
            },
            original_assignment_snapshot,
            conflict_snapshot,
        )
    except AssignmentScheduleRestDateValidationError as exc:
        _raise_by_validation_error(exc)
    except AssignmentScheduleConflictSnapshotDomainError as exc:
        if exc.code == "case_not_found":
            _raise_domain_error(
                "case does not exist",
                category="not_found",
                code=exc.code,
                details=exc.as_dict()["details"],
            )
        if exc.code == "assignment_identity_changed_during_snapshot":
            _raise_domain_error(
                "assignment identity changed while reading conflict snapshot",
                category="conflict",
                code=exc.code,
                details=exc.as_dict()["details"],
            )
        raise


def acquire_assignment_leave_resolution_batch_locked_facts(
    cursor: Any,
    preview_request: Mapping[str, Any],
) -> Dict[str, Any]:
    """Acquire one canonical batch apply snapshot under the caller transaction."""

    def application_error(field: str) -> None:
        raise AssignmentLeaveResolutionApplicationError(
            code="invalid_batch_locked_facts_request",
            reason="batch locked facts request is invalid",
            details={"field": field},
        )

    def data_integrity_error(source: str) -> None:
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_locked_facts",
            reason="batch locked facts are invalid",
            details={"source": source},
        )

    def infrastructure_error(cause: BaseException) -> None:
        raise AssignmentLeaveResolutionInfrastructureError(
            code="batch_locked_facts_unavailable",
            reason="batch locked facts are unavailable",
            details={"operation": "batch_locked_facts_acquisition"},
            cause=cause,
        ) from cause

    def strict_positive(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            application_error(field)
        return value

    def exact_date(value: Any, field: str) -> str:
        try:
            return _as_rest_date_string(value, field)
        except AssignmentScheduleRestDateValidationError:
            application_error(field)

    if cursor is None or not all(
        callable(getattr(cursor, method, None))
        for method in ("execute", "fetchone", "fetchall")
    ):
        application_error("cursor")
    if not isinstance(preview_request, Mapping) or set(preview_request) != {
        "contract_version", "case_no", "original_assignment_id", "items"
    }:
        application_error("preview_request")
    if preview_request["contract_version"] != "assignment-leave-substitution-batch-preview/v1":
        application_error("contract_version")
    case_no = preview_request["case_no"]
    if not isinstance(case_no, str) or not case_no or case_no != case_no.strip():
        application_error("case_no")
    original_assignment_id = strict_positive(
        preview_request["original_assignment_id"], "original_assignment_id"
    )
    raw_items = preview_request["items"]
    if not isinstance(raw_items, list) or not raw_items:
        application_error("items")

    required_item_fields = {
        "original_schedule_id", "work_date", "resolution_type", "substitute_staff_id"
    }
    canonical_items: list[dict[str, Any]] = []
    extra_staff_ids: set[int] = set()
    seen_schedule_ids: set[int] = set()
    seen_work_dates: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        field_prefix = f"items[{index}]"
        if (
            not isinstance(raw_item, Mapping)
            or not required_item_fields.issubset(raw_item)
            or set(raw_item) - (required_item_fields | {"is_double_pay"})
        ):
            application_error(field_prefix)
        schedule_id = strict_positive(
            raw_item["original_schedule_id"], f"{field_prefix}.original_schedule_id"
        )
        work_date = exact_date(raw_item["work_date"], f"{field_prefix}.work_date")
        if schedule_id in seen_schedule_ids or work_date in seen_work_dates:
            application_error(field_prefix)
        resolution_type = raw_item["resolution_type"]
        substitute_staff_id = raw_item["substitute_staff_id"]
        if resolution_type == "substitute":
            substitute_staff_id = strict_positive(
                substitute_staff_id, f"{field_prefix}.substitute_staff_id"
            )
            extra_staff_ids.add(substitute_staff_id)
        elif resolution_type == "defer_following_assignments":
            if substitute_staff_id is not None:
                application_error(f"{field_prefix}.substitute_staff_id")
        else:
            application_error(f"{field_prefix}.resolution_type")
        is_double_pay = raw_item.get("is_double_pay", False)
        if type(is_double_pay) is not bool:
            application_error(f"{field_prefix}.is_double_pay")
        if resolution_type == "defer_following_assignments" and is_double_pay:
            application_error(f"{field_prefix}.is_double_pay")
        seen_schedule_ids.add(schedule_id)
        seen_work_dates.add(work_date)
        canonical_items.append(
            {
                "original_schedule_id": schedule_id,
                "work_date": work_date,
                "resolution_type": resolution_type,
                "substitute_staff_id": substitute_staff_id,
                "is_double_pay": is_double_pay,
            }
        )
    if canonical_items != sorted(
        canonical_items,
        key=lambda item: (item["work_date"], item["original_schedule_id"]),
    ):
        application_error("items")

    try:
        cursor.execute(
            "SELECT case_no FROM orders WHERE case_no = %s FOR UPDATE", (case_no,)
        )
        order_row = cursor.fetchone()
        cursor.execute(
            "SELECT id, case_no, staff_id FROM case_staff_assignments "
            "WHERE case_no = %s ORDER BY id ASC FOR UPDATE",
            (case_no,),
        )
        assignment_rows = cursor.fetchall()
    except Exception as exc:
        infrastructure_error(exc)
    if not isinstance(order_row, Mapping) or set(order_row) != {"case_no"}:
        data_integrity_error("order")
    if order_row["case_no"] != case_no:
        data_integrity_error("order")
    if not isinstance(assignment_rows, (list, tuple)):
        data_integrity_error("assignments")

    original_staff_id: int | None = None
    locked_assignment_ids: list[int] = []
    for raw_row in assignment_rows:
        if not isinstance(raw_row, Mapping) or set(raw_row) != {
            "id", "case_no", "staff_id"
        }:
            data_integrity_error("assignments")
        assignment_id = raw_row["id"]
        staff_id = raw_row["staff_id"]
        if (
            isinstance(assignment_id, bool)
            or not isinstance(assignment_id, int)
            or assignment_id <= 0
            or isinstance(staff_id, bool)
            or not isinstance(staff_id, int)
            or staff_id <= 0
            or raw_row["case_no"] != case_no
        ):
            data_integrity_error("assignments")
        locked_assignment_ids.append(assignment_id)
        if assignment_id == original_assignment_id:
            if original_staff_id is not None:
                data_integrity_error("assignments")
            original_staff_id = staff_id
    if locked_assignment_ids != sorted(set(locked_assignment_ids)) or original_staff_id is None:
        data_integrity_error("assignments")

    mutex_staff_ids = sorted({original_staff_id, *extra_staff_ids})
    try:
        locked_staff_ids = lock_staff_occupancy_mutex(cursor, mutex_staff_ids)
    except (
        AssignmentLeaveResolutionApplicationError,
        AssignmentLeaveResolutionDataIntegrityError,
        AssignmentLeaveResolutionInfrastructureError,
    ):
        raise
    except (AssignmentScheduleConflictSnapshotDomainError, ValueError):
        data_integrity_error("staff_mutex")
    except Exception as exc:
        infrastructure_error(exc)
    if locked_staff_ids != mutex_staff_ids:
        data_integrity_error("staff_mutex")

    range_start = canonical_items[0]["work_date"]
    range_end = (
        _as_date(canonical_items[-1]["work_date"], "range_end") + timedelta(days=1)
    ).isoformat()
    try:
        conflict_snapshot = get_case_schedule_conflict_snapshot_with_cursor(
            cursor,
            case_no,
            sorted(extra_staff_ids),
            range_start,
            range_end,
            True,
        )
    except (
        AssignmentLeaveResolutionApplicationError,
        AssignmentLeaveResolutionDataIntegrityError,
        AssignmentLeaveResolutionInfrastructureError,
    ):
        raise
    except (AssignmentScheduleConflictSnapshotDomainError, ValueError):
        data_integrity_error("conflict_snapshot")
    except Exception as exc:
        infrastructure_error(exc)

    if not isinstance(conflict_snapshot, Mapping):
        data_integrity_error("conflict_snapshot")
    snapshot_assignments = conflict_snapshot.get("assignments")
    snapshot_schedule_days = conflict_snapshot.get("assignment_schedule_days")
    if not isinstance(snapshot_assignments, list) or not isinstance(snapshot_schedule_days, list):
        data_integrity_error("conflict_snapshot")
    matching_assignments = [
        dict(row)
        for row in snapshot_assignments
        if isinstance(row, Mapping) and row.get("id") == original_assignment_id
    ]
    if len(matching_assignments) != 1:
        data_integrity_error("conflict_snapshot")
    original_assignment = matching_assignments[0]
    if (
        original_assignment.get("case_no") != case_no
        or original_assignment.get("staff_id") != original_staff_id
    ):
        data_integrity_error("conflict_snapshot")

    original_schedule_days: list[dict[str, Any]] = []
    schedules_by_id: dict[int, tuple[str, str]] = {}
    for raw_row in snapshot_schedule_days:
        if not isinstance(raw_row, Mapping):
            data_integrity_error("conflict_snapshot")
        if raw_row.get("assignment_id") != original_assignment_id:
            continue
        schedule_id = raw_row.get("id")
        if isinstance(schedule_id, bool) or not isinstance(schedule_id, int) or schedule_id <= 0:
            data_integrity_error("conflict_snapshot")
        if raw_row.get("case_no") != case_no or raw_row.get("staff_id") != original_staff_id:
            data_integrity_error("conflict_snapshot")
        try:
            schedule_date = (
                raw_row["work_date"].isoformat()
                if isinstance(raw_row.get("work_date"), date)
                and not isinstance(raw_row.get("work_date"), datetime)
                else _as_rest_date_string(raw_row.get("work_date"), "schedule work_date")
            )
        except AssignmentScheduleRestDateValidationError:
            data_integrity_error("conflict_snapshot")
        if schedule_id in schedules_by_id:
            data_integrity_error("conflict_snapshot")
        schedules_by_id[schedule_id] = (schedule_date, case_no)
        original_schedule_days.append(dict(raw_row))
    for item in canonical_items:
        if schedules_by_id.get(item["original_schedule_id"]) != (item["work_date"], case_no):
            data_integrity_error("conflict_snapshot")

    return {
        "original_assignment_schedule": {
            "assignment": deepcopy(original_assignment),
            "schedule_days": deepcopy(original_schedule_days),
        },
        "conflict_snapshot": deepcopy(dict(conflict_snapshot)),
        "lock_identity": {
            "case_no": case_no,
            "staff_ids": list(mutex_staff_ids),
            "range_start": range_start,
            "range_end": range_end,
        },
    }


def authorize_assignment_leave_resolution_batch_apply(
    preview_request: Mapping[str, Any],
    requested_preview_fingerprint: str,
    canonical_apply_identity_metadata: Mapping[str, Any],
    locked_facts: Mapping[str, Any],
) -> Dict[str, Any]:
    """Decide a new batch apply from one fresh pure preview of locked facts."""
    from services.assignment_schedule_leave_resolution_preview import (
        compute_assignment_leave_resolution_batch_preview_from_snapshot,
    )

    def application_error(field: str) -> None:
        raise AssignmentLeaveResolutionApplicationError(
            code="invalid_batch_apply_authorization_request",
            reason="batch apply authorization request is invalid",
            details={"field": field},
        )

    def data_integrity_error(source: str) -> None:
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_apply_authorization_facts",
            reason="batch apply authorization facts are invalid",
            details={"source": source},
        )

    def infrastructure_error(cause: BaseException) -> None:
        raise AssignmentLeaveResolutionInfrastructureError(
            code="batch_apply_authorization_unavailable",
            reason="batch apply authorization is unavailable",
            details={"operation": "batch_apply_authorization"},
            cause=cause,
        ) from cause

    if not isinstance(preview_request, Mapping) or set(preview_request) != {
        "contract_version", "case_no", "original_assignment_id", "items"
    }:
        application_error("preview_request")
    if preview_request["contract_version"] != "assignment-leave-substitution-batch-preview/v1":
        application_error("contract_version")
    case_no = preview_request["case_no"]
    if not isinstance(case_no, str) or not case_no or case_no != case_no.strip():
        application_error("case_no")
    original_assignment_id = preview_request["original_assignment_id"]
    if (
        isinstance(original_assignment_id, bool)
        or not isinstance(original_assignment_id, int)
        or original_assignment_id <= 0
    ):
        application_error("original_assignment_id")
    raw_items = preview_request["items"]
    if not isinstance(raw_items, list) or not raw_items:
        application_error("items")
    required_item_fields = {
        "original_schedule_id", "work_date", "resolution_type", "substitute_staff_id"
    }
    previous_item_sort_key: tuple[str, int] | None = None
    seen_schedule_ids: set[int] = set()
    seen_work_dates: set[str] = set()
    for index, item in enumerate(raw_items):
        field_prefix = f"items[{index}]"
        if (
            not isinstance(item, Mapping)
            or not required_item_fields.issubset(item)
            or set(item) - (required_item_fields | {"is_double_pay"})
        ):
            application_error(field_prefix)
        schedule_id = item["original_schedule_id"]
        if isinstance(schedule_id, bool) or not isinstance(schedule_id, int) or schedule_id <= 0:
            application_error(f"{field_prefix}.original_schedule_id")
        try:
            work_date = _as_rest_date_string(item["work_date"], f"{field_prefix}.work_date")
        except AssignmentScheduleRestDateValidationError:
            application_error(f"{field_prefix}.work_date")
        if schedule_id in seen_schedule_ids or work_date in seen_work_dates:
            application_error(field_prefix)
        resolution_type = item["resolution_type"]
        substitute_staff_id = item["substitute_staff_id"]
        if resolution_type == "substitute":
            if (
                isinstance(substitute_staff_id, bool)
                or not isinstance(substitute_staff_id, int)
                or substitute_staff_id <= 0
            ):
                application_error(f"{field_prefix}.substitute_staff_id")
        elif resolution_type == "defer_following_assignments":
            if substitute_staff_id is not None:
                application_error(f"{field_prefix}.substitute_staff_id")
        else:
            application_error(f"{field_prefix}.resolution_type")
        is_double_pay = item.get("is_double_pay", False)
        if type(is_double_pay) is not bool:
            application_error(f"{field_prefix}.is_double_pay")
        if resolution_type == "defer_following_assignments" and is_double_pay:
            application_error(f"{field_prefix}.is_double_pay")
        item_sort_key = (work_date, schedule_id)
        if previous_item_sort_key is not None and item_sort_key <= previous_item_sort_key:
            application_error("items")
        previous_item_sort_key = item_sort_key
        seen_schedule_ids.add(schedule_id)
        seen_work_dates.add(work_date)
    if (
        not isinstance(requested_preview_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", requested_preview_fingerprint) is None
    ):
        application_error("requested_preview_fingerprint")
    if not isinstance(canonical_apply_identity_metadata, Mapping) or set(
        canonical_apply_identity_metadata
    ) != {"batch_key", "actor", "reason"}:
        application_error("canonical_apply_identity_metadata")
    for field in ("batch_key", "actor", "reason"):
        value = canonical_apply_identity_metadata[field]
        if not isinstance(value, str) or not value or value != value.strip():
            application_error(f"canonical_apply_identity_metadata.{field}")

    if not isinstance(locked_facts, Mapping) or set(locked_facts) != {
        "original_assignment_schedule", "conflict_snapshot", "lock_identity"
    }:
        data_integrity_error("locked_facts")
    original_assignment_schedule = locked_facts["original_assignment_schedule"]
    conflict_snapshot = locked_facts["conflict_snapshot"]
    lock_identity = locked_facts["lock_identity"]
    if (
        not isinstance(original_assignment_schedule, Mapping)
        or set(original_assignment_schedule) != {"assignment", "schedule_days"}
        or not isinstance(conflict_snapshot, Mapping)
        or not isinstance(lock_identity, Mapping)
        or set(lock_identity) != {"case_no", "staff_ids", "range_start", "range_end"}
    ):
        data_integrity_error("locked_facts")
    if (
        lock_identity["case_no"] != case_no
        or not isinstance(lock_identity["staff_ids"], list)
        or not lock_identity["staff_ids"]
        or any(
            isinstance(staff_id, bool)
            or not isinstance(staff_id, int)
            or staff_id <= 0
            for staff_id in lock_identity["staff_ids"]
        )
        or lock_identity["staff_ids"] != sorted(set(lock_identity["staff_ids"]))
    ):
        data_integrity_error("locked_facts")
    try:
        lock_range_start = _as_rest_date_string(
            lock_identity["range_start"], "lock_identity.range_start"
        )
        lock_range_end = _as_rest_date_string(
            lock_identity["range_end"], "lock_identity.range_end"
        )
    except AssignmentScheduleRestDateValidationError:
        data_integrity_error("locked_facts")
    if lock_range_start > lock_range_end:
        data_integrity_error("locked_facts")

    try:
        fresh_preview = compute_assignment_leave_resolution_batch_preview_from_snapshot(
            preview_request,
            original_assignment_schedule,
            conflict_snapshot,
        )
    except (
        AssignmentLeaveResolutionApplicationError,
        AssignmentLeaveResolutionDataIntegrityError,
        AssignmentLeaveResolutionInfrastructureError,
    ):
        raise
    except ValueError:
        data_integrity_error("batch_preview")
    except Exception as exc:
        infrastructure_error(exc)

    expected_preview_fields = {
        "contract_version",
        "canonical_intent",
        "double_pay_preferences",
        "service_plan_transition",
        "canonical_eligibility",
        "status",
        "requires_confirmation",
        "preview_fingerprint",
    }
    if not isinstance(fresh_preview, Mapping) or set(fresh_preview) != expected_preview_fields:
        data_integrity_error("batch_preview")
    if fresh_preview["contract_version"] != "assignment-leave-substitution-batch-preview/v1":
        data_integrity_error("batch_preview")
    canonical_intent = fresh_preview["canonical_intent"]
    double_pay_preferences = fresh_preview["double_pay_preferences"]
    service_plan_transition = fresh_preview["service_plan_transition"]
    canonical_eligibility = fresh_preview["canonical_eligibility"]
    status = fresh_preview["status"]
    requires_confirmation = fresh_preview["requires_confirmation"]
    fresh_fingerprint = fresh_preview["preview_fingerprint"]
    if (
        not isinstance(canonical_intent, Mapping)
        or not isinstance(double_pay_preferences, list)
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"item_ref", "is_double_pay"}
            or isinstance(item["item_ref"], bool)
            or not isinstance(item["item_ref"], int)
            or item["item_ref"] < 0
            or type(item["is_double_pay"]) is not bool
            for item in double_pay_preferences
        )
        or not isinstance(service_plan_transition, Mapping)
        or set(service_plan_transition) != {"before", "intent", "after", "impacts"}
        or not isinstance(canonical_eligibility, Mapping)
        or set(canonical_eligibility) != {
            "transition_valid", "applicable", "blocking_diagnostics", "review_diagnostics"
        }
        or type(canonical_eligibility["transition_valid"]) is not bool
        or type(canonical_eligibility["applicable"]) is not bool
        or not isinstance(canonical_eligibility["blocking_diagnostics"], list)
        or not isinstance(canonical_eligibility["review_diagnostics"], list)
        or status not in {"blocked", "requires_review", "ready"}
        or type(requires_confirmation) is not bool
        or not isinstance(fresh_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fresh_fingerprint) is None
    ):
        data_integrity_error("batch_preview")
    if (
        (status == "blocked" and requires_confirmation)
        or (status in {"requires_review", "ready"} and not requires_confirmation)
    ):
        data_integrity_error("batch_preview")

    if status != "ready" or fresh_fingerprint != requested_preview_fingerprint:
        return {
            "status": "rejected",
            "apply_authorization": None,
            "business_conflicts": {
                "status": "stale_preview"
                if fresh_fingerprint != requested_preview_fingerprint
                else status,
                "blocking_diagnostics": deepcopy(
                    canonical_eligibility["blocking_diagnostics"]
                ),
                "review_diagnostics": deepcopy(
                    canonical_eligibility["review_diagnostics"]
                ),
            },
        }

    return {
        "status": "apply",
        "apply_authorization": {
            "canonical_intent": deepcopy(dict(canonical_intent)),
            "double_pay_preferences": deepcopy(double_pay_preferences),
            "service_plan_transition": deepcopy(dict(service_plan_transition)),
            "canonical_eligibility": deepcopy(dict(canonical_eligibility)),
            "preview_fingerprint": fresh_fingerprint,
            "canonical_apply_identity": deepcopy(
                dict(canonical_apply_identity_metadata)
            ),
        },
        "business_conflicts": None,
    }


def build_assignment_leave_resolution_batch_mutation_command(
    apply_authorization: Mapping[str, Any],
    locked_facts: Mapping[str, Any],
) -> Dict[str, Any]:
    """Project an authorised pure transition into database-owned assignment refs."""
    if not isinstance(apply_authorization, Mapping) or set(apply_authorization) != {
        "canonical_intent",
        "double_pay_preferences",
        "service_plan_transition",
        "canonical_eligibility",
        "preview_fingerprint",
        "canonical_apply_identity",
    }:
        raise AssignmentLeaveResolutionApplicationError(
            code="invalid_batch_mutation_projection",
            reason="batch mutation projection request is invalid",
            details={"field": "apply_authorization"},
        )
    if not isinstance(locked_facts, Mapping) or set(locked_facts) != {
        "original_assignment_schedule",
        "conflict_snapshot",
        "lock_identity",
    }:
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "locked_facts"},
        )
    transition = apply_authorization["service_plan_transition"]
    eligibility = apply_authorization["canonical_eligibility"]
    identity = apply_authorization["canonical_apply_identity"]
    if (
        not isinstance(transition, Mapping)
        or set(transition) != {"before", "intent", "after", "impacts"}
        or not isinstance(eligibility, Mapping)
        or eligibility.get("transition_valid") is not True
        or eligibility.get("applicable") is not True
        or not isinstance(identity, Mapping)
        or set(identity) != {"batch_key", "actor", "reason"}
    ):
        raise AssignmentLeaveResolutionApplicationError(
            code="batch_mutation_not_authorized",
            reason="batch mutation is not authorized",
            details={"field": "apply_authorization"},
        )
    after = transition["after"]
    conflict_snapshot = locked_facts["conflict_snapshot"]
    if not isinstance(after, Mapping) or not isinstance(conflict_snapshot, Mapping):
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "transition"},
        )
    raw_assignments = conflict_snapshot.get("assignments")
    raw_schedules = conflict_snapshot.get("assignment_schedule_days")
    raw_locks = conflict_snapshot.get("active_lock_days")
    if not all(isinstance(value, list) for value in (raw_assignments, raw_schedules, raw_locks)):
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "conflict_snapshot"},
        )

    active = sorted(
        (
            dict(row)
            for row in raw_assignments
            if isinstance(row, Mapping) and row.get("status") != "cancelled"
        ),
        key=lambda row: (_as_date(row["assigned_start_date"], "assigned_start_date"), row["id"]),
    )
    if not active:
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "assignments"},
        )
    current_id_by_ref = {
        f"current:{index}": _as_positive_int(row["id"], "assignment id")
        for index, row in enumerate(active)
    }
    staff_ids = {
        _as_positive_int(row["staff_id"], "assignment staff_id")
        for row in active
    }
    for row in raw_schedules:
        if isinstance(row, Mapping):
            staff_ids.add(_as_positive_int(row.get("staff_id"), "schedule staff_id"))
    for row in raw_locks:
        if isinstance(row, Mapping):
            staff_ids.add(_as_positive_int(row.get("staff_id"), "lock staff_id"))
    lock_identity = locked_facts["lock_identity"]
    if not isinstance(lock_identity, Mapping) or not isinstance(
        lock_identity.get("staff_ids"), list
    ):
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "lock_identity"},
        )
    staff_ids.update(
        _as_positive_int(staff_id, "lock identity staff_id")
        for staff_id in lock_identity["staff_ids"]
    )
    canonical_intent = apply_authorization["canonical_intent"]
    if not isinstance(canonical_intent, Mapping):
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "canonical_intent"},
        )
    raw_items = canonical_intent.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "canonical_intent.items"},
        )
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "canonical_intent.items"},
            )
    raw_double_pay_preferences = apply_authorization["double_pay_preferences"]
    if not isinstance(raw_double_pay_preferences, list):
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "double_pay_preferences"},
        )
    double_pay_by_item_ref: dict[int, bool] = {}
    for preference in raw_double_pay_preferences:
        if (
            not isinstance(preference, Mapping)
            or set(preference) != {"item_ref", "is_double_pay"}
            or isinstance(preference["item_ref"], bool)
            or not isinstance(preference["item_ref"], int)
            or preference["item_ref"] < 0
            or type(preference["is_double_pay"]) is not bool
            or preference["item_ref"] in double_pay_by_item_ref
        ):
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "double_pay_preferences"},
            )
        double_pay_by_item_ref[preference["item_ref"]] = preference["is_double_pay"]
    if set(double_pay_by_item_ref) != set(range(len(raw_items))):
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "double_pay_preferences"},
        )
    deferred_service_days = {
        _as_date(item.get("service_day"), "canonical_intent service_day")
        for item in raw_items
        if item.get("resolution") == "defer"
    }
    for row in raw_assignments:
        if isinstance(row, Mapping):
            staff_ids.add(_as_positive_int(row.get("staff_id"), "assignment staff_id"))
    caregiver_ref_to_staff = {
        f"caregiver:{index}": staff_id
        for index, staff_id in enumerate(sorted(staff_ids))
    }

    segments = after.get("segments")
    ownership = after.get("daily_ownership")
    commitment = after.get("service_commitment")
    if (
        not isinstance(segments, list)
        or not isinstance(ownership, list)
        or not isinstance(commitment, Mapping)
    ):
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "after_service_plan"},
        )
    hours_per_day = Decimal(str(commitment.get("hours_per_service_day")))
    if not hours_per_day.is_finite() or hours_per_day <= 0:
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "service_commitment"},
        )
    ownership_counts: dict[str, int] = {}
    ownership_by_date: dict[str, str] = {}
    for row in ownership:
        if not isinstance(row, Mapping):
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "daily_ownership"},
            )
        service_day = _as_date(row.get("service_day"), "service_day").isoformat()
        if _as_date(row.get("service_day"), "service_day") in deferred_service_days:
            continue
        segment_ref = row.get("segment_ref")
        if not isinstance(segment_ref, str) or service_day in ownership_by_date:
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "daily_ownership"},
            )
        ownership_by_date[service_day] = segment_ref
        ownership_counts[segment_ref] = ownership_counts.get(segment_ref, 0) + 1

    projected = []
    seen_refs = set()
    for sequence, segment in enumerate(
        sorted(
            segments,
            key=lambda row: (
                _as_date(row["service_period"]["start"], "segment start"),
                str(row["segment_ref"]),
            ),
        ),
        1,
    ):
        if not isinstance(segment, Mapping):
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "segments"},
            )
        segment_ref = segment.get("segment_ref")
        caregiver_ref = segment.get("caregiver_ref")
        period = segment.get("service_period")
        if (
            not isinstance(segment_ref, str)
            or segment_ref in seen_refs
            or not isinstance(caregiver_ref, str)
            or caregiver_ref not in caregiver_ref_to_staff
            or not isinstance(period, Mapping)
        ):
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "segments"},
            )
        seen_refs.add(segment_ref)
        start = _as_date(period.get("start"), "segment start")
        end = _as_date(period.get("end"), "segment end")
        if end < start:
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "segments"},
            )
        projected.append(
            {
                "segment_ref": segment_ref,
                "existing_assignment_id": current_id_by_ref.get(segment_ref),
                "staff_id": caregiver_ref_to_staff[caregiver_ref],
                "assignment_sequence": sequence,
                "assigned_start_date": start,
                "assigned_end_date": end,
                "status": segment.get("status"),
                "segment_kind": segment.get("segment_kind"),
                "lineage": deepcopy(segment.get("lineage")),
                "actual_hours": Decimal(ownership_counts.get(segment_ref, 0))
                * hours_per_day,
            }
        )
    if set(ownership_counts) - seen_refs or not 1 <= len(projected) <= 4:
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "after_service_plan"},
        )
    cancelled_ids = sorted(
        set(current_id_by_ref.values())
        - {
            row["existing_assignment_id"]
            for row in projected
            if row["existing_assignment_id"] is not None
        }
    )
    original_assignment = locked_facts["original_assignment_schedule"].get("assignment")
    if not isinstance(original_assignment, Mapping):
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "original_assignment_schedule"},
        )
    original_assignment_id = _as_positive_int(
        original_assignment.get("id"), "original_assignment_id"
    )
    case_no = lock_identity.get("case_no")
    if (
        not isinstance(case_no, str)
        or not case_no
        or original_assignment.get("case_no") != case_no
    ):
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "original_assignment_schedule"},
        )
    original_schedule_by_day: dict[date, int] = {}
    for row in locked_facts["original_assignment_schedule"].get("schedule_days", []):
        if not isinstance(row, Mapping):
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "original_assignment_schedule"},
            )
        schedule_day = _as_date(row.get("work_date"), "schedule work_date")
        schedule_id = _as_positive_int(row.get("id"), "original_schedule_id")
        if schedule_day in original_schedule_by_day:
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "original_assignment_schedule"},
            )
        original_schedule_by_day[schedule_day] = schedule_id
    mutation_items = []
    for item in raw_items:
        service_day = _as_date(item.get("service_day"), "canonical_intent service_day")
        item_ref = item.get("item_ref")
        if isinstance(item_ref, bool) or not isinstance(item_ref, int) or item_ref < 0:
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "canonical_intent.items"},
            )
        resolution = item.get("resolution")
        if resolution not in {"defer", "substitute"}:
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "canonical_intent.items"},
            )
        schedule_id = original_schedule_by_day.get(service_day)
        if schedule_id is None:
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "canonical_intent.items"},
            )
        substitute_ref = f"substitute:{item_ref}" if resolution == "substitute" else None
        if substitute_ref is not None and substitute_ref not in seen_refs:
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "canonical_intent.items"},
            )
        mutation_items.append(
            {
                "batch_item_index": item_ref,
                "original_schedule_id": schedule_id,
                "work_date": service_day,
                "resolution_type": (
                    "substitute"
                    if resolution == "substitute"
                    else "defer_following_assignments"
                ),
                "substitute_segment_ref": substitute_ref,
                "is_double_pay": double_pay_by_item_ref[item_ref],
            }
        )
    mutation_items.sort(
        key=lambda item: (item["work_date"], item["original_schedule_id"])
    )
    if [item["batch_item_index"] for item in mutation_items] != list(
        range(len(mutation_items))
    ):
        raise AssignmentLeaveResolutionDataIntegrityError(
            code="invalid_batch_mutation_projection_facts",
            reason="batch mutation projection facts are invalid",
            details={"source": "canonical_intent.items"},
        )
    existing_double_pay_by_date: dict[str, bool] = {}
    for row in raw_schedules:
        if not isinstance(row, Mapping) or row.get("case_no") != case_no:
            continue
        schedule_day = _as_date(row.get("work_date"), "schedule work_date").isoformat()
        is_double_pay = row.get("is_double_pay")
        if type(is_double_pay) is not bool:
            raise AssignmentLeaveResolutionDataIntegrityError(
                code="invalid_batch_mutation_projection_facts",
                reason="batch mutation projection facts are invalid",
                details={"source": "conflict_snapshot.assignment_schedule_days"},
            )
        if schedule_day in ownership_by_date:
            if (
                schedule_day in existing_double_pay_by_date
                and existing_double_pay_by_date[schedule_day] != is_double_pay
            ):
                raise AssignmentLeaveResolutionDataIntegrityError(
                    code="invalid_batch_mutation_projection_facts",
                    reason="batch mutation projection facts are invalid",
                    details={"source": "conflict_snapshot.assignment_schedule_days"},
                )
            existing_double_pay_by_date[schedule_day] = is_double_pay
    return {
        "batch_key": identity["batch_key"],
        "case_no": case_no,
        "original_assignment_id": original_assignment_id,
        "actor": identity["actor"],
        "reason": identity["reason"],
        "preview_fingerprint": apply_authorization["preview_fingerprint"],
        "items": mutation_items,
        "assignments": projected,
        "cancelled_assignment_ids": cancelled_ids,
        "ownership_by_date": ownership_by_date,
        "existing_double_pay_by_date": existing_double_pay_by_date,
        "hours_per_day": hours_per_day,
    }


def execute_assignment_leave_resolution_batch_mutations(
    cursor: Any,
    mutation_command: Mapping[str, Any],
) -> Dict[str, Any]:
    """Apply one authorised batch command using the caller-owned transaction."""
    from services.assignment_payroll_reconciliation_service import (
        reconcile_assignment_payroll_with_cursor,
    )

    if cursor is None or not all(
        callable(getattr(cursor, method, None))
        for method in ("execute", "fetchone", "fetchall")
    ):
        raise ValueError("cursor must be a caller-owned DictCursor")
    expected_fields = {
        "batch_key",
        "case_no",
        "original_assignment_id",
        "actor",
        "reason",
        "preview_fingerprint",
        "items",
        "assignments",
        "cancelled_assignment_ids",
        "ownership_by_date",
        "existing_double_pay_by_date",
        "hours_per_day",
    }
    if not isinstance(mutation_command, Mapping) or set(mutation_command) != expected_fields:
        raise ValueError("batch mutation command is invalid")
    case_no = mutation_command["case_no"]
    if not isinstance(case_no, str) or not case_no:
        raise ValueError("batch mutation case_no is invalid")
    original_assignment_id = _as_positive_int(
        mutation_command["original_assignment_id"], "original_assignment_id"
    )
    for field in ("batch_key", "actor", "reason"):
        if (
            not isinstance(mutation_command[field], str)
            or not mutation_command[field]
            or mutation_command[field] != mutation_command[field].strip()
        ):
            raise ValueError(f"batch mutation {field} is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", mutation_command["preview_fingerprint"]) is None:
        raise ValueError("batch mutation preview_fingerprint is invalid")
    hours_per_day = Decimal(str(mutation_command["hours_per_day"]))
    if not hours_per_day.is_finite() or hours_per_day <= 0:
        raise ValueError("batch mutation hours_per_day is invalid")

    raw_assignments = mutation_command["assignments"]
    raw_items = mutation_command["items"]
    raw_ownership = mutation_command["ownership_by_date"]
    raw_double_pay = mutation_command["existing_double_pay_by_date"]
    raw_cancelled = mutation_command["cancelled_assignment_ids"]
    if (
        not isinstance(raw_assignments, list)
        or not 1 <= len(raw_assignments) <= 4
        or not isinstance(raw_items, list)
        or not raw_items
        or not isinstance(raw_ownership, Mapping)
        or not isinstance(raw_double_pay, Mapping)
        or not isinstance(raw_cancelled, list)
    ):
        raise ValueError("batch mutation command collections are invalid")

    assignments = []
    assignment_by_ref: dict[str, dict[str, Any]] = {}
    existing_ids: set[int] = set()
    for row in raw_assignments:
        if not isinstance(row, Mapping) or set(row) != {
            "segment_ref",
            "existing_assignment_id",
            "staff_id",
            "assignment_sequence",
            "assigned_start_date",
            "assigned_end_date",
            "status",
            "segment_kind",
            "lineage",
            "actual_hours",
        }:
            raise ValueError("batch mutation assignment row is invalid")
        segment_ref = row["segment_ref"]
        if (
            not isinstance(segment_ref, str)
            or not segment_ref
            or segment_ref in assignment_by_ref
        ):
            raise ValueError("batch mutation segment_ref is invalid")
        existing_id = row["existing_assignment_id"]
        if existing_id is not None:
            existing_id = _as_positive_int(existing_id, "existing_assignment_id")
            if existing_id in existing_ids:
                raise ValueError("batch mutation existing assignment is duplicated")
            existing_ids.add(existing_id)
        staff_id = _as_positive_int(row["staff_id"], "staff_id")
        sequence = _as_positive_int(row["assignment_sequence"], "assignment_sequence")
        start = _as_date(row["assigned_start_date"], "assigned_start_date")
        end = _as_date(row["assigned_end_date"], "assigned_end_date")
        if end < start or row["status"] not in {
            "planned", "active", "completed", "replaced"
        }:
            raise ValueError("batch mutation assignment interval/status is invalid")
        if row["segment_kind"] not in {
            "formal", "single_day_substitute", "substitute"
        }:
            raise ValueError("batch mutation segment_kind is invalid")
        actual_hours = Decimal(str(row["actual_hours"]))
        if not actual_hours.is_finite() or actual_hours < 0:
            raise ValueError("batch mutation actual_hours is invalid")
        canonical = {
            **dict(row),
            "existing_assignment_id": existing_id,
            "staff_id": staff_id,
            "assignment_sequence": sequence,
            "assigned_start_date": start,
            "assigned_end_date": end,
            "actual_hours": actual_hours,
        }
        assignments.append(canonical)
        assignment_by_ref[segment_ref] = canonical
    assignments.sort(key=lambda row: row["assignment_sequence"])
    if [row["assignment_sequence"] for row in assignments] != list(
        range(1, len(assignments) + 1)
    ):
        raise ValueError("batch mutation assignment sequence is invalid")

    cancelled_ids = sorted(
        {_as_positive_int(value, "cancelled_assignment_id") for value in raw_cancelled}
    )
    if existing_ids & set(cancelled_ids):
        raise ValueError("batch mutation assignment cannot be active and cancelled")
    ownership_by_date: dict[date, str] = {}
    ownership_counts: dict[str, int] = {}
    for raw_day, segment_ref in raw_ownership.items():
        day = _as_date(raw_day, "ownership work_date")
        if (
            not isinstance(segment_ref, str)
            or segment_ref not in assignment_by_ref
            or day in ownership_by_date
        ):
            raise ValueError("batch mutation ownership is invalid")
        owner = assignment_by_ref[segment_ref]
        if not (owner["assigned_start_date"] <= day <= owner["assigned_end_date"]):
            raise ValueError("batch mutation ownership is outside assignment interval")
        ownership_by_date[day] = segment_ref
        ownership_counts[segment_ref] = ownership_counts.get(segment_ref, 0) + 1
    if not ownership_by_date:
        raise ValueError("batch mutation ownership is empty")
    double_pay_by_date: dict[date, bool] = {}
    for raw_day, value in raw_double_pay.items():
        day = _as_date(raw_day, "double pay work_date")
        if day not in ownership_by_date or type(value) is not bool:
            raise ValueError("batch mutation double-pay override is invalid")
        double_pay_by_date[day] = value
    for row in assignments:
        expected_hours = Decimal(ownership_counts.get(row["segment_ref"], 0)) * hours_per_day
        if row["actual_hours"] != expected_hours:
            raise ValueError("batch mutation actual_hours does not match ownership")

    items = []
    seen_item_dates: set[date] = set()
    seen_schedule_ids: set[int] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping) or set(raw_item) != {
            "batch_item_index",
            "original_schedule_id",
            "work_date",
            "resolution_type",
            "substitute_segment_ref",
            "is_double_pay",
        }:
            raise ValueError("batch mutation item is invalid")
        ordinal = raw_item["batch_item_index"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise ValueError("batch mutation item index is invalid")
        schedule_id = _as_positive_int(
            raw_item["original_schedule_id"], "original_schedule_id"
        )
        work_date = _as_date(raw_item["work_date"], "work_date")
        if schedule_id in seen_schedule_ids or work_date in seen_item_dates:
            raise ValueError("batch mutation item is duplicated")
        resolution_type = raw_item["resolution_type"]
        substitute_ref = raw_item["substitute_segment_ref"]
        is_double_pay = raw_item["is_double_pay"]
        if type(is_double_pay) is not bool:
            raise ValueError("batch mutation item double-pay flag is invalid")
        if resolution_type == "substitute":
            if (
                not isinstance(substitute_ref, str)
                or substitute_ref not in assignment_by_ref
                or assignment_by_ref[substitute_ref]["segment_kind"]
                not in {"single_day_substitute", "substitute"}
                or ownership_by_date.get(work_date) != substitute_ref
            ):
                raise ValueError("batch mutation substitute item is invalid")
        elif resolution_type == "defer_following_assignments":
            if (
                substitute_ref is not None
                or work_date in ownership_by_date
                or is_double_pay
            ):
                raise ValueError("batch mutation defer item is invalid")
        else:
            raise ValueError("batch mutation resolution_type is invalid")
        seen_schedule_ids.add(schedule_id)
        seen_item_dates.add(work_date)
        items.append(
            {
                "batch_item_index": ordinal,
                "original_schedule_id": schedule_id,
                "work_date": work_date,
                "resolution_type": resolution_type,
                "substitute_segment_ref": substitute_ref,
                "is_double_pay": is_double_pay,
            }
        )
    items.sort(key=lambda item: item["batch_item_index"])
    if [item["batch_item_index"] for item in items] != list(range(len(items))):
        raise ValueError("batch mutation item ordinals are invalid")
    for item in items:
        if item["resolution_type"] == "substitute":
            double_pay_by_date[item["work_date"]] = item["is_double_pay"]

    cursor.execute(
        """SELECT id, staff_id, hourly_rate
             FROM case_staff_assignments
            WHERE id = %s AND case_no = %s""",
        (original_assignment_id, case_no),
    )
    original = cursor.fetchone()
    if not isinstance(original, Mapping) or original.get("id") != original_assignment_id:
        raise ValueError("batch mutation original assignment ownership mismatch")
    original_staff_id = _as_positive_int(original.get("staff_id"), "original staff_id")

    requested_schedule_ids = tuple(item["original_schedule_id"] for item in items)
    schedule_placeholders = ", ".join(["%s"] * len(requested_schedule_ids))
    cursor.execute(
        f"""SELECT id, assignment_id, case_no, staff_id, work_date
              FROM staff_schedule
             WHERE id IN ({schedule_placeholders})
             ORDER BY id ASC""",
        requested_schedule_ids,
    )
    locked_schedule_rows = [dict(row) for row in (cursor.fetchall() or [])]
    locked_schedule_by_id = {
        _as_positive_int(row.get("id"), "schedule id"): row
        for row in locked_schedule_rows
        if isinstance(row, Mapping)
    }
    if set(locked_schedule_by_id) != set(requested_schedule_ids):
        raise ValueError("batch mutation original schedule is missing")
    for item in items:
        row = locked_schedule_by_id[item["original_schedule_id"]]
        if (
            row.get("assignment_id") != original_assignment_id
            or row.get("case_no") != case_no
            or row.get("staff_id") != original_staff_id
            or _as_date(row.get("work_date"), "schedule work_date")
            != item["work_date"]
        ):
            raise ValueError("batch mutation original schedule ownership mismatch")

    cursor.execute(
        "UPDATE case_staff_assignments SET assignment_sequence = -id WHERE case_no = %s",
        (case_no,),
    )
    ref_to_id: dict[str, int] = {}
    for row in assignments:
        existing_id = row["existing_assignment_id"]
        if existing_id is not None:
            cursor.execute(
                """UPDATE case_staff_assignments
                      SET staff_id = %s, assignment_sequence = %s,
                          assigned_start_date = %s, assigned_end_date = %s,
                          planned_hours = %s, actual_hours = %s, status = %s
                    WHERE id = %s AND case_no = %s""",
                (
                    row["staff_id"],
                    row["assignment_sequence"],
                    row["assigned_start_date"],
                    row["assigned_end_date"],
                    row["actual_hours"],
                    row["actual_hours"],
                    row["status"],
                    existing_id,
                    case_no,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("batch mutation assignment update failed")
            ref_to_id[row["segment_ref"]] = existing_id
            continue
        cursor.execute(
            """INSERT INTO case_staff_assignments
                   (case_no, staff_id, assignment_sequence,
                    assigned_start_date, assigned_end_date, planned_hours,
                    actual_hours, hourly_rate, floor_fee_allocated, status,
                    replacement_reason, replaced_assignment_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0.00, %s, %s, %s)""",
            (
                case_no,
                row["staff_id"],
                row["assignment_sequence"],
                row["assigned_start_date"],
                row["assigned_end_date"],
                row["actual_hours"],
                row["actual_hours"],
                original.get("hourly_rate"),
                row["status"],
                "multi-date leave resolution",
                original_assignment_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("batch mutation assignment insert failed")
        ref_to_id[row["segment_ref"]] = _as_positive_int(
            cursor.lastrowid, "created assignment id"
        )

    next_cancelled_sequence = len(assignments) + 1
    cursor.execute(
        """SELECT id FROM case_staff_assignments
            WHERE case_no = %s AND assignment_sequence < 0
            ORDER BY id ASC""",
        (case_no,),
    )
    inactive_ids = [
        _as_positive_int(row.get("id"), "inactive assignment id")
        for row in (cursor.fetchall() or [])
        if isinstance(row, Mapping)
    ]
    for assignment_id in inactive_ids:
        cursor.execute(
            """UPDATE case_staff_assignments
                  SET assignment_sequence = %s,
                      actual_hours = 0.00,
                      floor_fee_allocated = 0.00,
                      status = 'cancelled'
                WHERE id = %s AND case_no = %s""",
            (next_cancelled_sequence, assignment_id, case_no),
        )
        if cursor.rowcount != 1:
            raise ValueError("batch mutation assignment cancellation failed")
        next_cancelled_sequence += 1
    if not set(cancelled_ids).issubset(set(inactive_ids)):
        raise ValueError("batch mutation cancelled assignment set changed")

    scope_dates = set(ownership_by_date) | {item["work_date"] for item in items}
    scope_start, scope_end = min(scope_dates), max(scope_dates)
    for day in ownership_by_date:
        double_pay_by_date.setdefault(day, False)
    cursor.execute(
        """UPDATE staff_schedule
              SET is_work_day = FALSE, is_double_pay = FALSE
            WHERE case_no = %s AND assignment_id IS NOT NULL
              AND work_date BETWEEN %s AND %s""",
        (case_no, scope_start, scope_end),
    )
    for day, segment_ref in sorted(ownership_by_date.items()):
        owner = assignment_by_ref[segment_ref]
        cursor.execute(
            """INSERT INTO staff_schedule
                   (assignment_id, case_no, staff_id, work_date,
                    is_work_day, is_double_pay)
               VALUES (%s, %s, %s, %s, TRUE, %s)
               ON DUPLICATE KEY UPDATE
                   assignment_id = VALUES(assignment_id),
                   case_no = VALUES(case_no),
                   staff_id = VALUES(staff_id),
                   is_work_day = TRUE,
                   is_double_pay = VALUES(is_double_pay)""",
            (
                ref_to_id[segment_ref],
                case_no,
                owner["staff_id"],
                day,
                double_pay_by_date[day],
            ),
        )

    cursor.execute(
        """SELECT id, staff_id, assignment_sequence, assigned_start_date,
                  assigned_end_date, status, actual_hours
             FROM case_staff_assignments
            WHERE case_no = %s AND status <> 'cancelled'
            ORDER BY assignment_sequence ASC""",
        (case_no,),
    )
    readback = [dict(row) for row in (cursor.fetchall() or [])]
    if len(readback) != len(assignments):
        raise ValueError("batch mutation assignment readback count changed")
    assignment_snapshot = []
    for expected, actual in zip(assignments, readback):
        assignment_id = ref_to_id[expected["segment_ref"]]
        actual_hours = Decimal(str(actual.get("actual_hours")))
        if (
            actual.get("id") != assignment_id
            or actual.get("staff_id") != expected["staff_id"]
            or actual.get("assignment_sequence") != expected["assignment_sequence"]
            or _as_date(actual.get("assigned_start_date"), "assigned_start_date")
            != expected["assigned_start_date"]
            or _as_date(actual.get("assigned_end_date"), "assigned_end_date")
            != expected["assigned_end_date"]
            or actual.get("status") != expected["status"]
            or actual_hours != expected["actual_hours"]
        ):
            raise ValueError("batch mutation assignment readback changed")
        assignment_snapshot.append(
            {
                "id": assignment_id,
                "segment_ref": expected["segment_ref"],
                "case_no": case_no,
                "staff_id": expected["staff_id"],
                "assignment_sequence": expected["assignment_sequence"],
                "assigned_start_date": expected["assigned_start_date"],
                "assigned_end_date": expected["assigned_end_date"],
                "status": expected["status"],
                "actual_hours": str(actual_hours),
            }
        )

    cursor.execute(
        """SELECT id, assignment_id, staff_id, work_date,
                  is_work_day, is_double_pay
             FROM staff_schedule
            WHERE case_no = %s AND work_date BETWEEN %s AND %s""",
        (case_no, scope_start, scope_end),
    )
    schedule_rows = [dict(row) for row in (cursor.fetchall() or [])]
    expected_work = {
        (ref_to_id[segment_ref], day, assignment_by_ref[segment_ref]["staff_id"])
        for day, segment_ref in ownership_by_date.items()
    }
    found_work = set()
    for row in schedule_rows:
        if not row.get("is_work_day"):
            continue
        key = (
            _as_positive_int(row.get("assignment_id"), "assignment_id"),
            _as_date(row.get("work_date"), "work_date"),
            _as_positive_int(row.get("staff_id"), "staff_id"),
        )
        if (
            key not in expected_work
            or bool(row.get("is_double_pay")) != double_pay_by_date[key[1]]
        ):
            raise ValueError("batch mutation stale schedule ownership remains")
        found_work.add(key)
    if found_work != expected_work:
        raise ValueError("batch mutation schedule ownership gap detected")
    for item in items:
        schedule = next(
            (
                row
                for row in schedule_rows
                if row.get("id") == item["original_schedule_id"]
            ),
            None,
        )
        if not isinstance(schedule, Mapping) or schedule.get("is_work_day"):
            raise ValueError("batch mutation original leave schedule remains active")

    schedule_snapshot = {
        "batch_key": mutation_command["batch_key"],
        "assignments": assignment_snapshot,
        "ownership_by_date": {
            day.isoformat(): ref_to_id[segment_ref]
            for day, segment_ref in sorted(ownership_by_date.items())
        },
        "double_pay_by_date": {
            day.isoformat(): value
            for day, value in sorted(double_pay_by_date.items())
        },
        "leave_days": [
            {
                "batch_item_index": item["batch_item_index"],
                "original_schedule_id": item["original_schedule_id"],
                "work_date": item["work_date"].isoformat(),
                "resolution_type": item["resolution_type"],
            }
            for item in items
        ],
    }
    pending_events = []
    payroll_snapshots = []
    for item in items:
        substitute_id = (
            None
            if item["substitute_segment_ref"] is None
            else ref_to_id[item["substitute_segment_ref"]]
        )
        event_key = "batch-" + hashlib.sha256(
            f"{mutation_command['batch_key']}:{item['batch_item_index']}".encode(
                "utf-8"
            )
        ).hexdigest()
        pending_substitution = None
        if substitute_id is not None:
            prefix_candidates = [
                row
                for row in assignments
                if row["segment_kind"] == "formal"
                and row["staff_id"] == original_staff_id
                and row["assigned_end_date"] < item["work_date"]
            ]
            suffix_candidates = [
                row
                for row in assignments
                if row["segment_kind"] == "formal"
                and row["staff_id"] == original_staff_id
                and row["assigned_start_date"] > item["work_date"]
            ]
            pending_substitution = {
                "case_no": case_no,
                "event_key": event_key,
                "original_assignment_id": original_assignment_id,
                "original_schedule_id": item["original_schedule_id"],
                "work_date": item["work_date"],
                "resolution_type": "substitute",
                "substitute_assignment_id": substitute_id,
                "prefix_assignment_id": (
                    None
                    if not prefix_candidates
                    else ref_to_id[
                        max(
                            prefix_candidates,
                            key=lambda row: row["assigned_end_date"],
                        )["segment_ref"]
                    ]
                ),
                "suffix_assignment_id": (
                    None
                    if not suffix_candidates
                    else ref_to_id[
                        min(
                            suffix_candidates,
                            key=lambda row: row["assigned_start_date"],
                        )["segment_ref"]
                    ]
                ),
            }
        reconciliation = reconcile_assignment_payroll_with_cursor(
            cursor,
            case_no,
            pending_substitution_event=pending_substitution,
        )
        if (
            reconciliation.get("errors")
            or reconciliation.get("can_create_staff_payments") is not True
        ):
            raise ValueError("batch mutation payroll reconciliation failed")
        payroll_snapshots.append(reconciliation)
        pending_events.append(
            {
                "batch_key": mutation_command["batch_key"],
                "batch_item_index": item["batch_item_index"],
                "case_no": case_no,
                "original_assignment_id": original_assignment_id,
                "original_schedule_id": item["original_schedule_id"],
                "work_date": item["work_date"].isoformat(),
                "resolution_type": item["resolution_type"],
                "substitute_assignment_id": substitute_id,
                "event_key": event_key,
                "actor": mutation_command["actor"],
                "reason": mutation_command["reason"],
                "schedule_snapshot": deepcopy(schedule_snapshot),
                "payroll_snapshot": reconciliation,
            }
        )
    return {
        "assignments": assignment_snapshot,
        "schedule_snapshot": schedule_snapshot,
        "payroll_snapshots": payroll_snapshots,
        "pending_event_payloads": pending_events,
    }


def apply_assignment_leave_resolution_batch(request: Mapping[str, Any]) -> Dict[str, Any]:
    """Apply one multi-date leave batch atomically and support exact retries."""
    connection = None
    cursor = None

    def close_resource(resource: Any) -> None:
        closer = getattr(resource, "close", None)
        if callable(closer):
            try:
                closer()
            except BaseException:
                pass

    try:
        envelope = canonicalize_assignment_leave_resolution_batch_apply_envelope(
            request
        )
        connection = get_connection()
        cursor = connection.cursor()
        replay_snapshot = read_assignment_leave_resolution_batch_replay_snapshot(
            cursor, envelope["batch_key"], True
        )
        replay_decision = decide_assignment_leave_resolution_batch_replay(
            replay_snapshot, envelope["replay_identity_seed"]
        )
        if replay_decision["status"] == "idempotent_replay":
            connection.rollback()
            return replay_decision["replay_result"]
        if replay_decision["status"] != "absent":
            raise ValueError("batch replay decision is invalid")

        locked_facts = acquire_assignment_leave_resolution_batch_locked_facts(
            cursor, envelope["preview_request"]
        )
        authorization = authorize_assignment_leave_resolution_batch_apply(
            envelope["preview_request"],
            envelope["requested_preview_fingerprint"],
            {
                "batch_key": envelope["batch_key"],
                "actor": envelope["actor"],
                "reason": envelope["reason"],
            },
            locked_facts,
        )
        if authorization["status"] == "rejected":
            connection.rollback()
            return {
                "status": "rejected",
                "business_conflicts": authorization["business_conflicts"],
            }
        if authorization["status"] != "apply":
            raise ValueError("batch authorization decision is invalid")
        mutation_command = build_assignment_leave_resolution_batch_mutation_command(
            authorization["apply_authorization"], locked_facts
        )
        mutation_result = execute_assignment_leave_resolution_batch_mutations(
            cursor, mutation_command
        )
        request_snapshot = envelope["preview_request"]
        cursor.execute(
            """INSERT INTO assignment_schedule_leave_substitution_batches
                   (batch_key, case_no, preview_fingerprint, item_count,
                    actor, reason, request_snapshot)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                envelope["batch_key"],
                request_snapshot["case_no"],
                envelope["requested_preview_fingerprint"],
                len(request_snapshot["items"]),
                envelope["actor"],
                envelope["reason"],
                json.dumps(
                    request_snapshot,
                    ensure_ascii=False,
                    default=str,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("batch header insert failed")
        event_ids = []
        for event in mutation_result["pending_event_payloads"]:
            cursor.execute(
                """INSERT INTO assignment_schedule_leave_substitution_events
                       (batch_key, batch_item_index, case_no,
                        original_assignment_id, original_schedule_id, work_date,
                        resolution_type, substitute_assignment_id, event_key,
                        actor, reason, schedule_snapshot, payroll_snapshot)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    event["batch_key"],
                    event["batch_item_index"],
                    event["case_no"],
                    event["original_assignment_id"],
                    event["original_schedule_id"],
                    _as_date(event["work_date"], "event work_date"),
                    event["resolution_type"],
                    event["substitute_assignment_id"],
                    event["event_key"],
                    event["actor"],
                    event["reason"],
                    json.dumps(
                        event["schedule_snapshot"],
                        ensure_ascii=False,
                        default=str,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        event["payroll_snapshot"],
                        ensure_ascii=False,
                        default=str,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("batch event insert failed")
            event_ids.append(_as_positive_int(cursor.lastrowid, "event_id"))
        connection.commit()
        return {
            "status": "applied",
            "batch_key": envelope["batch_key"],
            "event_ids": event_ids,
            "events": mutation_result["pending_event_payloads"],
            "assignments": mutation_result["assignments"],
            "schedule_snapshot": mutation_result["schedule_snapshot"],
            "payroll_snapshots": mutation_result["payroll_snapshots"],
        }
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except BaseException:
                pass
        raise
    finally:
        close_resource(cursor)
        close_resource(connection)


def prepare_assignment_leave_resolution_apply(
    cursor: Any,
    request: Any,
) -> Dict[str, Any]:
    """Lock canonical facts and prepare a write-free leave resolution command."""
    from collections.abc import Mapping

    from services.assignment_schedule_leave_resolution_preview import (
        compute_assignment_leave_resolution_preview_from_snapshot,
    )
    from services.multi_caregiver_schedule_read import (
        get_case_schedule_conflict_snapshot_with_cursor,
    )
    from services.staff_occupancy_mutex_service import lock_staff_occupancy_mutex

    if cursor is None or not callable(getattr(cursor, "execute", None)):
        raise ValueError("cursor must support execute")
    if not isinstance(request, Mapping):
        raise ValueError("request must be a mapping")
    allowed_fields = {
        "case_no",
        "original_assignment_id",
        "original_schedule_id",
        "work_date",
        "resolution_type",
        "substitute_staff_id",
        "preview_fingerprint",
        "event_key",
        "actor",
        "reason",
    }
    if set(request) - allowed_fields:
        raise ValueError("request contains server-derived or unsupported fields")

    case_no_value = request.get("case_no")
    if not isinstance(case_no_value, str) or not case_no_value.strip():
        raise ValueError("case_no must be a non-empty string")
    case_no = case_no_value.strip()
    assignment_id = _as_positive_int(
        request.get("original_assignment_id"), "original_assignment_id"
    )
    schedule_id = _as_positive_int(
        request.get("original_schedule_id"), "original_schedule_id"
    )
    work_date = _as_date(
        _as_rest_date_string(request.get("work_date"), "work_date"), "work_date"
    )
    resolution_type = request.get("resolution_type")
    if resolution_type not in {"defer_following_assignments", "substitute"}:
        raise ValueError(
            "resolution_type must be defer_following_assignments or substitute"
        )
    substitute_value = request.get("substitute_staff_id")
    if resolution_type == "substitute":
        substitute_staff_id = _as_positive_int(
            substitute_value, "substitute_staff_id"
        )
    elif substitute_value is not None:
        raise ValueError("substitute_staff_id must be null when deferring assignments")
    else:
        substitute_staff_id = None

    fingerprint = request.get("preview_fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("preview_fingerprint must be 64 lowercase hexadecimal characters")
    identity_text: dict[str, str] = {}
    for field in ("event_key", "actor", "reason"):
        value = request.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        identity_text[field] = value.strip()

    canonical_request = {
        "case_no": case_no,
        "original_assignment_id": assignment_id,
        "original_schedule_id": schedule_id,
        "work_date": work_date.isoformat(),
        "resolution_type": resolution_type,
        "substitute_staff_id": substitute_staff_id,
    }
    request_identity = {
        **canonical_request,
        "preview_fingerprint": fingerprint,
        **identity_text,
    }

    cursor.execute(
        """SELECT case_no, service_hours_per_day
             FROM orders
            WHERE case_no = %s
            FOR UPDATE""",
        (case_no,),
    )
    order_row = cursor.fetchone()
    if not isinstance(order_row, dict) or order_row.get("case_no") != case_no:
        raise ValueError("case does not exist")

    cursor.execute(
        """SELECT id, staff_id, assigned_start_date, assigned_end_date
             FROM case_staff_assignments
            WHERE case_no = %s
            ORDER BY id ASC
            FOR UPDATE""",
        (case_no,),
    )
    assignment_lock_rows = list(cursor.fetchall() or [])
    if not assignment_lock_rows:
        raise ValueError(
            "order assignment synchronization required before single-day resolution"
        )
    assignment_ids: set[int] = set()
    staff_ids: set[int] = set()
    range_dates = [work_date]
    for row in assignment_lock_rows:
        if not isinstance(row, dict):
            raise ValueError("invalid assignment lock row")
        locked_assignment_id = _as_positive_int(row.get("id"), "assignment id")
        locked_staff_id = _as_positive_int(row.get("staff_id"), "assignment staff_id")
        if locked_assignment_id in assignment_ids:
            raise ValueError("duplicate assignment lock row")
        assignment_ids.add(locked_assignment_id)
        staff_ids.add(locked_staff_id)
        range_dates.extend(
            [
                _as_date(row.get("assigned_start_date"), "assigned_start_date"),
                _as_date(row.get("assigned_end_date"), "assigned_end_date"),
            ]
        )
    if assignment_id not in assignment_ids:
        raise ValueError("original assignment ownership mismatch")
    if substitute_staff_id is not None:
        staff_ids.add(substitute_staff_id)
    canonical_staff_ids = sorted(staff_ids)
    lock_staff_occupancy_mutex(cursor, canonical_staff_ids)

    snapshot = get_case_schedule_conflict_snapshot_with_cursor(
        cursor,
        case_no,
        canonical_staff_ids,
        min(range_dates).isoformat(),
        (max(range_dates) + timedelta(days=1)).isoformat(),
        True,
    )
    original_rows = [
        dict(row)
        for row in snapshot.get("assignments") or []
        if row.get("id") == assignment_id
    ]
    if len(original_rows) != 1:
        raise ValueError("original assignment ownership mismatch")
    original_rows[0]["service_hours_per_day"] = order_row.get(
        "service_hours_per_day"
    )
    original_schedule_rows = [
        dict(row)
        for row in snapshot.get("assignment_schedule_days") or []
        if row.get("assignment_id") == assignment_id
    ]
    if not any(row.get("id") == schedule_id for row in original_schedule_rows):
        raise ValueError(
            "original_schedule_id does not belong to original_assignment_id; "
            "use order assignment synchronization for bootstrap"
        )
    original_snapshot = {
        "assignment": original_rows[0],
        "schedule_days": original_schedule_rows,
    }
    recomputed_preview = compute_assignment_leave_resolution_preview_from_snapshot(
        canonical_request,
        original_snapshot,
        snapshot,
    )

    cursor.execute(
        """SELECT id, case_no, original_assignment_id, original_schedule_id,
                  work_date, resolution_type, event_key, actor, reason,
                  schedule_snapshot
             FROM assignment_schedule_leave_substitution_events
            WHERE event_key = %s
            FOR UPDATE""",
        (identity_text["event_key"],),
    )
    existing_event = cursor.fetchone()
    if existing_event is not None:
        if not isinstance(existing_event, dict):
            raise ValueError("invalid existing event row")
        stored_snapshot = existing_event.get("schedule_snapshot")
        if isinstance(stored_snapshot, str):
            try:
                stored_snapshot = json.loads(stored_snapshot)
            except (TypeError, ValueError) as exc:
                raise ValueError("existing event identity is invalid") from exc
        stored_identity = (
            stored_snapshot.get("request_identity")
            if isinstance(stored_snapshot, dict)
            else None
        )
        if stored_identity != request_identity:
            raise ValueError("event_key already exists with a different request identity")
        return {
            "status": "idempotent_replay",
            "existing_event_identity": {
                "id": existing_event.get("id"),
                "event_key": identity_text["event_key"],
                "request_identity": stored_identity,
            },
            "locked_snapshot": snapshot,
            "recomputed_preview": recomputed_preview,
            "mutation_command": None,
        }

    if recomputed_preview.get("preview_fingerprint") != fingerprint:
        return {
            "status": "rejected",
            "reason": "preview_fingerprint_mismatch",
            "locked_snapshot": snapshot,
            "recomputed_preview": recomputed_preview,
            "mutation_command": None,
            "existing_event_identity": None,
        }
    preview_status = recomputed_preview.get("status")
    historical_state = recomputed_preview.get("historical_fact_state")
    may_apply = preview_status == "ready" or (
        preview_status == "requires_review"
        and historical_state == "unlocked"
        and recomputed_preview.get("requires_confirmation") is True
    )
    if not may_apply:
        return {
            "status": "rejected",
            "reason": "preview_not_ready",
            "locked_snapshot": snapshot,
            "recomputed_preview": recomputed_preview,
            "mutation_command": None,
            "existing_event_identity": None,
        }
    return {
        "status": "apply",
        "locked_snapshot": snapshot,
        "recomputed_preview": recomputed_preview,
        "mutation_command": {
            "request_identity": request_identity,
            "assignment_transition_plan": recomputed_preview.get(
                "assignment_transition_plan"
            ),
            "assignment_service_impacts": recomputed_preview.get(
                "assignment_service_impacts"
            ),
            "requires_audit": recomputed_preview.get("requires_audit") is True,
        },
        "existing_event_identity": None,
    }


def execute_assignment_leave_resolution_mutations(
    cursor: Any,
    mutation_command: Any,
) -> Dict[str, Any]:
    """Apply a preflight-authorised leave mutation without owning the transaction."""
    from collections.abc import Mapping

    from services.assignment_payroll_reconciliation_service import (
        reconcile_assignment_payroll_with_cursor,
    )

    if cursor is None or not all(
        callable(getattr(cursor, method, None))
        for method in ("execute", "fetchone", "fetchall")
    ):
        raise ValueError("cursor must be a caller-owned DictCursor")
    if not isinstance(mutation_command, Mapping) or set(mutation_command) != {
        "request_identity",
        "assignment_transition_plan",
        "assignment_service_impacts",
        "requires_audit",
    }:
        raise ValueError("mutation_command must be the canonical preflight command")
    identity = mutation_command["request_identity"]
    plan = mutation_command["assignment_transition_plan"]
    impacts = mutation_command["assignment_service_impacts"]
    if not isinstance(identity, Mapping) or not isinstance(plan, Mapping):
        raise ValueError("canonical mutation command is invalid")
    if not isinstance(impacts, list) or type(mutation_command["requires_audit"]) is not bool:
        raise ValueError("canonical mutation command is invalid")

    case_no = identity.get("case_no")
    if not isinstance(case_no, str) or not case_no:
        raise ValueError("canonical case_no is invalid")
    original_assignment_id = _as_positive_int(
        identity.get("original_assignment_id"), "original_assignment_id"
    )
    original_schedule_id = _as_positive_int(
        identity.get("original_schedule_id"), "original_schedule_id"
    )
    work_date = _as_date(identity.get("work_date"), "work_date")
    resolution_type = identity.get("resolution_type")
    if resolution_type not in {"defer_following_assignments", "substitute"}:
        raise ValueError("canonical resolution_type is invalid")
    for field in ("event_key", "actor", "reason"):
        if not isinstance(identity.get(field), str) or not identity[field]:
            raise ValueError(f"canonical {field} is invalid")
    after_rows = plan.get("after_assignments")
    ownership = plan.get("ownership_by_date")
    if (
        plan.get("case_no") != case_no
        or not isinstance(after_rows, list)
        or not isinstance(ownership, Mapping)
    ):
        raise ValueError("canonical assignment transition is invalid")

    def _normalise_transition_id(value: Any, field_name: str) -> str:
        if isinstance(value, bool) or value is None:
            raise ValueError(f"canonical {field_name} is invalid")
        if isinstance(value, int):
            if value <= 0:
                raise ValueError(f"canonical {field_name} is invalid")
            return str(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError(f"canonical {field_name} is invalid")
            if text.isdigit() and int(text) <= 0:
                raise ValueError(f"canonical {field_name} is invalid")
            return text
        raise ValueError(f"canonical {field_name} is invalid")

    active_rows = [dict(row) for row in after_rows if row.get("status") != "cancelled"]
    if not 1 <= len(active_rows) <= 4:
        raise ValueError("canonical transition must contain one to four active assignments")
    seen_keys: set[str] = set()
    covered_dates: dict[date, Any] = {}
    staff_by_key: dict[str, int] = {}
    row_id_by_key: dict[str, int] = {}
    for row in active_rows:
        if not isinstance(row, Mapping):
            raise ValueError("canonical assignment row is invalid")
        row_key = _normalise_transition_id(row.get("id"), "assignment id")
        if row_key in seen_keys:
            raise ValueError("canonical assignment ids must be unique")
        seen_keys.add(row_key)
        if row.get("case_no") != case_no:
            raise ValueError("canonical assignment case ownership mismatch")
        staff_by_key[row_key] = _as_positive_int(
            row.get("staff_id"), "assignment staff_id"
        )
        start = _as_date(row.get("assigned_start_date"), "assigned_start_date")
        end = _as_date(row.get("assigned_end_date"), "assigned_end_date")
        if end < start:
            raise ValueError("canonical assignment interval is invalid")
        day = start
        while day <= end:
            if not (row_key == str(original_assignment_id) and day == work_date):
                if day in covered_dates:
                    raise ValueError("canonical transition contains overlapping ownership")
                covered_dates[day] = row_key
            day += timedelta(days=1)
        if row_key.isdigit():
            row_id_by_key[row_key] = int(row_key)
    expected_dates = sorted(covered_dates)
    if expected_dates and expected_dates != list(
        map(
            lambda offset: expected_dates[0] + timedelta(days=offset),
            range((expected_dates[-1] - expected_dates[0]).days + 1),
        )
    ):
        raise ValueError("canonical transition contains a service gap")
    canonical_ownership = {
        _as_date(key, "ownership work_date"): _normalise_transition_id(
            value, "ownership assignment id"
        )
        for key, value in ownership.items()
    }
    if resolution_type == "defer_following_assignments":
        if canonical_ownership.get(work_date) != str(original_assignment_id):
            raise ValueError("canonical deferred leave ownership is invalid")
        canonical_ownership.pop(work_date)
    unknown_ownership_ids = set(canonical_ownership.values()) - seen_keys
    if unknown_ownership_ids:
        raise ValueError("canonical transition ownership references unknown assignment id")
    if canonical_ownership != covered_dates:
        raise ValueError("canonical transition ownership does not match assignment rows")

    impact_by_key: dict[str, Decimal] = {}
    for impact in impacts:
        if not isinstance(impact, Mapping):
            raise ValueError("canonical service impact is invalid")
        key = _normalise_transition_id(impact.get("assignment_id"), "impact assignment id")
        if key in impact_by_key or key not in seen_keys:
            raise ValueError("canonical service impact ownership is invalid")
        if _as_positive_int(impact.get("staff_id"), "impact staff_id") != staff_by_key[key]:
            raise ValueError("canonical service impact staff ownership mismatch")
        hours = Decimal(str(impact.get("actual_hours")))
        if not hours.is_finite() or hours < 0:
            raise ValueError("canonical actual_hours is invalid")
        impact_by_key[key] = hours
    if set(impact_by_key) != seen_keys:
        raise ValueError("canonical service impacts are incomplete")

    cursor.execute(
        """SELECT id, staff_id, hourly_rate, floor_fee_allocated
             FROM case_staff_assignments
            WHERE id = %s AND case_no = %s""",
        (original_assignment_id, case_no),
    )
    original = cursor.fetchone()
    if not isinstance(original, dict) or original.get("id") != original_assignment_id:
        raise ValueError("original assignment ownership mismatch")

    key_to_id: dict[str, int] = {}
    created_roles: dict[str, int | None] = {
        "prefix_assignment_id": None,
        "substitute_assignment_id": None,
        "suffix_assignment_id": None,
    }
    ordered_rows = sorted(
        active_rows,
        key=lambda row: (
            _as_date(row["assigned_start_date"], "assigned_start_date"),
            str(row["id"]),
        ),
    )
    for sequence, row in enumerate(ordered_rows, 1):
        row_key = _normalise_transition_id(row["id"], "assignment id")
        if row_key.isdigit():
            if row_key not in row_id_by_key:
                row_id_by_key[row_key] = int(row_key)
            assignment_id = row_id_by_key[row_key]
            key_to_id[row_key] = assignment_id
            cursor.execute(
                """UPDATE case_staff_assignments
                      SET assignment_sequence = %s,
                          assigned_start_date = %s,
                          assigned_end_date = %s,
                          actual_hours = %s,
                          status = %s
                    WHERE id = %s AND case_no = %s""",
                (
                    sequence,
                    _as_date(row["assigned_start_date"], "assigned_start_date"),
                    _as_date(row["assigned_end_date"], "assigned_end_date"),
                    impact_by_key[row_key],
                    row["status"],
                    assignment_id,
                    case_no,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "canonical assignment update did not affect exactly one row"
                )
        else:
            cursor.execute(
                """INSERT INTO case_staff_assignments
                       (case_no, staff_id, assignment_sequence,
                        assigned_start_date, assigned_end_date, planned_hours,
                        actual_hours, hourly_rate, floor_fee_allocated, status,
                        replacement_reason, replaced_assignment_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0.00, 'active',
                           %s, %s)""",
                (
                    case_no,
                    row["staff_id"],
                    sequence,
                    _as_date(row["assigned_start_date"], "assigned_start_date"),
                    _as_date(row["assigned_end_date"], "assigned_end_date"),
                    impact_by_key[row_key],
                    impact_by_key[row_key],
                    original.get("hourly_rate"),
                    "single-day leave resolution",
                    original_assignment_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "canonical assignment insert did not report expected affected rows"
                )
            new_id = _as_positive_int(cursor.lastrowid, "created assignment id")
            key_to_id[row_key] = new_id
            if row.get("kind") == "single_day_substitute":
                created_roles["substitute_assignment_id"] = new_id
            elif _as_date(row["assigned_end_date"], "assigned_end_date") < work_date:
                created_roles["prefix_assignment_id"] = new_id
            elif _as_date(row["assigned_start_date"], "assigned_start_date") > work_date:
                created_roles["suffix_assignment_id"] = new_id
            else:
                raise ValueError("replacement assignment lineage is ambiguous")

    cancelled_ids = {
        _as_positive_int(row.get("id"), "cancelled assignment id")
        for row in after_rows
        if row.get("status") == "cancelled"
    }
    for cancelled_id in sorted(cancelled_ids):
        cursor.execute(
            """UPDATE case_staff_assignments
                  SET actual_hours = 0.00, floor_fee_allocated = 0.00,
                      status = 'cancelled'
                WHERE id = %s AND case_no = %s""",
            (cancelled_id, case_no),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                "canonical assignment cancellation did not affect one row"
            )

    cursor.execute(
        """SELECT id, is_work_day, is_double_pay
             FROM staff_schedule
            WHERE id = %s AND assignment_id = %s AND case_no = %s
              AND work_date = %s""",
        (original_schedule_id, original_assignment_id, case_no, work_date),
    )
    existing_original_schedule = cursor.fetchone()
    if (
        not isinstance(existing_original_schedule, dict)
        or existing_original_schedule.get("id") != original_schedule_id
    ):
        raise ValueError("canonical original schedule was not found")

    cursor.execute(
        """UPDATE staff_schedule
              SET is_work_day = FALSE, is_double_pay = FALSE
            WHERE id = %s AND assignment_id = %s AND case_no = %s
              AND work_date = %s""",
        (original_schedule_id, original_assignment_id, case_no, work_date),
    )
    if cursor.rowcount != 1:
        raise ValueError("canonical leave schedule was not deactivated")

    scope_start: date | None = None
    scope_end: date | None = None
    if covered_dates:
        scope_start = min(covered_dates)
        scope_end = max(covered_dates)
    if scope_start is not None and scope_end is not None:
        for assignment_id in sorted(set(key_to_id.values())):
            cursor.execute(
                """UPDATE staff_schedule\n                      SET is_work_day = FALSE, is_double_pay = FALSE\n                    WHERE assignment_id = %s AND case_no = %s\n                      AND work_date BETWEEN %s AND %s""",
                (assignment_id, case_no, scope_start, scope_end),
            )
    for row in ordered_rows:
        row_key = _normalise_transition_id(row["id"], "assignment id")
        assignment_id = key_to_id[row_key]
        start = _as_date(row["assigned_start_date"], "assigned_start_date")
        end = _as_date(row["assigned_end_date"], "assigned_end_date")
        day = start
        while day <= end:
            if not (assignment_id == original_assignment_id and day == work_date):
                cursor.execute(
                    """INSERT INTO staff_schedule
                           (assignment_id, case_no, staff_id, work_date,
                            is_work_day, is_double_pay)
                       VALUES (%s, %s, %s, %s, TRUE, FALSE)
                       ON DUPLICATE KEY UPDATE
                           assignment_id = VALUES(assignment_id),
                           case_no = VALUES(case_no),
                           staff_id = VALUES(staff_id),
                           is_work_day = TRUE,
                           is_double_pay = FALSE""",
                           (assignment_id, case_no, row["staff_id"], day),
                )
            day += timedelta(days=1)

    assignment_ids = tuple(dict.fromkeys(key_to_id.values()))
    if not assignment_ids:
        raise ValueError("canonical transition produced no assignments")

    cursor.execute(
        """SELECT id, staff_id, assignment_sequence,
                  assigned_start_date, assigned_end_date, status, actual_hours
             FROM case_staff_assignments
            WHERE case_no = %s AND status <> 'cancelled'""",
        (case_no,),
    )
    assignment_rows = [dict(row) for row in (cursor.fetchall() or [])]
    by_id: dict[int, Any] = {}
    for row in assignment_rows:
        if not isinstance(row, Mapping):
            continue
        by_id[_as_positive_int(row.get("id"), "assignment id")] = row
    if set(by_id) != set(assignment_ids):
        raise ValueError("canonical assignment readback id set changed")

    actual_hours_total = Decimal("0")
    assignment_snapshot = []
    for sequence, expected_row in enumerate(ordered_rows, 1):
        row_key = _normalise_transition_id(expected_row["id"], "assignment id")
        assignment_id = key_to_id[row_key]
        row = by_id[assignment_id]
        actual_hours = Decimal(str(row.get("actual_hours")))
        if not actual_hours.is_finite():
            raise ValueError("canonical assignment readback actual_hours is invalid")
        if (
            row.get("staff_id") != staff_by_key[row_key]
            or row.get("status") != expected_row.get("status")
            or row.get("assignment_sequence") != sequence
            or _as_date(row.get("assigned_start_date"), "assigned_start_date")
            != _as_date(expected_row.get("assigned_start_date"), "assigned_start_date")
            or _as_date(row.get("assigned_end_date"), "assigned_end_date")
            != _as_date(expected_row.get("assigned_end_date"), "assigned_end_date")
            or actual_hours != impact_by_key[row_key]
        ):
            raise ValueError("canonical assignment readback does not match final plan")
        actual_hours_total += actual_hours
        assignment_snapshot.append(
            {
                "id": assignment_id,
                "case_no": case_no,
                "staff_id": row["staff_id"],
                "assignment_sequence": row["assignment_sequence"],
                "assigned_start_date": _as_date(
                    row["assigned_start_date"], "assigned_start_date"
                ),
                "assigned_end_date": _as_date(
                    row["assigned_end_date"], "assigned_end_date"
                ),
                "status": row["status"],
                "actual_hours": str(actual_hours),
            }
        )
    if not 1 <= len(assignment_snapshot) <= 4:
        raise ValueError("canonical transition active assignment count changed")

    expected_owned_dates: dict[tuple[int, date], int] = {}
    for day, row_key in covered_dates.items():
        expected_owned_dates[(key_to_id[row_key], day)] = staff_by_key[row_key]
    if scope_start is None or scope_end is None:
        raise ValueError("canonical schedule scope could not be computed")
    cursor.execute(
        """SELECT assignment_id, staff_id, work_date,
                  is_work_day, is_double_pay
             FROM staff_schedule
            WHERE case_no = %s AND work_date BETWEEN %s AND %s""",
        (case_no, scope_start, scope_end),
    )
    schedule_rows = [dict(row) for row in (cursor.fetchall() or [])]
    if not schedule_rows:
        raise ValueError("canonical schedule readback returned no rows")
    expected_work = set(expected_owned_dates)
    found_work = set()
    for row in schedule_rows:
        if not isinstance(row, Mapping):
            raise ValueError("canonical schedule row is invalid")
        assignment_id = _as_positive_int(row.get("assignment_id"), "assignment_id")
        day = _as_date(row.get("work_date"), "work_date")
        if not row.get("is_work_day"):
            continue
        owned_key = (assignment_id, day)
        if owned_key not in expected_owned_dates:
            raise ValueError("canonical stale schedule date was not removed")
        if row.get("staff_id") != expected_owned_dates[owned_key]:
            raise ValueError("canonical schedule staff ownership changed")
        if row.get("is_double_pay"):
            raise ValueError("canonical schedule double_pay flag changed")
        found_work.add(owned_key)
    if found_work != expected_work:
        raise ValueError("canonical schedule ownership gap detected")

    cursor.execute(
        """SELECT id, is_work_day, is_double_pay\n            FROM staff_schedule\n           WHERE id = %s AND case_no = %s""",
        (original_schedule_id, case_no),
    )
    original_schedule = cursor.fetchone()
    if (
        not isinstance(original_schedule, dict)
        or original_schedule.get("is_work_day")
        or original_schedule.get("is_double_pay")
    ):
        raise ValueError("canonical original schedule was not deactivated")

    pending_event = None
    if resolution_type == "substitute":
        substitute_assignment_id = created_roles["substitute_assignment_id"]
        if substitute_assignment_id is None:
            raise ValueError("canonical substitute assignment was not created")
        pending_event = {
            "case_no": case_no,
            "event_key": identity["event_key"],
            "original_assignment_id": original_assignment_id,
            "original_schedule_id": original_schedule_id,
            "work_date": work_date,
            "resolution_type": "substitute",
            "substitute_assignment_id": substitute_assignment_id,
            "prefix_assignment_id": created_roles["prefix_assignment_id"],
            "suffix_assignment_id": created_roles["suffix_assignment_id"],
        }
    reconciliation = reconcile_assignment_payroll_with_cursor(
        cursor, case_no, pending_substitution_event=pending_event
    )
    if reconciliation.get("errors") or reconciliation.get("can_create_staff_payments") is not True:
        raise ValueError("assignment payroll reconciliation failed")
    target_hours = Decimal(str(reconciliation.get("target_hours")))
    if not target_hours.is_finite() or target_hours != actual_hours_total:
        raise ValueError("assignment payroll target hours mismatch")

    actual_hours_snapshot = {
        row["id"]: row["actual_hours"] for row in assignment_snapshot
    }
    schedule_snapshot = {
        "request_identity": dict(identity),
        "assignments": assignment_snapshot,
        "ownership_by_date": {
            day.isoformat(): key_to_id[key] for day, key in sorted(covered_dates.items())
        },
        "original_schedule": {
            "id": original_schedule_id,
            "assignment_id": original_assignment_id,
            "work_date": work_date.isoformat(),
            "is_work_day": False,
            "is_double_pay": False,
        },
    }
    event_payload = {
        "case_no": case_no,
        "original_assignment_id": original_assignment_id,
        "original_schedule_id": original_schedule_id,
        "work_date": work_date.isoformat(),
        "resolution_type": resolution_type,
        "substitute_assignment_id": created_roles["substitute_assignment_id"],
        "event_key": identity["event_key"],
        "actor": identity["actor"],
        "reason": identity["reason"],
        "schedule_snapshot": schedule_snapshot,
        "payroll_snapshot": reconciliation,
    }
    return {
        "assignments": assignment_snapshot,
        "schedules": schedule_snapshot,
        "actual_hours": actual_hours_snapshot,
        "payroll_snapshot": reconciliation,
        "pending_event_payload": event_payload,
    }


def apply_assignment_leave_resolution(request: Any) -> Dict[str, Any]:
    """Run a single leave-substitution transaction and append immutable event."""
    connection = None
    cursor = None

    def _close_resource(resource: Any) -> None:
        if resource is None:
            return
        closer = getattr(resource, "close", None)
        if callable(closer):
            try:
                closer()
            except BaseException:
                pass

    try:
        connection = get_connection()
        cursor = connection.cursor()
        preflight = prepare_assignment_leave_resolution_apply(cursor, request)
        preflight_status = preflight.get("status")
        if preflight_status == "rejected":
            connection.rollback()
            return {**preflight, "status": "rejected"}
        if preflight_status == "idempotent_replay":
            connection.rollback()
            return {**preflight, "status": "idempotent_replay"}
        if preflight_status != "apply":
            raise ValueError("preflight returned unsupported status")

        mutation_result = execute_assignment_leave_resolution_mutations(
            cursor,
            preflight["mutation_command"],
        )
        pending_event_payload = mutation_result.get("pending_event_payload")
        if not isinstance(pending_event_payload, Mapping):
            raise ValueError("mutation did not produce pending event payload")
        required_payload_fields = {
            "case_no",
            "original_assignment_id",
            "original_schedule_id",
            "work_date",
            "resolution_type",
            "event_key",
            "actor",
            "reason",
            "schedule_snapshot",
            "payroll_snapshot",
        }
        if not required_payload_fields.issubset(set(pending_event_payload)):
            raise ValueError("mutation pending_event_payload is incomplete")
        if not isinstance(pending_event_payload["schedule_snapshot"], dict) or not isinstance(
            pending_event_payload["payroll_snapshot"], dict
        ):
            raise ValueError("mutation pending_event_payload snapshots must be objects")

        cursor.execute(
            """
            INSERT INTO assignment_schedule_leave_substitution_events
              (case_no, original_assignment_id, original_schedule_id, work_date,
               resolution_type, substitute_assignment_id, event_key, actor, reason,
               schedule_snapshot, payroll_snapshot)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                pending_event_payload["case_no"],
                pending_event_payload["original_assignment_id"],
                pending_event_payload["original_schedule_id"],
                _as_date(
                    pending_event_payload["work_date"],
                    "pending_event_payload.work_date",
                ),
                pending_event_payload["resolution_type"],
                pending_event_payload.get("substitute_assignment_id"),
                pending_event_payload["event_key"],
                pending_event_payload["actor"],
                pending_event_payload["reason"],
                json.dumps(
                    pending_event_payload["schedule_snapshot"],
                    ensure_ascii=False,
                    default=str,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    pending_event_payload["payroll_snapshot"],
                    ensure_ascii=False,
                    default=str,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        if getattr(cursor, "rowcount", 0) != 1:
            raise ValueError("event insert did not affect exactly one row")
        event_id = _as_positive_int(cursor.lastrowid, "event_id")
        connection.commit()
        return {
            "status": "applied",
            "result": "applied",
            "event_id": event_id,
            "event_payload": pending_event_payload,
            "assignments": mutation_result.get("assignments", []),
            "schedules": mutation_result.get("schedules", {}),
            "actual_hours": mutation_result.get("actual_hours", {}),
            "payroll_snapshot": pending_event_payload["payroll_snapshot"],
            "schedule_snapshot": pending_event_payload["schedule_snapshot"],
        }
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except BaseException:
                pass
        raise
    finally:
        _close_resource(cursor)
        _close_resource(connection)


def _load_case_assignments(cursor: Any, case_no: str) -> list[dict[str, Any]]:
    cursor.execute(
        """SELECT id, status, assigned_start_date, assigned_end_date
             FROM case_staff_assignments
            WHERE case_no = %s
            FOR UPDATE""",
        (case_no,),
    )
    return list(cursor.fetchall() or [])


def _assert_assignment_is_unlocked(cursor: Any, assignment_id: int) -> None:
    cursor.execute(
        """SELECT id FROM staff_payments
            WHERE assignment_id = %s AND payment_status <> 'cancelled'
            LIMIT 1 FOR UPDATE""",
        (assignment_id,),
    )
    if cursor.fetchone() is not None:
        raise ValueError("assignment is locked by non-cancelled staff payment")

    cursor.execute(
        """SELECT d.id
             FROM staff_monthly_settlement_details d
             JOIN staff_monthly_settlements s ON s.id = d.settlement_id
            WHERE d.assignment_id = %s AND s.status <> 'cancelled'
            LIMIT 1 FOR UPDATE""",
        (assignment_id,),
    )
    if cursor.fetchone() is not None:
        raise ValueError("assignment is locked by non-cancelled monthly settlement")

    cursor.execute(
        """SELECT id FROM actual_hours_adjustments
            WHERE assignment_id = %s
            LIMIT 1 FOR UPDATE""",
        (assignment_id,),
    )
    if cursor.fetchone() is not None:
        raise ValueError("assignment requires actual-hours review before changing schedule")


def save_assignment_rest_dates(assignment_id: int, rest_dates: Iterable[Any]) -> Dict[str, Any]:
    """
    更新特定 assignment_id 的排休與順延完工日。
    """
    try:
        assignment_id = _as_positive_int(assignment_id, "assignment_id")
    except AssignmentScheduleRestDateValidationError as exc:
        return _snapshot_failure("validation_error", str(exc), exc.code)
    except ValueError as exc:
        return _snapshot_failure("validation_error", str(exc), "validation_error")

    normalised_rest_dates = None
    try:
        normalised_rest_dates = _normalise_rest_dates(rest_dates)
    except AssignmentScheduleRestDateValidationError as exc:
        return _snapshot_failure("validation_error", str(exc), "invalid_rest_dates")
    except ValueError as exc:
        return _snapshot_failure("validation_error", str(exc), "invalid_rest_dates")

    try:
        rest_dates_set = {_as_date(item, "rest_date") for item in normalised_rest_dates}
    except AssignmentScheduleRestDateValidationError as exc:
        return _snapshot_failure(
            "validation_error", str(exc), "invalid_rest_dates"
        )
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT csa.id AS assignment_id, csa.case_no, csa.staff_id, csa.status,
                       csa.assigned_start_date AS assigned_start_date,
                       csa.planned_hours, o.service_hours_per_day,
                       o.actual_start_date AS order_actual_start_date,
                       o.start_date AS order_start_date
                FROM case_staff_assignments csa
                JOIN orders o ON csa.case_no = o.case_no
                WHERE csa.id = %s
                FOR UPDATE
                """,
                (assignment_id,),
            )
            assignment = cursor.fetchone()
            if assignment is None:
                return _snapshot_failure("not_found", "assignment_id does not exist", "assignment_not_found")
            if assignment.get("status") == "cancelled":
                return _snapshot_failure("validation_error", "assignment is cancelled", "assignment_cancelled")

            case_no = assignment["case_no"]
            staff_id = assignment["staff_id"]

            start_date = assignment.get("assigned_start_date") or assignment.get("order_actual_start_date") or assignment.get(
                "order_start_date"
            )
            try:
                assigned_start_date = _as_date(start_date, "assigned_start_date")
            except ValueError as exc:
                return _snapshot_failure("validation_error", str(exc), "assignment_start_date_invalid")

            try:
                planned_hours = _as_positive_decimal(assignment.get("planned_hours"), "planned_hours")
                service_hours_per_day = _as_positive_decimal(
                    assignment.get("service_hours_per_day"), "service_hours_per_day"
                )
                target_service_days = int(
                    (planned_hours / service_hours_per_day).quantize(Decimal("1"), rounding=ROUND_CEILING)
                )
            except ValueError as exc:
                return _snapshot_failure("validation_error", str(exc), "assignment_allocation_invalid")

            if target_service_days < 1:
                return _snapshot_failure(
                    "validation_error",
                    "assignment has no positive target work-days",
                    "assignment_target_zero",
                )

            try:
                _assert_assignment_is_unlocked(cursor, assignment_id)
            except ValueError as exc:
                return _snapshot_failure("locked", str(exc), "assignment_locked")

            calc_res = calculate_attendance_schedule(
                actual_start_date=assigned_start_date,
                target_service_days=target_service_days,
                service_mode="週休1日",
                custom_leave_dates=rest_dates_set,
                custom_holiday_rest_dates=None,
            )
            actual_end_date = calc_res.get("actual_end_date")
            if actual_end_date is None:
                return _snapshot_failure("validation_error", "cannot calculate actual_end_date", "schedule_calculation_failed")

            day_by_day = calc_res.get("day_by_day") or []
            if not isinstance(day_by_day, list) or not day_by_day:
                return _snapshot_failure("validation_error", "invalid schedule calculation result", "schedule_empty")

            case_assignments = _load_case_assignments(cursor, case_no)
            try:
                validate_non_overlapping_assignment_interval(
                    assigned_start_date,
                    actual_end_date,
                    case_assignments,
                    candidate_assignment_id=assignment_id,
                )
            except ValueError as exc:
                return _snapshot_failure("conflict", str(exc), "assignment_interval_overlap")

            work_dates = [item.get("date") for item in day_by_day]
            min_date, max_date = min(work_dates), max(work_dates)
            cursor.execute(
                """SELECT work_date, assignment_id, case_no
                     FROM staff_schedule
                    WHERE staff_id = %s
                      AND work_date BETWEEN %s AND %s
                    FOR UPDATE""",
                (staff_id, min_date, max_date),
            )
            for row in cursor.fetchall() or []:
                row_assignment_id = row.get("assignment_id")
                row_case_no = row.get("case_no")
                if row_assignment_id != assignment_id:
                    return _snapshot_failure(
                        "conflict",
                        "required schedule date is occupied by another assignment",
                        "schedule_conflict",
                    )
                if row_case_no != case_no:
                    return _snapshot_failure(
                        "conflict",
                        "schedule ownership case mismatch",
                        "schedule_case_conflict",
                    )

            cursor.execute("DELETE FROM staff_schedule WHERE assignment_id = %s", (assignment_id,))

            for item in day_by_day:
                work_date = item.get("date")
                is_work_day = item.get("is_work_day")
                notes = item.get("holiday_name") if not item.get("is_work_day") else None
                if notes is None and is_work_day is False:
                    notes = "排休"
                cursor.execute(
                    """
                    INSERT INTO staff_schedule
                        (assignment_id, case_no, staff_id, work_date, is_work_day, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (assignment_id, case_no, staff_id, work_date, 1 if is_work_day else 0, notes),
                )

            cursor.execute(
                """
                UPDATE case_staff_assignments
                SET assigned_end_date = %s
                WHERE id = %s
                """,
                (actual_end_date, assignment_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("assignment could not be updated")

            conn.commit()
            return {
                "success": True,
                "status": "ok",
                "assignment_id": assignment_id,
                "case_no": case_no,
                "staff_id": staff_id,
                "actual_end_date": str(actual_end_date),
                "rest_dates": normalised_rest_dates,
            }

    except ValueError as exc:
        conn.rollback()
        return _snapshot_failure("validation_error", str(exc), "validation_error")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
