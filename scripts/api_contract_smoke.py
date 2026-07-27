"""Repeatable, read-only OpenAPI smoke and response-contract runner."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from jsonschema import RefResolver, ValidationError, validate


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DATA_BROWSER_PATH = "/api/v1/admin/data-browser/{table}"
DATA_BROWSER_TABLES = (
    "actual_hours_adjustments",
    "beclass_records",
    "case_staff_assignments",
    "client_payment_transactions",
    "client_payments",
    "clients",
    "holidays",
    "line_confirmation_requests",
    "matching_records",
    "orders",
    "payment_migration_reviews",
    "staff",
    "staff_bank_accounts",
    "staff_bookings",
    "staff_payment_transactions",
    "staff_payments",
    "staff_schedule",
)
BUILTIN_VALUES: dict[str, Any] = {
    "year": 2026,
    "month": 7,
}
FAILURE_KINDS = {
    "FAIL_AUTH",
    "FAIL_HTTP_4XX",
    "FAIL_HTTP_5XX",
    "FAIL_UNDECLARED_STATUS",
    "FAIL_SCHEMA",
    "FAIL_CONTENT_TYPE",
    "NETWORK_ERROR",
}


@dataclass
class SmokeResult:
    method: str
    template_path: str
    resolved_path: str
    status: int | None
    kind: str
    detail: str = ""


def _load_fixtures(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("fixtures JSON must be an object")
    return payload


def _operation_fixture(
    fixtures: dict[str, Any],
    method: str,
    template_path: str,
) -> dict[str, Any]:
    operations = fixtures.get("operations", fixtures)
    fixture = operations.get(f"{method} {template_path}", {}) if isinstance(operations, dict) else {}
    return fixture if isinstance(fixture, dict) else {}


def _matches_filters(path: str, only: list[str], exclude: list[str]) -> bool:
    if only and not any(fnmatch.fnmatch(path, pattern) for pattern in only):
        return False
    return not any(fnmatch.fnmatch(path, pattern) for pattern in exclude)


def _expand_operations(
    openapi: dict[str, Any],
    fixtures: dict[str, Any],
    only: list[str],
    exclude: list[str],
    allow_writes: bool,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for template_path, path_item in sorted(openapi.get("paths", {}).items()):
        if not template_path.startswith("/api/v1") or not _matches_filters(template_path, only, exclude):
            continue
        shared_parameters = path_item.get("parameters", []) if isinstance(path_item, dict) else []
        for method_name, operation in sorted(path_item.items()):
            method = method_name.upper()
            if method_name.lower() not in {"get", "head", "options", "post", "put", "patch", "delete"}:
                continue
            if method not in SAFE_METHODS and not allow_writes:
                continue

            fixture = _operation_fixture(fixtures, method, template_path)
            parameters = list(shared_parameters or []) + list(operation.get("parameters", []) or [])
            expansions = DATA_BROWSER_TABLES if method == "GET" and template_path == DATA_BROWSER_PATH else (None,)
            for table_name in expansions:
                path_values = dict(fixture.get("path", {}))
                query_values = dict(fixture.get("query", {}))
                if table_name is not None:
                    path_values["table"] = table_name

                missing: list[str] = []
                resolved_path = template_path
                for parameter in parameters:
                    name = parameter.get("name")
                    location = parameter.get("in")
                    required = bool(parameter.get("required"))
                    if location == "path":
                        value = path_values.get(name, BUILTIN_VALUES.get(name))
                        if value is None:
                            missing.append(str(name))
                        else:
                            resolved_path = resolved_path.replace(
                                "{" + str(name) + "}",
                                quote(str(value), safe=""),
                            )
                    elif location == "query" and required:
                        value = query_values.get(name, BUILTIN_VALUES.get(name))
                        if value is None:
                            missing.append(str(name))
                        else:
                            query_values[name] = value

                expanded.append(
                    {
                        "method": method,
                        "template_path": template_path,
                        "resolved_path": resolved_path,
                        "query": query_values,
                        "operation": operation,
                        "missing": sorted(set(missing)),
                    }
                )
    return expanded


def _response_spec(operation: dict[str, Any], status: int) -> dict[str, Any] | None:
    responses = operation.get("responses", {})
    return responses.get(str(status)) or responses.get("default")


def _json_schema(response_spec: dict[str, Any] | None) -> dict[str, Any] | None:
    if not response_spec:
        return None
    content = response_spec.get("content", {})
    json_content = content.get("application/json")
    return json_content.get("schema") if isinstance(json_content, dict) else None


def _detail(payload: Any, text: str) -> str:
    if isinstance(payload, dict):
        value = payload.get("detail", payload.get("message", payload.get("error", "")))
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value or "")
    return text[:500]


def _auth_kind(detail: str) -> str:
    lowered = detail.lower()
    if "內部服務金鑰" in detail or "internal" in lowered and "key" in lowered:
        return "api_key_missing_or_invalid"
    if "缺少有效的管理員" in detail or "bearer" in lowered and "missing" in lowered:
        return "bearer_missing"
    if "失效" in detail or "過期" in detail or "expired" in lowered or "invalid token" in lowered:
        return "bearer_expired_or_invalid"
    return "auth_unknown"


def _validate_success_payload(
    payload: Any,
    schema: dict[str, Any] | None,
    openapi: dict[str, Any],
) -> str | None:
    if schema is not None:
        try:
            validate(
                instance=payload,
                schema=schema,
                resolver=RefResolver.from_schema(openapi),
            )
        except ValidationError as exc:
            location = ".".join(str(part) for part in exc.absolute_path)
            return f"OpenAPI schema mismatch at {location or '<root>'}: {exc.message}"

    if isinstance(payload, dict) and "success" in payload:
        if not isinstance(payload.get("success"), bool):
            return "BaseResponse.success must be boolean"
        if payload["success"] is not True:
            return "2xx BaseResponse.success must be true"
        if "message" not in payload or not isinstance(payload.get("message"), str):
            return "BaseResponse.message must be string"
    return None


def _redact(value: str, secrets: list[str]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _run_one(
    session: requests.Session,
    base_url: str,
    target: dict[str, Any],
    openapi: dict[str, Any],
    timeout: float,
    profile: str,
    secrets: list[str],
) -> SmokeResult:
    method = target["method"]
    template_path = target["template_path"]
    resolved_path = target["resolved_path"]
    if target["missing"]:
        return SmokeResult(
            method,
            template_path,
            resolved_path,
            None,
            "SKIP_MISSING_FIXTURE",
            "missing: " + ", ".join(target["missing"]),
        )

    try:
        response = session.request(
            method,
            base_url + resolved_path,
            params=target["query"],
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return SmokeResult(
            method,
            template_path,
            resolved_path,
            None,
            "NETWORK_ERROR",
            _redact(f"{type(exc).__name__}: {exc}", secrets),
        )

    content_type = response.headers.get("content-type", "")
    payload: Any = None
    if "json" in content_type.lower():
        try:
            payload = response.json()
        except ValueError as exc:
            return SmokeResult(
                method,
                template_path,
                resolved_path,
                response.status_code,
                "FAIL_SCHEMA",
                f"invalid JSON: {exc}",
            )
    if payload is None and response.status_code < 400:
        detail = f"{len(response.content)} bytes; {content_type or 'unknown content type'}"
    else:
        detail = _redact(_detail(payload, response.text), secrets)

    if response.status_code == 401:
        auth_detail = _auth_kind(detail)
        kind = "EXPECTED_AUTH_DENIAL" if profile == "public" else "FAIL_AUTH"
        return SmokeResult(method, template_path, resolved_path, 401, kind, auth_detail)
    if response.status_code == 403:
        kind = "EXPECTED_AUTH_DENIAL" if profile == "public" else "FAIL_AUTH"
        return SmokeResult(method, template_path, resolved_path, 403, kind, "insufficient_role")
    if response.status_code >= 500:
        return SmokeResult(method, template_path, resolved_path, response.status_code, "FAIL_HTTP_5XX", detail)
    if response.status_code >= 400:
        return SmokeResult(method, template_path, resolved_path, response.status_code, "FAIL_HTTP_4XX", detail)

    spec = _response_spec(target["operation"], response.status_code)
    if spec is None:
        return SmokeResult(
            method,
            template_path,
            resolved_path,
            response.status_code,
            "FAIL_UNDECLARED_STATUS",
            "status is not declared in OpenAPI",
        )
    expected_content = spec.get("content", {})
    if expected_content and not any(media in content_type for media in expected_content):
        return SmokeResult(
            method,
            template_path,
            resolved_path,
            response.status_code,
            "FAIL_CONTENT_TYPE",
            f"received {content_type or '<missing>'}; expected {sorted(expected_content)}",
        )

    schema_error = _validate_success_payload(payload, _json_schema(spec), openapi)
    if schema_error:
        return SmokeResult(method, template_path, resolved_path, response.status_code, "FAIL_SCHEMA", schema_error)
    return SmokeResult(method, template_path, resolved_path, response.status_code, "PASS", detail)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("API_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--profile", choices=("public", "admin"), default="public")
    parser.add_argument("--api-key-env", default="INTERNAL_API_KEY")
    parser.add_argument("--bearer-env", default="ADMIN_ACCESS_TOKEN")
    parser.add_argument("--fixtures")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output-json")
    parser.add_argument("--allow-writes", action="store_true")
    return parser


def _write_json_report(output_path: str, report: dict[str, Any]) -> None:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        raise RuntimeError(f"unable to write JSON report {target}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_url = args.base_url.rstrip("/")
    api_key = os.getenv(args.api_key_env, "").strip()
    bearer = os.getenv(args.bearer_env, "").strip()
    secrets = [api_key, bearer]
    try:
        fixtures = _load_fixtures(args.fixtures)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"runner configuration error: {exc}", file=sys.stderr)
        return 2

    session = requests.Session()
    session.headers.update({"User-Agent": "labor-union-api-contract-smoke/1.0"})
    if api_key:
        session.headers["X-Internal-API-Key"] = api_key
    if bearer:
        session.headers["Authorization"] = f"Bearer {bearer}"

    try:
        openapi_response = session.get(f"{base_url}/openapi.json", timeout=args.timeout)
        openapi_response.raise_for_status()
        openapi = openapi_response.json()
    except (requests.RequestException, ValueError) as exc:
        print(_redact(f"unable to load OpenAPI: {exc}", secrets), file=sys.stderr)
        return 2

    targets = _expand_operations(
        openapi,
        fixtures,
        args.only,
        args.exclude,
        args.allow_writes,
    )
    results = [
        _run_one(session, base_url, target, openapi, args.timeout, args.profile, secrets)
        for target in targets
    ]
    summary = dict(sorted(Counter(result.kind for result in results).items()))
    report = {
        "base_url": base_url,
        "profile": args.profile,
        "request_count": len(results),
        "summary": summary,
        "results": [asdict(result) for result in results],
    }

    for result in results:
        status = "-" if result.status is None else str(result.status)
        suffix = f" | {result.detail}" if result.detail else ""
        print(f"{result.kind:24} {status:>3} {result.method} {result.resolved_path}{suffix}")
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False, sort_keys=True))

    if args.output_json:
        try:
            _write_json_report(args.output_json, report)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    return 1 if any(result.kind in FAILURE_KINDS for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
