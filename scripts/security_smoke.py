#!/usr/bin/env python3
"""Live security smoke test for QueryHub Web.

Exercises the web-facing attack surface against a RUNNING instance:
authentication bypass, JWT attacks (incl. alg=none), session revocation,
IDOR / ownership, the whitelist gate, static path traversal to host
secrets, error-envelope info leak, OAuth state forgery, and WebSocket
auth. A PASS means the SECURE behavior held.

Run against the local instance (needs the bot env for DB + master key):

    source .venv/bin/activate && set -a && source /etc/queryhub/env \\
      && source /etc/queryhub/web.env && set +a \\
      && python3 scripts/security_smoke.py

Base URL override: QH_WEB_BASE (default https://127.0.0.1:8080).
Exit code 0 iff every check passed — safe to wire into CI.

No real identifiers live in this file: the "valid user" is discovered
from the admins table at runtime; negative cases use synthetic ids. All
test sessions / seed rows are cleaned up before exit.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import sys
import time

import httpx

from queryhub import db
from queryhub.web import sessions

BASE = os.environ.get("QH_WEB_BASE", "https://127.0.0.1:8080")
UA = "security-smoke"
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

# Synthetic, non-real ids for negative cases (never a real employee).
FAKE_UNWHITELISTED = "U0SECSMOKE01"
FAKE_VICTIM = "U0SECSMOKEVIC"

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((bool(ok), name, str(detail)))


def _valid_user() -> str:
    row = db.fetch_one(
        "SELECT slack_user_id FROM admins WHERE enabled = TRUE "
        "ORDER BY added_at LIMIT 1")
    if not row:
        print("no enabled admin found — cannot run authed checks", file=sys.stderr)
        sys.exit(2)
    return row["slack_user_id"]


def _mint(uid: str):
    sid, refresh = sessions.create_session(uid, provider="slack", user_agent=UA)
    tok = sessions.mint_access(
        {"slack_user_id": uid, "name": "smoke", "email": None, "provider": "slack"},
        sid)
    return sid, refresh, tok


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def run_http(c: httpx.Client, valid_uid: str) -> None:
    prot = ["/api/me", "/api/connections", "/api/saved", "/api/history"]

    # --- A. authentication ---
    for p in prot:
        r = c.get(BASE + p)
        check(f"no-auth {p} -> 401",
              r.status_code == 401
              and r.json().get("error", {}).get("code") == "unauthenticated",
              r.status_code)

    r = c.get(BASE + "/api/me", headers=_h("garbage.token.here"))
    check("garbage JWT -> 401", r.status_code == 401, r.status_code)

    _, _, tok = _mint(valid_uid)
    r = c.get(BASE + "/api/me", headers=_h(tok[:-3] + ("aaa" if tok[-3:] != "aaa" else "bbb")))
    check("tampered signature -> 401", r.status_code == 401, r.status_code)

    import jwt as _jwt
    forged = _jwt.encode({"sub": valid_uid, "sid": 999999, "exp": int(time.time()) + 600},
                         "attacker-secret", algorithm="HS256")
    r = c.get(BASE + "/api/me", headers=_h(forged))
    check("wrong-secret JWT -> 401", r.status_code == 401, r.status_code)

    hdr = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    pl = base64.urlsafe_b64encode(
        json.dumps({"sub": valid_uid, "sid": 999999, "exp": int(time.time()) + 600}).encode()
    ).rstrip(b"=").decode()
    r = c.get(BASE + "/api/me", headers=_h(f"{hdr}.{pl}."))
    check("alg=none JWT -> 401", r.status_code == 401, r.status_code)

    sid_e, _, _ = _mint(valid_uid)
    _orig = sessions.access_ttl_minutes
    sessions.access_ttl_minutes = lambda: -5
    try:
        exp_tok = sessions.mint_access(
            {"slack_user_id": valid_uid, "name": "x", "email": None, "provider": "slack"}, sid_e)
    finally:
        sessions.access_ttl_minutes = _orig
    r = c.get(BASE + "/api/me", headers=_h(exp_tok))
    check("expired JWT -> 401", r.status_code == 401, r.status_code)

    sid_r, _, tok_r = _mint(valid_uid)
    sessions.revoke_session(sid_r, "security-smoke")
    r = c.get(BASE + "/api/me", headers=_h(tok_r))
    check("revoked session -> 401", r.status_code == 401, r.status_code)

    ne = sessions.mint_access(
        {"slack_user_id": valid_uid, "name": "x", "email": None, "provider": "slack"}, 987654321)
    r = c.get(BASE + "/api/me", headers=_h(ne))
    check("nonexistent session id -> 401", r.status_code == 401, r.status_code)

    # --- B. whitelist gate ---
    _, _, tok_nw = _mint(FAKE_UNWHITELISTED)
    r = c.get(BASE + "/api/me", headers=_h(tok_nw))
    check("non-whitelisted /me -> 403",
          r.status_code == 403 and r.json().get("error", {}).get("code") == "forbidden",
          r.status_code)
    r = c.get(BASE + "/api/connections", headers=_h(tok_nw))
    check("non-whitelisted /connections -> 403", r.status_code == 403, r.status_code)

    # --- C. IDOR / ownership ---
    vid = db.fetch_one(
        "INSERT INTO requests (requester_slack_id, requester_name, target_server_id, "
        " database_name, query, status) "
        "VALUES (%s,'smoke',1,'queryhub','SELECT 1 -- idor-smoke','completed') "
        "RETURNING id", (FAKE_VICTIM,))["id"]
    _, _, tok = _mint(valid_uid)
    for suffix, label in [("", "status"), ("/result", "result"), ("/result.csv", "csv")]:
        r = c.get(BASE + f"/api/queries/{vid}{suffix}", headers=_h(tok))
        check(f"IDOR /queries/:id{suffix} ({label}) -> 404", r.status_code == 404, r.status_code)
    db.execute("DELETE FROM requests WHERE id = %s", (vid,))

    # --- D. static path traversal to host secrets ---
    for pt in ["/../etc/queryhub/env", "/../etc/queryhub/web.env",
               "/..%2f..%2f..%2f..%2fetc%2fslackbot%2fenv",
               "/%2e%2e/%2e%2e/%2e%2e/etc/passwd", "/app.py",
               "/../src/queryhub/web/sessions.py"]:
        r = c.get(BASE + pt)
        body = r.text if r.status_code == 200 else ""
        leaked = any(s in body for s in
                     ("SLACK_CLIENT_SECRET", "BOT_DB_PASSWORD", "root:", "signing_secret"))
        check(f"traversal {pt[:38]} -> no secret leak", not leaked, r.status_code)

    # --- E. error envelope / info leak ---
    _, _, tok = _mint(valid_uid)
    r = c.get(BASE + "/api/queries/not-an-int", headers=_h(tok))
    check("bad id type -> clean 4xx, no stack",
          r.status_code in (404, 422) and "Traceback" not in r.text, r.status_code)
    r = c.post(BASE + "/api/queries", headers=_h(tok), json={"bogus": 1})
    check("malformed body -> 422 envelope",
          r.status_code == 422 and r.json().get("error", {}).get("code") == "validation",
          r.status_code)
    r = c.get(BASE + "/api/does-not-exist", headers=_h(tok))
    check("unknown route -> 404, no stack",
          r.status_code == 404 and "Traceback" not in r.text, r.status_code)

    # --- F. OAuth state forgery ---
    from queryhub.web import auth_providers as ap
    r = c.get(BASE + f"/api/auth/slack/callback?code=x&state={ap.make_state()}",
              follow_redirects=False)  # signed state but no matching browser cookie
    setc = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") \
        else [r.headers.get("set-cookie", "")]
    # A session grant would set qh_session/qh_refresh to a real value; clearing
    # the pre-auth qh_oauth_state cookie (Max-Age=0) is expected and fine.
    granted = any(
        (sc.startswith("qh_session=") and not sc.startswith("qh_session=;"))
        or (sc.startswith("qh_refresh=") and not sc.startswith("qh_refresh=;"))
        for sc in setc)
    check("forged/unbound OAuth state -> bad_state, no session granted",
          r.status_code == 302 and "auth_error=bad_state" in r.headers.get("location", "")
          and not granted,
          r.status_code)


async def run_ws(valid_uid: str) -> None:
    import websockets

    vid = db.fetch_one(
        "INSERT INTO requests (requester_slack_id, requester_name, target_server_id, "
        " database_name, query, status) "
        "VALUES (%s,'smoke',1,'queryhub','SELECT 1 -- ws-idor-smoke','completed') "
        "RETURNING id", (FAKE_VICTIM,))["id"]
    _, _, tok = _mint(valid_uid)
    ws_base = BASE.replace("https://", "wss://").replace("http://", "ws://")

    async def _connect(cookie: str | None, qid: int):
        hdr = [("Cookie", cookie)] if cookie else []
        try:
            return await websockets.connect(ws_base + f"/api/queries/{qid}/stream",
                                             ssl=_SSL, additional_headers=hdr)
        except TypeError:
            return await websockets.connect(ws_base + f"/api/queries/{qid}/stream",
                                             ssl=_SSL, extra_headers=hdr)

    async def _rejected(cookie, qid) -> bool:
        """True if the server refused to stream (handshake reject or an
        immediate close without any data)."""
        try:
            ws = await _connect(cookie, qid)
        except Exception:
            return True  # handshake rejected
        try:
            await asyncio.wait_for(ws.recv(), timeout=3)
            return False  # got data → it streamed (BAD)
        except websockets.ConnectionClosed:
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            await ws.close()

    check("WS no-cookie -> rejected", await _rejected(None, vid))
    check("WS cross-user (IDOR) -> rejected", await _rejected(f"qh_session={tok}", vid))
    db.execute("DELETE FROM requests WHERE id = %s", (vid,))


def main() -> int:
    valid_uid = _valid_user()
    try:
        with httpx.Client(verify=False, timeout=20) as c:
            run_http(c, valid_uid)
        asyncio.run(run_ws(valid_uid))
    finally:
        # Clean up every artifact this run created.
        db.execute(
            "UPDATE web_sessions SET revoked_at = NOW(), revoked_reason = 'security-smoke' "
            "WHERE user_agent = %s AND revoked_at IS NULL", (UA,))
        db.execute("DELETE FROM web_sessions WHERE user_agent = %s", (UA,))
        db.execute("DELETE FROM requests WHERE requester_slack_id IN (%s, %s)",
                   (FAKE_VICTIM, FAKE_UNWHITELISTED))

    npass = sum(1 for ok, _, _ in _results if ok)
    nfail = len(_results) - npass
    print("\n===== QueryHub Web — security smoke =====")
    for ok, name, detail in _results:
        print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f"   [{detail}]"))
    print(f"\n{npass} PASS / {nfail} FAIL  (PASS = secure behavior)")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
