#!/usr/bin/env python3
"""Drive a real submit -> approve -> execute round trip against the demo stack.

This is the end-to-end check the mocked unit suite cannot do: it exercises
credential decryption, an actual query against an actual database, PII masking
on the way out, and the tier gate refusing a write. CI runs it against
`docker compose up`; you can run it by hand against any demo stack.

    python scripts/ci_demo_roundtrip.py --base-url http://localhost:8080

Exits non-zero with a specific message on the first failure. Two bugs were
found by writing it, both of which the unit suite passed straight through:
a hardcoded results directory that left the request stuck in 'executing' with
nothing logged, and target_ssl_mode=require against a TLS-less container.

Standard library only — CI should not need to install anything to run it.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request


class Client:
    """Minimal cookie-jar HTTP client. One instance per signed-in user."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("content-type", "application/json")
        # The CSRF gate compares Origin against Host, so a same-origin header is
        # required for any unsafe method.
        req.add_header("origin", self.base)
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw.decode(errors="replace")}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--password", default="queryhub-demo")
    ap.add_argument("--dev-user", default="demo-dev")
    ap.add_argument("--admin-user", default="demo-admin")
    ap.add_argument("--connection", default="demo-postgres")
    ap.add_argument("--database", default="shop")
    ap.add_argument("--timeout", type=int, default=60,
                    help="seconds to wait for execution to finish")
    args = ap.parse_args()

    dev, admin = Client(args.base_url), Client(args.base_url)

    print("1. sign in")
    for who, client, user in (("developer", dev, args.dev_user),
                              ("admin", admin, args.admin_user)):
        code, body = client.request("POST", "/api/auth/local/login",
                                    {"username": user, "password": args.password})
        if code != 200:
            fail(f"{who} login returned {code}: {body}")
    print("   ok")

    print("2. the developer sees the demo connection")
    code, body = dev.request("GET", "/api/connections")
    aliases = [c["id"] for c in body.get("connections", [])]
    if args.connection not in aliases:
        fail(f"{args.connection} not visible to the developer; saw {aliases}")
    print(f"   ok ({aliases})")

    print("3. submit a read touching PII columns")
    # Unique per run on purpose. QueryHub auto-approves a query whose
    # fingerprint a human already approved (that is the feature), so a fixed
    # string makes the second run skip the approval path entirely — which is
    # exactly what happened the first time this script ran twice.
    sql = ("SELECT id, email, phone FROM users ORDER BY id LIMIT 3; "
           f"-- roundtrip {int(time.time())}")
    code, body = dev.request("POST", "/api/queries", {
        "connectionId": args.connection, "databaseId": args.database, "sql": sql})
    if code != 201:
        fail(f"submit returned {code}: {body}")
    request_id = body["id"]
    if body.get("classification", {}).get("tier") != "RO":
        fail(f"a SELECT was not classified RO: {body.get('classification')}")
    if not body.get("pii", {}).get("columns"):
        fail("no PII columns were detected in a query selecting email and phone")
    print(f"   ok (request {request_id}, "
          f"{len(body['pii']['columns'])} PII column(s) flagged)")

    print("4. the admin sees it in the queue and approves")
    code, status_body = dev.request("GET", f"/api/queries/{request_id}")
    if status_body.get("status") == "pending":
        code, body = admin.request("GET", "/api/admin/queue")
        if code != 200:
            fail(f"queue returned {code}: {body}")
        if not any(str(q["id"]) == str(request_id) for q in body.get("queue", [])):
            fail(f"request {request_id} is pending but absent from the admin "
                 f"queue: {body}")
        code, body = admin.request(
            "POST", f"/api/admin/queue/{request_id}/decision",
            {"decision": "approve"})
        if code != 200:
            fail(f"approve returned {code}: {body}")
        print("   ok (approved by the admin)")
    else:
        # An auto-approve grant or a fingerprint match can legitimately take a
        # read straight to execution. Say so rather than failing: the assertions
        # that matter are the masked result and the refused write.
        print(f"   skipped — already {status_body.get('status')!r} "
              f"(auto-approved)")

    print("5. wait for execution")
    deadline = time.time() + args.timeout
    status = None
    while time.time() < deadline:
        code, body = dev.request("GET", f"/api/queries/{request_id}")
        status = body.get("status")
        if status in ("done", "failed", "cancelled"):
            break
        time.sleep(2)
    if status != "done":
        fail(f"execution ended as {status!r} (expected 'done'): {body}")
    print(f"   ok ({body.get('rowCount')} rows in {body.get('runMs')} ms)")

    print("6. the result comes back MASKED")
    code, body = dev.request("GET", f"/api/queries/{request_id}/result?limit=3")
    if code != 200:
        fail(f"result returned {code}: {body}")
    rows = body.get("rows") or []
    if len(rows) != 3:
        fail(f"expected 3 rows, got {len(rows)}: {body}")
    for row in rows:
        if "*" not in str(row.get("email", "")):
            fail(f"email came back unmasked: {row}")
        if "*" not in str(row.get("phone", "")):
            fail(f"phone came back unmasked: {row}")
    print(f"   ok ({rows[0]})")

    print("7. a write is refused for a read-only grant")
    code, body = dev.request("POST", "/api/queries", {
        "connectionId": args.connection, "databaseId": args.database,
        "sql": "UPDATE users SET status = 'x' WHERE id = 1;"})
    if code != 403:
        fail(f"a write on an RO grant returned {code}, expected 403: {body}")
    print(f"   ok ({body.get('error', {}).get('code')})")

    print("8. an always-true WHERE is refused")
    code, body = dev.request("POST", "/api/queries", {
        "connectionId": args.connection, "databaseId": args.database,
        "sql": "UPDATE users SET status = 'x' WHERE id IS NOT NULL OR id IS NULL;"})
    if code not in (403, 422):
        fail(f"an unfiltered UPDATE returned {code}, expected 422/403: {body}")
    print(f"   ok ({body.get('error', {}).get('code')})")

    print("\nround trip OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
