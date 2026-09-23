"""Two connections that want one name.

The name is the connection's id in the web API, MCP and the admin screens, so
it stays unique. What changed is who gets it: a name held only by a DISABLED
connection is free -- that one takes its engine's suffix and the newcomer gets
the plain name. The case that asked for it: a service moved from RDS to
ClickHouse Cloud, kept its name, and was imported as `<name>-ch` because the
dead RDS row still held it.
"""
import pytest

from queryhub import targets


def _holders(*rows):
    return {r[1].lower(): {"id": r[0], "alias": r[1], "engine": r[2], "enabled": r[3]}
            for r in rows}


def test_a_free_name_is_taken_as_is():
    claim = targets.claim_alias("prod-orders", "clickhouse", _holders(), yield_to_live=True)
    assert claim == targets.AliasClaim("prod-orders")


def test_a_disabled_holder_moves_aside_with_its_own_engine_suffix():
    h = _holders((18, "prod-orders", "postgres", False))
    claim = targets.claim_alias("prod-orders", "clickhouse", h, yield_to_live=True)
    assert claim.alias == "prod-orders"
    assert claim.displaced == (18, "prod-orders", "prod-orders-pg")


def test_a_person_typing_a_disabled_holders_name_gets_it_too():
    h = _holders((18, "prod-orders", "mssql", False))
    claim = targets.claim_alias("prod-orders", "postgres", h, yield_to_live=False)
    assert claim.displaced == (18, "prod-orders", "prod-orders-mssql")


def test_an_enabled_holder_keeps_its_name_and_the_importer_takes_a_suffix():
    h = _holders((18, "prod-orders", "postgres", True))
    claim = targets.claim_alias("prod-orders", "clickhouse", h, yield_to_live=True)
    assert claim == targets.AliasClaim("prod-orders-ch")


def test_an_enabled_holder_refuses_a_name_a_person_typed():
    h = _holders((18, "prod-orders", "postgres", True))
    assert targets.claim_alias("prod-orders", "clickhouse", h, yield_to_live=False) is None


def test_a_suffix_already_in_use_is_numbered():
    h = _holders((18, "prod-orders", "postgres", True), (19, "prod-orders-ch", "clickhouse", True))
    claim = targets.claim_alias("prod-orders", "clickhouse", h, yield_to_live=True)
    assert claim.alias == "prod-orders-ch-2"
    h = _holders((18, "prod-orders", "postgres", False), (19, "prod-orders-pg", "postgres", True))
    claim = targets.claim_alias("prod-orders", "clickhouse", h, yield_to_live=True)
    assert claim.displaced == (18, "prod-orders", "prod-orders-pg-2")


def test_names_differing_only_in_case_clash():
    h = _holders((18, "Prod-Orders", "postgres", True))
    assert targets.claim_alias("prod-orders", "clickhouse", h, yield_to_live=False) is None


def test_a_run_sees_its_own_earlier_claims():
    h = _holders((18, "prod-orders", "postgres", False))
    first = targets.claim_alias("prod-orders", "clickhouse", h, yield_to_live=True)
    targets.note_claim(h, first, engine="clickhouse")
    assert h["prod-orders-pg"]["id"] == 18 and h["prod-orders"]["engine"] == "clickhouse"
    # The newcomer is disabled too, so a second service wanting the same name
    # moves IT aside -- which is why two imports in one run cannot both land
    # on one name.
    second = targets.claim_alias("prod-orders", "postgres", h, yield_to_live=True)
    assert second.displaced[2] == "prod-orders-ch"


class _Cur:
    def __init__(self, rowcount):
        self.rowcount, self.calls = rowcount, []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))


def test_moving_a_holder_aside_is_guarded_on_its_name_and_on_it_being_disabled():
    cur = _Cur(rowcount=1)
    targets.displace_in(cur, targets.AliasClaim("prod-orders", (18, "prod-orders", "prod-orders-pg")))
    sql, params = cur.calls[0]
    assert "AND alias = %s AND NOT enabled" in sql
    assert params == ("prod-orders-pg", 18, "prod-orders")


def test_a_holder_enabled_since_the_plan_is_not_renamed():
    with pytest.raises(targets.AliasTaken):
        targets.displace_in(_Cur(rowcount=0),
                            targets.AliasClaim("prod-orders", (18, "prod-orders", "prod-orders-pg")))


def test_a_free_claim_writes_nothing():
    cur = _Cur(rowcount=0)
    targets.displace_in(cur, targets.AliasClaim("prod-orders"))
    assert cur.calls == []
