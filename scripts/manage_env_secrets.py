"""CLI for the bot's encrypted env-secret store at /etc/queryhub/secrets.enc.

Stores SLACK_BOT_TOKEN, SLACK_APP_TOKEN, BOT_DB_PASSWORD encrypted with
the same Fernet master key (`/etc/queryhub/master.key`) that protects
target-server credentials.

Usage:
    sudo .venv/bin/python scripts/manage_env_secrets.py list
    sudo .venv/bin/python scripts/manage_env_secrets.py init      # one-shot, prompts for all
    sudo .venv/bin/python scripts/manage_env_secrets.py set SLACK_BOT_TOKEN
    sudo .venv/bin/python scripts/manage_env_secrets.py remove    # soft-delete the file

After any change: `sudo systemctl restart queryhub` so the new values
load into the bot's environment.

The script never echoes secret values to stdout. Reads use getpass
(no terminal echo). The `list` subcommand prints metadata (mode, mtime,
key names) but not values.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import secrets_store  # noqa: E402


def cmd_list(_args) -> int:
    info = secrets_store.metadata()
    if not info["exists"]:
        print(f"No secrets file at {info['path']} (does not exist).")
        return 0
    print(f"Path:     {info['path']}")
    print(f"Mode:     {info['mode']}")
    print(f"Size:     {info['size_bytes']} bytes")
    print(f"Modified: {info['modified']}")
    print(f"Status:   {info['status']}")
    if info.get("keys"):
        print("Keys:")
        for k in info["keys"]:
            print(f"  - {k}")
    return 0


def _prompt_secret(label: str) -> str:
    while True:
        v = getpass.getpass(f"{label}: ")
        if not v:
            print("  (empty input, try again or press Ctrl-C to abort)")
            continue
        v2 = getpass.getpass(f"{label} (confirm): ")
        if v != v2:
            print("  values don't match, try again")
            continue
        return v


def cmd_init(_args) -> int:
    if secrets_store.exists():
        print(f"Secrets file already exists at {secrets_store.default_path()}.")
        print("Use `set <KEY>` to update individual values, or `remove` first.")
        return 1
    print("Initializing encrypted secrets store. You will be prompted for "
          "each value. Input is hidden — you'll be asked twice for confirmation.")
    print()
    secrets = {}
    for key in secrets_store.KNOWN_KEYS:
        secrets[key] = _prompt_secret(key)
    path = secrets_store.save(secrets)
    print(f"\nWrote encrypted secrets to {path} (mode 0600, "
          f"{len(secrets)} key(s)).")
    print("Restart the bot to pick up the new values:")
    print("  sudo systemctl restart queryhub")
    return 0


def cmd_set(args) -> int:
    key = args.key
    if key not in secrets_store.KNOWN_KEYS:
        print(f"Unknown key {key!r}. Known keys: "
              f"{', '.join(secrets_store.KNOWN_KEYS)}", file=sys.stderr)
        return 1

    if secrets_store.exists():
        try:
            current = secrets_store.load()
        except Exception as e:
            print(f"Could not read existing secrets file: {e}", file=sys.stderr)
            return 1
    else:
        print(f"No secrets file yet at {secrets_store.default_path()} — "
              f"creating one with just {key}. Other keys must be added "
              f"separately or via `init`.")
        current = {}

    current[key] = _prompt_secret(key)
    path = secrets_store.save(current)
    print(f"Updated {key} in {path}.")
    print("Restart the bot to pick up the new value:")
    print("  sudo systemctl restart queryhub")
    return 0


def cmd_remove(_args) -> int:
    p = secrets_store.default_path()
    if not p.exists():
        print(f"No secrets file at {p} — nothing to remove.")
        return 0

    print(f"About to soft-delete the secrets file at {p}.")
    print("It will be renamed to <path>.deleted[_N], not erased.")
    print("To confirm, type the word DELETE (uppercase):")
    typed = input("> ").strip()
    if typed != "DELETE":
        print("Aborted.")
        return 1
    target = secrets_store.remove()
    print(f"Soft-deleted to {target}.")
    print("Bot will fall back to plaintext env vars on next restart "
          "(if they are still present in /etc/queryhub/env).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage encrypted env secrets for QueryHub.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Show metadata (no secret values).").set_defaults(
        func=cmd_list,
    )
    sub.add_parser("init", help="First-time setup; prompts for all known keys.").set_defaults(
        func=cmd_init,
    )
    s_set = sub.add_parser("set", help="Update a single key (prompts for value).")
    s_set.add_argument("key", help=f"One of: {', '.join(secrets_store.KNOWN_KEYS)}")
    s_set.set_defaults(func=cmd_set)
    sub.add_parser("remove", help="Soft-delete the secrets file (typed confirmation).").set_defaults(
        func=cmd_remove,
    )

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
