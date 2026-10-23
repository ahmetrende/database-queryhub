"""The end-date controls of the access screens and the routes behind them agree.

The Grants, Auto-approve and Admin scopes screens (2026-10-23) send three
writes: an Extend (PATCH /admin/grants/{id}), a pause or resume (PATCH
/admin/auto-grants/{id}) and a scope end (POST /admin/scopes with
`expiresAt`). Two of those bodies are `extra = "forbid"` on the server, so a
field the client adds and the route does not know is a 422, not a field that
is quietly dropped. And the screens read `enabled` and `runCount` from the
rule list. These tests read both sides and compare them, without a database.
"""
from __future__ import annotations

import re
from pathlib import Path

from queryhub.web import routes_admin

ROOT = Path(__file__).resolve().parents[1]
CLIENT = (ROOT / "QueryHubWeb" / "qh-api.jsx").read_text(encoding="utf-8")
SCREEN = (ROOT / "QueryHubWeb" / "qh-admin-access.jsx").read_text(encoding="utf-8")


def _route_methods(path: str) -> set[str]:
    out: set[str] = set()
    for r in routes_admin.router.routes:
        if getattr(r, "path", None) == path:
            out |= set(getattr(r, "methods", set()))
    return out


def _client_body_keys(method: str) -> set[str]:
    m = re.search(rf"{method}:\(.*?JSON\.stringify\(\{{(.*?)\}}\)", CLIENT, flags=re.S)
    assert m, f"qhApi.{method} is missing or sends no JSON body"
    return set(re.findall(r"(\w+)\s*:", m.group(1)))


def test_extend_is_a_patch_the_route_serves():
    assert "PATCH" in _route_methods("/api/admin/grants/{gid}")
    assert re.search(r"adminExtendGrant:.*'/admin/grants/'.*method: 'PATCH'", CLIENT)


def test_extend_sends_only_fields_the_route_accepts():
    accepted = set(routes_admin.GrantPatchIn.model_fields)
    sent = _client_body_keys("adminExtendGrant")
    assert sent == {"expiresAt", "reason"}
    assert sent <= accepted
    assert routes_admin.GrantPatchIn.model_config.get("extra") == "forbid"


def test_pause_is_a_patch_with_enabled_only():
    assert "PATCH" in _route_methods("/api/admin/auto-grants/{grant_id}")
    assert _client_body_keys("adminSetAutoGrant") == set(routes_admin.AutoGrantPatchIn.model_fields)


def test_scope_end_is_a_field_the_scope_body_takes():
    assert "expiresAt" in routes_admin.ScopeIn.model_fields
    assert "expiresAt" in SCREEN.split("function ScopeForm", 1)[1].split("function ScopesView", 1)[0]


def test_the_rule_list_sends_what_the_screen_reads():
    src = (ROOT / "src" / "queryhub" / "web" / "routes_admin.py").read_text(encoding="utf-8")
    body = src.split("def admin_auto_grants", 1)[1].split("\ndef ", 1)[0]
    for field in ("enabled", "runCount"):
        assert f'"{field}"' in body, f"GET /admin/auto-grants no longer sends {field}"
        assert f"a.{field}" in SCREEN, f"the screen no longer reads {field}"


def test_the_screen_names_the_refusal_codes_the_routes_send():
    src = (ROOT / "src" / "queryhub" / "web" / "routes_admin.py").read_text(encoding="utf-8")
    for code in ("grant_ended", "rule_ended"):
        assert f'"{code}"' in src, f"the server no longer answers {code}"
        assert f"'{code}'" in SCREEN, f"the screen no longer reads {code}"
