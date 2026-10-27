"""Single definition of the product metrics: inputs, buckets, formulas.

There are two consumers, and they used to define everything twice:

  - `web/metrics.py` — aggregates in Python and serves JSON to the admin panel.
  - `scripts/build_metrics_dashboard.py` — ships raw rows into a static HTML page
    and aggregates in embedded JavaScript, so the page can re-filter in the
    browser without a server.

Both read the same four `p_metrics_*` views and the same `bot_config` keys, and
both computed cost-avoided, the terminal-status set and the tier buckets from
their own copies. Changing a metric meant changing it twice, in two languages,
and nothing checked that the results agreed.

This module is the one definition. The Python consumer calls it directly. The
dashboard cannot — its whole point is recomputing client-side as the user
filters, so the arithmetic has to exist in JS as well — but it now takes the
view names, the config keys and the coefficient set from here, and
`tests/test_metrics_parity.py` runs the JS implementation against the functions
below on the same inputs and fails the build if they disagree.

That is a deliberately smaller change than "one module computes everything":
moving aggregation entirely server-side would remove the static dashboard's
offline filtering, which is the feature it exists for. What is fixed is the
part that was actually dangerous — two definitions with nothing comparing them.
"""
from __future__ import annotations

import math
from collections import defaultdict

from . import origins

# ---------------------------------------------------------------- inputs

#: The reporting views. Each already excludes operator self-test traffic (the
#: `*_reportable` filtering lives in the view, not in the callers).
VIEWS = (
    "p_metrics_request_facts",
    "p_metrics_csv_imports",
    "p_metrics_rating_low_with_feedback",
    "p_metrics_who_can_what",
)

#: bot_config keys both consumers read. Kept as one tuple so a new coefficient
#: cannot be added to one query and forgotten in the other.
CONFIG_KEYS = (
    "cost_dba_minutes_per_request",
    "cost_dba_hourly_usd",
    "cost_avoided_replicas",
    "cost_per_replica_monthly_usd",
    "cost_other_monthly_usd",
    "report_start_date",
    "report_timezone",
)

#: Just the numeric cost coefficients (CONFIG_KEYS minus the window settings).
COST_KEYS = CONFIG_KEYS[:5]

#: A request has reached a terminal state — nothing more will happen to it.
TERMINAL_STATUSES = ("completed", "failed", "rejected", "cancelled")

#: The status that counts as work the DBA did not have to do by hand. Only
#: completed runs saved anyone time; a rejected request cost time.
COST_COUNTED_STATUS = "completed"

#: Tier buckets, in ascending privilege. Used for breakdowns and for ordering.
TIERS = ("ro", "rw", "ddl")


def config_key_sql_list() -> str:
    """`'a','b',...` for an `IN (...)` clause, so both queries select the same
    keys from the same list."""
    return ",".join(f"'{k}'" for k in CONFIG_KEYS)


def coerce_float(value, default: float = 0.0) -> float:
    """bot_config values are text. A blank or unparseable coefficient must read
    as zero rather than raise — a mis-typed cost setting should show $0, not
    take the metrics page down."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- formulas


def cost_savings(completed_count: int, config: dict) -> dict:
    """Estimated cost avoided, from the completed-request count.

    Returns the panel shape both consumers render:

        dbaHoursSaved   completed × minutes-per-request / 60
        dbaSavingUsd    those hours × the DBA hourly rate
        infraUsdPerMonth  avoided replicas × per-replica cost + other

    Rounding is part of the definition: the JS side must round the same way or
    the two dashboards show different numbers for the same data.
    """
    minutes = coerce_float(config.get("cost_dba_minutes_per_request"))
    hourly = coerce_float(config.get("cost_dba_hourly_usd"))
    replicas = coerce_float(config.get("cost_avoided_replicas"))
    per_replica = coerce_float(config.get("cost_per_replica_monthly_usd"))
    other = coerce_float(config.get("cost_other_monthly_usd"))

    dba_hours = completed_count * minutes / 60
    return {
        "completed": completed_count,
        "dbaMinutesPerRequest": minutes,
        "dbaHourlyUsd": hourly,
        "dbaHoursSaved": round(dba_hours, 1),
        "dbaSavingUsd": round(dba_hours * hourly),
        "avoidedReplicas": replicas,
        "infraUsdPerMonth": round(replicas * per_replica + other),
    }


def count_completed(rows) -> int:
    """Rows in the cost-counted status. Takes dicts or objects with .status."""
    n = 0
    for r in rows:
        status = r.get("status") if isinstance(r, dict) else getattr(r, "status", None)
        if status == COST_COUNTED_STATUS:
            n += 1
    return n


# ---------------------------------------------------------------- channel mix
#
# The channel mix answers one question: through which door did requests come,
# and how many different people used each door. The rule lives here. The
# static dashboard repeats it in JavaScript (`channelMixStats` in
# `scripts/build_metrics_dashboard.py`), and `tests/test_metrics_channel_mix.py`
# runs both over the same rows, so the two pages cannot drift apart.
#
#   - A request's channel is `origins.normalize(origin)`: lower-cased and
#     trimmed, and a blank value reads as `slack`, the column's own default.
#   - A person who used two channels counts once in each channel row and once
#     in the total, so the people column can add up to more than the total.
#   - Known channels come first, in `origins.ORDER`. A value nobody taught us
#     comes after them, by name, and is shown as itself.


def _channel_rank(key: str) -> tuple:
    known = origins.ORDER
    return (known.index(key) if key in known else len(known), key)


def share_pct(part: int, whole: int) -> float:
    """`part` of `whole` as a percent with one decimal. Rounds halves up like
    JavaScript's Math.round, and divides first like the page does, so the two
    pages print the same digit."""
    return math.floor(1000 * (part / whole) + 0.5) / 10 if whole else 0


def channel_mix(rows) -> dict:
    """Requests and DISTINCT people per channel, and how people spread over the
    channels. `rows` are fact rows: `origin` and `requester_slack_id` are read.

    {totalRequests, totalUsers,
     channels: [{key, label, requests, requestShare, users, userShare}],
     segments: [{label, users}]}   # "Slack + Web": people who used both
    """
    requests: dict[str, int] = defaultdict(int)
    users: dict[str, set] = defaultdict(set)
    chans: dict[str, set] = defaultdict(set)
    total = 0
    for r in rows:
        key = origins.normalize(r.get("origin"))
        total += 1
        requests[key] += 1
        users[key].add(r.get("requester_slack_id"))
        chans[r.get("requester_slack_id")].add(key)
    total_users = len(chans)
    channels = [{"key": k, "label": origins.label(k),
                 "requests": requests[k], "requestShare": share_pct(requests[k], total),
                 "users": len(users[k]), "userShare": share_pct(len(users[k]), total_users)}
                for k in sorted(requests, key=_channel_rank)]
    spread: dict[str, int] = defaultdict(int)
    for keys in chans.values():
        spread[" + ".join(origins.label(k) for k in sorted(keys, key=_channel_rank))] += 1
    segments = [{"label": label, "users": n}
                for label, n in sorted(spread.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"totalRequests": total, "totalUsers": total_users,
            "channels": channels, "segments": segments}
