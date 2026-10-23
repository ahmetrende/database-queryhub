"""Connection health: whether QueryHub can reach each target, checked on a timer.

`scripts/check_connection_health.py` runs from the host scheduler. Each run
checks the targets that are due and keeps one row per target in
`connection_health` (migration 190). `GET /admin/connections` shows that row
as `health` (`by_target`).

The settings are `bot_config` keys (migration 191). A run reads them when it
starts, so a change needs no edit to the scheduled job:

- `connection_health_enabled`: `on` or `off`. Off: the run exits at once,
  and the connection list shows no result.
- `connection_health_interval_sec`: how old a result may get before a run
  checks that target again.
- `connection_health_timeout_sec`: the longest time one check may take,
  1 to 60 seconds.
- `connection_health_exclude_engines`: engines that are never checked, as a
  comma list, for example `clickhouse,athena`.

What one check does:

- A server engine (PostgreSQL, SQL Server, ClickHouse) gets the probe of the
  admin Test button (`connection_probe.probe`), with the stored read-only
  login (`targets.get_credentials(id, "ro")`). A read replica has no login of
  its own. Its check uses its primary's login, as replica routing does.
- Athena has no host and no stored login, and the probe has no Athena branch.
  Its check is the Glue catalog listing that the catalog job and the schema
  refresh already make (`schema_catalog.list_target_databases`). It runs no
  Athena query.

Cost: a ClickHouse check is a query. It wakes an idle ClickHouse Cloud
service, bills its compute and starts its idle timer again. That is why the
catalog job reads ClickHouse once a day. Put `clickhouse` in the exclusion
list to stop it.

The writes are telemetry, as the catalog job's `schema_refresh_health` is: no
audit row. They change no permission and no setting.
"""
from __future__ import annotations

import contextlib
import logging
import re
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import config as cfg
from . import connection_probe, db, engines, targets

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 300
DEFAULT_TIMEOUT_SEC = 5
MAX_TIMEOUT_SEC = 60
# At most this many checks run at once.
MAX_PARALLEL = 8
# The stored error, at most. The table's CHECK holds the same number.
ERROR_MAX = 200
# The run waits this much longer than the timeout that the driver gets, so
# the driver's own message wins when it gives up on time.
GRACE_SEC = 1
# The two-key form: its lock space is apart from the one-key locks that the
# migration runner and the submit path take.
LOCK_KEYS = (728041, 190)
APPLICATION_NAME = "queryhub-connection-health"

NO_CREDENTIALS = "This connection has no stored read-only credentials yet."
NO_PRIMARY_CREDENTIALS = ("The primary of this replica has no stored read-only "
                          "credentials yet.")

_ON = frozenset({"on", "true", "yes", "1"})
_ARN = re.compile(r"\barn:aws[\w-]*:[^\s\"',]+", re.IGNORECASE)

_FLEET_SQL = """
SELECT t.id, t.alias, COALESCE(t.engine, 'postgres') AS engine, t.host, t.port,
       t.default_database, t.enabled, t.deleted_at, t.replica_of,
       COALESCE(t.engine_config, '{}'::jsonb) AS engine_config,
       h.ok AS last_ok, h.checked_at AS last_checked_at,
       h.failing_since AS last_failing_since
  FROM target_servers t
  LEFT JOIN connection_health h ON h.target_server_id = t.id
 ORDER BY t.id
"""

