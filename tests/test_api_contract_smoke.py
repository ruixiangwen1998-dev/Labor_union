import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path("scripts/api_contract_smoke.py")
SPEC = importlib.util.spec_from_file_location("api_contract_smoke", SCRIPT_PATH)
api_contract_smoke = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = api_contract_smoke
SPEC.loader.exec_module(api_contract_smoke)


class FakeResponse:
    def __init__(self, status, payload, content_type="application/json", content=None):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else str(payload)
        self.content = content if content is not None else self.text.encode()
        self.headers = {"content-type": content_type}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def _openapi(path="/api/v1/health"):
    return {
        "openapi": "3.1.0",
        "paths": {
            path: {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["success", "message", "data"],
                                        "properties": {
                                            "success": {"type": "boolean"},
                                            "message": {"type": "string"},
                                            "data": {},
                                        },
                                    }
                                }
                            }
                        }
                    }
                },
                "post": {"responses": {"200": {"description": "write"}}},
            }
        },
    }


def test_default_expansion_is_get_only():
    targets = api_contract_smoke._expand_operations(_openapi(), {}, [], [], False)

    assert [target["method"] for target in targets] == ["GET"]


def test_data_browser_path_expands_every_table():
    openapi = _openapi(api_contract_smoke.DATA_BROWSER_PATH)
    openapi["paths"][api_contract_smoke.DATA_BROWSER_PATH]["get"]["parameters"] = [
        {"name": "table", "in": "path", "required": True, "schema": {"type": "string"}}
    ]

    targets = api_contract_smoke._expand_operations(openapi, {}, [], [], False)

    assert len(targets) == len(api_contract_smoke.DATA_BROWSER_TABLES)
    assert {target["resolved_path"].rsplit("/", 1)[-1] for target in targets} == set(
        api_contract_smoke.DATA_BROWSER_TABLES
    )


def test_missing_required_fixture_is_skipped_without_request():
    openapi = _openapi("/api/v1/orders/{case_no}")
    operation = openapi["paths"]["/api/v1/orders/{case_no}"]["get"]
    operation["parameters"] = [
        {"name": "case_no", "in": "path", "required": True, "schema": {"type": "string"}}
    ]
    target = api_contract_smoke._expand_operations(openapi, {}, [], [], False)[0]
    session = FakeSession(FakeResponse(200, {}))

    result = api_contract_smoke._run_one(
        session,
        "http://test",
        target,
        openapi,
        1,
        "public",
        [],
    )

    assert result.kind == "SKIP_MISSING_FIXTURE"
    assert session.calls == []


def test_database_identifier_is_never_guessed_from_builtin_values():
    openapi = _openapi("/api/v1/staff/{staff_id}/monthly-schedule")
    operation = openapi["paths"]["/api/v1/staff/{staff_id}/monthly-schedule"]["get"]
    operation["parameters"] = [
        {"name": "staff_id", "in": "path", "required": True, "schema": {"type": "integer"}},
        {"name": "year", "in": "query", "required": True, "schema": {"type": "integer"}},
        {"name": "month", "in": "query", "required": True, "schema": {"type": "integer"}},
    ]

    target = api_contract_smoke._expand_operations(openapi, {}, [], [], False)[0]

    assert target["missing"] == ["staff_id"]


def test_admin_401_classifies_internal_key_without_leaking_secret():
    openapi = _openapi()
    target = api_contract_smoke._expand_operations(openapi, {}, [], [], False)[0]
    secret = "do-not-print-this"
    session = FakeSession(
        FakeResponse(401, {"detail": f"內部服務金鑰錯誤 {secret}"})
    )

    result = api_contract_smoke._run_one(
        session,
        "http://test",
        target,
        openapi,
        1,
        "admin",
        [secret],
    )

    assert result.kind == "FAIL_AUTH"
    assert result.detail == "api_key_missing_or_invalid"
    assert secret not in result.detail


def test_public_401_is_expected_auth_denial():
    openapi = _openapi()
    target = api_contract_smoke._expand_operations(openapi, {}, [], [], False)[0]
    session = FakeSession(FakeResponse(401, {"detail": "缺少有效的管理員 Session"}))

    result = api_contract_smoke._run_one(
        session,
        "http://test",
        target,
        openapi,
        1,
        "public",
        [],
    )

    assert result.kind == "EXPECTED_AUTH_DENIAL"
    assert result.detail == "bearer_missing"


def test_200_base_response_schema_mismatch_fails():
    openapi = _openapi()
    target = api_contract_smoke._expand_operations(openapi, {}, [], [], False)[0]
    session = FakeSession(FakeResponse(200, {"success": "yes", "message": "bad", "data": {}}))

    result = api_contract_smoke._run_one(
        session,
        "http://test",
        target,
        openapi,
        1,
        "public",
        [],
    )

    assert result.kind == "FAIL_SCHEMA"


def test_matching_records_integer_options_are_caught_by_openapi_schema():
    openapi = {
        "openapi": "3.1.0",
        "paths": {},
    }
    operation = {
        "responses": {
            "200": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["success", "message", "data"],
                            "properties": {
                                "success": {"type": "boolean"},
                                "message": {"type": "string"},
                                "data": {
                                    "type": "object",
                                    "properties": {
                                        "valid_options": {
                                            "type": "object",
                                            "additionalProperties": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                        }
                                    },
                                },
                            },
                        }
                    }
                }
            }
        }
    }
    target = {
        "method": "GET",
        "template_path": "/api/v1/admin/data-browser/{table}",
        "resolved_path": "/api/v1/admin/data-browser/matching_records",
        "query": {},
        "operation": operation,
        "missing": [],
    }
    session = FakeSession(
        FakeResponse(
            200,
            {
                "success": True,
                "message": "ok",
                "data": {"valid_options": {"caregiver_accepted": [0, 1]}},
            },
        )
    )

    result = api_contract_smoke._run_one(
        session,
        "http://test",
        target,
        openapi,
        1,
        "admin",
        [],
    )

    assert result.kind == "FAIL_SCHEMA"


def test_binary_success_reports_size_without_dumping_content():
    xlsx_media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    binary = b"PK\x03\x04secret-looking-binary"
    operation = {
        "responses": {
            "200": {
                "content": {
                    xlsx_media_type: {}
                }
            }
        }
    }
    target = {
        "method": "GET",
        "template_path": "/api/v1/report/export",
        "resolved_path": "/api/v1/report/export",
        "query": {},
        "operation": operation,
        "missing": [],
    }
    session = FakeSession(
        FakeResponse(200, None, content_type=xlsx_media_type, content=binary)
    )

    result = api_contract_smoke._run_one(
        session,
        "http://test",
        target,
        {"openapi": "3.1.0", "paths": {}},
        1,
        "admin",
        [],
    )

    assert result.kind == "PASS"
    assert result.detail == f"{len(binary)} bytes; {xlsx_media_type}"
    assert "secret-looking-binary" not in result.detail


def test_json_report_writer_creates_parent_directory():
    output_path = "reports/report.json"

    with (
        patch.object(Path, "mkdir") as mkdir,
        patch.object(Path, "write_text") as write_text,
    ):
        api_contract_smoke._write_json_report(
            output_path,
            {"summary": {"PASS": 1}},
        )

    mkdir.assert_called_once_with(parents=True, exist_ok=True)
    written_json = write_text.call_args.args[0]
    assert '"PASS": 1' in written_json
    assert write_text.call_args.kwargs == {"encoding": "utf-8"}
