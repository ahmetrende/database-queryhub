#!/usr/bin/env python3
"""Fill in `target_servers.tags['account']` -- which cloud account each target
lives in -- by asking the clouds, not by parsing the hostname.

Why not the hostname: an RDS endpoint is
`<name>.<resource-id>.<region>.rds.amazonaws.com`, and the middle token is an
opaque per-account-per-region identifier, NOT the account number. It groups
targets that share an account (this fleet has eight distinct ones) but it
cannot name any of them, and a tag called `account` holding `cbqssr0jkkf1`
would be a label nobody can look up.

So each endpoint is matched against the RDS inventory of every account this
host can actually reach, and the account comes off the instance ARN. Access is
by cross-account role assumed from the EC2 instance profile -- the `ec2-*`
entries in `~/.aws/config`, which use `credential_source =
Ec2InstanceMetadata` and therefore need no SSO login. Any profile that fails
to assume is reported and skipped; it is a gap in coverage, never a reason to
clear a tag.

Huawei targets are left alone unless `--huawei-project` is given. KooCLI needs
an interactive SSO login, so a scheduled run cannot have it, and guessing a
project name from `tr-west-1` (a REGION) would put a wrong answer in a field
whose whole value is being right.

Read-only by default: it prints the plan and changes nothing. `--apply` writes,
inside one transaction, with an `audit_log` row -- it is a bulk operational
change to the registry every screen reads.

    python3 scripts/populate_target_accounts.py                 # plan
    python3 scripts/populate_target_accounts.py --apply
    python3 scripts/populate_target_accounts.py --apply \
        --huawei-project tr-west-1-exchange-prod
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from queryhub import audit, db  # noqa: E402

# Only profiles that authenticate from the instance itself. An SSO profile in
# the same file would prompt for a browser login halfway through a job.
_EC2_CREDENTIAL_SOURCE = "Ec2InstanceMetadata"


def _instance_profiles() -> list[str]:
    """Profiles that assume a role using the EC2 instance identity."""
    cfg = configparser.ConfigParser()
    cfg.read(os.path.expanduser("~/.aws/config"))
    out = []
    for section in cfg.sections():
        name = section[len("profile "):] if section.startswith("profile ") else section
        block = cfg[section]
        if block.get("credential_source") == _EC2_CREDENTIAL_SOURCE \
                and block.get("role_arn"):
            out.append(name)
    return sorted(out)


def _rds_endpoints(profile: str, regions: list[str]) -> tuple[dict[str, str], str | None]:
    """{endpoint: account_id} for one profile, or ({}, error)."""
    import boto3
    found: dict[str, str] = {}
    try:
        session = boto3.Session(profile_name=profile)
        for region in regions:
            client = session.client("rds", region_name=region)
            for page in client.get_paginator("describe_db_instances").paginate():
                for inst in page["DBInstances"]:
                    endpoint = (inst.get("Endpoint") or {}).get("Address")
                    if endpoint:
                        # arn:aws:rds:<region>:<account>:db:<name>
                        found[endpoint.lower()] = inst["DBInstanceArn"].split(":")[4]
            # Clusters answer on their own endpoints, which is what a target
            # row usually holds for Aurora.
            for page in client.get_paginator("describe_db_clusters").paginate():
                for cl in page["DBClusters"]:
                    account = cl["DBClusterArn"].split(":")[4]
                    for key in ("Endpoint", "ReaderEndpoint"):
                        if cl.get(key):
                            found[cl[key].lower()] = account
    except Exception as e:  # noqa: BLE001 — one unreachable account is a gap
        return {}, str(e)
    return found, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the tags (default: print the plan only)")
    ap.add_argument("--regions", default="eu-central-1",
                    help="comma-separated AWS regions to scan")
    ap.add_argument("--huawei-project", default=None,
                    help="account label for *.myhuaweicloud.com targets; "
                         "omitted, they are left untouched")
    ap.add_argument("--profile", action="append", default=[], metavar="NAME",
                    help="an extra AWS profile to ask, on top of the "
                         "instance-role ones. For an SSO profile after `aws "
                         "sso login` — never picked up automatically, so a "
                         "scheduled run cannot stall on a browser prompt.")
    args = ap.parse_args()
    regions = [r.strip() for r in args.regions.split(",") if r.strip()]

    targets = db.fetch_all(
        "SELECT id, alias, host, enabled, COALESCE(tags, '{}'::jsonb) AS tags "
        "  FROM target_servers WHERE host <> '' ORDER BY alias")
    print(f"{len(targets)} target(s) with a host.")

    profiles = _instance_profiles()
    extra = [p for p in args.profile if p not in profiles]
    if not profiles and not extra:
        print("no instance-profile roles in ~/.aws/config — nothing to ask.")
        return 1
    print(f"asking {len(profiles)} account(s) via the instance role"
          + (f" plus {len(extra)} named: {', '.join(extra)}" if extra else "")
          + f": {', '.join(profiles)}")
    profiles = profiles + extra

    endpoints: dict[str, str] = {}
    for profile in profiles:
        found, err = _rds_endpoints(profile, regions)
        if err:
            print(f"  {profile:22} UNREACHABLE — {err[:90]}")
            continue
        accounts = sorted(set(found.values()))
        print(f"  {profile:22} {len(found):4} endpoint(s)  account(s): "
              f"{', '.join(accounts) or '-'}")
        # First answer wins: two profiles pointing at one account see the same
        # instances, and re-writing the same value is not a conflict.
        for k, v in found.items():
            endpoints.setdefault(k, v)

    plan: list[tuple] = []
    unmatched: list[str] = []
    for t in targets:
        host = (t["host"] or "").lower()
        tags = dict(t["tags"] or {})
        if host.endswith("myhuaweicloud.com"):
            account = args.huawei_project
        else:
            account = endpoints.get(host)
        if not account:
            if not tags.get("account"):
                unmatched.append(t["alias"])
            continue
        if tags.get("account") == account:
            continue
        plan.append((t["id"], t["alias"], tags.get("account"), account))

    print(f"\n{len(plan)} target(s) to label, {len(unmatched)} still unknown.")
    for _tid, alias, was, now in plan:
        print(f"  {alias:32} {was or '(none)':>16} -> {now}")
    if unmatched:
        print("\nno account found for (left untouched):")
        for alias in unmatched:
            print(f"  {alias}")

    if not args.apply:
        print("\ndry run — nothing written. Re-run with --apply.")
        return 0
    if not plan:
        print("\nnothing to write.")
        return 0

    with db.transaction() as cur:
        for tid, _alias, _was, account in plan:
            cur.execute(
                "UPDATE target_servers "
                "   SET tags = COALESCE(tags, '{}'::jsonb) || %s::jsonb "
                " WHERE id = %s", (json.dumps({"account": account}), tid))
        audit.log_in(cur, None, "script", "populate_target_accounts",
                     "target_accounts_labelled",
                     {"count": len(plan), "regions": regions,
                      "huawei_project": args.huawei_project,
                      "targets": [{"id": t, "alias": a, "account": acc}
                                  for t, a, _w, acc in plan]})
    print(f"\nwrote {len(plan)} tag(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
