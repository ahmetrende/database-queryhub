"""Pure tests for web/routes_admin.py helpers."""
from queryhub.web import routes_admin


def test_parse_grant_id_user():
    p = routes_admin._parse_grant_id("u:U0AB12CD34:7")
    assert p == {"kind": "u", "subject": "U0AB12CD34", "target_id": 7}


def test_parse_grant_id_team():
    p = routes_admin._parse_grant_id("t:12:3")
    assert p == {"kind": "t", "subject": "12", "target_id": 3}


def test_parse_grant_id_rejects_bad():
    assert routes_admin._parse_grant_id("") is None
    assert routes_admin._parse_grant_id("x:1:2") is None        # bad kind
    assert routes_admin._parse_grant_id("u:U1") is None          # too few parts
    assert routes_admin._parse_grant_id("u:U1:notint") is None   # non-int target
    assert routes_admin._parse_grant_id("u:U1:2:3") is None      # too many parts
