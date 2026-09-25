"""Split the metadata database's roles: an owner, a migrator, a runtime login.

Until this split, one login owns the metadata database and every object in it,
and both services and the scheduled jobs connect with it. Owning a table means
UPDATE, DELETE and TRUNCATE on `audit_log` whatever the code does, so a leaked
runtime credential could rewrite the trail QueryHub exists to keep
(SEC-ROLES / SEC-AUDIT).

After the split:

    owner     NOLOGIN. Owns the database and every object in `public`.
    migrator  LOGIN, a member of owner. scripts/apply_migrations.py connects as
              it and runs `SET ROLE owner`, so new objects are owned by owner.
    runtime   The existing service login, unchanged in name and password. It
              keeps SELECT/INSERT/UPDATE/DELETE on tables, SELECT only on
              views, USAGE on sequences, and on `audit_log` only SELECT and
              INSERT. It has no access to `schema_migrations`.

Views get SELECT only because a simple view such as `audit_log_reportable`
is auto-updatable, and an UPDATE through it is checked against the view's
owner. A blanket DML grant on "all tables" includes views, and would have left
the audit rows editable one step removed.

The runtime needs TEMPORARY on the database: the access-model mirror trigger
(migration 109) creates a temporary table on every write to the legacy access
tables, including the profile refresh on each /sql submission.

Everything here is computed from the live catalog, so the plan is exactly what
the database holds. `plan_split` and `plan_rollback` return statements for the
caller to print or run; `runtime_grants` is the grant policy on its own, which
the migration runner re-applies after every run so a new table or view gets
the same treatment as the old ones.
"""
from __future__ import annotations

from dataclasses import dataclass

from psycopg import sql

# Tables the runtime may add to and read, never change.
APPEND_ONLY = frozenset({"audit_log"})
# Tables the runtime never touches: with write access it could forge the
# migration ledger.
MIGRATOR_ONLY = frozenset({"schema_migrations"})

_TABLE_DML = ("SELECT", "INSERT", "UPDATE", "DELETE")


@dataclass(frozen=True)
class Roles:
    runtime: str
    owner: str
    # Who owns the objects before the split: the runtime login on a normal
    # install, a superuser in CI.
    current_owner: str


def _ident(name: str) -> sql.Identifier:
    return sql.Identifier(name)


def _db(cur) -> str:
    cur.execute("SELECT current_database() AS d")
    return cur.fetchone()["d"]


def _owned_objects(cur, owner: str) -> list[dict]:
    """Every object in `public` that `owner` owns and that ALTER ... OWNER
    moves on its own. Indexes, and sequences a column owns, follow their
    table. Objects an extension created belong to the extension."""
    cur.execute(
        """
        WITH ext AS (
            SELECT objid FROM pg_depend WHERE deptype = 'e'
        )
        SELECT 'table' AS kind, c.oid, c.relname AS name, c.relkind AS rk
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
           AND pg_get_userbyid(c.relowner) = %(o)s
           AND c.oid NOT IN (SELECT objid FROM ext)
        UNION ALL
        SELECT CASE c.relkind WHEN 'm' THEN 'matview' ELSE 'view' END,
               c.oid, c.relname, c.relkind
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm')
           AND pg_get_userbyid(c.relowner) = %(o)s
           AND c.oid NOT IN (SELECT objid FROM ext)
        UNION ALL
        SELECT 'sequence', c.oid, c.relname, c.relkind
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind = 'S'
           AND pg_get_userbyid(c.relowner) = %(o)s
           AND c.oid NOT IN (SELECT objid FROM ext)
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.classid = 'pg_class'::regclass
                              AND d.objid = c.oid AND d.deptype IN ('a', 'i'))
        UNION ALL
        SELECT 'function', p.oid, p.oid::regprocedure::text, NULL
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public' AND pg_get_userbyid(p.proowner) = %(o)s
           AND p.prokind IN ('f', 'p')
           AND p.oid NOT IN (SELECT objid FROM ext)
        UNION ALL
        -- Enums, domains, ranges and stand-alone composite types. A table's
        -- row type, an array type and a multirange follow their parent.
        SELECT 'type', t.oid, t.typname, NULL
          FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
         WHERE n.nspname = 'public' AND pg_get_userbyid(t.typowner) = %(o)s
           AND (t.typtype IN ('e', 'd', 'r')
                OR (t.typtype = 'c' AND EXISTS (
                        SELECT 1 FROM pg_class k
                         WHERE k.oid = t.typrelid AND k.relkind = 'c')))
           AND t.oid NOT IN (SELECT objid FROM ext)
         ORDER BY 1, 3
        """, {"o": owner})
    return list(cur.fetchall())


