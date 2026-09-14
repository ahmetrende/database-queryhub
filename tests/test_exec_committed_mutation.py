"""A mutation that committed (autocommit) but then failed during
result delivery / finalization must be recorded as completed-with-a-warning,
never 'failed' — which would tell the user nothing happened while the change
is in fact applied, and could prompt a duplicate re-run.
"""
from dba_slack_bot import executor as ex


def test_on_committed_fires_right_after_execute():
    class _Cur:
        description = None       # no result set -> returns after rowcount
        rowcount = 1

        def execute(self, sql):
            pass

    marker = {"committed": False}
    stmt = type("S", (), {"rewritten": "UPDATE t SET x = 1", "leading": "UPDATE"})()
    res = ex._execute_main_statement(
        _Cur(), stmt, 1, 99, False, 100, 1000,
        on_committed=lambda: marker.__setitem__("committed", True),
    )
    assert marker["committed"] is True
    assert res.rowcount == 1


class _Cur:
    rowcount = 1

    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append(sql)


class _Txn:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return _Cur(self.sink)

    def __exit__(self, *a):
        return False


def test_delivery_warning_marks_completed_not_failed(monkeypatch):
    sqls: list[str] = []
    monkeypatch.setattr(ex.db, "transaction", lambda: _Txn(sqls))
    monkeypatch.setattr(ex.audit, "log_in", lambda *a, **k: None)
    monkeypatch.setattr(ex, "_deliver_result_to_requester", lambda r: False)
    # bundle branch -> stubbed, and avoids the admin-message f-string path
    monkeypatch.setattr(ex.notifications, "update_bundle_admin_dms",
                        lambda *a, **k: None)

    req = {"id": 5, "bundle_id": 3, "requester_slack_id": "U0R",
           "target_server_id": 7, "database_name": "db"}
    ex._complete_with_delivery_warning(client=None, request=req, message="boom")

    assert any("status = 'completed'" in s for s in sqls)
    assert not any("'failed'" in s for s in sqls)
