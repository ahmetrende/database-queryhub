"""Create or update a LOCAL QueryHub account (vanilla-profile login).

The vanilla profile runs QueryHub Web without Slack or any external IdP.
Users log in with a built-in username/password account whose principal id
is `local:<username>` — a first-class identity in the same namespace as
Slack ids, so it flows through grants / admins / audit unchanged.

Run on the host with the bot environment loaded (DB creds + master key),
exactly like the other scripts:

    source .venv/bin/activate && set -a && source /etc/slackbot/env && set +a \\
        && python3 scripts/create_local_user.py --username alice --admin

The password is read INTERACTIVELY (getpass): it is never passed on the
command line, never echoed to the terminal, and never logged. Only its
salted PBKDF2 hash is stored — the database never holds a cleartext
password. Re-running for an existing username resets the password.

  --admin        also grant a permanent (unrestricted / super) admin row,
                 so this account can approve and manage everything. Use for
                 the first operator account. Without it, the account is
                 added as an enabled requester (a developer who still needs
                 target grants to run anything).
  --display-name / --email   optional profile fields.
  --disabled     create the account disabled (cannot log in until enabled).
  --keep-config  do NOT flip web_auth_local_enabled on (leave it as-is).
  --if-absent    do nothing if the username already exists. For unattended
                 bootstrap (the container entrypoint): a container restarts,
                 and the default behaviour of resetting the password on every
                 start would silently undo the operator's own password change.
"""
from __future__ import annotations

import argparse
import getpass
import re
import sys

from dba_slack_bot import admins, db, local_users, passwords

_MIN_PW_LEN = 8
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,59}$")


def _read_password(from_stdin: bool = False) -> str:
    """Prompt twice, confirm they match, enforce a minimum length.

    `from_stdin` reads one line from stdin instead, for automation: the
    container entrypoint has to create the demo accounts with no TTY. It is a
    separate, explicit flag rather than a fallback, so an interactive run can
    never silently take a password from a pipe — and it is not a way to sneak a
    production password past the prompt: the value still lands in the process's
    stdin, which on a shared host is exactly what the TTY prompt avoids.
    """
    if from_stdin:
        pw = sys.stdin.readline().rstrip("\n")
        if len(pw) < _MIN_PW_LEN:
            sys.exit(f"error: password must be at least {_MIN_PW_LEN} characters")
        return pw
    pw = getpass.getpass("New password: ")
    if len(pw) < _MIN_PW_LEN:
        sys.exit(f"error: password must be at least {_MIN_PW_LEN} characters")
    again = getpass.getpass("Confirm password: ")
    if pw != again:
        sys.exit("error: passwords do not match")
    return pw


def _ensure_requester(conn, identity: str, name: str | None,
                      email: str | None) -> None:
    conn.execute(
        "INSERT INTO requesters (slack_user_id, name, email, enabled, added_by) "
        "VALUES (%s, %s, %s, TRUE, %s) "
        "ON CONFLICT (slack_user_id) DO UPDATE "
        "SET enabled = TRUE, "
        "    name  = COALESCE(EXCLUDED.name,  requesters.name), "
        "    email = COALESCE(EXCLUDED.email, requesters.email)",
        (identity, name, email, "bootstrap:create_local_user"),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create/update a local QueryHub account.")
    ap.add_argument("--username", required=True)
    ap.add_argument("--display-name", default=None)
    ap.add_argument("--email", default=None)
    ap.add_argument("--admin", action="store_true",
                    help="grant a permanent super-admin row (first operator)")
    ap.add_argument("--disabled", action="store_true",
                    help="create the account disabled")
    ap.add_argument("--must-change-pw", action="store_true",
                    help="force a password change at first login (hand-off)")
    ap.add_argument("--keep-config", action="store_true",
                    help="do not turn web_auth_local_enabled on")
    ap.add_argument("--password-stdin", action="store_true",
                    help="read the password from stdin instead of prompting "
                         "(for automation; see the docker entrypoint)")
    ap.add_argument("--if-absent", action="store_true",
                    help="no-op if the account already exists (unattended "
                         "bootstrap; does not reset an existing password)")
    args = ap.parse_args(argv)

    username = local_users.normalize_username(args.username)
    if not _USERNAME_RE.match(username):
        sys.exit("error: username must match ^[a-z0-9][a-z0-9._-]{0,59}$ "
                 "(lowercase, start with a letter/digit)")
    identity = local_users.to_identity(username)
    display_name = args.display_name or username

    existed = local_users.exists(username)
    if existed and args.if_absent:
        # Before reading the password, so an unattended caller does not have to
        # supply one just to be told there is nothing to do.
        print(f"OK: local account '{username}' already exists — left untouched.")
        return 0
    password = _read_password(from_stdin=args.password_stdin)
    pw_hash = passwords.hash_password(password)
    # Drop the plaintext reference as soon as it is hashed.
    del password

    with db.transaction() as conn:
        if existed:
            conn.execute(
                "UPDATE local_users SET password_hash = %s, display_name = %s, "
                "email = COALESCE(%s, email), enabled = %s, must_change_pw = %s "
                "WHERE username = %s",
                (pw_hash, display_name, args.email, not args.disabled,
                 args.must_change_pw, username),
            )
        else:
            conn.execute(
                "INSERT INTO local_users "
                "  (username, password_hash, display_name, email, enabled, "
                "   must_change_pw, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (username, pw_hash, display_name, args.email,
                 not args.disabled, args.must_change_pw,
                 "bootstrap:create_local_user"),
            )
        # Authorization: admin (unrestricted permanent row = super-admin) OR
        # an enabled requester. Both key on the local:<username> principal id.
        if args.admin:
            conn.execute(
                "INSERT INTO admins (slack_user_id, name, added_by) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (slack_user_id) DO UPDATE "
                "SET enabled = TRUE, name = EXCLUDED.name",
                (identity, display_name, "bootstrap:create_local_user"),
            )
        else:
            _ensure_requester(conn, identity, display_name, args.email)

        if not args.keep_config:
            conn.execute(
                "INSERT INTO bot_config (key, value) "
                "VALUES ('web_auth_local_enabled', 'on') "
                "ON CONFLICT (key) DO UPDATE SET value = 'on'",
            )

    verb = "updated" if existed else "created"
    role = "super-admin" if args.admin else "requester (developer)"
    state = "DISABLED" if args.disabled else "enabled"
    print(f"\nOK: {verb} local account '{username}' ({state}) as {role}.")
    print(f"    principal id: {identity}")
    if not args.keep_config:
        print("    web_auth_local_enabled = on")
    # Confirm we really did enforce super-admin when asked.
    if args.admin and not admins.is_super_admin(identity):
        print("    WARNING: admin row is not unrestricted — check scope columns.")
    print("\nLog in at the web UI with this username and password.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
