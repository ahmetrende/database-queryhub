"""Target Postgres RDS server registry."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import db, secrets_providers
from psycopg.types.json import Json

from .crypto import decrypt, encrypt


# Placeholder password written when a target is registered before its database
# user exists. The executor, the schema snapshot and the admin panel all read it
# as "no credentials yet", so an unprovisioned endpoint can sit in the registry
# (disabled) without pretending to be usable. Inventing a second representation
# for the same state — an empty string, a NULL in a NOT NULL column — would mean
# the executor recognises one of them and not the other.
SENTINEL_PASSWORD = "PASSWORD_NOT_SET"

# Fleet-standard read-only role. A target registered without an explicit RO
# username gets this one, the same default the inventory importer writes, so a
# hand-added target and an auto-imported one are indistinguishable afterwards.
DEFAULT_RO_USERNAME = "queryhub_ro"

# Per-tier credential columns. Mirrors the local secrets provider's own map;
# duplicated rather than imported because that one is a private implementation
# detail of the provider and this one is the registry's own schema knowledge.
_TIER_COLUMNS = {
    "ro": ("username", "password_encrypted"),
    "rw": ("username_rw", "password_rw_encrypted"),
    "ddl": ("username_ddl", "password_ddl_encrypted"),
}

# Columns a connection editor may change. A whitelist, not a filter on the
# caller's dict: the column name is interpolated into the UPDATE, so the set of
# legal names has to be fixed here rather than come from a request body.
_EDITABLE_COLUMNS = ("alias", "host", "port", "default_database", "engine",
                     "notes", "enabled", "tags")
# Columns whose Python value is a dict/list and must reach postgres as jsonb.
_JSON_COLUMNS = ("tags",)


@dataclass(frozen=True)
class TargetServer:
    id: int
    alias: str
    host: str
    port: int
    default_database: str
    username: str
    enabled: bool
    notes: str | None
    engine: str = "postgres"
    # Where this target runs: {provider, service, account, ...}. Display-only —
    # see migration 095. Defaults to an empty bag so every existing caller and
    # every untagged row behaves exactly as before.
    tags: dict = field(default_factory=dict)


def _row_to_target(row: dict) -> TargetServer:
    return TargetServer(
        id=row["id"],
        alias=row["alias"],
        host=row["host"],
        port=row["port"],
        default_database=row["default_database"],
        username=row["username"],
        enabled=row["enabled"],
        tags=row.get("tags") or {},
        notes=row.get("notes"),
        # Default to postgres if a SELECT forgot the column — a missing
        # engine must read as the safe legacy default, never crash.
        engine=(row.get("engine") or "postgres"),
    )


def cloud_provider(host: str | None) -> str:
    """Best-effort cloud provider label from an RDS host, for display in
    pickers (e.g. an aws-hosted alias shows '<alias> (AWS)', a Huawei one
    '<alias> (Huawei)'). Empty string when it's neither (unknown/on-prem)."""
    h = (host or "").lower()
    if "amazonaws.com" in h:
        return "AWS"
    if "myhuaweicloud.com" in h:
        return "Huawei"
    return ""


def label_with_provider(alias: str, host: str | None) -> str:
    """`alias (Provider)` when the provider is known, else just `alias`."""
    prov = cloud_provider(host)
    return f"{alias} ({prov})" if prov else alias


def list_enabled() -> list[TargetServer]:
    rows = db.fetch_all(
        "SELECT id, alias, host, port, default_database, username, enabled, notes, "
        "       COALESCE(engine, 'postgres') AS engine, "
        "       COALESCE(tags, '{}'::jsonb) AS tags "
        "FROM target_servers WHERE enabled = TRUE ORDER BY alias"
    )
    return [_row_to_target(r) for r in rows]


def list_all() -> list[TargetServer]:
    """Every target, enabled or not (each carries `.enabled`). Callers that
    surface disabled targets — e.g. the admin-visible connection list, since
    admins can submit to disabled targets they hold a grant on — filter by
    role themselves."""
    rows = db.fetch_all(
        "SELECT id, alias, host, port, default_database, username, enabled, notes, "
        "       COALESCE(engine, 'postgres') AS engine, "
        "       COALESCE(tags, '{}'::jsonb) AS tags "
        # Disabled targets sort last. They are unusable, so alphabetical
        # placement buries a working connection between two that are not —
        # `prod-archive` sitting between `beta` and `gamma` is noise in
        # every picker that shows them.
        "FROM target_servers ORDER BY enabled DESC, alias"
    )
    return [_row_to_target(r) for r in rows]