_SAVE_SQL = """
INSERT INTO connection_health AS h
       (target_server_id, ok, checked_at, failing_since, error, latency_ms)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (target_server_id) DO UPDATE SET
       ok = EXCLUDED.ok, checked_at = EXCLUDED.checked_at,
       failing_since = EXCLUDED.failing_since, error = EXCLUDED.error,
       latency_ms = EXCLUDED.latency_ms
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- settings ------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    enabled: bool
    interval_sec: int
    timeout_sec: int
    exclude_engines: frozenset[str] = frozenset()

    @property
    def unknown_engines(self) -> list[str]:
        """Names in the exclusion list that no engine has. A typo there
        excludes nothing, and the run says so."""
        return sorted(self.exclude_engines - engines.WIRED_ENGINES)


def parse_engines(raw: str | None) -> frozenset[str]:
    """`clickhouse, Athena` -> {'clickhouse', 'athena'}. Commas or spaces."""
    return frozenset(p for p in re.split(r"[,\s]+", (raw or "").strip().lower()) if p)


def settings() -> Settings:
    """The four keys, read now. A number that does not parse falls back to its
    default, with a warning. The interval is at least 1 second. The timeout is
    1 to 60 seconds, because 0 means "wait for ever" to some drivers."""
    enabled = (cfg.get_setting("connection_health_enabled", "on")
               or "").strip().lower() in _ON
    try:
        interval = cfg.get_int("connection_health_interval_sec", DEFAULT_INTERVAL_SEC)
    except ValueError:
        log.warning("connection_health_interval_sec is not a whole number; "
                    "using %s", DEFAULT_INTERVAL_SEC)
        interval = DEFAULT_INTERVAL_SEC
    try:
        timeout = cfg.get_int("connection_health_timeout_sec", DEFAULT_TIMEOUT_SEC)
    except ValueError:
        log.warning("connection_health_timeout_sec is not a whole number; "
                    "using %s", DEFAULT_TIMEOUT_SEC)
        timeout = DEFAULT_TIMEOUT_SEC
    excluded = parse_engines(cfg.get_setting("connection_health_exclude_engines", ""))
    return Settings(enabled=enabled, interval_sec=max(1, interval),
                    timeout_sec=min(max(1, timeout), MAX_TIMEOUT_SEC),
                    exclude_engines=excluded)


# ---- the rules (pure) -------------------------------------------------------------


def scope_reason(target: dict, s: Settings) -> str | None:
    """Why the check does not cover `target`, or None when it does.

    One rule for the run and for the connection list: the run checks only
    the targets it covers, and deletes the stored result of every other one.
    The list shows a result only for a target that the check covers. So a
    disabled, deleted or excluded target never shows a result from before."""
    if not s.enabled:
        return "the check is off"
    if target.get("deleted_at") is not None:
        return "deleted"
    if not target.get("enabled"):
        return "disabled"
    engine = target.get("engine") or "postgres"
    if engine in s.exclude_engines:
        return f"engine {engine} is excluded"
    return None


def in_scope(target: dict, s: Settings) -> bool:
    return scope_reason(target, s) is None


def due(last_checked_at: datetime | None, now: datetime, interval_sec: int) -> bool:
    """Whether a target last checked at `last_checked_at` is due at `now`.

    The rule gives the scheduler a tenth of the interval, at most 30
    seconds. A run every 5 minutes starts a little early or late. With a
    300-second interval and no allowance, it would find each result 299
    seconds old and check only on every second run."""
    if last_checked_at is None:
        return True
    allowance = min(30, interval_sec // 10)
    return (now - last_checked_at).total_seconds() >= interval_sec - allowance


def next_failing_since(prev: dict | None, ok: bool, at: datetime) -> datetime | None:
    """`failing_since` after a check at `at`. `prev` is the target's stored
    result (`last_ok`, `last_failing_since`), or None for the first check.

    None while the target answers. The first failure after an answer, or on
    the first check, sets it to `at`. A failure after a failure keeps the
    time when the failures started."""
    if ok:
        return None
    if prev and prev.get("last_ok") is False and prev.get("last_failing_since"):
        since: datetime = prev["last_failing_since"]
        return since
    return at


@dataclass
class Plan:
    check: list[dict] = field(default_factory=list)
    skip: list[tuple[dict, str]] = field(default_factory=list)
    # Out of scope, but the table still holds a result from before.
    delete: list[dict] = field(default_factory=list)


def plan(fleet: list[dict], s: Settings, now: datetime) -> Plan:
    """What a run does with each target. Pure."""
    p = Plan()
    for t in fleet:
        why = scope_reason(t, s)
        if why is not None:
            p.skip.append((t, why))
            if t.get("last_checked_at") is not None:
                p.delete.append(t)
        elif due(t.get("last_checked_at"), now, s.interval_sec):
            p.check.append(t)
        else:
            age = int((now - t["last_checked_at"]).total_seconds())
            p.skip.append((t, f"checked {age} s ago"))
    return p


def clean_error(message: str | None, secrets: tuple[str, ...] = ()) -> str:
    """The error as the table keeps it and the list shows it: no password, no
    login name, no ARN, at most ERROR_MAX characters.

    The probe scrubs it first: errors.scrub, then the host and the login by
    value. This replaces the values once more, and every ARN too: an Athena
    failure names the role and the account."""
    text = connection_probe.hide_values(message or "The check failed.", secrets)
    text = _ARN.sub(connection_probe.REDACTED, text)
    return text[:ERROR_MAX]


# ---- one check ------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    ok: bool
    error: str | None
    latency_ms: int | None


Probe = Callable[..., dict]


def check(target: dict, timeout_sec: int, *,
          probe: Probe | None = None) -> Outcome:
    """Check one target. Never raises an Exception.

    The driver gets `timeout_sec`. Not every driver keeps to it: ClickHouse
    and SQL Server connect with their own longer limits, and the AWS SDK
    retries. So the check runs on a thread of its own. The run waits
    GRACE_SEC longer, then stores "no answer" and goes on. The thread ends when
    its driver gives up, or with the process."""
    dial_with = probe or connection_probe.probe
    secrets: list[str] = []
    try:
        result = _within(lambda: _dial(target, timeout_sec, dial_with, secrets),
                         timeout_sec + GRACE_SEC)
    except Exception as e:  # noqa: BLE001 -- a credential or config read failed
        log.warning("%s: the check raised %s", target.get("alias"), type(e).__name__)
        message = connection_probe.failure_message(
            e, host=target.get("host"), redact=secrets)
        return Outcome(False, clean_error(message, tuple(secrets)), None)
    if result is None:
        return Outcome(False, f"No answer in {timeout_sec} s.", None)
    if result.get("ok"):
        return Outcome(True, None, result.get("latencyMs"))
    return Outcome(False, clean_error(result.get("error"), tuple(secrets)),
                   result.get("latencyMs"))


def _within(work: Callable[[], dict], seconds: float) -> dict | None:
    """`work()` on a daemon thread, waited for at most `seconds`. None when
    it did not finish. When `work` raised an exception, this raises it."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = work()
        except BaseException as e:  # noqa: BLE001 -- handed to the caller below
            box["error"] = e

    worker = threading.Thread(target=run, name="connection-health-check",
                              daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        return None
    if "error" in box:
        raise box["error"]
    value: dict = box["value"]
    return value


def _dial(target: dict, timeout_sec: int, probe: Probe,
          secrets: list[str]) -> dict:
    """The probe for one target, with its login. Fills `secrets` with the
    values that its error text must not show."""
    engine = target.get("engine") or "postgres"
    if not engines.spec(engine).requires_credentials:
        role = (target.get("engine_config") or {}).get("role_arn")
        if role:
            secrets.append(role)
        server = _as_target(target)
        return connection_probe.measure(lambda: _list_databases(server),
                                        engine=engine, redact=secrets)
    # A read replica has its primary's roles and passwords and no login of
    # its own (migration 131): routing connects to it with the primary's.
    owner = target.get("replica_of") or target["id"]
    try:
        user, password = targets.get_credentials(owner, "ro")
    except LookupError:
        user = password = ""
    if not user or not password or password == targets.SENTINEL_PASSWORD:
        missing = NO_PRIMARY_CREDENTIALS if target.get("replica_of") else NO_CREDENTIALS
        return {"ok": False, "latencyMs": None, "serverVersion": None,
                "error": missing}
    secrets.extend((password, user))
    return probe(engine, target["host"], target["port"],
                 target["default_database"], user, password,
                 timeout_sec=timeout_sec, redact=(password, user),
                 application_name=APPLICATION_NAME)


def _list_databases(server: targets.TargetServer) -> None:
    from . import schema_catalog
    schema_catalog.list_target_databases(server, "")


def _as_target(row: dict) -> targets.TargetServer:
    return targets.TargetServer(
        id=row["id"], alias=row["alias"], host=row.get("host") or "",
        port=row.get("port") or 0,
        default_database=row.get("default_database") or "", username="",
        enabled=bool(row.get("enabled")), notes=None,
        engine=row.get("engine") or "postgres",
        engine_config=row.get("engine_config") or {},
        replica_of=row.get("replica_of"))


# ---- the run ------------------------------------------------------------------


class Store:
    """Where a run reads the fleet and writes its results: the metadata
    database. Tests pass a fake with the same three methods."""

    def load(self) -> list[dict]:
        """Every target with its stored result, in one read."""
        return db.fetch_all(_FLEET_SQL)

    def save(self, target_id: int, outcome: Outcome, checked_at: datetime,
             failing_since: datetime | None) -> None:
        with db.transaction() as cur:
            cur.execute(_SAVE_SQL, (target_id, outcome.ok, checked_at,
                                    failing_since, outcome.error,
                                    outcome.latency_ms))

    def delete(self, target_ids: list[int]) -> int:
        if not target_ids:
            return 0
        with db.transaction() as cur:
            cur.execute("DELETE FROM connection_health "
                        " WHERE target_server_id = ANY(%s)", (target_ids,))
            deleted: int = cur.rowcount
        return deleted


@contextlib.contextmanager
def run_lock() -> Iterator[bool]:
    """Hold the run's advisory lock for the whole run, or say that another
    run holds it. Two runs never overlap.

    Session-level, on a connection kept for the run. This commits at once:
    the pool ends a session that sits idle in a transaction for 30 seconds,
    and the lock would end with it."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s::int, %s::int) AS held",
                        LOCK_KEYS)
            row = cur.fetchone()
        conn.commit()
        held = bool(row and row["held"])
        try:
            yield held
        finally:
            if held:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(%s::int, %s::int)",
                                    LOCK_KEYS)
                    conn.commit()
                except Exception:  # noqa: BLE001 -- it ends with the session
                    log.warning("could not release the connection health lock",
                                exc_info=True)


@dataclass
class Summary:
    checked: int = 0
    failing: int = 0
    skipped: int = 0
    deleted: int = 0


def run_once(store: Any, s: Settings, *,
             clock: Callable[[], datetime] = utcnow,
             check_fn: Callable[[dict, int], Outcome] | None = None) -> Summary:
    """One run: delete the results that are out of scope, check every target
    that is due, at most MAX_PARALLEL at once, and store each result when it
    arrives. The caller holds `run_lock`."""
    check_fn = check_fn or check
    p = plan(store.load(), s, clock())
    summary = Summary(skipped=len(p.skip))
    if p.delete:
        summary.deleted = store.delete([t["id"] for t in p.delete])
    if not p.check:
        return summary
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL,
                            thread_name_prefix="connection-health") as pool:
        futures = {pool.submit(check_fn, t, s.timeout_sec): t for t in p.check}
        for future in as_completed(futures):
            t = futures[future]
            outcome = future.result()
            at = clock()
            try:
                store.save(t["id"], outcome, at,
                           next_failing_since(t, outcome.ok, at))
            except Exception:  # noqa: BLE001 -- e.g. somebody deleted the target
                log.warning("%s: the result was not stored", t.get("alias"),
                            exc_info=True)
                continue
            summary.checked += 1
            if not outcome.ok:
                summary.failing += 1
                log.warning("%s: failing: %s", t.get("alias"), outcome.error)
    return summary


# ---- the connection list --------------------------------------------------------


def by_target(rows: list[dict]) -> dict[int, dict]:
    """The stored result of each target in `rows` that the check covers, in
    one read for the whole list: {target id: row}.

    `rows` are the admin list's targets (`id`, `enabled`, `deleted_at`,
    `engine`). A target that is out of scope gets nothing, so a disabled,
    deleted or excluded target never shows an old result, and no target does
    while the check is off.

    Never raises. The result is for a screen, so a failed read gives {} and
    the screen shows no result, not an error."""
    try:
        s = settings()
        ids = [r["id"] for r in rows if in_scope(r, s)]
        if not ids:
            return {}
        found = db.fetch_all(
            "SELECT target_server_id, ok, checked_at, failing_since, error "
            "  FROM connection_health WHERE target_server_id = ANY(%s)", (ids,))
        return {h["target_server_id"]: h for h in found}
    except Exception:  # noqa: BLE001 -- a screen, not a request
        log.warning("connection health could not be read", exc_info=True)
        return {}
