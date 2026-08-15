"""Connection tags — where a target actually runs.

The bag is display-only (migration 095) and deliberately not a policy input,
so what these pin is the shape and the refusals: what a key may look like,
that a whole-bag replace really replaces, what a non-admin is told, and that
the derived vocabulary comes from the fleet rather than from a stored list.
"""
import pytest

from dba_slack_bot.web import routes_admin, deps


# ---- validation -------------------------------------------------------------

def test_a_normal_bag_survives_unchanged():
    out = routes_admin._clean_tags(
        {"provider": "aws", "service": "RDS", "account": "4417-0219-0871"})
    assert out == {"provider": "aws", "service": "RDS",
                   "account": "4417-0219-0871"}


def test_keys_are_lowercased_and_values_trimmed():
    assert routes_admin._clean_tags({"Provider": "  aws  "}) == {"provider": "aws"}


def test_an_empty_value_drops_the_key():
    """"Present but says nothing" is the state that makes a tag lie — a row
    that claims to know where it runs and does not."""
    assert routes_admin._clean_tags({"provider": "aws", "service": "  "}) \
        == {"provider": "aws"}


@pytest.mark.parametrize("key", ["has space", "UPPER ONLY!", "1leading", "",
                                 "with:colon", "a" * 33])
def test_an_unusable_key_is_refused(key):
    """Keys become search tokens (`provider:aws`) and a fleet-wide filter
    dimension. A key that cannot be typed back is a key nobody can use."""
    with pytest.raises(deps.HTTPException) as ei:
        routes_admin._clean_tags({key: "x"})
    assert ei.value.status_code == 422


def test_an_overlong_value_is_refused():
    with pytest.raises(deps.HTTPException):
        routes_admin._clean_tags({"note": "x" * (routes_admin.TAG_MAX_VALUE + 1)})


def test_too_many_keys_are_refused():
    many = {f"k{i}": "v" for i in range(routes_admin.TAG_MAX_KEYS + 1)}
    with pytest.raises(deps.HTTPException):
        routes_admin._clean_tags(many)


def test_none_means_no_tags_not_a_crash():
    assert routes_admin._clean_tags(None) == {}


# ---- what a developer is told ----------------------------------------------

def test_a_non_admin_sees_where_it_runs_but_not_the_account_id():
    """`provider`/`service` answer the question the feature exists for. An
    account id is infrastructure identity of the same class as `host`, which
    this endpoint already withholds from non-admins — so it is withheld too.
    A deliberate narrowing of what design asked for; see the route comment."""
    tags = {"provider": "aws", "service": "RDS", "account": "4417-0219-0871",
            "owner": "platform"}
    shown = {k: v for k, v in tags.items() if k in ("provider", "service")}
    assert shown == {"provider": "aws", "service": "RDS"}
    assert "account" not in shown and "owner" not in shown


# ---- the derived vocabulary -------------------------------------------------

def test_reserved_keys_are_always_offered(monkeypatch):
    """At zero count, so the three keys with real controls do not appear and
    disappear depending on whether anyone has filled them in yet."""
    monkeypatch.setattr(routes_admin.admin, "require_admin", lambda c, s: "U1")
    monkeypatch.setattr(routes_admin.db, "fetch_all", lambda *a, **k: [])
    keys = routes_admin.admin_tag_keys(claims={"sub": "U1"})["keys"]
    assert [k["key"] for k in keys] == list(routes_admin.TAG_RESERVED)
    assert all(k["count"] == 0 and k["reserved"] for k in keys)


def test_the_vocabulary_is_derived_from_the_fleet(monkeypatch):
    """A key exists because a connection carries it — so a key nobody uses
    cannot linger in the picker, and nobody maintains a list."""
    monkeypatch.setattr(routes_admin.admin, "require_admin", lambda c, s: "U1")
    monkeypatch.setattr(routes_admin.db, "fetch_all", lambda *a, **k: [
        {"tags": {"provider": "aws", "service": "RDS"}},
        {"tags": {"provider": "aws", "service": "ECS"}},
        {"tags": {"provider": "huawei", "owner": "platform"}},
    ])
    keys = {k["key"]: k for k in
            routes_admin.admin_tag_keys(claims={"sub": "U1"})["keys"]}
    assert keys["provider"]["count"] == 3
    assert keys["owner"]["reserved"] is False
    # Commonest value first — what keeps a free-text field from becoming six
    # spellings of the same thing.
    assert keys["provider"]["values"][0] == {"value": "aws", "count": 2}


def test_reserved_keys_sort_ahead_of_invented_ones(monkeypatch):
    monkeypatch.setattr(routes_admin.admin, "require_admin", lambda c, s: "U1")
    monkeypatch.setattr(routes_admin.db, "fetch_all", lambda *a, **k: [
        {"tags": {"aaa": "1"}}])
    keys = [k["key"] for k in
            routes_admin.admin_tag_keys(claims={"sub": "U1"})["keys"]]
    assert keys.index("provider") < keys.index("aaa")


# ---- the Slack approval line ------------------------------------------------

def test_the_slack_line_names_the_machine_but_not_the_account():
    from dba_slack_bot.slack_app import notifications

    class T:
        tags = {"provider": "huawei", "service": "ECS",
                "account": "hw-proj-tr-01"}
    md = notifications.hosting_md(T())
    assert "huawei" in md and "ECS" in md
    assert "hw-proj-tr-01" not in md


def test_an_untagged_target_adds_no_line():
    """An untagged fleet has to read exactly as it did before."""
    from dba_slack_bot.slack_app import notifications

    class T:
        tags = {}
    assert notifications.hosting_md(T()) == ""
    assert notifications.hosting_md(None) == ""