def search(prefix: str, limit: int = 100) -> list[TargetServer]:
    rows = db.fetch_all(
        "SELECT id, alias, host, port, default_database, username, enabled, notes, "
        "       COALESCE(engine, 'postgres') AS engine, "
        "       COALESCE(tags, '{}'::jsonb) AS tags "
        "FROM target_servers "
        "WHERE enabled = TRUE AND alias ILIKE %s "
        "ORDER BY alias LIMIT %s",
        (f"%{prefix}%", limit),
    )
    return [_row_to_target(r) for r in rows]


def get(target_id: int) -> TargetServer | None:
    row = db.fetch_one(
        "SELECT id, alias, host, port, default_database, username, enabled, notes, "
        "       COALESCE(engine, 'postgres') AS engine, "
        "       COALESCE(tags, '{}'::jsonb) AS tags "
        "FROM target_servers WHERE id = %s",
        (target_id,),
    )
    return _row_to_target(row) if row else None


def by_alias(alias: str) -> TargetServer | None:
    """Look a target up by the alias the API paths and Slack pickers use.

    Case-SENSITIVE on purpose. Aliases are unique case-sensitively, and every
    other lookup in the codebase (grants, admin scopes, schema refresh) matches
    exactly — a case-insensitive variant here could resolve `Prod-Beta` to a row
    that the grant check would then decide is a different target.
    """
    row = db.fetch_one(
        "SELECT id, alias, host, port, default_database, username, enabled, notes, "
        "       COALESCE(engine, 'postgres') AS engine, "
        "       COALESCE(tags, '{}'::jsonb) AS tags "
        "FROM target_servers WHERE alias = %s",
        (alias,),
    )
    return _row_to_target(row) if row else None


def get_password(target_id: int) -> str:
    row = db.fetch_one(
        "SELECT password_encrypted FROM target_servers WHERE id = %s",
        (target_id,),
    )
    if row is None:
        raise LookupError(f"target_servers id={target_id} not found")
    return decrypt(row["password_encrypted"])


def get_credentials(target_id: int, mode: str) -> tuple[str, str]:
    """Return (username, plaintext_password) for the given mode tier on a
    target. Modes: 'ro', 'rw', 'ddl'. Raises LookupError if the target's
    credentials at the requested tier aren't configured — e.g. the bot wasn't
    given RW creds for this target so write queries can't run there.

    The actual source is the target's configured secrets provider
    (target_servers.secrets_provider); the default 'local' provider decrypts
    the per-tier Fernet columns, exactly as before. See secrets_providers.py.
    """
    if mode not in ("ro", "rw", "ddl"):
        raise ValueError(f"unknown mode: {mode}")
    row = db.fetch_one(
        "SELECT id, "
        "       username,     password_encrypted, "
        "       username_rw,  password_rw_encrypted, "
        "       username_ddl, password_ddl_encrypted, "
        "       secrets_provider, secrets_ref "
        "FROM target_servers WHERE id = %s",
        (target_id,),
    )
    if row is None:
        raise LookupError(f"target_servers id={target_id} not found")
    return secrets_providers.resolve_credentials(row, mode)


def add(
    alias: str,
    host: str,
    port: int,
    default_database: str,
    username: str,
    password: str,
    notes: str | None = None,
) -> int:
    row = db.insert_returning(
        "INSERT INTO target_servers "
        "(alias, host, port, default_database, username, password_encrypted, notes) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (alias, host, port, default_database, username, encrypt(password), notes),
    )
    return row["id"]


def update_password(target_id: int, new_password: str) -> None:
    db.execute(
        "UPDATE target_servers SET password_encrypted = %s, updated_at = NOW() "
        "WHERE id = %s",
        (encrypt(new_password), target_id),
    )


class CredentialNotProvisioned(Exception):
    """Enabling a target whose RO password is still the import sentinel."""


def set_enabled(target_id: int, enabled: bool, *, force: bool = False) -> None:
    """Flip a target's visibility. Refuses to enable one that cannot be used.

    Onboarding is two steps -- enable it, and give it a password -- and only the
    first is visible, so the second gets skipped. A target enabled with the
    import sentinel still looks finished: it appears in the picker, a grant can
    be issued on it, the tier badge renders. Nothing fails until a real person
    runs a query, and the schema snapshot silently cannot connect either, so the
    tree shows a database with no tables. That is exactly how it reached a user's
    screen on 2026-08-06 -- enabled, granted, reported broken from the outside.

    So the sentinel is refused here rather than warned about further down. This
    is the one choke point every caller goes through, and a check anywhere later
    would be a check the enable path can skip.

    `force` exists for the operator who genuinely wants a placeholder row
    visible (staging a target before its credential arrives); it has to be asked
    for, which is the whole point.
    """
    if enabled and not force:
        row = db.fetch_one(
            "SELECT alias, password_encrypted FROM target_servers WHERE id = %s",
            (target_id,),
        )
        if row is None:
            raise LookupError(f"target_servers id={target_id} not found")
        if _is_placeholder(row["password_encrypted"]):
            raise CredentialNotProvisioned(
                f"{row['alias']}: the read-only password is still the import "
                f"placeholder, so queries and schema snapshots would both fail. "
                f"Set the credential first (see scripts/adopt_target_credential.py), "
                f"or pass force=True to list it anyway."
            )
    db.execute(
        "UPDATE target_servers SET enabled = %s, updated_at = NOW() WHERE id = %s",
        (enabled, target_id),
    )


