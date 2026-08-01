from services import legacy_caregiver_matching_service as service


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.executed.append((query, params))

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row):
        self.cursor_obj = _Cursor(row)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


def test_legacy_information_service_owns_db_lookup_and_visible_result(monkeypatch):
    sent = []
    connection = _Connection(
        {
            "id": 9,
            "case_no": "CASE-9",
            "staff_id": 8,
            "name": "王月嫂",
            "line_user_id": "U123",
            "client_name": "客戶",
        }
    )
    monkeypatch.setattr(
        service.db_service,
        "update_matching_info_sent",
        lambda match_id, info_type: sent.append((match_id, info_type)),
    )
    monkeypatch.setattr(service.db_service, "get_connection", lambda: connection)

    result = service.send_legacy_matching_information(9, 1)

    assert sent == [(9, 1)]
    assert result["data"] == {
        "match_id": 9,
        "line_pushed": True,
        "info_type": 1,
    }
    assert "王月嫂" in result["message"]
    assert connection.closed is True


def test_legacy_resume_actions_record_delivery_state(monkeypatch):
    match_calls = []
    case_calls = []
    monkeypatch.setattr(
        service.db_service,
        "mark_resume_sent",
        lambda match_id: match_calls.append(match_id) or True,
    )
    monkeypatch.setattr(
        service.db_service,
        "mark_resume_sent_for_case",
        lambda case_no: case_calls.append(case_no) or 17,
    )

    assert service.send_legacy_resume_to_client(9) is True
    assert service.send_legacy_resume_for_case(" CASE-9 ") == 17
    assert match_calls == [9]
    assert case_calls == ["CASE-9"]
