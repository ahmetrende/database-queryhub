"""Nobody can present themselves as an admin or a super-admin.

Every authority decision in this system is keyed on one string: the principal id.
`admins.is_super_admin(uid)` decides whether tier gates and the approval
requirement apply at all, so the whole model rests on that id being the one the
platform authenticated — never one the caller chose.

These tests attack that from both transports:

  web   — the id lives in a signed access token. Forge it, re-sign it, expire it,
          swap the session it rides, or smuggle an authority claim into it.
  Slack — the id comes from the event payload's authenticated `user` field. The
          attack there is a handler reading an id out of somewhere the submitting
          user controls: `private_metadata`, a button's `value`, form state.

Two of these are structural (they read the source), because the property is
"no code path does X" and no single call can demonstrate that.
"""
from __future__ import annotations

import os
import pathlib
import re
import time

os.environ.setdefault("WEB_SESSION_SECRET", "test-secret-not-for-prod")

import jwt  # noqa: E402

from dba_slack_bot.web import sessions  # noqa: E402

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "dba_slack_bot"

VICTIM = "U0SUPERADMIN1"   # a placeholder, not a real account
ATTACKER = "U0999ATTACK1"

IDENTITY = {"slack_user_id": ATTACKER, "name": "A", "email": "a@example.com",
            "provider": "slack", "avatar": None}


# ---------------------------------------------------------------------------
# web: the token itself
# ---------------------------------------------------------------------------

def test_a_valid_token_round_trips():
    """The control. If this ever fails the negatives below prove nothing."""
    claims = sessions.verify_access(sessions.mint_access(IDENTITY, 1))
    assert claims and claims["sub"] == ATTACKER


def test_an_unsigned_token_is_rejected():
    """`alg: none` is the classic one: strip the signature, keep the payload."""
    forged = jwt.encode({"sub": VICTIM, "sid": 1}, key="", algorithm="none")
    assert sessions.verify_access(forged) is None


def test_a_token_signed_with_another_secret_is_rejected():
    forged = jwt.encode({"sub": VICTIM, "sid": 1,
                         "exp": int(time.time()) + 600},
                        "attacker-secret", algorithm="HS256")
    assert sessions.verify_access(forged) is None