def _alter_owner(obj: dict, to: str) -> sql.Composed:
    kind = obj["kind"]
    if kind == "function":
        # regprocedure text is already a quoted, argument-typed signature.
        return sql.SQL("ALTER ROUTINE {} OWNER TO {}").format(
            sql.SQL(obj["name"]), _ident(to))
    word = {"table": "TABLE", "view": "VIEW", "matview": "MATERIALIZED VIEW",
            "sequence": "SEQUENCE", "type": "TYPE"}[kind]
    return sql.SQL("ALTER {} public.{} OWNER TO {}").format(
        sql.SQL(word), _ident(obj["name"]), _ident(to))


def runtime_grants(cur, runtime: str, owner: str) -> list[sql.Composed]:
    """The runtime's privileges as they should be, as the statements that
    close the gap from what it holds now. Empty when nothing has drifted, so
    running it after every migration costs nothing."""
    out: list[sql.Composed] = []
    dbname = _db(cur)
    cur.execute(
        "SELECT has_database_privilege(%s, current_database(), 'CONNECT') AS c, "
        "       has_database_privilege(%s, current_database(), 'TEMPORARY') AS t, "
        "       has_schema_privilege(%s, 'public', 'USAGE') AS u",
        (runtime, runtime, runtime))
    r = cur.fetchone()
    if not (r["c"] and r["t"]):
        out.append(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
            _ident(dbname), _ident(runtime)))
    if not r["u"]:
        out.append(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(_ident(runtime)))

    cur.execute(
        """
        SELECT c.relname AS name, c.relkind AS rk,
               has_table_privilege(%(r)s, c.oid, 'SELECT') AS s,
               has_table_privilege(%(r)s, c.oid, 'INSERT') AS i,
               has_table_privilege(%(r)s, c.oid, 'UPDATE') AS u,
               has_table_privilege(%(r)s, c.oid, 'DELETE') AS d,
               has_table_privilege(%(r)s, c.oid, 'TRUNCATE') AS t,
               has_table_privilege(%(r)s, c.oid, 'REFERENCES') AS f,
               has_table_privilege(%(r)s, c.oid, 'TRIGGER') AS g,
               CASE WHEN c.relkind = 'S' THEN has_sequence_privilege(%(r)s, c.oid, 'USAGE') END AS su
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
           AND pg_get_userbyid(c.relowner) = %(o)s
         ORDER BY c.relname
        """, {"r": runtime, "o": owner})
    for row in cur.fetchall():
        name, rk = row["name"], row["rk"]
        if rk == "S":
            if not (row["su"] and row["s"]):
                out.append(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE public.{} TO {}").format(
                    _ident(name), _ident(runtime)))
            continue
        held = {p for p, k in (("SELECT", "s"), ("INSERT", "i"), ("UPDATE", "u"),
                               ("DELETE", "d"), ("TRUNCATE", "t"),
                               ("REFERENCES", "f"), ("TRIGGER", "g")) if row[k]}
        if name in MIGRATOR_ONLY:
            want: set[str] = set()
        elif name in APPEND_ONLY:
            want = {"SELECT", "INSERT"}
        elif rk in ("v", "m"):
            want = {"SELECT"}
        else:
            want = set(_TABLE_DML)
        extra, missing = held - want, want - held
        if extra:
            out.append(sql.SQL("REVOKE {} ON public.{} FROM {}").format(
                sql.SQL(", ").join(sql.SQL(p) for p in sorted(extra)),
                _ident(name), _ident(runtime)))
        if missing:
            out.append(sql.SQL("GRANT {} ON public.{} TO {}").format(
                sql.SQL(", ").join(sql.SQL(p) for p in sorted(missing)),
                _ident(name), _ident(runtime)))
    return out