def unprovisioned_enabled() -> list[dict]:
    """Enabled targets whose RO password is still the sentinel.

    The fleet-health question: which targets are visible to users but cannot
    actually run anything. Should be empty; anything here is a half-finished
    onboarding waiting to surprise someone.
    """
    rows = db.fetch_all(
        "SELECT id, alias, password_encrypted FROM target_servers "
        "WHERE enabled ORDER BY id"
    )
    return [{"id": r["id"], "alias": r["alias"]}
            for r in rows if _is_placeholder(r["password_encrypted"])]


# ---------------------------------------------------------------------------
# Registry administration (the web admin's Connections screen).
#
# Everything below writes through a caller-supplied cursor rather than opening
# its own transaction, so the change and its audit_log row commit together —
# the same contract audit.log_in() is built around. A half-applied credential
# rotation with no record of who did it is exactly the state this avoids.
# ---------------------------------------------------------------------------

_ADMIN_COLS = (
    "SELECT id, alias, host, port, default_database, enabled, notes, "
    "       COALESCE(engine, 'postgres') AS engine, "
    "       COALESCE(secrets_provider, 'local') AS secrets_provider, "
    "       username, username_rw, username_ddl, "
    "       password_encrypted, password_rw_encrypted, password_ddl_encrypted, "
    "       COALESCE(tags, '{}'::jsonb) AS tags "
    "FROM target_servers"
)


def _is_placeholder(ciphertext: str | None) -> bool:
    """True when a stored password is the not-provisioned-yet sentinel.

    Needs an actual decrypt — there is no marker column — and must never raise.
    A target whose ciphertext predates a completed key rotation should show up
    as "has a password we cannot read", not take the whole connection list down
    with it.
    """
    if not ciphertext:
        return False
    try:
        return decrypt(ciphertext) == SENTINEL_PASSWORD
    except Exception:
        return False


def _admin_row(row: dict) -> dict:
    """One target as the admin editor needs it, with credential PRESENCE in
    place of credentials.

    The ciphertext is dropped here rather than at the route, because the route
    serialises what it is handed: a boolean cannot be leaked by a later
    `**row` spread the way a `password_encrypted` key can.
    """
    return {
        "id": row["id"],
        "alias": row["alias"],
        "host": row["host"],
        "port": row["port"],
        "default_database": row["default_database"],
        "enabled": row["enabled"],
        "notes": row["notes"],
        "engine": row["engine"],
        "secrets_provider": row["secrets_provider"],
        "tags": row.get("tags") or {},
        "credentials": {
            mode: {
                "username": row[ucol],
                "configured": bool(row[ucol] and row[pcol]),
                "placeholder": _is_placeholder(row[pcol]),
            }
            for mode, (ucol, pcol) in _TIER_COLUMNS.items()
        },
    }


def list_admin_rows() -> list[dict]:
    """Every target — enabled or not — in admin-editor shape."""
    return [_admin_row(r)
            for r in db.fetch_all(f"{_ADMIN_COLS} ORDER BY enabled DESC, alias")]


def admin_row(target_id: int) -> dict | None:
    """One target in admin-editor shape, or None if it is gone."""
    row = db.fetch_one(f"{_ADMIN_COLS} WHERE id = %s", (target_id,))
    return _admin_row(row) if row else None


def create_in(
    cur,
    *,
    alias: str,
    host: str,
    port: int,
    default_database: str,
    engine: str = "postgres",
    notes: str | None = None,
    tags: dict | None = None,
    credentials: dict[str, tuple[str | None, str | None]] | None = None,
) -> int:
    """Register a new target, DISABLED, and return its id.

    New targets never start enabled. That is the same rule the hourly inventory
    sync follows (it imports endpoints disabled and `target_policy.enforce()`
    only ever disables), and it is what makes a broad host-allow pattern safe:
    registering an endpoint is cheap and reversible, exposing it to developers
    is the decision that needs a human.

    `credentials` maps a tier to (username, password); a password is encrypted
    here so no call site can store one in the clear. The RO columns are NOT
    NULL, so a target created without RO credentials gets the fleet-default
    role name and the sentinel password rather than a second, unrecognised
    spelling of "not provisioned".
    """
    creds = credentials or {}
    ro_user, ro_password = creds.get("ro") or (None, None)
    rw_user, rw_password = creds.get("rw") or (None, None)
    ddl_user, ddl_password = creds.get("ddl") or (None, None)
    cur.execute(
        "INSERT INTO target_servers "
        "(alias, host, port, default_database, engine, username, "
        " password_encrypted, username_rw, password_rw_encrypted, "
        " username_ddl, password_ddl_encrypted, enabled, notes, tags) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s) "
        "RETURNING id",
        (alias, host, port, default_database, engine,
         ro_user or DEFAULT_RO_USERNAME,
         encrypt(ro_password or SENTINEL_PASSWORD),
         rw_user, encrypt(rw_password) if rw_password else None,
         ddl_user, encrypt(ddl_password) if ddl_password else None,
         notes, Json(tags or {})),
    )
    return cur.fetchone()["id"]