def test_editing_sub_in_a_real_token_invalidates_it():
    """The realistic attempt: take your own valid token, change one field."""
    good = sessions.mint_access(IDENTITY, 1)
    header, payload, sig = good.split(".")
    import base64
    import json as _json

    def b64d(s):
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def b64e(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    body = _json.loads(b64d(payload))
    body["sub"] = VICTIM
    tampered = f"{header}.{b64e(_json.dumps(body).encode())}.{sig}"
    assert sessions.verify_access(tampered) is None


def test_an_expired_token_is_rejected():
    """Signed with the real secret, so only `exp` stands between it and a pass."""
    stale = jwt.encode({"sub": ATTACKER, "sid": 1,
                        "exp": int(time.time()) - 10},
                       sessions._signing_secret(), algorithm="HS256")
    assert sessions.verify_access(stale) is None
    # The control: same shape, future exp, accepted. Without this the assertion
    # above would also pass if verify_access rejected everything.
    fresh = jwt.encode({"sub": ATTACKER, "sid": 1,
                        "exp": int(time.time()) + 600},
                       sessions._signing_secret(), algorithm="HS256")
    assert (sessions.verify_access(fresh) or {}).get("sub") == ATTACKER


def test_a_refresh_token_is_not_an_access_token():
    """Refresh tokens are opaque random strings, not JWTs signed with the same
    secret — so one cannot be replayed on a protected route."""
    import secrets as _s
    opaque = _s.token_urlsafe(48)
    assert sessions.verify_access(opaque) is None
    assert "." not in opaque or sessions.verify_access(opaque) is None


# ---------------------------------------------------------------------------
# web: the token carries no authority
# ---------------------------------------------------------------------------

def test_the_token_contains_no_authority_claim():
    """If admin status were a claim, a stale token would keep it after the row
    was removed — and a secret leak would mint it directly. It must not be there.
    """
    claims = sessions.verify_access(sessions.mint_access(IDENTITY, 1))
    for forbidden in ("is_admin", "admin", "is_super_admin", "super_admin",
                      "scope", "max_tier", "role", "roles", "can_grant",
                      "unrestricted", "tier"):
        assert forbidden not in claims, (
            f"the access token carries `{forbidden}` — authority must be read "
            f"from the database on every request, not from the token")


def test_smuggling_an_authority_claim_gains_nothing():
    """A validly signed token with extra claims still only says WHO you are."""
    token = jwt.encode({"sub": ATTACKER, "sid": 1, "is_super_admin": True,
                        "max_tier": None, "unrestricted": True,
                        "exp": int(time.time()) + 600},
                       sessions._signing_secret(), algorithm="HS256")
    claims = sessions.verify_access(token)
    assert claims is not None, "signature is valid, so it decodes"
    # The gate is that nothing reads these. Proven structurally below.
    assert claims["sub"] == ATTACKER


def test_no_code_reads_authority_from_session_claims():
    """Structural: the only source of admin status is admins.py, keyed by id.

    A route that trusted `claims["is_admin"]` would pass every behavioural test
    written against a well-formed token, so the property has to be read off the
    source.
    """
    offenders = []
    for path in (SRC / "web").rglob("*.py"):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r'claims(\.get\(|\[)\s*["\'](is_)?(admin|super_admin|'
                         r'max_tier|scope|role|unrestricted)', line):
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, (
        "authority read out of the session token:\n  " + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# web: the session must belong to the identity claiming it
# ---------------------------------------------------------------------------

def test_session_alive_binds_the_row_to_the_principal(monkeypatch):
    """A token asserting `sub`=victim while riding the attacker's `sid` must not
    pass. Reaching this needs the signing secret, so it is defence in depth —
    but it is the difference between one secret and two independent facts."""
    seen = {}

    def fake_fetch_one(sql, params):
        seen["sql"], seen["params"] = sql, params
        # Emulate the row: session 7 belongs to the ATTACKER.
        if "slack_user_id = %s" in sql:
            return {"ok": 1} if params == (7, ATTACKER) else None
        return {"ok": 1}

    monkeypatch.setattr(sessions.db, "fetch_one", fake_fetch_one)

    assert sessions.session_alive(7, ATTACKER) is True
    assert sessions.session_alive(7, VICTIM) is False, (
        "a session row was accepted for a different principal")
    assert "slack_user_id = %s" in seen["sql"], (
        "the binding is not in the SQL, so it is not enforced by the database")


def test_the_request_path_passes_the_principal(monkeypatch):
    """The check is worthless if the call site omits the second argument."""
    for f in ("deps.py", "routes_queries.py"):
        src = (SRC / "web" / f).read_text()
        calls = re.findall(r"session_alive[^\n]*", src)
        assert calls, f"no session_alive call in {f}"
        for c in calls:
            assert "sub" in c, (
                f"{f}: session_alive called without the principal: {c.strip()}")


# ---------------------------------------------------------------------------
# Slack: identity comes from the authenticated payload field, nowhere else
# ---------------------------------------------------------------------------

def test_slack_handlers_take_identity_only_from_the_user_field():
    """Every `user_id = ...` assignment in the Slack handlers must read
    body["user"]["id"] or body["user_id"] — the fields Slack itself populates.

    Anything else (private_metadata, a block action's value, view state) is
    round-tripped through the client and therefore chooseable by the submitter.
    """
    src = (SRC / "slack_app" / "handlers.py").read_text()
    bad = []
    for i, line in enumerate(src.splitlines(), 1):
        m = re.match(r"\s*(?:actor_id|user_id|principal|requester_id)\s*=\s*(.+)$",
                     line)
        if not m:
            continue
        rhs = m.group(1)
        if re.search(r'body(\.get\(|\[)\s*["\'](user|user_id)', rhs):
            continue
        if re.search(r'user\[["\']id["\']\]', rhs):     # loop over a payload user
            continue
        if re.match(r"^[a-z_]+(\.[a-z_]+)*\s*$", rhs.strip()):   # passthrough
            continue
        bad.append(f"handlers.py:{i}: {line.strip()}")
    assert not bad, ("identity assigned from something other than the "
                     "authenticated payload field:\n  " + "\n  ".join(bad))


def test_private_metadata_never_carries_an_identity():
    """private_metadata is server-set but client-returned, so treating it as
    identity would be trusting the round trip. It carries target/db/request ids
    on purpose; this pins that it stays that way."""
    offenders = []
    for path in (SRC / "slack_app").rglob("*.py"):
        text = path.read_text()
        for m in re.finditer(r'"private_metadata":\s*(.+)', text):
            payload = m.group(1)
            if re.search(r'user|slack_id|actor|admin', payload, re.IGNORECASE):
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}: {payload.strip()[:70]}")
    assert not offenders, (
        "an identity is being written into private_metadata:\n  "
        + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# the unrestricted flag: derived from the DB, never from a request
# ---------------------------------------------------------------------------

def _rhs_after(line: str, start: int) -> str:
    """The expression assigned at `start`, ending at a top-level `,` or `)`.

    Paren-aware on purpose: a naive `.+?(?:,|\\)|$)` stops inside
    `is_super_admin(request["requester_slack_id"])` and hands back a truncated
    string that matches nothing, which read as a violation of the correct call.
    """
    depth = 0
    for i in range(start, len(line)):
        c = line[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return line[start:i]
            depth -= 1
        elif c == "," and depth == 0:
            return line[start:i]
    return line[start:]


def test_unrestricted_is_only_ever_set_from_is_super_admin():
    """`analyze(..., unrestricted=True)` removes the bulk-destructive refusals.
    It must be reachable only via admins.is_super_admin — never from a request
    body, a query parameter or a Slack form field."""
    # The rule is shape, not vocabulary: the right-hand side must be exactly one
    # call to is_super_admin and nothing else. That rejects
    # `confirmed or admins.is_super_admin(uid)` — the loophole a mutation run
    # found in the first version of this check — while accepting either
    # server-derived argument (`user_id` at submit, `request["requester_slack_id"]`
    # at execution; both were written by this process from an authenticated
    # identity). An earlier version banned lines mentioning `request`, which
    # flagged the correct execution-time call as if it were client input.
    ONLY_THE_CALL = re.compile(
        r"^\s*(?:admins\.)?is_super_admin\([^()]*(?:\([^()]*\))?[^()]*\)\s*$")
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        # Only files that actually call the classifier. `unrestricted` is an
        # ordinary word: teams.py has a local of that name meaning "this grant
        # names no databases", which has nothing to do with the safety path.
        if "query_safety.analyze(" not in text and "def analyze(" not in text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "unrestricted" not in line or "def analyze" in line:
                continue
            m = re.search(r"unrestricted\s*=\s*", line)
            if not m:
                continue
            rhs = _rhs_after(line, m.end())
            if re.fullmatch(r"\s*(False|unrestricted)\s*", rhs):
                continue
            if not ONLY_THE_CALL.match(rhs):
                offenders.append(f"{path.relative_to(SRC)}:{i}: {line.strip()}")
    assert not offenders, (
        "`unrestricted` must be exactly one is_super_admin() call — anything "
        "else can mix in something the caller supplied:\n  "
        + "\n  ".join(offenders))
