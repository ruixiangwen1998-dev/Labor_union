import pytest

from scripts.imports import import_finance_excel as importer


@pytest.mark.parametrize(
    "reason",
    [
        "virtual_account_invalid",
        "client_payment_not_found",
        "client_payment_not_unique",
        "receipt_exceeds_remaining",
        "snapshot_terms_incomplete",
    ],
)
def test_client_receipt_boundary_result_is_preserved_as_pending(monkeypatch, reason):
    """FinanceImport must not replace a receipt service boundary decision with a guess."""
    expected = {"result": "pending", "reason": reason}
    calls = []

    def reconcile(cursor, row_id):
        calls.append(row_id)
        return expected

    monkeypatch.setattr(importer, "reconcile_client_receipt", reconcile)

    result = importer._dispatch_inserted_row(
        object(),
        {"row_id": 801, "classification_type": "client_receipt", "result": "inserted"},
    )

    assert calls == [801]
    assert result == expected


def test_non_business_incoming_row_is_pending_without_client_receipt_side_effect(monkeypatch):
    called = []
    monkeypatch.setattr(
        importer,
        "reconcile_client_receipt",
        lambda cursor, row_id: called.append(row_id),
    )
    monkeypatch.setattr(
        importer,
        "_load_dispatch_row",
        lambda cursor, row_id: {
            "id": row_id,
            "classification_reason": "sinopac_invalid_or_missing_virtual_account",
        },
    )

    result = importer._dispatch_inserted_row(
        object(),
        {"row_id": 802, "classification_type": "non_business_review", "result": "inserted"},
    )

    assert result == {
        "result": "pending",
        "reason": "sinopac_invalid_or_missing_virtual_account",
    }
    assert called == []
