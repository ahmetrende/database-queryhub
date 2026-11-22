"""A mutation that committed (autocommit) but then failed during
result delivery / finalization must be recorded as completed-with-a-warning,
never 'failed' — which would tell the user nothing happened while the change
is in fact applied, and could prompt a duplicate re-run.
"""
from queryhub import executor as ex


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


# ----------------------------------------------- size cap after a commit

def _capped_result():
    return type("R", (), {"truncated_size": True})()


def _stub_size_cap(monkeypatch):
    calls = {"warn": [], "fail": [], "alert": []}
    monkeypatch.setattr(
        ex, "_complete_with_delivery_warning",
        lambda client, request, message: calls["warn"].append(message))
    monkeypatch.setattr(
        ex, "_fail", lambda client, request, message, **k: calls["fail"].append(message))
    monkeypatch.setattr(
        ex, "_alert_admins_size_cap",
        lambda client, request, max_mb, **k: calls["alert"].append(k))
    return calls


def test_size_cap_after_committed_write_completes_with_warning(monkeypatch):
    calls = _stub_size_cap(monkeypatch)
    ex._finalize(None, {"id": 5}, [_capped_result()], [],
                 max_csv_bytes=10 * 1024 * 1024, target=None, elapsed=0.1,
                 committed_mutation=True)
    assert len(calls["warn"]) == 1 and "10 MB" in calls["warn"][0]
    assert calls["fail"] == []
    assert calls["alert"] == [{"applied": True}]


def test_size_cap_on_a_read_still_fails(monkeypatch):
    calls = _stub_size_cap(monkeypatch)
    ex._finalize(None, {"id": 5}, [_capped_result()], [],
                 max_csv_bytes=10 * 1024 * 1024, target=None, elapsed=0.1)
    assert calls["warn"] == []
    assert len(calls["fail"]) == 1 and "result-size limit" in calls["fail"][0]
    assert calls["alert"] == [{}]
