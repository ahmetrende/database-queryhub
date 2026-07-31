#!/usr/bin/env python3
"""Verify a REAL install — the docker-compose.install.yml path, not the demo.

`ci_demo_roundtrip.py` proves the front door works. This proves the thing people
actually deploy works, which is a different set of claims:

  - the first start created an admin, so there is a way in without a shell
  - that bootstrap password cannot be used to do anything except change itself
  - changing it unlocks the app
  - the install is CLEAN: no demo accounts, no seeded connections

The third and fourth are the ones worth automating. A bootstrap password printed
into container logs is only acceptable because it is single-use; if the
must_change_pw gate ever stopped applying to a fresh install, the logs would
become a working credential and nothing would look broken. And an install file
that quietly grew demo seeding would ship published passwords to production.

    python scripts/ci_install_check.py --base-url http://localhost:8080 \\
        --username admin --password <the printed one>

Standard library only — CI should not need to install anything to run it.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request

_NEW_PASSWORD = "ci-rotated-password-9tK2"


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
        # The CSRF gate compares Origin against Host.
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
    ap.add_argument("--username", required=True,
                    help="the QH_ADMIN_USER the first start created")
    ap.add_argument("--password", required=True,
                    help="its bootstrap password (printed once, or from .env)")
    args = ap.parse_args()

    admin = Client(args.base_url)

    print("1. the app is up")
    code, _ = admin.request("GET", "/healthz")
    if code != 200:
        fail(f"/healthz returned {code}")
    print("   ok")

    print("2. the bootstrap admin can sign in")
    code, body = admin.request("POST", "/api/auth/local/login",
                               {"username": args.username,
                                "password": args.password})
    if code != 200:
        fail(f"login as {args.username} returned {code}: {body}. "
             "The first start should have created this account.")
    print("   ok")

    print("3. ...but cannot do anything until the password is changed")
    code, body = admin.request("GET", "/api/connections")
    # Envelope is {"error": {"code": ..., "message": ...}} (app.py's handler).
    if code != 403 or body.get("error", {}).get("code") != "password_change_required":
        fail("the bootstrap credential was accepted for a real request "
             f"({code}: {body}). must_change_pw is not being enforced, which "
             "makes the password in the container logs a working credential.")
    print("   ok (403 password_change_required)")

    print("4. changing it revokes the session and unlocks the app")
    code, body = admin.request("POST", "/api/auth/local/change-password",
                               {"currentPassword": args.password,
                                "newPassword": _NEW_PASSWORD})
    if code != 200:
        fail(f"change-password returned {code}: {body}")
    if not body.get("reauth"):
        fail("change-password did not ask for re-authentication; every session "
             "is supposed to be revoked")
    admin = Client(args.base_url)  # cookies were deleted; sign in again
    code, body = admin.request("POST", "/api/auth/local/login",
                               {"username": args.username,
                                "password": _NEW_PASSWORD})
    if code != 200:
        fail(f"login with the new password returned {code}: {body}")
    print("   ok")

    print("5. the account really is an admin")
    code, body = admin.request("GET", "/api/admin/queue")
    if code != 200:
        fail(f"/api/admin/queue returned {code}: {body}. --admin should have "
             "created an unrestricted admin row.")
    print("   ok")

    print("6. nothing was seeded: no connections")
    code, body = admin.request("GET", "/api/connections")
    if code != 200:
        fail(f"/api/connections returned {code}: {body}")
    conns = body.get("connections", [])
    if conns:
        fail(f"a fresh install already has connections: {conns}. The install "
             "file is seeding something it should not.")
    print("   ok")

    print("7. nothing was seeded: the demo accounts do not exist")
    # The demo's credentials are published in docker-compose.yml. If the install
    # path ever sets QH_DEMO, they exist in production with a known password.
    for user, pw in (("demo-admin", "queryhub-demo"),
                     ("demo-dev", "queryhub-demo")):
        code, _ = Client(args.base_url).request(
            "POST", "/api/auth/local/login", {"username": user, "password": pw})
        if code == 200:
            fail(f"the demo account '{user}' exists with its published "
                 "password — QH_DEMO leaked into the install path")
    print("   ok")

    print("\nPASS: the install path works and is clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
