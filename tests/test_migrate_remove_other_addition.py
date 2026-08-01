"""
File: test_migrate_remove_other_addition.py
Description: 測試 scripts/migrate_remove_other_addition.py 的純函式契約。
"""
import pytest

from scripts.migrate_remove_other_addition import RemoveOtherAdditionMigration

def test_migration_pure_function_logic():
    # 測試純函式邏輯與 Invariants
    res = RemoveOtherAdditionMigration(50, 0)
    assert res["contract_complete"] is True
    assert res["row_count"] == 50

    with pytest.raises(ValueError, match="nonzero other_addition"):
        RemoveOtherAdditionMigration(50, 100)
