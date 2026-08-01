"""Acceptance coverage for the DbService order-field cleanup contract."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from services import db_service


DB_SERVICE = Path(__file__).resolve().parents[1] / "services" / "db_service.py"


def _function_node(name: str) -> ast.FunctionDef:
    tree = ast.parse(DB_SERVICE.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert name in functions, f"missing DbService function: {name}"
    return functions[name]


def _function_source(name: str) -> str:
    source = DB_SERVICE.read_text(encoding="utf-8")
    segment = ast.get_source_segment(source, _function_node(name))
    assert segment is not None
    return segment


def test_get_order_by_case_no_never_selects_other_addition():
    source = _function_source("get_order_by_case_no")

    assert "other_addition" not in source
    assert "o.floor_fee" in source
    assert "_resolve_case_no(case_no)" in source


def test_create_order_has_no_other_addition_contract_and_keeps_floor_fee():
    node = _function_node("create_order")
    source = _function_source("create_order")
    argument_names = [argument.arg for argument in node.args.args]

    assert "other_addition" not in argument_names
    assert "other_addition" not in source
    assert "floor_fee" in argument_names
    assert "_resolve_case_no(case_no)" in source
    assert "INSERT IGNORE INTO client_payments" in source


def test_create_order_insert_placeholders_match_parameter_tuple():
    node = _function_node("create_order")
    inserts = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not call.args:
            continue
        query = call.args[0]
        if isinstance(query, ast.Constant) and isinstance(query.value, str) and "INSERT INTO orders" in query.value:
            inserts.append(call)

    assert len(inserts) == 1
    insert_call = inserts[0]
    query = insert_call.args[0].value
    parameters = insert_call.args[1]
    assert isinstance(parameters, ast.Tuple)
    assert query.count("%s") == len(parameters.elts)


def test_order_crud_uses_client_identity_status_not_legacy_eligibility_field():
    source = DB_SERVICE.read_text(encoding="utf-8")

    assert "clients.identity_status" not in source
    assert "c.identity_status AS identity_status" in _function_source("get_order_by_case_no")
    assert "c.identity_status AS identity_status" in _function_source("get_table_data")
    assert "identity_status" not in [argument.arg for argument in _function_node("create_order").args.args]


@pytest.mark.parametrize(
    ("entrypoint", "arguments"),
    [
        ("get_table_data", ("payments",)),
        ("get_table_columns", ("payments",)),
        ("update_table_row", ("payments", 1, {"payment_status": "paid"})),
    ],
)
def test_legacy_payments_entrypoints_fail_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    arguments: tuple,
):
    connection_attempts = 0

    def forbidden_connection():
        nonlocal connection_attempts
        connection_attempts += 1
        raise AssertionError("legacy payments rejection must happen before connection")

    monkeypatch.setattr(db_service, "get_connection", forbidden_connection)

    with pytest.raises(ValueError):
        getattr(db_service, entrypoint)(*arguments)

    assert connection_attempts == 0


def test_legacy_payments_is_absent_from_all_browser_allowlists():
    module = ast.parse(DB_SERVICE.read_text(encoding="utf-8"))
    table_primary_keys = next(
        node.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "TABLE_PRIMARY_KEYS" for target in node.targets)
    )
    assert isinstance(table_primary_keys, ast.Dict)
    primary_key_tables = {
        key.value
        for key in table_primary_keys.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }

    for function_name in ("get_table_data", "get_table_columns"):
        string_literals = {
            node.value
            for node in ast.walk(_function_node(function_name))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert "payments" not in string_literals
        assert {"client_payments", "staff_payments"} <= string_literals

    assert "payments" not in primary_key_tables
    assert {"client_payments", "staff_payments"} <= primary_key_tables


def test_order_write_entrypoints_cannot_modify_identity_status():
    write_entrypoints = (
        "create_order",
        "assign_staff_to_order",
        "update_order_status",
        "update_order_full_details",
    )

    for function_name in write_entrypoints:
        node = _function_node(function_name)
        source = _function_source(function_name)
        argument_names = [argument.arg for argument in node.args.args]
        assert "identity_status" not in argument_names
        assert "identity_status" not in source


def test_public_order_matching_and_schedule_entrypoints_are_case_no_only():
    case_scoped_entrypoints = (
        "get_order_by_case_no",
        "create_order",
        "assign_staff_to_order",
        "update_order_status",
        "update_order_full_details",
        "save_order_rest_dates",
        "generate_default_schedule",
        "update_schedule_day",
        "get_order_matches",
        "create_or_get_match_record",
    )

    for function_name in case_scoped_entrypoints:
        node = _function_node(function_name)
        argument_names = [argument.arg for argument in node.args.args]
        source = _function_source(function_name)
        assert "case_no" in argument_names
        assert "order_id" not in argument_names
        assert "legacy_id" not in argument_names
        assert "_resolve_case_no(case_no)" in source

    for function_name in ("update_matching_info_sent", "reply_matching_inquiry"):
        argument_names = [
            argument.arg for argument in _function_node(function_name).args.args
        ]
        assert "match_id" in argument_names
        assert "order_id" not in argument_names
        assert "legacy_id" not in argument_names


def test_beclass_details_are_joined_by_query_no_equal_to_case_no():
    source = _function_source("get_order_details")

    assert "SELECT query_no, survey_details FROM beclass_records" in source
    assert "survey_map[str(br['query_no']).strip()]" in source
    assert "survey_map.get(str(r.get('case_no') or '').strip(), {})" in source
    assert "survey_map.get(str(r.get('client_name')" not in source


def test_beclass_json_is_flattened_to_all_fifteen_contract_fields():
    raw_details = {
        "月子餐點調理喜好/飲食習慣：": "diet",
        "呈上題，若遇無法媒合到葷食服務人員時，是否可以接受蛋奶素服務人員？": "vegetarian",
        "2．餐飲含酒比例：": "alcohol",
        "3．料理用油：(可接受種類)": "oil",
        "5媽咪有無過敏體質：": "allergy",
        "特殊照護時應注意事項：": "care",
        "餐點喜忌備註：": "meal",
        "烹煮工具": "tools",
        "洗澡水準備：": "bath",
        "哺乳方式：": "feeding",
        "特殊計費:甲方同意需另支付當日薪資1倍予乙方。": "holiday",
        "特殊計費:胎數": "births",
        "透天服務樓層方式(會加收樓層費)": "stairs",
        "提供服務人員轎車停車位": "parking",
        "服務時間內是否有其他寶寶": "babies",
    }
    expected = {
        "dietary_habits": "diet",
        "vegetarian_preference": "vegetarian",
        "alcohol_ratio": "alcohol",
        "cooking_oil_type": "oil",
        "maternal_allergy": "allergy",
        "special_care_notes": "care",
        "meal_preferences": "meal",
        "cooking_tools": "tools",
        "bath_water_prep": "bath",
        "breastfeeding_method": "feeding",
        "holiday_pricing_terms": "holiday",
        "multi_birth_count": "births",
        "stair_floor_fee_mode": "stairs",
        "parking_space_provided": "parking",
        "other_babies_present": "babies",
    }

    flattened = db_service.parse_beclass_survey_details(
        json.dumps(raw_details, ensure_ascii=False)
    )

    assert flattened == expected
    assert len(flattened) == 15
