from importlib import import_module

import pytest

from ui.pages.order.tab2_assign import _single_caregiver_covers_service_period


class _Tab:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _SchedulingShell:
    def __init__(self):
        self.tab_labels = None

    def title(self, _label):
        pass

    def tabs(self, labels):
        self.tab_labels = labels
        return [_Tab() for _label in labels]

    def error(self, message):
        raise AssertionError(message)


def test_scheduling_page_renders_three_product_tabs(monkeypatch):
    page = import_module("ui.pages.03_calendar")
    shell = _SchedulingShell()
    rendered = []

    monkeypatch.setattr(page, "st", shell)
    monkeypatch.setattr(page.nav_helper, "current_queue_item", lambda _key: None)
    monkeypatch.setattr(page, "_render_staff_calendar", lambda: rendered.append("calendar"))
    monkeypatch.setattr(page, "_load_matching_center_data", lambda: (["order"], ["staff"]))
    monkeypatch.setattr(
        page,
        "render_matching_center",
        lambda orders, staff: rendered.append(("matching", orders, staff)),
    )
    monkeypatch.setattr(page, "render_case_staffing", lambda: rendered.append("staffing"))

    page.show()

    assert shell.tab_labels == ["服務人員月曆", "月嫂配對中心", "案件人力配置"]
    assert rendered == [
        "calendar",
        ("matching", ["order"], ["staff"]),
        "staffing",
    ]


@pytest.mark.parametrize(
    ("complete_combinations", "expected"),
    [
        ([{"staff_ids": [531]}], True),
        ([], False),
    ],
)
def test_single_caregiver_gate_uses_complete_full_period_combinations(
    monkeypatch, complete_combinations, expected
):
    captured = {}

    def fake_request(path, **kwargs):
        captured["path"] = path
        captured["payload"] = kwargs["payload"]
        return {"complete_combinations": complete_combinations}

    monkeypatch.setattr(
        "ui.pages.order.tab2_assign._api_request",
        fake_request,
    )
    result = _single_caregiver_covers_service_period(
        {
            "case_no": "115000015",
            "start_date": "2026-12-06",
            "end_date": "2026-12-20",
        },
        headers={"X-Internal-API-Key": "test"},
    )

    assert result is expected
    assert captured["path"] == (
        "/api/v1/orders/115000015/caregiver-single-eligibility/check"
    )
    assert captured["payload"]["start_date"] == "2026-12-06"
    assert captured["payload"]["end_date"] == "2026-12-20"