def update_in(cur, target_id: int, changes: dict) -> list[str]:
    """Apply whitelisted column changes; returns the column names written.

    Unknown keys are ignored rather than rejected, so a caller can hand over a
    request body it has already validated without also having to strip it.
    """
    sets, params = [], []
    for col in _EDITABLE_COLUMNS:
        if col in changes:
            sets.append(f"{col} = %s")
            # jsonb columns need the adapter told what they are; psycopg maps a
            # bare dict to hstore-ish guessing otherwise.
            params.append(Json(changes[col]) if col in _JSON_COLUMNS
                          else changes[col])
    if not sets:
        return []
    params.append(target_id)
    cur.execute(
        f"UPDATE target_servers SET {', '.join(sets)}, updated_at = NOW() "
        f"WHERE id = %s",
        tuple(params),
    )
    return [c for c in _EDITABLE_COLUMNS if c in changes]


def set_credentials_in(cur, target_id: int, mode: str,
                       username: str | None = None,
                       password: str | None = None) -> None:
    """Write one tier's credentials, encrypting the password on the way in.

    Passing None for either half leaves that half alone — correcting a
    mistyped role name should not require re-typing the password, and rotating
    a password should not require re-sending the username the caller was never
    shown.
    """
    if mode not in _TIER_COLUMNS:
        raise ValueError(f"unknown mode: {mode}")
    ucol, pcol = _TIER_COLUMNS[mode]
    sets, params = [], []
    if username is not None:
        sets.append(f"{ucol} = %s")
        params.append(username)
    if password is not None:
        sets.append(f"{pcol} = %s")
        params.append(encrypt(password))
    if not sets:
        return
    params.append(target_id)
    cur.execute(
        f"UPDATE target_servers SET {', '.join(sets)}, updated_at = NOW() "
        f"WHERE id = %s",
        tuple(params),
    )


def reference_counts(target_id: int) -> dict[str, int]:
    """How much of the system still points at this target.

    Two different kinds of answer live in here. `requests` and `csv_imports`
    are HISTORY — the record of what ran where — and history is never rewritten
    to make a delete convenient. The grant counts are live access
    configuration, which a delete would cascade away silently: the operator
    would see the connection disappear and not the six people who lost access
    with it. Either kind is a reason to disable instead.

    `schema_tables` is neither: it is a cache of the target's catalog, so it is
    reported for context but deleted along with the target.
    """
    row = db.fetch_one(
        "SELECT "
        "(SELECT count(*) FROM requests WHERE target_server_id = %s) AS requests, "
        "(SELECT count(*) FROM csv_imports WHERE target_server_id = %s) AS csv_imports, "
        "(SELECT count(*) FROM user_target_grants WHERE target_server_id = %s "
        "   AND revoked_at IS NULL) AS user_grants, "
        "(SELECT count(*) FROM team_target_grants WHERE target_server_id = %s) "
        "   AS team_grants, "
        "(SELECT count(*) FROM auto_approve_grants WHERE target_server_id = %s "
        "   AND (expires_at IS NULL OR expires_at > NOW())) AS auto_grants, "
        "(SELECT count(*) FROM access_requests WHERE target_server_id = %s) "
        "   AS access_requests, "
        "(SELECT count(*) FROM schema_tables WHERE target_server_id = %s) "
        "   AS schema_tables",
        (target_id,) * 7,
    )
    return {k: int(v or 0) for k, v in (row or {}).items()}


def delete_in(cur, target_id: int) -> None:
    """Remove a target and the catalog snapshot derived from it.

    schema_tables holds a restricting foreign key, so it has to go first or the
    DELETE errors — and dropping it is right anyway: it is a cache of a server
    that is no longer registered. Everything else that references a target
    either cascades (grants, pod ownership) or nulls out (saved queries, access
    requests) by design. The caller is expected to have checked
    `reference_counts()` first, which is what keeps history out of reach.
    """
    cur.execute("DELETE FROM schema_tables WHERE target_server_id = %s",
                (target_id,))
    cur.execute("DELETE FROM target_servers WHERE id = %s", (target_id,))
