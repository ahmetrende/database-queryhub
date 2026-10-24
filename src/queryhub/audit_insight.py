"""Audit insight: one auto-approve rule that approved most of the queries.

An over-broad rule is a risk. This module holds the pure part. It takes the
audit rows that are already grouped and says which tiers have a rule worth a
look. It never reads the database.
"""
from __future__ import annotations

from typing import Callable, Iterable

WINDOW_DAYS = 30

MIN_TOTAL = 20
"""Fewest auto-approved queries of one tier in the window before the tier can
produce an insight. Below this the share of a rule means nothing."""

MIN_PERCENT = 50
"""Least share of the tier that the top rule must hold, as a whole percent
rounded down. A top rule below this share is not a concern."""

TIERS = ("RO", "RW")


def _default_label(rule_id: str) -> str:
    return f"rule {rule_id}"


def pick_insights(rows: Iterable[dict],
                  label_of: Callable[[str, str], str] | None = None) -> list[dict]:
    """Return one entry for each tier where one rule holds most of the queries.

    `rows` are dicts with `tier` ("RO" or "RW"), `grant_id` and `n` (the count
    of queries for that pair). A row without a rule id is skipped. The top rule
    of a tier has the largest count. A tie goes to the smaller id string.
    `label_of(tier, rule_id)` names a rule. The default is `rule <id>`.
    """
    label = label_of or (lambda _tier, rid: _default_label(rid))
    per_tier: dict[str, dict[str, int]] = {t: {} for t in TIERS}
    for r in rows:
        tier = r.get("tier")
        gid = r.get("grant_id")
        if tier not in per_tier or gid is None:
            continue
        gid = str(gid)
        per_tier[tier][gid] = per_tier[tier].get(gid, 0) + int(r.get("n") or 0)

    out: list[dict] = []
    for tier in TIERS:
        counts = per_tier[tier]
        total = sum(counts.values())
        if total < MIN_TOTAL:
            continue
        top_id = min(counts, key=lambda k: (-counts[k], k))
        top = counts[top_id]
        percent = (top * 100) // total
        if percent < MIN_PERCENT:
            continue
        out.append({"tier": tier, "total": total, "ruleId": top_id,
                    "ruleLabel": label(tier, top_id), "count": top,
                    "percent": percent})
    return out