def default_privileges(runtime: str, owner: str) -> list[sql.Composed]:
    """What a table or sequence created later by the owner starts with. A view
    gets the table grants too (PostgreSQL does not tell them apart here), which
    `runtime_grants` narrows to SELECT after each migration run."""
    return [
        sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(
            _ident(owner), _ident(runtime)),
        sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO {}").format(
            _ident(owner), _ident(runtime)),
    ]


def check_split_preconditions(cur, roles: Roles) -> list[str]:
    """Reasons the split cannot run as this login, in plain words. Empty when
    it can."""
    problems = []
    cur.execute("SELECT current_user AS me")
    me = cur.fetchone()["me"]
    cur.execute(
        "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = ANY(%s)",
        ([roles.runtime, roles.owner, roles.current_owner],))
    found = {r["rolname"]: r for r in cur.fetchall()}
    for name in {roles.runtime, roles.owner, roles.current_owner}:
        if name not in found:
            problems.append(f"role {name} does not exist")
    if problems:
        return problems
    if found[roles.owner]["rolcanlogin"]:
        problems.append(f"{roles.owner} can log in; the owner role must be NOLOGIN")
    if roles.owner == roles.runtime:
        problems.append("the owner and the runtime must be different roles")
    cur.execute("SELECT pg_has_role(%s, %s, 'MEMBER') AS m", (roles.runtime, roles.owner))
    if cur.fetchone()["m"]:
        problems.append(f"{roles.runtime} is a member of {roles.owner}: it could "
                        f"SET ROLE back to owner, which undoes the split")
    for name in (roles.owner, roles.current_owner):
        cur.execute("SELECT pg_has_role(%s, %s, 'SET') AS ok", (me, name))
        if not cur.fetchone()["ok"]:
            problems.append(f"{me} cannot SET ROLE {name}; ALTER ... OWNER needs it "
                            f"(GRANT {name} TO {me})")
    return problems


def plan_split(cur, roles: Roles) -> list[sql.Composed]:
    """Ownership to the owner, then the runtime's grants. Run in one
    transaction: nothing changes unless all of it does."""
    stmts: list[sql.Composed] = [
        sql.SQL("ALTER DATABASE {} OWNER TO {}").format(_ident(_db(cur)), _ident(roles.owner))]
    stmts += [_alter_owner(o, roles.owner) for o in _owned_objects(cur, roles.current_owner)]
    stmts += default_privileges(roles.runtime, roles.owner)
    return stmts


def plan_rollback(cur, roles: Roles) -> list[sql.Composed]:
    """Everything back to one owning login, the pre-split state."""
    stmts: list[sql.Composed] = [
        sql.SQL("ALTER DATABASE {} OWNER TO {}").format(_ident(_db(cur)), _ident(roles.runtime))]
    stmts += [_alter_owner(o, roles.runtime) for o in _owned_objects(cur, roles.owner)]
    stmts += [
        sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {}").format(
            _ident(roles.owner), _ident(roles.runtime)),
        sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "REVOKE USAGE, SELECT ON SEQUENCES FROM {}").format(
            _ident(roles.owner), _ident(roles.runtime)),
    ]
    return stmts


def still_owned_by(cur, owner: str) -> int:
    """Objects in `public` that `owner` still owns; 0 once a split is done."""
    return len(_owned_objects(cur, owner))
